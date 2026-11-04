#!/usr/bin/env python3
"""
Telsiz server: a dedicated room that runs on a small Linux server (VPS).

Everyone, the owner included, joins it with the normal Telsiz app ("Join a room").
The server only relays packets between people; it never decodes audio or video,
so it needs no sound card, no window and nothing beyond Python's standard library.

Since 1.3 the server has rooms (you only hear people in your own room; admins
create and delete rooms) and relays screen sharing (one sharer per room, sent
only to the people who chose to watch).

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

SERVER_VERSION = "1.4"
PROTOCOL = 2          # same wire protocol as the Telsiz 1.1+ apps; new features are negotiated
DEFAULT_PORT = 50505
MAX_NAME = 32
MAX_ROOM_NAME = 24
MAX_ROOMS = 25
LOBBY_ID = 1
HELLO_MIN = 256
TIMEOUT = 6.0
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
                 bans_path: Path | None = None, bind="0.0.0.0", log=None, rooms_path: Path | None = None):
        self.port = port
        self.password = password
        self.admin_password = admin_password
        self.max_clients = max(1, min(int(max_clients), 50))
        self.bans = BanList(bans_path)
        self.rooms = RoomList(rooms_path)
        self.log = log or (lambda msg: print(f"{_now_text()} {msg}", flush=True))
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
        caps &= C_ROOMS | C_SCREEN  # ignore abilities this server doesn't know (newer apps)
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
        return (F_ADMIN if c.admin else 0) | F_ROOMS | F_SCREEN

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
                             rooms_path=rooms_path)
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
