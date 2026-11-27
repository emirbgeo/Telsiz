#!/usr/bin/env python3
"""
Telsiz server: a dedicated room that runs on a small Linux server (VPS).

Everyone, the owner included, joins it with the normal Telsiz app ("Join a room").
The server only relays packets between people; it never decodes audio or video,
so it needs no sound card, no window and nothing beyond Python's standard library.

Since 1.3 the server has rooms (you only hear people in your own room; admins
create and delete rooms) and relays screen sharing (one sharer per room, sent
only to the people who chose to watch). Since 1.5 every room has a text chat;
its messages are saved in the chat folder next to the settings file.

    python3 telsiz_server.py --init       create telsiz_server.json with new passwords
    python3 telsiz_server.py              run the server
    python3 telsiz_server.py --list-bans  show bans
    python3 telsiz_server.py --unban 3    remove ban number 3 (also works while the server runs)

Joining with the admin password (instead of the room password) gives Kick, Ban,
Bans, New room and Delete room in the app.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import secrets
import signal
import socket
import struct
import sys
import threading
import time
from pathlib import Path

SERVER_VERSION = "1.5"
PROTOCOL = 2          # same wire protocol as the Telsiz 1.1+ apps; new features are negotiated
DEFAULT_PORT = 50505
MAX_NAME = 32
MAX_ROOM_NAME = 24
MAX_ROOMS = 25
LOBBY_ID = 1
HELLO_MIN = 256
TIMEOUT = 15.0        # silence before someone counts as gone (a VPS stalling for 7 s must not drop everyone)
ROSTER_EVERY = 2.0
BANS_IN_PACKET = 20
LOCKOUT_FAILS = 10    # wrong passwords from one IP within LOCKOUT_WINDOW ...
LOCKOUT_WINDOW = 60.0  # ... and its join requests are ignored until the window ends
VIDEO_BYTES_PER_SEC = 1_000_000  # per sharer (8 Mbit/s); anything above is dropped
KEYREQ_GAP = 0.5      # seconds between keyframe requests forwarded to one sharer
VIDEO_CACHE_FRAMES = 90   # recent pictures kept per sharer (about 6 s) to resend lost pieces
RESEND_PER_SEC = 400      # resent video packets per viewer per second, at most

(T_HELLO, T_WELCOME, T_REJECT, T_ROSTER, T_AUDIO, T_PING, T_PONG, T_BYE,
 T_ADMIN, T_BANS, T_ROOMS, T_MOVE, T_SHARE, T_WATCH, T_VIDEO, T_KEYREQ, T_NOTICE, T_NACK,
 T_STATUS, T_STATUSES) = range(1, 21)
S_MUTED, S_DEAFENED = 1, 2
A_KICK, A_BAN, A_UNBAN, A_ROOM_NEW, A_ROOM_DEL = 1, 2, 3, 4, 5
F_ADMIN, F_ROOMS, F_SCREEN = 1, 2, 4          # welcome flags (server -> app)
C_ROOMS, C_SCREEN = 1, 2                      # capability bits in the join request (app -> server)

KICK_MSG = "You were kicked from this room. You can rejoin after everyone has left or the server restarts."
BAN_MSG = "You are banned from this room."


def _pstr(s: str) -> bytes:
    b = s.encode("utf-8")[:255]
    return bytes([len(b)]) + b


def _ustr(buf: bytes, off: int):
    n = buf[off]
    end = off + 1 + n
    if end > len(buf):
        raise ValueError("truncated string")
    return buf[off + 1:end].decode("utf-8", "replace"), end


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _write_json(path: Path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


# ---- chat core: the same code in telsiz.py and server/telsiz_server.py ----
# Text chat (apps 1.8+, server 1.5+). One chat per room. The server (or the PC
# that hosts) numbers every message, keeps the last CHAT_KEEP of each room in
# memory and appends every message to a JSON lines file per room. Apps send a
# message again until it is acknowledged (the server ignores the copies), and
# fetch whatever they missed: every ROSTER_EVERY seconds the server tells each
# app the newest message number of its room.
T_CHAT_SEND, T_CHAT_MSGS, T_CHAT_ACK, T_CHAT_GET, T_CHAT_HEAD, T_CHAT_DEL = range(21, 27)
C_CHAT, F_CHAT = 4, 8          # capability bit in the join request, flag in the welcome
CHAT_MAX_CHARS = 500
CHAT_MAX_BYTES = 1000          # UTF-8; a message always fits in one packet
CHAT_MAX_LINES = 15
CHAT_KEEP = 1000               # messages per room kept in memory and offered as history
CHAT_JOIN = 100                # sent to someone who arrives; older ones when they ask
CHAT_PAGE = 50                 # older messages per request
CHAT_REVS_MAX = 25             # deletions per answer
CHAT_FILE_MAX = 8 << 20        # a log file this big is archived next to it and a new one started
CHAT_PACKET = 1300
CHAT_BURST, CHAT_RATE = 8, 1.0  # messages: 8 in a row, then one a second
CHAT_GETS_PER_SEC = 10
CHAT_GET_BYTES = 256_000       # history sent to one person per 10 s at most: a small question can never
                               # be turned into a flood aimed at someone else's address
ACK_OK, ACK_SLOW, ACK_BAD, ACK_ROOM = 0, 1, 2, 3
GET_AFTER, GET_BEFORE, GET_REVS = 0, 1, 2
MSG_LIVE, MSG_REVS = 1, 2      # T_CHAT_MSGS: just posted (or deleted); carries deletions up to its rev
M_DELETED = 1                  # per message

# control characters (except newline) and the bidirectional overrides that can make text read backwards
_CHAT_DROP = dict.fromkeys([*range(0, 9), 11, 12, *range(14, 32), 127, *range(0x80, 0xA0),
                            *range(0x202A, 0x202F), *range(0x2066, 0x206A)])


def clean_chat(text: str) -> str:
    """What is left of a message after removing what has no business in chat."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").translate(_CHAT_DROP)
    lines, blank = [], 0
    for line in text.split("\n"):
        line = line.rstrip()
        blank = blank + 1 if not line else 0
        if blank < 2:                          # at most one empty line in a row
            lines.append(line)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > CHAT_MAX_LINES:            # a wall of short lines: the rest goes on the last line
        lines = lines[:CHAT_MAX_LINES - 1] + [" ".join(x for x in lines[CHAT_MAX_LINES - 1:] if x)]
    return "\n".join(lines).strip()


def chat_entry(m: dict) -> bytes:
    tb = m["text"].encode("utf-8")[:CHAT_MAX_BYTES]
    return (struct.pack("!IIHB", m["id"], m["t"] & 0xFFFFFFFF, m["uid"] & 0xFFFF, m["flags"])
            + _pstr(m["name"]) + struct.pack("!H", len(tb)) + tb)


def parse_chat_msgs(data: bytes):
    """T_CHAT_MSGS -> (room, epoch, rev, flags, [messages])."""
    room, epoch, rev, flags, n = struct.unpack_from("!HIIBB", data, 1)
    off, out = 13, []
    for _ in range(n):
        mid, t, uid, mflags = struct.unpack_from("!IIHB", data, off)
        name, off = _ustr(data, off + 11)
        (ln,) = struct.unpack_from("!H", data, off)
        if off + 2 + ln > len(data):
            raise ValueError("truncated message")
        text = data[off + 2:off + 2 + ln].decode("utf-8", "replace")
        off += 2 + ln
        out.append({"id": mid, "t": t, "uid": uid, "flags": mflags, "name": name, "text": text})
    return room, epoch, rev, flags, out


class ChatLog:
    """One room's chat: the recent messages in memory, every message appended to a file.

    The file is JSON lines: a header line {"telsiz_chat": 1, "epoch": ...}, then one
    line per message, and {"del": id} lines for deleted ones. The epoch is a random
    number that changes when a room's chat starts over (a new room with an old
    number, a log deleted by hand), so apps know to forget what they had.
    """

    def __init__(self, path, room: int, log=None):
        self.path, self.room = path, room
        self.log = log or (lambda msg: None)
        self.msgs: collections.deque = collections.deque(maxlen=CHAT_KEEP)
        self.epoch, self.next_id, self.rev = 0, 1, 0
        self.revs: collections.deque = collections.deque(maxlen=CHAT_KEEP)  # (rev, id) of deletions
        self._header_written = False
        self._warned_at = -1e9
        self._load()
        if not self.epoch:
            self.epoch = secrets.randbits(32) or 1

    # ---- the file ----
    def _load(self):
        if self.path is None:
            return
        try:
            f = open(self.path, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return
        except OSError as e:
            self.log(f"warning: could not read the chat log {self.path}: {e}")
            return
        byid = {}
        with f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue                    # a line cut short by a crash
                if not isinstance(d, dict):
                    continue
                try:
                    if "telsiz_chat" in d:
                        if not self.epoch:
                            self.epoch = int(d.get("epoch", 0)) & 0xFFFFFFFF
                            self._header_written = bool(self.epoch)
                        self.next_id = max(self.next_id, int(d.get("next_id", 1)))
                        self.rev = max(self.rev, int(d.get("rev", 0)))
                    elif "del" in d:
                        self.rev += 1                # every deletion counts, also of messages no longer kept
                        m = byid.get(int(d["del"]))
                        if m is not None and not m["flags"] & M_DELETED:
                            m["flags"] |= M_DELETED
                            m["text"] = ""
                            self.revs.append((self.rev, m["id"]))
                    elif isinstance(d.get("text"), str) and isinstance(d.get("name"), str):
                        mid = int(d["id"])
                        if not 0 < mid <= 0xFFFFFFFF:
                            continue
                        m = {"id": mid, "t": int(d.get("t", 0)), "uid": int(d.get("uid", 0)) & 0xFFFF,
                             "name": d["name"][:MAX_NAME], "text": d["text"][:CHAT_MAX_CHARS], "flags": 0,
                             "dev": str(d.get("dev", ""))[:32]}
                        if len(self.msgs) == CHAT_KEEP:
                            byid.pop(self.msgs[0]["id"], None)
                        self.msgs.append(m)
                        byid[mid] = m
                        self.next_id = max(self.next_id, mid + 1)
                except (TypeError, ValueError, KeyError):
                    continue

    def _append(self, obj: dict):
        if self.path is None:
            return
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            new = not self.path.exists()
            if new or not self._header_written:
                line = json.dumps({"telsiz_chat": 1, "room": self.room, "epoch": self.epoch,
                                   "next_id": self.next_id - (1 if "id" in obj else 0), "rev": self.rev}) + "\n" + line
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                size = f.tell()
            if new:
                try:
                    os.chmod(self.path, 0o600)      # private conversations
                except OSError:
                    pass
            self._header_written = True
            if size > CHAT_FILE_MAX:
                self._rotate()
        except OSError as e:
            now = time.monotonic()
            if now - self._warned_at > 600:
                self._warned_at = now
                self.log(f"warning: chat is working, but could not be saved to {self.path}: {e}")

    def _rotate(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(self.path, self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}"))
        self._header_written = False   # the next message starts a new file with the same epoch and numbers

    def archive(self, why: str = "deleted"):
        """The room is gone: keep its log under another name, so a new room with this number starts clean."""
        if self.path is None or not self.path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(self.path, self.path.with_name(f"{self.path.stem}.{why}-{stamp}{self.path.suffix}"))
        except OSError as e:
            self.log(f"warning: could not archive the chat log {self.path}: {e}")

    # ---- messages ----
    @property
    def first(self) -> int:
        return self.msgs[0]["id"] if self.msgs else self.next_id

    @property
    def last(self) -> int:
        return self.next_id - 1

    def post(self, uid: int, name: str, text: str, dev: str = "") -> dict:
        m = {"id": self.next_id, "t": int(time.time()), "uid": uid, "name": name, "text": text,
             "flags": 0, "dev": dev}
        self.next_id += 1
        self.msgs.append(m)
        self._append({"id": m["id"], "t": m["t"], "uid": uid, "name": name, "text": text, "dev": dev})
        return m

    def get(self, mid: int):
        for m in reversed(self.msgs):
            if m["id"] == mid:
                return m
            if m["id"] < mid:
                break
        return None

    def delete(self, mid: int, by: str):
        m = self.get(mid)
        if m is None or m["flags"] & M_DELETED:
            return None
        m["flags"] |= M_DELETED
        m["text"] = ""
        self.rev += 1
        self.revs.append((self.rev, mid))
        self._append({"del": mid, "by": by, "t": int(time.time())})
        return m

    def after(self, mid: int, limit: int = CHAT_JOIN) -> list:
        out = [m for m in self.msgs if m["id"] > mid]
        return out[-limit:]

    def before(self, mid: int, limit: int = CHAT_PAGE) -> list:
        return [m for m in self.msgs if m["id"] < mid][-limit:]

    def deletions_since(self, rev: int):
        """Up to CHAT_REVS_MAX deleted messages after `rev`, and the rev an app is at once it has them."""
        picked = [(r, i) for r, i in self.revs if r > rev][:CHAT_REVS_MAX]
        if not picked:
            return [], self.rev
        msgs = [m for m in (self.get(i) for _, i in picked) if m is not None]
        return msgs, (picked[-1][0] if len(picked) == CHAT_REVS_MAX else self.rev)

    # ---- packets ----
    def head_packet(self, deaf: int) -> bytes:
        return struct.pack("!BHIIIIB", T_CHAT_HEAD, self.room, self.epoch, self.first, self.last, self.rev,
                           min(deaf, 255))

    def packets(self, msgs, flags: int = 0, rev=None) -> list:
        head = struct.pack("!BHIIB", T_CHAT_MSGS, self.room, self.epoch, self.rev if rev is None else rev, flags)
        out, body, n = [], b"", 0
        for m in msgs:
            e = chat_entry(m)
            if n and (len(head) + 1 + len(body) + len(e) > CHAT_PACKET or n == 255):
                out.append(head + bytes([n]) + body)
                body, n = b"", 0
            body += e
            n += 1
        if n or not out:  # an empty answer still says "that is all"
            out.append(head + bytes([n]) + body)
        return out


class _Bucket:
    __slots__ = ("level", "at")

    def __init__(self, cap: float, now: float):
        self.level, self.at = cap, now

    def take(self, cap: float, rate: float, now: float) -> bool:
        self.level = min(cap, self.level + (now - self.at) * rate)
        self.at = now
        if self.level >= 1.0:
            self.level -= 1.0
            return True
        return False


class ChatDesk:
    """The chat of a server or a hosting PC: a ChatLog per room, limits, and the answers to apps.

    The caller decides who may do what (which room someone is in, whether they are
    an admin) and delivers the packets these methods return.
    """

    def __init__(self, folder, log=None):
        self.folder = folder
        self.log = log or (lambda msg: None)
        self.logs: dict[int, ChatLog] = {}
        self._seen: dict[str, collections.OrderedDict] = {}   # who -> message number they chose -> answer
        self._buckets: dict[str, _Bucket] = {}
        self._gets: dict[str, list] = {}

    def room(self, rid: int) -> ChatLog:
        cl = self.logs.get(rid)
        if cl is None:
            path = self.folder / f"room-{rid}.jsonl" if self.folder is not None else None
            cl = self.logs[rid] = ChatLog(path, rid, self.log)
        return cl

    def drop_room(self, rid: int):
        self.room(rid).archive()
        self.logs.pop(rid, None)

    def _tidy(self):
        for d in (self._seen, self._buckets, self._gets):
            if len(d) > 4096:
                d.clear()

    def send(self, data: bytes, who: str, uid: int, name: str, dev: str, room: int, now: float):
        """T_CHAT_SEND -> (acknowledgement, packets for everyone in the room or None)."""
        want_room, cid, n = struct.unpack_from("!HIH", data, 1)
        raw = data[9:9 + n]
        if len(raw) != n:
            return None, None
        self._tidy()
        seen = self._seen.setdefault(who, collections.OrderedDict())
        if cid in seen:                         # a copy sent again because our answer got lost
            r, mid = seen[cid]
            return struct.pack("!BIBHI", T_CHAT_ACK, cid, ACK_OK, r, mid), None
        if want_room != room:
            return struct.pack("!BIBHI", T_CHAT_ACK, cid, ACK_ROOM, want_room, 0), None
        text = clean_chat(raw.decode("utf-8", "replace"))
        if not text or n > CHAT_MAX_BYTES or len(text) > CHAT_MAX_CHARS:
            return struct.pack("!BIBHI", T_CHAT_ACK, cid, ACK_BAD, room, 0), None
        bucket = self._buckets.get(who)
        if bucket is None:
            bucket = self._buckets[who] = _Bucket(CHAT_BURST, now)
        if not bucket.take(CHAT_BURST, CHAT_RATE, now):
            return struct.pack("!BIBHI", T_CHAT_ACK, cid, ACK_SLOW, room, 0), None
        cl = self.room(room)
        m = cl.post(uid, name, text, dev)
        seen[cid] = (room, m["id"])
        while len(seen) > 64:
            seen.popitem(last=False)
        return struct.pack("!BIBHI", T_CHAT_ACK, cid, ACK_OK, room, m["id"]), cl.packets([m], MSG_LIVE)

    def get(self, data: bytes, who: str, room: int, now: float) -> list:
        """T_CHAT_GET -> the packets that answer it (only about the asker's own room)."""
        rid, mode, ref = struct.unpack_from("!HBI", data, 1)
        if rid != room:
            return []
        self._tidy()
        g = self._gets.setdefault(who, [now, 0, now, 0])  # questions this second, bytes these 10 s
        if now - g[0] >= 1.0:
            g[0], g[1] = now, 0
        if now - g[2] >= 10.0:
            g[2], g[3] = now, 0
        if g[1] >= CHAT_GETS_PER_SEC or g[3] >= CHAT_GET_BYTES:
            return []
        g[1] += 1
        cl = self.room(room)
        if mode == GET_AFTER:
            out = cl.packets(cl.after(ref, CHAT_JOIN))
        elif mode == GET_BEFORE:
            out = cl.packets(cl.before(ref, CHAT_PAGE))
        elif mode == GET_REVS:
            msgs, rev = cl.deletions_since(ref)
            out = cl.packets(msgs, MSG_REVS, rev)
        else:
            return []
        g[3] += sum(len(pkt) for pkt in out)
        return out

    def delete(self, data: bytes, room: int, dev: str, admin: bool, by: str):
        """T_CHAT_DEL -> packets for everyone in the room, or None. Admins delete anything, others their own."""
        rid, mid = struct.unpack_from("!HI", data, 1)
        if rid != room:
            return None
        cl = self.room(room)
        m = cl.get(mid)
        if m is None or not (admin or (dev and m.get("dev") == dev)):
            return None
        if cl.delete(mid, by) is None:
            return None
        return cl.packets([m], MSG_LIVE | MSG_REVS)
# ---- end of chat core ----


class _Peer:
    __slots__ = ("id", "name", "last", "device", "ip", "admin", "caps", "room", "watching",
                 "status", "status_aware")

    def __init__(self, pid, name, last, device, ip, admin, caps=0, room=LOBBY_ID):
        self.id, self.name, self.last = pid, name, last
        self.device, self.ip, self.admin = device, ip, admin
        self.caps, self.room, self.watching = caps, room, 0
        self.status, self.status_aware = 0, False  # mute/deafen, reported by apps 1.4+


class BanList:
    """Bans kept in a JSON file. Changes made by --unban while the server runs are picked up."""

    def __init__(self, path: Path | None):
        self.path = path
        self.items: list[dict] = []
        self._mtime = None
        self.reload()

    def reload(self):
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            self.items = [b for b in data if isinstance(b, dict)] if isinstance(data, list) else []
            self._mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.items, self._mtime = [], None
        except (OSError, ValueError) as e:
            print(f"{_now_text()} warning: could not read {self.path}: {e}", flush=True)

    def changed_on_disk(self) -> bool:
        if self.path is None:
            return False
        try:
            return self.path.stat().st_mtime != self._mtime
        except FileNotFoundError:
            return self._mtime is not None

    def save(self):
        if self.path is None:
            return
        _write_json(self.path, self.items)
        self._mtime = self.path.stat().st_mtime

    def add(self, name, device, ip) -> dict:
        bid = max([b.get("id", 0) for b in self.items] + [0]) + 1
        entry = {"id": bid, "name": name, "device": device, "ip": ip, "when": _now_text()}
        self.items.append(entry)
        self.save()
        return entry

    def remove(self, bid: int):
        for b in self.items:
            if b.get("id") == bid:
                self.items.remove(b)
                self.save()
                return b
        return None

    def match(self, device: str, ip: str):
        for b in self.items:
            if (device and b.get("device") == device) or (ip and b.get("ip") == ip):
                return b
        return None


class RoomList:
    """Rooms kept in a JSON file. Room 1, the Lobby, always exists."""

    def __init__(self, path: Path | None):
        self.path = path
        self.items: list[dict] = []
        if path is not None:
            try:
                data = json.loads(path.read_text("utf-8"))
                if isinstance(data, list):
                    self.items = [{"id": int(r["id"]), "name": str(r["name"])[:MAX_ROOM_NAME]}
                                  for r in data if isinstance(r, dict) and "id" in r and "name" in r]
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as e:
                print(f"{_now_text()} warning: could not read {path}: {e}", flush=True)
        if not any(r["id"] == LOBBY_ID for r in self.items):
            self.items.insert(0, {"id": LOBBY_ID, "name": "Lobby"})

    def save(self):
        if self.path is not None:
            _write_json(self.path, self.items)

    def get(self, rid: int):
        return next((r for r in self.items if r["id"] == rid), None)

    def add(self, name: str):
        name = " ".join(name.split())[:MAX_ROOM_NAME]
        if not name:
            return None, "Give the room a name."
        if len(self.items) >= MAX_ROOMS:
            return None, f"There can be at most {MAX_ROOMS} rooms."
        same = next((r for r in self.items if r["name"].casefold() == name.casefold()), None)
        if same is not None:
            return None, f"A room called {same['name']} already exists."
        rid = max(r["id"] for r in self.items) + 1
        if rid > 0xFFFF:
            rid = next(i for i in range(2, 0xFFFF) if self.get(i) is None)
        room = {"id": rid, "name": name}
        self.items.append(room)
        self.save()
        return room, ""

    def remove(self, rid: int):
        room = self.get(rid)
        if room is None or rid == LOBBY_ID:
            return None
        self.items.remove(room)
        self.save()
        return room


class RelayServer:
    def __init__(self, port=DEFAULT_PORT, password="", admin_password="", max_clients=15,
                 bans_path: Path | None = None, bind="0.0.0.0", log=None, rooms_path: Path | None = None,
                 chat_dir: Path | None = None):
        self.port = port
        self.password = password
        self.admin_password = admin_password
        self.max_clients = max(1, min(int(max_clients), 50))
        self.bans = BanList(bans_path)
        self.rooms = RoomList(rooms_path)
        self.log = log or (lambda msg: print(f"{_now_text()} {msg}", flush=True))
        self.chat_dir = chat_dir
        self.chat = ChatDesk(chat_dir, self.log)
        self.clients: dict[tuple, _Peer] = {}
        self.lock = threading.Lock()
        self.kicked: set[str] = set()
        self.sharers: dict[int, int] = {}       # room id -> id of the person sharing a screen there
        self._video_budget: dict[int, list] = {}  # sharer id -> [window start, bytes in window]
        self._keyreq_at: dict[int, float] = {}
        self._vcache: dict[int, collections.OrderedDict] = {}  # sharer -> picture -> {piece: packet}
        self._resend_budget: dict[tuple, list] = {}
        self._nack_fwd_at: dict[int, float] = {}
        self._gone: dict[tuple, str] = {}
        self._next_id = 1
        self._reply_log: dict[str, float] = {}
        self._reply_window = [0.0, 0]
        self._fails: dict[str, list] = {}
        self._fail_logged: dict[str, float] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((bind, port))
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.25)
        self.alive = True

    # ---------------- lifecycle ----------------
    def serve_forever(self):
        self.log(f"Telsiz server {SERVER_VERSION} listening on UDP port {self.port}, "
                 f"up to {self.max_clients} people, rooms: "
                 + ", ".join(r["name"] for r in self.rooms.items))
        if not self.password:
            self.log("WARNING: no room password set; anyone who knows the address can join")
        if not self.admin_password:
            self.log("note: no admin password set; nobody can kick, ban or manage rooms")
        self.log(f"chat is saved in {self.chat_dir}" if self.chat_dir else "chat is not saved (no chat folder)")
        last_sweep = last_roster = time.monotonic()
        while self.alive:
            data = addr = None
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                pass
            except ConnectionResetError:
                pass
            except OSError:
                if not self.alive:
                    break
            if data:
                try:
                    self._handle(data, addr)
                except (ValueError, IndexError, struct.error):
                    pass
            now = time.monotonic()
            if now - last_sweep > 1.0:
                last_sweep = now
                self._sweep(now)
                if self.bans.changed_on_disk():
                    self.bans.reload()
                    self.log(f"ban list reloaded from disk ({len(self.bans.items)} entries)")
                    self._send_bans_to_admins()
            if now - last_roster > ROSTER_EVERY:
                last_roster = now
                self._broadcast_roster()

    def stop(self):
        if not self.alive:
            return
        self.alive = False
        with self.lock:
            addrs = list(self.clients)
            self.clients.clear()
        for a in addrs:
            self._send(bytes([T_BYE]), a)
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------------- helpers ----------------
    def roster(self, room=None):
        with self.lock:
            return sorted((c.id, c.name) for c in self.clients.values() if room is None or c.room == room)

    def _send(self, data: bytes, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _notice(self, addr, text: str):
        self._send(bytes([T_NOTICE]) + _pstr(text), addr)

    def _reject(self, addr, reason: str):
        """Answer a stranger; rate limited per IP and overall so the server can't be used to flood anyone."""
        now = time.monotonic()
        w = self._reply_window
        if now - w[0] >= 1.0:
            w[0], w[1] = now, 0
        if w[1] >= 20 or now - self._reply_log.get(addr[0], -10.0) < 0.5:
            return
        if len(self._reply_log) > 4096:
            self._reply_log.clear()
        self._reply_log[addr[0]] = now
        w[1] += 1
        self._send(bytes([T_REJECT]) + _pstr(reason), addr)

    def _rooms_body(self, peers) -> bytes:
        """All rooms with their members and sharer; the same for everyone except the leading 'your room'."""
        b = bytearray([len(self.rooms.items)])
        for r in self.rooms.items:
            members = sorted((p.id, p.name) for p in peers if p.room == r["id"])
            b += struct.pack("!H", r["id"]) + _pstr(r["name"]) + struct.pack("!HB", self.sharers.get(r["id"], 0),
                                                                             len(members))
            for uid, nm in members:
                b += struct.pack("!H", uid) + _pstr(nm)
        return bytes(b)

    def _broadcast_roster(self):
        with self.lock:
            items = list(self.clients.items())
        peers = [c for _, c in items]
        body = self._rooms_body(peers)
        per_room: dict[int, bytes] = {}
        for addr, c in items:
            pkt = per_room.get(c.room)
            if pkt is None:  # the classic people list: your own room only (all apps understand it)
                members = sorted((p.id, p.name) for p in peers if p.room == c.room)
                b = bytearray([T_ROSTER, len(members)])
                for uid, nm in members:
                    b += struct.pack("!H", uid) + _pstr(nm)
                pkt = per_room[c.room] = bytes(b)
            self._send(pkt, addr)
            if c.caps & C_ROOMS:
                self._send(struct.pack("!BH", T_ROOMS, c.room) + body, addr)
        self._broadcast_statuses(items)
        self._broadcast_chat_heads(items)

    def _broadcast_chat_heads(self, items):
        heads: dict[int, bytes] = {}
        for addr, c in items:
            if not c.caps & C_CHAT:
                continue
            pkt = heads.get(c.room)
            if pkt is None:
                deaf = sum(1 for _, p in items if p.room == c.room and not p.caps & C_CHAT)
                pkt = heads[c.room] = self.chat.room(c.room).head_packet(deaf)
            self._send(pkt, addr)

    def _chat_room_send(self, room: int, packets):
        with self.lock:
            addrs = [a for a, p in self.clients.items() if p.room == room and p.caps & C_CHAT]
        for pkt in packets:
            for a in addrs:
                self._send(pkt, a)

    def _chat(self, t: int, data: bytes, addr, now):
        with self.lock:
            c = self.clients.get(addr)
            if c is not None:
                c.last = now
        if c is None or not c.caps & C_CHAT:
            return
        who = c.device or f"{addr[0]}:{addr[1]}"
        if t == T_CHAT_SEND and len(data) >= 9:
            ack, live = self.chat.send(data, who, c.id, c.name, c.device, c.room, now)
            if ack is not None:
                self._send(ack, addr)
            if live:
                self._chat_room_send(c.room, live)
        elif t == T_CHAT_GET and len(data) >= 8:
            for pkt in self.chat.get(data, who, c.room, now):
                self._send(pkt, addr)
        elif t == T_CHAT_DEL and len(data) >= 7:
            out = self.chat.delete(data, c.room, c.device, c.admin, c.name)
            if out:
                self.log(f"chat message deleted in {self._room_name(c.room)} by {c.name}")
                self._chat_room_send(c.room, out)

    def _broadcast_statuses(self, items=None):
        """Everyone's mute/deafen state, to every app that reports its own (so it understands it)."""
        if items is None:
            with self.lock:
                items = list(self.clients.items())
        entries = sorted((c.id, c.status) for _, c in items)[:255]
        pkt = bytes([T_STATUSES, len(entries)]) + b"".join(struct.pack("!HB", u, s) for u, s in entries)
        for addr, c in items:
            if c.status_aware:
                self._send(pkt, addr)

    def _bans_packet(self) -> bytes:
        items = self.bans.items[-BANS_IN_PACKET:]
        b = bytearray([T_BANS, len(items)])
        for e in items:
            b += struct.pack("!H", int(e.get("id", 0)) & 0xFFFF) + _pstr(str(e.get("name", "?"))) \
                + _pstr(str(e.get("when", "")))
        return bytes(b)

    def _send_bans_to_admins(self):
        pkt = self._bans_packet()
        with self.lock:
            addrs = [a for a, c in self.clients.items() if c.admin]
        for a in addrs:
            self._send(pkt, a)

    def _stop_share(self, room: int, why: str = "stopped sharing"):
        uid = self.sharers.pop(room, None)
        if uid is None:
            return
        self._video_budget.pop(uid, None)
        self._vcache.pop(uid, None)
        with self.lock:
            for c in self.clients.values():
                if c.watching == uid:
                    c.watching = 0
            name = next((c.name for c in self.clients.values() if c.id == uid), "someone")
        self.log(f"screen share ended ({why}): {name}")

    def _leave_room(self, c: _Peer):
        """Undo everything that ties a person to their current room."""
        if self.sharers.get(c.room) == c.id:
            self._stop_share(c.room, "left the room")
        c.watching = 0

    def _remove(self, addr, why: str):
        with self.lock:
            c = self.clients.pop(addr, None)
            empty = not self.clients
        if c is None:
            return None
        self._leave_room(c)
        with self.lock:
            for p in self.clients.values():
                if p.watching == c.id:
                    p.watching = 0
        self.log(f"{why}: {c.name} ({c.ip})")
        if empty and self.kicked:
            self.kicked.clear()  # the session is over: kicks end when the server empties
            self.log("server is empty: kicks cleared")
        self._broadcast_roster()
        return c

    def _sweep(self, now):
        with self.lock:
            stale = [a for a, c in self.clients.items() if now - c.last > TIMEOUT]
        for a in stale:
            self._remove(a, "timed out")
        if len(self._fails) > 4096:
            self._fails.clear()

    def _locked_out(self, ip, now) -> bool:
        f = self._fails.get(ip)
        if f is None:
            return False
        if now - f[1] > LOCKOUT_WINDOW:
            del self._fails[ip]
            return False
        return f[0] >= LOCKOUT_FAILS

    def _note_fail(self, ip, now):
        f = self._fails.get(ip)
        if f is None or now - f[1] > LOCKOUT_WINDOW:
            self._fails[ip] = [1, now]
        else:
            f[0] += 1
        if now - self._fail_logged.get(ip, -1e9) > 60.0:
            self._fail_logged[ip] = now
            self.log(f"wrong password from {ip}")

    def _member(self, addr):
        with self.lock:
            return self.clients.get(addr)

    def _room_name(self, rid) -> str:
        r = self.rooms.get(rid)
        return r["name"] if r else "?"

    # ---------------- packets ----------------
    def _handle(self, data: bytes, addr):
        t = data[0]
        now = time.monotonic()
        if t == T_AUDIO:
            with self.lock:
                c = self.clients.get(addr)
                if c is not None:
                    c.last = now
                    others = [a for a, p in self.clients.items() if a != addr and p.room == c.room]
            if c is None:
                self._reject(addr, self._gone.get(addr, "You are not in this room (the server may have restarted)."))
                return
            if len(data) <= 5 or len(data) > 1400:
                return
            fwd = struct.pack("!BH", T_AUDIO, c.id) + data[3:]
            for a in others:
                self._send(fwd, a)
        elif t == T_VIDEO:
            self._video(data, addr, now)
        elif t == T_PING:
            with self.lock:
                c = self.clients.get(addr)
                if c is not None:
                    c.last = now
            if c is None:
                self._reject(addr, self._gone.get(addr, "You are not in this room (the server may have restarted)."))
                return
            self._send(bytes([T_PONG]) + data[1:17], addr)
        elif t == T_HELLO:
            self._hello(data, addr, now)
        elif t == T_BYE:
            self._remove(addr, "left")
        elif t == T_ADMIN:
            self._admin(data, addr)
        elif t == T_MOVE:
            self._move(data, addr)
        elif t == T_SHARE:
            self._share(data, addr)
        elif t == T_WATCH:
            self._watch(data, addr, now)
        elif t == T_KEYREQ:
            c = self._member(addr)
            if c is not None and c.watching and len(data) >= 3:
                self._ask_keyframe(c.watching, now)
        elif t == T_NACK:
            self._nack(data, addr, now)
        elif t == T_STATUS and len(data) >= 2:
            with self.lock:
                c = self.clients.get(addr)
                changed = c is not None and (c.status != (data[1] & 3) or not c.status_aware)
                if c is not None:
                    c.status, c.status_aware, c.last = data[1] & 3, True, now
            if changed:
                self._broadcast_statuses()
        elif t in (T_CHAT_SEND, T_CHAT_GET, T_CHAT_DEL):
            self._chat(t, data, addr, now)

    def _hello(self, data: bytes, addr, now):
        if data[1] != PROTOCOL:
            newer = "a newer" if data[1] < PROTOCOL else "an older"
            self._reject(addr, f"Version mismatch: the server runs {newer} Telsiz. Everyone needs the same version.")
            return
        if len(data) < HELLO_MIN:
            return
        ip = addr[0]
        with self.lock:
            c = self.clients.get(addr)
            if c is not None:
                c.last = now
        if c is not None:  # repeated join request while the welcome was on its way
            self._send(struct.pack("!BHB", T_WELCOME, c.id, self._flags(c)), addr)
            return
        if self._locked_out(ip, now):
            return
        name, off = _ustr(data, 2)
        pw, off = _ustr(data, off)
        device, off = _ustr(data, off)
        device = device[:32]
        caps, want_room = 0, LOBBY_ID  # 1.1/1.2 apps send zero padding here, or nothing at all
        if len(data) >= off + 3:
            caps, want_room = data[off], struct.unpack_from("!H", data, off + 1)[0]
        caps &= C_ROOMS | C_SCREEN | C_CHAT  # ignore abilities this server doesn't know (newer apps)
        ban = self.bans.match(device, ip)
        if ban is not None:
            self._reject(addr, BAN_MSG)
            return
        if device and device in self.kicked:
            self._reject(addr, KICK_MSG)
            return
        admin = bool(self.admin_password) and secrets.compare_digest(pw.encode(), self.admin_password.encode())
        if not admin and self.password and not secrets.compare_digest(pw.encode(), self.password.encode()):
            self._note_fail(ip, now)
            self._reject(addr, "Wrong room password.")
            return
        self._fails.pop(ip, None)
        room = want_room if (caps & C_ROOMS) and self.rooms.get(want_room) else LOBBY_ID
        stale = []
        with self.lock:
            if device:  # same install back from a new address (app restarted): drop the old entry
                stale = [(a, p) for a, p in self.clients.items() if p.device == device]
                for a, _ in stale:
                    self.clients.pop(a)
            if len(self.clients) >= self.max_clients:
                c = None
            else:
                name = (name.strip() or "Guest")[:MAX_NAME]
                taken = {p.name for p in self.clients.values()}
                if name in taken:
                    name = f"{name} ({self._next_id})"
                c = _Peer(self._next_id, name, now, device, ip, admin, caps, room)
                self._next_id = self._next_id + 1 if self._next_id < 0xFFFE else 1
                self.clients[addr] = c
                self._gone.pop(addr, None)
        for _, old in stale:
            self._leave_room(old)
        if c is None:
            self._reject(addr, "The server is full.")
            return
        self._send(struct.pack("!BHB", T_WELCOME, c.id, self._flags(c)), addr)
        self.log(f"joined {self._room_name(room)}: {c.name} ({ip})" + (" as admin" if admin else ""))
        if admin:
            self._send(self._bans_packet(), addr)
        self._broadcast_roster()

    @staticmethod
    def _flags(c: _Peer) -> int:
        return (F_ADMIN if c.admin else 0) | F_ROOMS | F_SCREEN | F_CHAT

    def _move(self, data: bytes, addr):
        c = self._member(addr)
        if c is None or len(data) < 3:
            return
        rid = struct.unpack_from("!H", data, 1)[0]
        if rid == c.room:
            return
        if self.rooms.get(rid) is None:
            self._notice(addr, "That room no longer exists.")
            self._broadcast_roster()
            return
        self._leave_room(c)
        c.room = rid
        self.log(f"moved to {self._room_name(rid)}: {c.name}")
        self._broadcast_roster()

    def _share(self, data: bytes, addr):
        c = self._member(addr)
        if c is None or len(data) < 2 or not (c.caps & C_SCREEN):
            return
        current = self.sharers.get(c.room)
        if data[1]:
            if current == c.id:
                return
            if current is not None:
                other = next((p.name for p in self.clients.values() if p.id == current), "Someone")
                self._notice(addr, f"{other} is already sharing a screen in this room.")
                return
            self.sharers[c.room] = c.id
            self.log(f"screen share started in {self._room_name(c.room)}: {c.name}")
        elif current == c.id:
            self._stop_share(c.room)
        self._broadcast_roster()

    def _watch(self, data: bytes, addr, now):
        c = self._member(addr)
        if c is None or len(data) < 4:
            return
        uid, on = struct.unpack_from("!H", data, 1)[0], data[3]
        if not on:
            if c.watching == uid:
                c.watching = 0
            return
        if self.sharers.get(c.room) != uid or uid == c.id:
            self._notice(addr, "That screen share has ended.")
            return
        c.watching = uid
        self._keyreq_at.pop(uid, None)  # a new viewer needs a full picture right away
        self._ask_keyframe(uid, now)

    def _ask_keyframe(self, uid: int, now):
        if now - self._keyreq_at.get(uid, -10.0) < KEYREQ_GAP:
            return
        with self.lock:
            target = next((a for a, p in self.clients.items() if p.id == uid), None)
        if target is not None and uid in self.sharers.values():
            self._keyreq_at[uid] = now
            self._send(struct.pack("!BH", T_KEYREQ, 0), target)

    def _video(self, data: bytes, addr, now):
        if len(data) <= 10 or len(data) > 1400:
            return
        with self.lock:
            c = self.clients.get(addr)
            if c is None or self.sharers.get(c.room) != c.id:
                return
            c.last = now
            viewers = [a for a, p in self.clients.items() if p.watching == c.id and p.room == c.room]
        budget = self._video_budget.setdefault(c.id, [now, 0])
        if now - budget[0] >= 1.0:
            budget[0], budget[1] = now, 0
        budget[1] += len(data)
        if budget[1] > VIDEO_BYTES_PER_SEC:
            return
        fwd = struct.pack("!BH", T_VIDEO, c.id) + data[3:]
        seq, idx = struct.unpack_from("!HH", data, 3)
        cache = self._vcache.setdefault(c.id, collections.OrderedDict())
        pieces = cache.get(seq)
        if pieces is None:
            pieces = cache[seq] = {}
            while len(cache) > VIDEO_CACHE_FRAMES:
                cache.popitem(last=False)
        pieces[idx] = fwd
        for a in viewers:
            self._send(fwd, a)

    def _nack(self, data: bytes, addr, now):
        """A viewer missed pieces of a picture: resend what we have, ask the sharer for the rest."""
        c = self._member(addr)
        if c is None or not c.watching or len(data) < 6:
            return
        uid, seq, n = struct.unpack_from("!HHB", data, 1)
        if uid != c.watching:
            return
        wanted = list(struct.unpack_from(f"!{n}H", data, 6)) if n and len(data) >= 6 + 2 * n else []
        pieces = self._vcache.get(uid, {}).get(seq)
        budget = self._resend_budget.setdefault(addr, [now, 0])
        if now - budget[0] >= 1.0:
            budget[0], budget[1] = now, 0
        if len(self._resend_budget) > 1024:
            self._resend_budget.clear()
        have = (list(pieces) if not wanted else [i for i in wanted if i in pieces]) if pieces else []
        for i in have:
            if budget[1] >= RESEND_PER_SEC:
                return
            budget[1] += 1
            self._send(pieces[i], addr)
        missing = [i for i in wanted if not pieces or i not in pieces]
        if (missing or not pieces) and now - self._nack_fwd_at.get(uid, -1.0) > 0.02:
            self._nack_fwd_at[uid] = now  # lost on the way from the sharer: the sharer resends to everyone
            with self.lock:
                target = next((a for a, p in self.clients.items() if p.id == uid), None)
            if target is not None:
                ids = missing[:100] if pieces else []
                self._send(struct.pack("!BHHB", T_NACK, 0, seq, len(ids)) + struct.pack(f"!{len(ids)}H", *ids), target)

    def _admin(self, data: bytes, addr):
        me = self._member(addr)
        if me is None or not me.admin or len(data) < 4:
            return
        action, target = data[1], struct.unpack_from("!H", data, 2)[0]
        if action == A_UNBAN:
            b = self.bans.remove(target)
            if b is not None:
                self.log(f"unbanned by {me.name}: {b.get('name')}")
                self._send_bans_to_admins()
            return
        if action == A_ROOM_NEW:
            name, _ = _ustr(data, 4)
            room, err = self.rooms.add(name)
            if room is None:
                self._notice(addr, err)
                return
            self.log(f"room created by {me.name}: {room['name']}")
            self._broadcast_roster()
            return
        if action == A_ROOM_DEL:
            room = self.rooms.get(target)
            if room is None or target == LOBBY_ID:
                return
            with self.lock:
                members = [p for p in self.clients.values() if p.room == target]
            for p in members:
                self._leave_room(p)
                p.room = LOBBY_ID
            self.sharers.pop(target, None)
            self.rooms.remove(target)
            self.chat.drop_room(target)  # its log is kept under another name; a new room starts clean
            self.log(f"room deleted by {me.name}: {room['name']} ({len(members)} moved to Lobby)")
            self._broadcast_roster()
            return
        if action not in (A_KICK, A_BAN):
            return
        with self.lock:
            found = next(((a, p) for a, p in self.clients.items() if p.id == target), None)
        if found is None:
            return
        addr_t, victim = found
        if victim.admin or addr_t == addr:
            return  # admins can't remove each other or themselves
        ban = action == A_BAN
        reason = BAN_MSG if ban else KICK_MSG
        if ban:  # a ban stands on its own, so an unban fully lets the person back in
            self.bans.add(victim.name, victim.device, victim.ip)
        elif victim.device:
            self.kicked.add(victim.device)
        self._gone[addr_t] = reason
        pkt = bytes([T_REJECT]) + _pstr(reason)
        for _ in range(3):
            self._send(pkt, addr_t)
        self._remove(addr_t, f"{'banned' if ban else 'kicked'} by {me.name}")
        if ban:
            self._send_bans_to_admins()


# ---------------- configuration and command line ----------------
def load_config(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text("utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}


def init_config(path: Path, port: int) -> dict:
    cfg = {
        "port": port,
        "password": secrets.token_urlsafe(9),
        "admin_password": secrets.token_urlsafe(15),
        "max_clients": 15,
        "bans_file": "telsiz_bans.json",
        "rooms_file": "telsiz_rooms.json",
        "chat_dir": "chat",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, cfg)
    try:
        os.chmod(path, 0o600)  # passwords inside
    except OSError:
        pass
    return cfg


def _beside(cfg_path: Path, name: str) -> Path:
    return Path(name) if os.path.isabs(name) else cfg_path.parent / name


def main(argv=None):
    ap = argparse.ArgumentParser(description="Telsiz voice chat server (relay only, no audio devices needed)")
    ap.add_argument("--config", default="telsiz_server.json", help="settings file (default: %(default)s)")
    ap.add_argument("--init", action="store_true", help="create the settings file with new passwords and exit")
    ap.add_argument("--port", type=int, help="UDP port (overrides the settings file)")
    ap.add_argument("--password", help="room password (overrides the settings file)")
    ap.add_argument("--admin-password", help="admin password (overrides the settings file)")
    ap.add_argument("--list-bans", action="store_true", help="print the bans and exit")
    ap.add_argument("--unban", type=int, metavar="ID", help="remove a ban by its number and exit")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config).expanduser().resolve()
    cfg = load_config(cfg_path)
    if args.init:
        if cfg:
            print(f"{cfg_path} already exists; not touching it.")
        else:
            cfg = init_config(cfg_path, args.port or DEFAULT_PORT)
            print(f"Created {cfg_path}")
        print(f"  port:           {cfg.get('port', DEFAULT_PORT)} (UDP)")
        print(f"  room password:  {cfg.get('password', '')}")
        print(f"  admin password: {cfg.get('admin_password', '')}")
        return 0

    bans_path = _beside(cfg_path, cfg.get("bans_file", "telsiz_bans.json"))
    rooms_path = _beside(cfg_path, cfg.get("rooms_file", "telsiz_rooms.json"))
    chat_name = cfg.get("chat_dir", "chat")  # "" or null: chat works but is not saved
    chat_dir = _beside(cfg_path, str(chat_name)) if chat_name else None
    if args.list_bans or args.unban is not None:
        bans = BanList(bans_path)
        if args.unban is not None:
            b = bans.remove(args.unban)
            print(f"Unbanned {b.get('name')}." if b else f"No ban with number {args.unban}.")
            return 0 if b else 1
        if not bans.items:
            print("Nobody is banned.")
        for b in bans.items:
            print(f"{b.get('id'):>4}  {b.get('name', '?'):<{MAX_NAME}}  {b.get('when', '')}  {b.get('ip', '')}")
        return 0

    if not cfg and not (args.password or args.admin_password):
        print(f"No settings file at {cfg_path}. Run with --init first, or pass --password and --admin-password.")
        return 2
    port = args.port or int(cfg.get("port", DEFAULT_PORT))
    password = args.password if args.password is not None else str(cfg.get("password", ""))
    admin_pw = args.admin_password if args.admin_password is not None else str(cfg.get("admin_password", ""))
    if admin_pw and admin_pw == password:
        print("The admin password must differ from the room password.")
        return 2
    try:
        server = RelayServer(port, password, admin_pw, cfg.get("max_clients", 15), bans_path,
                             rooms_path=rooms_path, chat_dir=chat_dir)
    except OSError as e:
        print(f"Could not open UDP port {port}: {e}")
        return 1

    def shutdown(*_):
        server.log("stopping")
        server.stop()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
