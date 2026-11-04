#!/usr/bin/env python3
"""
Telsiz: minimal host based voice chat for a gaming group.

One person hosts (opens one UDP port), everyone else joins with the host's
address. Audio is Opus, 48 kHz mono, 20 ms frames. The host relays each
speaker's packets to everyone else without re-encoding, so quality stays at
the sender's level and the host's CPU load stays tiny.

Run:  python telsiz.py
Needs: numpy, sounddevice, av, pywebview (the window; it shows ui/index.html)
       optional: pynput for push to talk, pystray + Pillow for the system
       tray icon on Windows, mss + Pillow for screen sharing
"""
from __future__ import annotations

import collections
import errno
import hashlib
import hmac
import http.server
import inspect
import io
import ipaddress
import json
import math
import os
import queue
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np


APP_NAME = "Telsiz"
APP_VERSION = "1.6"
PROTOCOL = 2
DEFAULT_PORT = 50505

SR = 48000            # sample rate
FRAME = 960           # 20 ms at 48 kHz
BITRATE = 32000       # Opus voice bitrate (bits per second)
MAX_CLIENTS = 15
MAX_NAME = 32
PREBUFFER = 2         # starting jitter buffer: 2 frames (40 ms)
MAX_TARGET = 6        # jitter buffer never aims above 6 frames (120 ms)
TRIM_SLACK = 4        # backlog this far above target gets cut (caps latency)
HANG_FRAMES = 15      # voice gate stays open 300 ms after speech drops
PING_EVERY = 1.0
ROSTER_EVERY = 2.0
TIMEOUT = 6.0
JOIN_TIMEOUT = 8.0
SPEAKING_WINDOW = 0.3
METER_MIN_DB = -80.0
DEFAULT_DEVICE = "System default"
DOT_ON, DOT_OFF = "\u25cf", "\u25cb"
HELLO_MIN = 256       # join requests are padded to this size, so a reply is never larger than the request
INSTANCE_PORT = 47571  # localhost port used to keep Telsiz to a single running copy
KICK_MSG = "You were kicked from this room. You can rejoin after the host restarts it."
BAN_MSG = "You are banned from this room."

T_HELLO, T_WELCOME, T_REJECT, T_ROSTER, T_AUDIO, T_PING, T_PONG, T_BYE = range(1, 9)
T_ADMIN, T_BANS = 9, 10        # used with a dedicated Telsiz server (server/telsiz_server.py)
T_ROOMS, T_MOVE, T_SHARE, T_WATCH, T_VIDEO, T_KEYREQ, T_NOTICE, T_NACK = range(11, 19)  # rooms, screens (1.3+)
A_KICK, A_BAN, A_UNBAN, A_ROOM_NEW, A_ROOM_DEL = 1, 2, 3, 4, 5
T_STATUS, T_STATUSES = 19, 20         # mute/deafen: app -> host or server, and everyone's back (1.4+)
S_MUTED, S_DEAFENED = 1, 2
STATUS_EVERY = 3.0                     # repeat our mute/deafen state this often, in case a packet was lost
F_ADMIN, F_ROOMS, F_SCREEN = 1, 2, 4   # welcome flags: admin password accepted, server has rooms / screens
C_ROOMS, C_SCREEN = 1, 2               # what this app can do, sent in the join request
LOBBY_ID = 1
MAX_ROOM_NAME = 24
VIDEO_CHUNK = 1150                     # bytes of video per packet (10 byte header, stays far below 1400)
SHARE_PRESETS = {
    "smooth": {"label": "Smooth: 720p, 15 fps (games, videos)", "height": 720, "fps": 15, "bitrate": 1_500_000},
    "sharp": {"label": "Sharp text: 1080p, 5 fps (code, documents)", "height": 1080, "fps": 5,
              "bitrate": 1_500_000},
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _pstr(s: str) -> bytes:
    b = s.encode("utf-8")[:255]
    return bytes([len(b)]) + b


def _ustr(buf: bytes, off: int):
    n = buf[off]
    end = off + 1 + n
    if end > len(buf):
        raise ValueError("truncated string")
    return buf[off + 1:end].decode("utf-8", "replace"), end


def lan_ip() -> str:
    """Address of the interface used for outbound traffic (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_ipv4s() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def parse_address(text: str, default_port: int = DEFAULT_PORT):
    text = text.strip()
    if not text:
        raise ValueError("Enter the host's address.")
    host, port = text, default_port
    if text.count(":") == 1:
        host, p = text.split(":")
        try:
            port = int(p)
        except ValueError:
            raise ValueError(f"'{p}' is not a valid port number.")
    if not (1 <= port <= 65535):
        raise ValueError("Port must be between 1 and 65535.")
    return host.strip(), port


def friendly_error(e: Exception) -> str:
    if isinstance(e, socket.gaierror):
        return "Could not find that host. Check the address."
    if isinstance(e, OSError):
        if getattr(e, "winerror", None) == 10048 or e.errno == errno.EADDRINUSE:
            return "That port is already in use. Close the other program or pick another port."
        if getattr(e, "winerror", None) == 10013 or e.errno == errno.EACCES:
            return "Windows refused to open that port. Try another port, or allow Telsiz in the firewall."
    return str(e)


# --------------------------------------------------------------------------
# Opus codec (through PyAV, which ships libopus inside its wheels)
# --------------------------------------------------------------------------
class OpusEncoder:
    def __init__(self, bitrate: int = BITRATE):
        import av
        self._av = av
        ctx = av.CodecContext.create("libopus", "w")
        ctx.sample_rate = SR
        ctx.layout = "mono"
        ctx.format = "s16"
        ctx.bit_rate = bitrate
        ctx.options = {"application": "voip", "frame_duration": "20", "vbr": "on"}
        ctx.open()
        self.ctx = ctx
        self.pts = 0

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        s16 = np.clip(pcm * 32767.0, -32768, 32767).astype(np.int16).reshape(1, -1)
        frame = self._av.AudioFrame.from_ndarray(s16, format="s16", layout="mono")
        frame.sample_rate = SR
        frame.pts = self.pts
        self.pts += s16.shape[1]
        return [bytes(p) for p in self.ctx.encode(frame)]


class OpusDecoder:
    def __init__(self):
        import av
        self._av = av
        ctx = av.CodecContext.create("opus", "r")
        ctx.sample_rate = SR
        ctx.layout = "mono"
        self.ctx = ctx

    def decode(self, payload: bytes) -> list[np.ndarray]:
        out = []
        for frame in self.ctx.decode(self._av.Packet(payload)):
            a = frame.to_ndarray()
            a = a[0] if a.ndim == 2 else a
            if a.dtype == np.int16:
                a = a.astype(np.float32) / 32768.0
            out.append(np.ascontiguousarray(a, dtype=np.float32))
        return out


# --------------------------------------------------------------------------
# Jitter buffer and remote voices
# --------------------------------------------------------------------------
class VoiceBuffer:
    """Adaptive jitter buffer of decoded PCM.

    Playback starts once `target` frames are queued. If the buffer runs dry and
    the stream continues right away (a jitter underrun, not the end of a
    sentence), the target grows by one frame; it shrinks back slowly when the
    network is calm. Excess backlog is trimmed so latency cannot creep up.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.chunks: collections.deque = collections.deque()
        self.size = 0
        self.playing = False
        self.target = PREBUFFER
        self._dry_at = None
        self._changed_at = clock()

    def push(self, pcm: np.ndarray):
        now = self.clock()
        with self.lock:
            if self._dry_at is not None:
                if now - self._dry_at < 0.08 and self.target < MAX_TARGET:
                    self.target += 1          # real underrun: buffer a bit more
                    self._changed_at = now
                self._dry_at = None
            elif self.target > PREBUFFER and now - self._changed_at > 15.0:
                self.target -= 1              # calm network: win latency back
                self._changed_at = now
            self.chunks.append(pcm)
            self.size += len(pcm)
            if self.size > (self.target + TRIM_SLACK) * FRAME:
                while self.size > (self.target + 1) * FRAME and self.chunks:
                    self.size -= len(self.chunks.popleft())

    def clear(self):
        with self.lock:
            self.chunks.clear()
            self.size = 0
            self.playing = False
            self._dry_at = None

    def pull(self, n: int):
        with self.lock:
            if not self.playing:
                if self.size >= self.target * FRAME:
                    self.playing = True
                else:
                    return None
            if self.size == 0:
                self.playing = False
                self._dry_at = self.clock()
                return None
            out = np.zeros(n, np.float32)
            i = 0
            while i < n and self.chunks:
                c = self.chunks[0]
                take = min(len(c), n - i)
                out[i:i + take] = c[:take]
                if take == len(c):
                    self.chunks.popleft()
                else:
                    self.chunks[0] = c[take:]
                i += take
            self.size -= i
            if i < n:  # ran dry: play what we had, then rebuffer
                self.playing = False
                self._dry_at = self.clock()
            return out


def status_flags(engine) -> int:
    """Our own mute/deafen state as sent to others."""
    return (S_MUTED if engine.muted else 0) | (S_DEAFENED if engine.deafened else 0)


def _statuses_packet(items) -> bytes:
    items = list(items)[:255]
    return bytes([T_STATUSES, len(items)]) + b"".join(struct.pack("!HB", uid, st) for uid, st in items)


def _parse_statuses(data: bytes) -> dict:
    n, out = data[1], {}
    for i in range(n):
        uid, st = struct.unpack_from("!HB", data, 2 + 3 * i)
        out[uid] = st & (S_MUTED | S_DEAFENED)
    return out


def _before(a: int, b: int) -> bool:
    """True if 16 bit sequence number a comes before b (wraparound safe)."""
    return a != b and ((b - a) & 0xFFFF) < 0x8000


class RemoteVoice:
    """One speaker's jitter buffer.

    Opus packets are stored by sequence number and decoded in order at playout
    time, so packets that arrive out of order are put back in order. A frame
    that is still missing when its turn comes is concealed (faded repeat of
    the previous frame). Playback starts once `target` packets are queued; the
    target grows after real underruns and shrinks again on a calm network.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.decoder = OpusDecoder()
        self.lock = threading.Lock()
        self.packets: dict[int, bytes] = {}
        self.playing = False
        self.target = PREBUFFER
        self.next_seq = 0
        self.last_heard = 0.0
        self._newest = None
        self._last_played = None
        self._last_pcm = None
        self._miss = 0
        self._carry = np.zeros(0, np.float32)
        self._dry_at = None
        self._changed_at = clock()
        self.stats = {"late": 0, "concealed": 0, "underruns": 0}

    def _oldest(self) -> int:
        ref = self._newest
        return max(self.packets, key=lambda s: (ref - s) & 0xFFFF)

    def receive(self, seq: int, payload: bytes):
        now = self.clock()
        with self.lock:
            fresh = now - self.last_heard > 2.0  # long silence: sender may have restarted
            if not fresh:
                if seq in self.packets:
                    return  # duplicate
                if self._last_played is not None and not _before(self._last_played, seq):
                    self.stats["late"] += 1
                    return  # its turn has already passed
            else:
                self._last_played = None
                self.packets.clear()
                self._newest = None
            if self._dry_at is not None:
                if now - self._dry_at < 0.08:
                    self.stats["underruns"] += 1
                    if self.target < MAX_TARGET:
                        self.target += 1  # stream continued right after running dry
                        self._changed_at = now
                self._dry_at = None
            elif self.target > PREBUFFER and now - self._changed_at > 15.0:
                self.target -= 1      # calm network: win the latency back
                self._changed_at = now
            self.packets[seq] = payload
            self.last_heard = now
            if self._newest is None or _before(self._newest, seq):
                self._newest = seq
            if len(self.packets) > self.target + TRIM_SLACK:  # backlog: skip ahead
                keep = sorted(self.packets, key=lambda s: (self._newest - s) & 0xFFFF)[: self.target + 1]
                self.packets = {s: self.packets[s] for s in keep}
                if self.playing:
                    self.next_seq = self._oldest()

    def pull(self, n: int):
        """Return n samples for the mixer, or None when this speaker is silent."""
        with self.lock:
            while len(self._carry) < n:
                pcm = self._next_frame()
                if pcm is None:
                    break
                self._carry = np.concatenate([self._carry, pcm])
            if len(self._carry) == 0:
                return None
            out = np.zeros(n, np.float32)
            k = min(n, len(self._carry))
            out[:k] = self._carry[:k]
            self._carry = self._carry[k:]
            return out

    def _next_frame(self):
        if not self.playing:
            if self.packets and len(self.packets) >= self.target:
                self.playing = True
                self.next_seq = self._oldest()
                self._miss = 0
            else:
                return None
        if not self.packets:  # end of a sentence, or an underrun
            self.playing = False
            self._dry_at = self.clock()
            self._last_pcm = None
            return None
        payload = self.packets.pop(self.next_seq, None)
        if payload is None:
            self._miss += 1
            if self._miss <= 3:  # conceal a short hole, later packets are waiting
                self.stats["concealed"] += 1
                self._last_played = self.next_seq
                self.next_seq = (self.next_seq + 1) & 0xFFFF
                if self._last_pcm is None:
                    return np.zeros(FRAME, np.float32)
                self._last_pcm = self._last_pcm * 0.5
                return self._last_pcm
            self.next_seq = self._oldest()  # long hole: jump to what we have
            payload = self.packets.pop(self.next_seq)
        self._miss = 0
        self._last_played = self.next_seq
        self.next_seq = (self.next_seq + 1) & 0xFFFF
        try:
            frames = self.decoder.decode(payload)
        except Exception:
            frames = []
        pcm = np.concatenate(frames) if frames else np.zeros(FRAME, np.float32)
        self._last_pcm = pcm
        return pcm


# --------------------------------------------------------------------------
# Audio devices
# --------------------------------------------------------------------------
_sd = None


def get_sd():
    global _sd
    if _sd is None:
        import sounddevice
        _sd = sounddevice
    return _sd


def _api_index(sd) -> int:
    """WASAPI on Windows (low latency, full device names); system default elsewhere."""
    if sys.platform == "win32":
        for i, a in enumerate(sd.query_hostapis()):
            if a["name"] == "Windows WASAPI":
                return i
    try:
        return int(sd.default.hostapi)
    except Exception:
        return 0


def _chan_key(kind: str) -> str:
    return "max_input_channels" if kind == "input" else "max_output_channels"


def list_devices(kind: str) -> list[str]:
    sd = get_sd()
    api = _api_index(sd)
    key = _chan_key(kind)
    names: list[str] = []
    for d in sd.query_devices():
        n = d["name"]
        if d["hostapi"] == api and d[key] > 0 and "[Loopback]" not in n and n not in names:
            names.append(n)
    return names


def _resolve(sd, name, kind):
    """Map a saved device name to (index, extra_settings). Falls back to the API default."""
    api = _api_index(sd)
    key = _chan_key(kind)
    idx = None
    if name:
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == api and d["name"] == name and d[key] > 0:
                idx = i
                break
    if idx is None:
        dflt = sd.query_hostapis(api)["default_input_device" if kind == "input" else "default_output_device"]
        idx = dflt if dflt is not None and dflt >= 0 else None
    extra = None
    if idx is not None and sys.platform == "win32" and sd.query_hostapis(api)["name"] == "Windows WASAPI":
        try:
            extra = sd.WasapiSettings(auto_convert=True)  # lets Windows resample/remix to 48 kHz mono
        except TypeError:
            extra = None
    return idx, extra


# --------------------------------------------------------------------------
# Push to talk chimes (generated, so there are no sound files to ship)
# --------------------------------------------------------------------------
def _chime(freqs, note_ms=70, gap_ms=12, peak=0.16):
    n = int(SR * note_ms / 1000)
    t = np.arange(n) / SR
    att, rel = int(SR * 0.004), int(SR * 0.012)
    parts = []
    for f in freqs:
        env = np.exp(-t * 26.0)                    # bell-like decay
        env[:att] *= np.linspace(0.0, 1.0, att)    # soft start: no click
        env[-rel:] *= np.linspace(1.0, 0.0, rel)   # soft end: no click
        tone = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)
        parts += [tone * env, np.zeros(int(SR * gap_ms / 1000))]
    x = np.concatenate(parts)
    return (x * (peak / np.abs(x).max())).astype(np.float32)


CHIME_ON = _chime((784.0, 1046.5))                          # G5 then C6: rising, "you are live"
CHIME_OFF = _chime((1046.5, 784.0), note_ms=55, peak=0.11)  # falling and softer: "you stopped"


# --------------------------------------------------------------------------
# Audio engine: capture -> gate -> encode -> send ; receive -> decode -> mix -> play
# --------------------------------------------------------------------------
class AudioEngine:
    def __init__(self):
        self.encoder = OpusEncoder()
        self.voices: dict[int, RemoteVoice] = {}
        self.voices_lock = threading.Lock()
        self.volumes: dict[int, float] = {}
        self.local = VoiceBuffer()
        self.in_gain = 1.0
        self.out_gain = 1.0
        self.threshold_db = -45.0
        self.mode = "voice"           # "voice" or "ptt"
        self.muted = False
        self.deafened = False
        self.ptt_down = False
        self.ptt_sounds = True
        self._ptt_chimed = False
        self._fx = None               # chime currently playing (local only, never sent)
        self._fx_pos = 0
        self._fx_lock = threading.Lock()
        self.loopback = False
        self.level_db = METER_MIN_DB
        self.transmitting = False
        self.sender = None            # callable(seq, payload) set by the network session
        self.seq = 0
        self.mic_error = ""
        self.out_error = ""
        self._hang = 0
        self._in = None
        self._out = None
        self._thread = None
        self._running = False

    # ---- device control ----
    def start(self, mic_name=None, out_name=None):
        self.stop()
        sd = get_sd()
        self.mic_error = self.out_error = ""
        idx, extra = _resolve(sd, out_name, "output")
        self._out = self._open(sd, "output", idx, extra, out_name)
        idx, extra = _resolve(sd, mic_name, "input")
        self._in = self._open(sd, "input", idx, extra, mic_name)
        if self._out is not None:
            self._out.start()
        if self._in is not None:
            self._in.start()
            self._running = True
            self._thread = threading.Thread(target=self._capture_loop, daemon=True)
            self._thread.start()

    def _open(self, sd, kind, idx, extra, wanted_name):
        attempts = [(idx, extra)]
        if idx is not None:
            attempts.append((None, None))  # host system default as a last resort
        last = None
        for dev, ex in attempts:
            try:
                if kind == "input":
                    ch = 1
                    cls = sd.InputStream
                    kw = {}
                else:
                    info = sd.query_devices(dev, "output") if dev is not None else sd.query_devices(kind="output")
                    ch = 2 if info["max_output_channels"] >= 2 else 1
                    cls = sd.OutputStream
                    kw = {"callback": self._out_cb}
                stream = cls(device=dev, samplerate=SR, channels=ch, dtype="float32",
                             blocksize=FRAME, latency="low", extra_settings=ex, **kw)
                note = ""
                if wanted_name and (dev is None or sd.query_devices(dev)["name"] != wanted_name):
                    note = f"'{wanted_name}' unavailable, using the system default."
                if dev is None and idx is not None:
                    note = f"Selected device failed ({last}); using the system default."
                if kind == "input":
                    self.mic_error = note
                else:
                    self.out_error = note
                return stream
            except Exception as e:
                last = e
        msg = f"{'Microphone' if kind == 'input' else 'Headphones'} could not be opened: {last}"
        if kind == "input":
            self.mic_error = msg
        else:
            self.out_error = msg
        return None

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        for s in (self._in, self._out):
            if s is not None:
                try:
                    s.abort()
                    s.close()
                except Exception:
                    pass
        self._in = self._out = None
        self.level_db = METER_MIN_DB
        self.transmitting = False

    # ---- push to talk ----
    def play_fx(self, sound: np.ndarray):
        with self._fx_lock:
            self._fx, self._fx_pos = sound, 0

    def ptt_changed(self, down: bool):
        """Push to talk key went down or up (edges only, key repeat filtered out by the caller)."""
        if down:
            self.ptt_down = True
            # chime only when pressing really puts you on air (not while muted or deafened)
            self._ptt_chimed = self.ptt_sounds and not (self.muted or self.deafened)
            if self._ptt_chimed:
                self.play_fx(CHIME_ON)
        else:
            self.ptt_down = False
            if self._ptt_chimed:
                self._ptt_chimed = False
                if self.ptt_sounds:
                    self.play_fx(CHIME_OFF)

    # ---- capture side ----
    def _capture_loop(self):
        while self._running:
            try:
                data, _overflow = self._in.read(FRAME)
            except Exception as e:
                self.mic_error = f"Microphone stopped: {e}. Press Refresh."
                self.level_db = METER_MIN_DB
                self.transmitting = False
                break
            self.process_frame(data[:, 0].copy())

    def process_frame(self, x: np.ndarray):
        """Gain, level meter, voice gate, encode and send one 20 ms frame."""
        x *= self.in_gain
        rms = float(np.sqrt(np.mean(x * x)))
        db = 20.0 * math.log10(rms + 1e-9)
        self.level_db = db
        if self.muted or self.deafened:
            open_ = False
            self._hang = 0
        elif self.mode == "ptt":
            open_ = self.ptt_down
        else:
            if db >= self.threshold_db:
                self._hang = HANG_FRAMES
            elif self._hang > 0:
                self._hang -= 1
            open_ = self._hang > 0
        self.transmitting = open_
        if not open_:
            return
        np.clip(x, -1.0, 1.0, out=x)
        if self.loopback:
            self.local.push(x.copy())
        send = self.sender
        if send is not None:
            for pkt in self.encoder.encode(x):
                try:
                    send(self.seq, pkt)
                except Exception:
                    pass
                self.seq = (self.seq + 1) & 0xFFFF

    # ---- playback side ----
    def _out_cb(self, outdata, frames, time_info, status):
        try:
            mix = np.zeros(frames, np.float32)
            with self.voices_lock:
                items = list(self.voices.items())
            for sid, v in items:
                pcm = v.pull(frames)
                if pcm is not None:
                    mix += pcm * self.volumes.get(sid, 1.0)
            if self.loopback:
                pcm = self.local.pull(frames)
                if pcm is not None:
                    mix += pcm
            with self._fx_lock:
                fx = self._fx
                if fx is not None:
                    seg = fx[self._fx_pos:self._fx_pos + frames]
                    mix[:len(seg)] += seg
                    self._fx_pos += frames
                    if self._fx_pos >= len(fx):
                        self._fx = None
            if self.deafened:
                mix[:] = 0.0
            else:
                mix *= self.out_gain
                np.clip(mix, -1.0, 1.0, out=mix)
            outdata[:] = mix[:, None]
        except Exception:
            outdata.fill(0)

    # ---- called by the network side ----
    def receive(self, sid: int, seq: int, payload: bytes):
        with self.voices_lock:
            v = self.voices.get(sid)
            if v is None:
                v = self.voices[sid] = RemoteVoice()
        v.receive(seq, payload)

    def retain(self, ids):
        with self.voices_lock:
            for sid in [s for s in self.voices if s not in ids]:
                del self.voices[sid]

    def clear_voices(self):
        with self.voices_lock:
            self.voices.clear()

    def speaking(self) -> set[int]:
        now = time.monotonic()
        with self.voices_lock:
            return {sid for sid, v in self.voices.items() if now - v.last_heard < SPEAKING_WINDOW}


# --------------------------------------------------------------------------
# Network: host relays, clients connect to the host
# --------------------------------------------------------------------------
class _Peer:
    __slots__ = ("id", "name", "last", "device", "ip", "status", "status_aware")

    def __init__(self, pid, name, last, device="", ip=""):
        self.id, self.name, self.last = pid, name, last
        self.device, self.ip = device, ip
        self.status, self.status_aware = 0, False


class HostSession:
    role = "host"

    def __init__(self, engine: AudioEngine, name: str, port: int, password: str = "", bans=None):
        self.engine = engine
        self.name = (name.strip() or "Host")[:MAX_NAME]
        self.port = port
        self.password = password
        self.bans = bans if bans is not None else []  # shared with the app's settings; entries are dicts
        self.kicked: set[str] = set()                   # device ids kicked during this hosting run
        self._gone: dict[tuple, str] = {}               # address -> reason, for late packets after a kick
        self._reply_log: dict[str, float] = {}          # stranger IP -> last reply time (rate limit)
        self._reply_window = [0.0, 0]
        self.my_id = 0
        self.state = "connected"
        self.status = f"Hosting on UDP port {port}. Waiting for friends."
        self.clients: dict[tuple, _Peer] = {}
        self.lock = threading.Lock()
        self._next_id = 1
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("0.0.0.0", port))
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.25)
        self.alive = True
        engine.sender = self.send_audio
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # public
    def roster(self):
        with self.lock:
            return [(0, self.name)] + sorted((c.id, c.name) for c in self.clients.values())

    def info(self) -> str:
        n = len(self.clients)
        return f"{n} friend{'s' if n != 1 else ''} connected"

    def send_audio(self, seq: int, payload: bytes):
        pkt = struct.pack("!BHH", T_AUDIO, 0, seq) + payload
        with self.lock:
            addrs = list(self.clients)
        for a in addrs:
            self._send(pkt, a)

    def kick(self, uid: int, ban: bool = False):
        """Remove a guest. A kick blocks their device until hosting restarts.
        Returns a ban entry (name, device, ip, when) for the caller to keep, or None."""
        with self.lock:
            found = next(((a, c) for a, c in self.clients.items() if c.id == uid), None)
            if found is None:
                return None
            addr, c = found
            del self.clients[addr]
            if c.device:
                self.kicked.add(c.device)
        reason = BAN_MSG if ban else KICK_MSG
        self._gone[addr] = reason
        pkt = bytes([T_REJECT]) + _pstr(reason)
        for _ in range(3):  # UDP can drop one; the guest also gets it again if it keeps sending
            self._send(pkt, addr)
        with self.engine.voices_lock:
            self.engine.voices.pop(uid, None)
        self._broadcast_roster()
        return {"name": c.name, "device": c.device, "ip": c.ip, "when": time.strftime("%Y-%m-%d %H:%M")}

    def forgive(self, entry: dict):
        """Undo the session block that came with a ban (used when unbanning)."""
        self.kicked.discard(entry.get("device", ""))
        for a in [a for a in self._gone if a[0] == entry.get("ip")]:
            self._gone.pop(a, None)

    def _blocked(self, device: str, ip: str):
        for b in list(self.bans):  # bans first: a ban also blocks the device for this run
            if (device and b.get("device") == device) or (ip and b.get("ip") == ip):
                return BAN_MSG
        if device and device in self.kicked:
            return KICK_MSG
        return None

    def close(self):
        if not self.alive:
            return
        self.alive = False
        self.engine.sender = None
        with self.lock:
            addrs = list(self.clients)
            self.clients.clear()
        for a in addrs:
            self._send(bytes([T_BYE]), a)
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)
        self.engine.clear_voices()
        self.state = "closed"

    # internals
    def _send(self, data: bytes, addr):
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass

    def _roster_packet(self) -> bytes:
        entries = self.roster()
        b = bytearray([T_ROSTER, len(entries)])
        for uid, nm in entries:
            b += struct.pack("!H", uid) + _pstr(nm)
        return bytes(b)

    def _broadcast_roster(self):
        pkt = self._roster_packet()
        with self.lock:
            addrs = list(self.clients)
        for a in addrs:
            self._send(pkt, a)
        self._broadcast_statuses()
        self._update_status()

    @property
    def statuses(self) -> dict:
        with self.lock:
            d = {c.id: c.status for c in self.clients.values()}
        d[0] = status_flags(self.engine)
        return d

    def _broadcast_statuses(self):
        pkt = _statuses_packet(sorted(self.statuses.items()))
        with self.lock:
            addrs = [a for a, c in self.clients.items() if c.status_aware]
        for a in addrs:
            self._send(pkt, a)

    def _update_status(self):
        n = len(self.clients)
        self.status = (f"Hosting on UDP port {self.port}. "
                       + ("Waiting for friends." if n == 0 else self.info() + "."))

    def _reject(self, addr, reason: str):
        """Answer a stranger. Rate limited per IP and overall, so the room can't be used to flood anyone."""
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

    def _loop(self):
        last_sweep = last_roster = time.monotonic()
        my_status = status_flags(self.engine)
        while self.alive:
            data = addr = None
            if status_flags(self.engine) != my_status:  # the host muted or deafened: tell everyone now
                my_status = status_flags(self.engine)
                self._broadcast_statuses()
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                pass
            except ConnectionResetError:  # Windows reports ICMP "port unreachable" this way
                pass
            except OSError:
                if not self.alive:
                    break
            if data:
                try:
                    self._handle(data, addr)
                except (ValueError, IndexError, struct.error):
                    pass  # malformed packet
            now = time.monotonic()
            if now - last_sweep > 1.0:
                last_sweep = now
                gone = []
                with self.lock:
                    for a, c in list(self.clients.items()):
                        if now - c.last > TIMEOUT:
                            gone.append(c.id)
                            del self.clients[a]
                if gone:
                    with self.engine.voices_lock:
                        for gid in gone:
                            self.engine.voices.pop(gid, None)
                    self._broadcast_roster()
                    last_roster = now
            if now - last_roster > ROSTER_EVERY:
                last_roster = now
                self._broadcast_roster()

    def _handle(self, data: bytes, addr):
        t = data[0]
        now = time.monotonic()
        if t == T_AUDIO:
            with self.lock:
                c = self.clients.get(addr)
                if c is not None:
                    c.last = now
                    others = [a for a in self.clients if a != addr]
            if c is None:
                self._reject(addr, self._gone.get(addr, "You are not in this room (the host may have restarted)."))
                return
            if len(data) <= 5 or len(data) > 1400:
                return
            seq = struct.unpack_from("!H", data, 3)[0]
            payload = data[5:]
            fwd = struct.pack("!BHH", T_AUDIO, c.id, seq) + payload
            for a in others:
                self._send(fwd, a)
            self.engine.receive(c.id, seq, payload)
        elif t == T_PING:
            with self.lock:
                c = self.clients.get(addr)
                if c is not None:
                    c.last = now
            if c is None:
                self._reject(addr, self._gone.get(addr, "You are not in this room (the host may have restarted)."))
                return
            self._send(bytes([T_PONG]) + data[1:17], addr)
        elif t == T_HELLO:
            proto = data[1]
            if proto != PROTOCOL:
                newer = "a newer" if proto < PROTOCOL else "an older"
                self._reject(addr, f"Version mismatch: the host runs {newer} {APP_NAME}. "
                                   f"Everyone needs the same version.")
                return
            if len(data) < HELLO_MIN:
                return
            name, off = _ustr(data, 2)
            pw, off = _ustr(data, off)
            device, off = _ustr(data, off)
            device = device[:32]
            with self.lock:
                known = addr in self.clients
            if not known:
                why = self._blocked(device, addr[0])
                if why:
                    self._reject(addr, why)
                    return
            if self.password and pw != self.password:
                self._reject(addr, "Wrong room password.")
                return
            changed = False
            stale = None
            with self.lock:
                c = self.clients.get(addr)
                if c is None:
                    if device:  # same device back from a new address (app restarted): drop the old entry
                        for a, p in list(self.clients.items()):
                            if p.device == device:
                                stale = self.clients.pop(a).id
                    if len(self.clients) >= MAX_CLIENTS:
                        c = None
                    else:
                        name = (name.strip() or "Guest")[:MAX_NAME]
                        taken = {p.name for p in self.clients.values()} | {self.name}
                        if name in taken:
                            name = f"{name} ({self._next_id})"
                        c = _Peer(self._next_id, name, now, device, addr[0])
                        self._next_id = self._next_id + 1 if self._next_id < 0xFFFE else 1
                        self.clients[addr] = c
                        self._gone.pop(addr, None)
                        changed = True
                else:
                    c.last = now
            if stale is not None:
                with self.engine.voices_lock:
                    self.engine.voices.pop(stale, None)
            if c is None:
                self._reject(addr, "The room is full.")
                return
            self._send(struct.pack("!BH", T_WELCOME, c.id), addr)
            if changed:
                self._broadcast_roster()
        elif t == T_BYE:
            with self.lock:
                c = self.clients.pop(addr, None)
            if c is not None:
                with self.engine.voices_lock:
                    self.engine.voices.pop(c.id, None)
                self._broadcast_roster()
        elif t == T_STATUS and len(data) >= 2:
            with self.lock:
                c = self.clients.get(addr)
                changed = c is not None and (c.status != (data[1] & 3) or not c.status_aware)
                if c is not None:
                    c.status, c.status_aware, c.last = data[1] & 3, True, now
            if changed:
                self._broadcast_statuses()


class ClientSession:
    role = "client"

    def __init__(self, engine: AudioEngine, name: str, host: str, port: int, password: str = "",
                 device: str = "", want_room: int = LOBBY_ID):
        self.engine = engine
        self.name = (name.strip() or "Player")[:MAX_NAME]
        self.password = password
        self.device = device
        self.want_room = want_room
        self.flags = 0
        self.connected_at = None
        self.rooms = None              # list of rooms, when the server has rooms
        self.room_id = None
        self.rooms_version = 0
        self.notice = ""
        self.notice_version = 0
        self.viewer = None             # ScreenViewer that receives the screen we watch
        self.on_keyreq = None          # called when a viewer needs a full picture from our share
        self.on_nack = None            # called with (picture, pieces) the server could not resend itself
        self.statuses: dict[int, int] = {}  # mute/deafen state of others (hosts and servers 1.4+)
        self._status_sent, self._status_at = -1, 0.0
        self.addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        self.my_id = None
        self.rtt = None
        self.state = "connecting"
        self.status = "Connecting to the host..."
        self._roster: list[tuple[int, str]] = []
        self._refused = False
        self.is_admin = False          # set when a Telsiz server accepts the admin password
        self.server_bans: list[dict] = []
        self.bans_version = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.25)
        self.alive = True
        self._started = self._last_rx = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def roster(self):
        return list(self._roster)

    def info(self) -> str:
        return f"Ping {self.rtt:.0f} ms" if self.rtt is not None else ""

    def send_audio(self, seq: int, payload: bytes):
        if self.my_id is None:
            return
        self._send(struct.pack("!BHH", T_AUDIO, self.my_id, seq) + payload)

    def admin_kick(self, uid: int, ban: bool = False):
        if self.is_admin:
            self._send(struct.pack("!BBH", T_ADMIN, A_BAN if ban else A_KICK, uid))

    def admin_unban(self, ban_id: int):
        if self.is_admin:
            self._send(struct.pack("!BBH", T_ADMIN, A_UNBAN, ban_id))

    # ---- rooms and screens (Telsiz server 1.3+) ----
    @property
    def supports_screen(self) -> bool:
        return bool(self.flags & F_SCREEN) and self.rooms is not None

    def my_room(self):
        return next((r for r in self.rooms or () if r["id"] == self.room_id), None)

    def sharer_here(self) -> int:
        r = self.my_room()
        return r["sharer"] if r else 0

    def move(self, room_id: int):
        self._send(struct.pack("!BH", T_MOVE, room_id))

    def admin_room_new(self, name: str):
        if self.is_admin:
            self._send(struct.pack("!BBH", T_ADMIN, A_ROOM_NEW, 0) + _pstr(name[:MAX_ROOM_NAME]))

    def admin_room_del(self, room_id: int):
        if self.is_admin:
            self._send(struct.pack("!BBH", T_ADMIN, A_ROOM_DEL, room_id))

    def set_sharing(self, on: bool):
        self._send(bytes([T_SHARE, 1 if on else 0]))

    def watch(self, uid: int, on: bool):
        self._send(struct.pack("!BHB", T_WATCH, uid, 1 if on else 0))

    def request_keyframe(self, uid: int):
        self._send(struct.pack("!BH", T_KEYREQ, uid))

    def send_nack(self, uid: int, seq: int, pieces):
        pieces = list(pieces)[:100]
        self._send(struct.pack("!BHHB", T_NACK, uid, seq, len(pieces)) + struct.pack(f"!{len(pieces)}H", *pieces))

    def send_video(self, packet: bytes):
        self._send(packet)

    def close(self):
        if not self.alive:
            return
        if self.state == "connected":
            self._send(bytes([T_BYE]))
        self._shutdown("closed", "Not connected")

    def _send(self, data: bytes):
        try:
            self.sock.sendto(data, self.addr)
        except OSError:
            pass

    def _shutdown(self, state: str, status: str):
        self.alive = False
        self.engine.sender = None
        self.engine.clear_voices()
        self.status = status
        self.state = state
        try:
            self.sock.close()
        except OSError:
            pass

    def _fail(self, msg: str):
        self._shutdown("failed", msg)

    def _loop(self):
        last_hello = last_ping = 0.0
        hello = bytes([T_HELLO, PROTOCOL]) + _pstr(self.name) + _pstr(self.password) + _pstr(self.device)
        hello += bytes([C_ROOMS | C_SCREEN]) + struct.pack("!H", self.want_room)  # older hosts ignore this
        hello += bytes(max(0, HELLO_MIN - len(hello)))
        while self.alive:
            now = time.monotonic()
            if self.state == "connecting":
                if now - last_hello > 0.5:
                    last_hello = now
                    self._send(hello)
                if now - self._started > JOIN_TIMEOUT:
                    self._fail(self._no_answer_message())
                    break
            elif self.state == "connected":
                if now - last_ping > PING_EVERY:
                    last_ping = now
                    self._send(bytes([T_PING]) + struct.pack("!d", time.perf_counter()))
                st = status_flags(self.engine)
                if st != self._status_sent or now - self._status_at > STATUS_EVERY:
                    self._status_sent, self._status_at = st, now
                    self._send(bytes([T_STATUS, st]))  # older hosts and servers ignore this
                if now - self._last_rx > TIMEOUT:
                    self._fail("Connection to the host was lost.")
                    break
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:  # Windows: an ICMP "port unreachable" came back
                if self.state == "connecting":
                    self._refused = True
                continue
            except OSError:
                if not self.alive:
                    break
                continue
            if addr[1] != self.addr[1] or not data:
                continue
            self._last_rx = time.monotonic()
            try:
                self._handle(data)
            except (ValueError, IndexError, struct.error):
                pass

    def _no_answer_message(self) -> str:
        msg = "No answer from the host."
        try:
            ip = ipaddress.ip_address(self.addr[0])
        except ValueError:
            ip = None
        if ip is not None and ip.is_private and not ip.is_loopback:
            return (msg + " That is a home network (private) address: it only works inside the host's "
                    "home network or a shared virtual LAN. Over the internet, use the host's Internet address.")
        if self._refused:
            return (msg + " Something at that address replied that nothing is listening on the port. "
                    "Is the room started, and does the router forward it to the right PC?")
        return (msg + " The packets never reached Telsiz on the host's PC: check that the router forwards "
                "UDP (not TCP) to the host's PC, the host's firewall, and CGNAT. A virtual LAN avoids all three.")

    def _handle(self, data: bytes):
        t = data[0]
        if t == T_AUDIO:
            if len(data) <= 5:
                return
            sid, seq = struct.unpack_from("!HH", data, 1)
            if sid != self.my_id:
                self.engine.receive(sid, seq, data[5:])
        elif t == T_VIDEO:
            v = self.viewer
            if v is not None and len(data) > 10 and struct.unpack_from("!H", data, 1)[0] == v.sharer_id:
                v.feed(data)
        elif t == T_ROOMS:
            (my_room,) = struct.unpack_from("!H", data, 1)
            count, off, rooms = data[3], 4, []
            for _ in range(count):
                (rid,) = struct.unpack_from("!H", data, off)
                name, off = _ustr(data, off + 2)
                sharer, n = struct.unpack_from("!HB", data, off)
                off += 3
                members = []
                for _ in range(n):
                    (uid,) = struct.unpack_from("!H", data, off)
                    nm, off = _ustr(data, off + 2)
                    members.append((uid, nm))
                rooms.append({"id": rid, "name": name, "sharer": sharer, "members": members})
            self.rooms, self.room_id = rooms, my_room
            self.rooms_version += 1
        elif t == T_STATUSES:
            self.statuses = _parse_statuses(data)
        elif t == T_KEYREQ:
            cb = self.on_keyreq
            if cb is not None:
                cb()
        elif t == T_NACK:
            cb = self.on_nack
            if cb is not None and len(data) >= 6:
                seq, n = struct.unpack_from("!HB", data, 3)
                cb(seq, list(struct.unpack_from(f"!{n}H", data, 6)) if len(data) >= 6 + 2 * n else [])
        elif t == T_NOTICE:
            self.notice, _ = _ustr(data, 1)
            self.notice_version += 1
        elif t == T_PONG:
            (ts,) = struct.unpack_from("!d", data, 1)
            rtt = (time.perf_counter() - ts) * 1000.0
            self.rtt = rtt if self.rtt is None else 0.7 * self.rtt + 0.3 * rtt
        elif t == T_ROSTER:
            count = data[1]
            off = 2
            entries = []
            for _ in range(count):
                (uid,) = struct.unpack_from("!H", data, off)
                nm, off = _ustr(data, off + 2)
                entries.append((uid, nm))
            self._roster = entries
            self.engine.retain({uid for uid, _ in entries})
        elif t == T_WELCOME:
            (self.my_id,) = struct.unpack_from("!H", data, 1)
            if len(data) >= 4:  # a Telsiz server adds a flags byte; a hosting PC does not
                self.flags = data[3]
                self.is_admin = bool(data[3] & F_ADMIN)
            if self.state == "connecting":
                self.state = "connected"
                self.connected_at = time.monotonic()
                self.status = "Connected. You are an admin of this room." if self.is_admin else "Connected."
                self.engine.sender = self.send_audio
        elif t == T_BANS:
            count, off, items = data[1], 2, []
            for _ in range(count):
                (bid,) = struct.unpack_from("!H", data, off)
                name, off = _ustr(data, off + 2)
                when, off = _ustr(data, off)
                items.append({"id": bid, "name": name, "when": when})
            self.server_bans = items
            self.bans_version += 1
        elif t == T_REJECT:
            reason, _ = _ustr(data, 1)
            self._fail(reason)
        elif t == T_BYE:
            self._fail("The host closed the room.")


# --------------------------------------------------------------------------
# Screen sharing: capture (mss) -> H.264 (libx264 inside PyAV) -> packets -> server -> viewers
# --------------------------------------------------------------------------
_ARROW = ["X", "XX", "X.X", "X..X", "X...X", "X....X", "X.....X", "X......X", "X.......X", "X........X",
          "X.....XXXXX", "X..X..X", "X.X X..X", "XX  X..X", "X    X..X", "     X..X", "      XX"]


def _arrow_masks(scale: int):
    h, w = len(_ARROW), max(len(r) for r in _ARROW)
    black, white = np.zeros((h, w), bool), np.zeros((h, w), bool)
    for y, row in enumerate(_ARROW):
        for x, ch in enumerate(row):
            black[y, x], white[y, x] = ch == "X", ch == "."
    if scale > 1:
        black = black.repeat(scale, 0).repeat(scale, 1)
        white = white.repeat(scale, 0).repeat(scale, 1)
    return black, white


def _draw_cursor(arr: np.ndarray, mon: dict, masks):
    """Windows screen capture leaves the mouse pointer out; draw a simple arrow where it is."""
    try:
        import ctypes
        import ctypes.wintypes
        pt = ctypes.wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return
    except Exception:
        return
    black, white = masks
    x0, y0 = pt.x - mon["left"], pt.y - mon["top"]
    h, w = black.shape
    H, W = arr.shape[:2]
    if x0 >= W or y0 >= H or x0 + w <= 0 or y0 + h <= 0:
        return
    ax0, ay0 = max(x0, 0), max(y0, 0)
    ax1, ay1 = min(x0 + w, W), min(y0 + h, H)
    region = arr[ay0:ay1, ax0:ax1]
    mb = black[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0]
    mw = white[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0]
    region[mb] = (0, 0, 0, 255)
    region[mw] = (255, 255, 255, 255)


def list_screens():
    """[(index, 'Screen 1: 2560 x 1440'), ...] for the share dialog."""
    import mss as mss_mod
    factory = getattr(mss_mod, "MSS", None) or mss_mod.mss
    with factory() as sct:
        return [(i, f"Screen {i}: {m['width']} x {m['height']}") for i, m in enumerate(sct.monitors) if i > 0]


class ScreenSharer:
    """Captures one screen, encodes H.264 and sends it in small packets. Runs in its own thread."""

    def __init__(self, send, monitor_index: int = 1, preset: str = "smooth"):
        self.send = send
        self.monitor_index = monitor_index
        self.preset = SHARE_PRESETS.get(preset, SHARE_PRESETS["smooth"])
        self.error = ""
        self.alive = True
        self.frames = 0
        self.bytes_sent = 0
        self.size = None
        self._key_wanted = True
        self._seq = 0
        self._sent: "collections.OrderedDict[int, list]" = collections.OrderedDict()  # recent pictures
        self._sent_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_keyframe(self):
        self._key_wanted = True

    def resend(self, seq: int, pieces):
        """The server lost pieces of a picture on the way from us: send them again."""
        with self._sent_lock:
            pkts = self._sent.get(seq)
            if pkts is None:
                return
            todo = [pkts[i] for i in pieces if 0 <= i < len(pkts)] if pieces else list(pkts)
        for pkt in todo[:100]:
            self.send(pkt)

    def stop(self):
        self.alive = False

    def _run(self):
        sct = None
        try:
            from fractions import Fraction
            import av
            import mss as mss_mod
            factory = getattr(mss_mod, "MSS", None) or mss_mod.mss
            sct = factory(with_cursor=True) if sys.platform.startswith("linux") else factory()
            mons = sct.monitors
            mon = mons[self.monitor_index] if 0 < self.monitor_index < len(mons) else mons[1]
            fps, br = self.preset["fps"], self.preset["bitrate"]
            H = max(2, min(self.preset["height"], mon["height"]) // 2 * 2)
            W = max(2, int(round(mon["width"] * H / mon["height"] / 2)) * 2)
            self.size = (W, H)
            enc = av.CodecContext.create("libx264", "w")
            enc.width, enc.height, enc.pix_fmt = W, H, "yuv420p"
            enc.time_base = Fraction(1, fps)
            enc.framerate = Fraction(fps, 1)
            enc.bit_rate = br
            kb = br // 1000
            enc.options = {
                "preset": "veryfast", "tune": "zerolatency", "threads": "2",
                # a full picture every 4 s and on request; picture headers repeated so late viewers can start
                "x264-params": f"keyint={fps * 4}:min-keyint={fps * 4}:scenecut=0:repeat-headers=1:bframes=0:"
                               f"forced-idr=1:vbv-maxrate={kb}:vbv-bufsize={kb // 2}",
            }
            enc.open()
            masks = _arrow_masks(max(1, mon["height"] // 1080)) if sys.platform == "win32" else None
            interval, next_t, last_key, pts = 1.0 / fps, time.perf_counter(), -10.0, 0
            while self.alive:
                shot = sct.grab(mon)
                arr = np.frombuffer(shot.raw, np.uint8).reshape(shot.height, shot.width, 4)
                if masks is not None:
                    _draw_cursor(arr, mon, masks)
                frame = av.VideoFrame.from_ndarray(arr, format="bgra").reformat(
                    width=W, height=H, format="yuv420p", interpolation="AREA")
                frame.pts = pts
                pts += 1
                now = time.perf_counter()
                if self._key_wanted and now - last_key > 0.4:
                    self._key_wanted, last_key = False, now
                    try:
                        frame.pict_type = av.video.frame.PictureType.I
                    except AttributeError:
                        frame.pict_type = "I"
                for pkt in enc.encode(frame):
                    self._send_frame(bytes(pkt), bool(pkt.is_keyframe))
                self.frames += 1
                next_t += interval
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.perf_counter()  # running behind: lower the frame rate instead of catching up
        except Exception as e:
            self.error = f"Screen sharing stopped: {e}"
        finally:
            self.alive = False
            if sct is not None:
                try:
                    sct.close()
                except Exception:
                    pass

    def _send_frame(self, data: bytes, key: bool):
        n = (len(data) + VIDEO_CHUNK - 1) // VIDEO_CHUNK
        if n == 0 or n > 0xFFFF:
            return
        seq = self._seq
        self._seq = (seq + 1) & 0xFFFF
        flags = 1 if key else 0
        pkts = [struct.pack("!BHHHHB", T_VIDEO, 0, seq, i, n, flags) + data[i * VIDEO_CHUNK:(i + 1) * VIDEO_CHUNK]
                for i in range(n)]
        with self._sent_lock:
            self._sent[seq] = pkts
            while len(self._sent) > 64:
                self._sent.popitem(last=False)
        for i, pkt in enumerate(pkts):
            self.send(pkt)
            if i % 8 == 7:
                time.sleep(0.002)  # spread big pictures out a little so home routers don't drop them
        self.bytes_sent += len(data) + n * 38


class ScreenViewer:
    """Puts a watched screen back together in order, asks for lost pieces again, decodes,
    and keeps the newest picture for the window.

    Every picture builds on the one before it, so a picture can only be shown once all earlier
    ones have arrived. Missing pieces are requested again (the server keeps recent video); if a
    picture still can't be completed in time, the viewer skips ahead to the next full picture.
    """
    NACK_WAIT = 0.06    # a piece not here this long after its picture started is asked for again
    NACK_EVERY = 0.08   # repeat the request at most this often
    GIVE_UP = 0.5       # skip ahead when the next picture is still incomplete after this long

    def __init__(self, sharer_id: int, request_key, send_nack=None):
        self.sharer_id = sharer_id
        self._request_key = request_key
        self._send_nack = send_nack or (lambda seq, pieces: None)
        self._frames: dict[int, list] = {}   # picture -> [pieces, {i: bytes}, keyframe, first seen]
        self._next = None                    # next picture to decode
        self._newest = None
        self._waiting_key = True             # nothing decodable until a full picture arrives
        self._miss_since = None
        self._nack_at: dict[int, float] = {}
        self._last_req = -10.0
        self._asm = threading.Lock()
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=6)
        self._box = (960, 540)
        self._latest = None
        self._new = False
        self._lock = threading.Lock()
        self.size = None
        self.shown = self.lost = self.received = self.nacks = 0
        self.error = ""
        self.alive = True
        threading.Thread(target=self._decode_loop, daemon=True).start()
        self._ask_key()

    def set_box(self, w: int, h: int):
        self._box = (max(2, w), max(2, h))

    def take(self):
        with self._lock:
            if not self._new:
                return None
            self._new = False
            return self._latest

    def close(self):
        self.alive = False

    def _ask_key(self):
        now = time.monotonic()
        if now - self._last_req > 0.5:
            self._last_req = now
            try:
                self._request_key()
            except Exception:
                pass

    def feed(self, data: bytes):
        """One video packet (network thread)."""
        seq, idx, cnt, flags = struct.unpack_from("!HHHB", data, 3)
        if cnt == 0 or idx >= cnt:
            return
        now = time.monotonic()
        with self._asm:
            if self._next is not None and not self._waiting_key and _before(seq, self._next):
                return  # already shown or skipped
            f = self._frames.get(seq)
            if f is None:
                if len(self._frames) > 150:  # far behind: start over at the next full picture
                    self._frames.clear()
                    self._waiting_key, self._next = True, None
                f = self._frames[seq] = [cnt, {}, flags & 1, now]
            f[1][idx] = data[10:]
            if self._newest is None or _before(self._newest, seq):
                self._newest = seq
            out = self._advance(now)
        self._deliver(out)

    def poll(self):
        """Called regularly even when no packets come, so stalls are noticed."""
        with self._asm:
            out = self._advance(time.monotonic())
        self._deliver(out)

    def _deliver(self, out):
        for payload in out:
            self.received += 1
            try:
                self._q.put_nowait(payload)
            except queue.Full:  # decoder can't keep up: restart at the next full picture
                with self._asm:
                    self._waiting_key, self._next = True, None
                self._ask_key()
                return

    @staticmethod
    def _complete(f) -> bool:
        return f is not None and len(f[1]) == f[0]

    def _advance(self, now):
        out = []
        for _ in range(2):  # a second pass after skipping ahead
            if self._waiting_key:
                start = None
                for s_, f in self._frames.items():
                    if f[2] and self._complete(f) and (start is None or _before(start, s_)):
                        start = s_
                if start is None:
                    self._nack_scan(now, keys_only=True)
                    self._ask_key()
                    return out
                for old in [k for k in self._frames if _before(k, start)]:
                    del self._frames[old]
                self._next, self._waiting_key, self._miss_since = start, False, None
            while True:
                f = self._frames.get(self._next)
                if self._complete(f):
                    del self._frames[self._next]
                    self._nack_at.pop(self._next, None)
                    out.append(b"".join(f[1][i] for i in range(f[0])))
                    self._next = (self._next + 1) & 0xFFFF
                    self._miss_since = None
                    continue
                later = self._newest is not None and _before(self._next, self._newest)
                if f is None and not later:
                    break  # not sent yet
                if self._miss_since is None:
                    self._miss_since = now if f is None else f[3]
                break
            if self._miss_since is not None and now - self._miss_since > self.GIVE_UP:
                self.lost += 1  # could not complete the picture in time: skip to the next full one
                self._frames.pop(self._next, None)
                self._waiting_key, self._miss_since = True, None
                self._ask_key()
                continue
            self._nack_scan(now)
            break
        return out

    def _nack_scan(self, now, keys_only=False):
        """Ask again for pieces of pictures that should be complete by now."""
        if self._newest is None:
            return
        s_ = self._next if (self._next is not None and not keys_only) else None
        if s_ is None:
            cands = [k for k, f in self._frames.items() if f[2] and not self._complete(f)]
        else:
            cands, k = [], s_
            for _ in range(32):
                if _before(self._newest, k):
                    break  # past the newest picture received: not sent yet, nothing to ask for
                cands.append(k)
                if k == self._newest:
                    break
                k = (k + 1) & 0xFFFF
        for k in cands:
            f = self._frames.get(k)
            if self._complete(f):
                continue
            overdue = k != self._newest or (f is not None and now - f[3] > self.NACK_WAIT)
            if not overdue or now - self._nack_at.get(k, -1.0) < self.NACK_EVERY:
                continue
            self._nack_at[k] = now
            missing = [] if f is None else [i for i in range(f[0]) if i not in f[1]]
            self.nacks += 1
            try:
                self._send_nack(k, missing)
            except Exception:
                pass
        if len(self._nack_at) > 256:
            self._nack_at.clear()

    def _decode_loop(self):
        try:
            import av
            dec = av.CodecContext.create("h264", "r")
        except Exception as e:
            self.error = f"Video decoder unavailable: {e}"
            return
        while self.alive:
            try:
                payload = self._q.get(timeout=0.05)
            except queue.Empty:
                self.poll()
                continue
            try:
                frames = dec.decode(av.Packet(payload))
            except Exception:
                with self._asm:
                    self._waiting_key, self._next = True, None
                self._ask_key()
                continue
            for fr in frames:
                self.size = (fr.width, fr.height)
                bw, bh = self._box
                sc = min(bw / fr.width, bh / fr.height, 1.0)  # never enlarge: the window scales the picture up
                w = max(2, int(fr.width * sc) // 2 * 2)
                h = max(2, int(fr.height * sc) // 2 * 2)
                rgb = fr.reformat(width=w, height=h, format="rgb24").to_ndarray()
                with self._lock:
                    self._latest, self._new = rgb, True
                self.shown += 1


# --------------------------------------------------------------------------
# Push to talk (global key or mouse side button, via pynput)
# --------------------------------------------------------------------------
class PushToTalk:
    def __init__(self, engine: AudioEngine):
        self.engine = engine
        try:
            from pynput import keyboard, mouse
            self._kb, self._ms = keyboard, mouse
            self.available = True
        except Exception:
            self.available = False
        self.key_id = None
        self._capturing = False
        self._captured = None
        self._lock = threading.Lock()
        self._kl = self._ml = None

    def start(self):
        if not self.available or self._kl is not None:
            return
        try:
            self._kl = self._kb.Listener(on_press=self._press, on_release=self._release)
            self._ml = self._ms.Listener(on_click=self._click)
            self._kl.daemon = self._ml.daemon = True
            self._kl.start()
            self._ml.start()
        except Exception:
            self.available = False
            self._kl = self._ml = None

    def stop(self):
        for listener in (self._kl, self._ml):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._kl = self._ml = None
        self.engine.ptt_down = False
        self.engine._ptt_chimed = False

    def begin_capture(self):
        self._capturing = True
        self.start()

    def take_captured(self):
        with self._lock:
            c, self._captured = self._captured, None
        return c

    def _ident(self, key):
        if isinstance(key, self._kb.Key):
            return "key:" + key.name, key.name.replace("_", " ").title()
        vk = getattr(key, "vk", None)
        ch = getattr(key, "char", None)
        label = ch.upper() if ch and ch.isprintable() else (f"Key {vk}" if vk is not None else "?")
        if vk is not None:
            return f"vk:{vk}", label
        if ch:
            return "char:" + ch.lower(), label
        return None, None

    def _press(self, key):
        kid, label = self._ident(key)
        if self._capturing:
            self._capturing = False
            with self._lock:
                self._captured = ("cancel", "") if kid == "key:esc" else (kid, label)
            return
        if kid is not None and kid == self.key_id and not self.engine.ptt_down:
            self.engine.ptt_changed(True)   # held keys auto-repeat; only the first press counts

    def _release(self, key):
        kid, _ = self._ident(key)
        if kid is not None and kid == self.key_id and self.engine.ptt_down:
            self.engine.ptt_changed(False)

    def _click(self, x, y, button, pressed):
        bid = "mouse:" + button.name
        if self._capturing:
            if pressed and button.name not in ("left", "right"):
                self._capturing = False
                with self._lock:
                    self._captured = (bid, "Mouse " + button.name.upper())
            return
        if bid == self.key_id and pressed != self.engine.ptt_down:
            self.engine.ptt_changed(pressed)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
DEFAULTS = {
    "name": os.environ.get("USERNAME") or os.environ.get("USER") or "Player",
    "role": "host",
    "port": DEFAULT_PORT,
    "host_password": "",
    "address": "",
    "join_password": "",
    "mic": None,
    "speaker": None,
    "in_gain": 1.0,
    "out_gain": 1.0,
    "threshold": -45.0,
    "mode": "voice",
    "ptt_key": None,
    "ptt_label": "",
    "ptt_sounds": True,
    "user_volumes": {},
    "device_id": "",
    "bans": [],
    "tray_hint_shown": False,
    "remember_address": True,
    "remember_password": True,
    "share_preset": "smooth",
    "share_monitor": 1,
    "last_rooms": {},
    "update_check": True,   # ask GitHub once a day whether a newer Telsiz exists
    "update_skip": "",      # a version the user chose to skip
}


def settings_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME / "settings.json"


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(settings_path().read_text("utf-8"))
        if isinstance(data, dict):
            s.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not isinstance(s.get("user_volumes"), dict):
        s["user_volumes"] = {}
    if not isinstance(s.get("bans"), list):
        s["bans"] = []
    if not isinstance(s.get("last_rooms"), dict):
        s["last_rooms"] = {}
    s["bans"] = [b for b in s["bans"] if isinstance(b, dict)]
    if not isinstance(s.get("device_id"), str) or len(s["device_id"]) < 8:
        s["device_id"] = secrets.token_hex(8)  # random id of this install, used for kick and ban
    return s


def save_settings(s: dict):
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Tray icon, window icon, single running copy
# --------------------------------------------------------------------------
UI_EVENTS: "queue.Queue[str]" = queue.Queue()  # commands from the tray and from a second launch
# FocusDock palette: survey navy when idle, teal on air, hot red when muted
ICON_COLORS = {"idle": (42, 60, 99), "live": (31, 194, 176), "muted": (255, 79, 110)}
ICON_GLYPH = {"idle": (234, 240, 251, 255), "live": (11, 20, 40, 255), "muted": (11, 20, 40, 255)}


def make_icon(state: str = "idle", size: int = 64):
    """Round microphone badge: navy idle, teal while transmitting, red with a slash when muted."""
    from PIL import Image, ImageDraw
    S = size * 4  # draw large, shrink for smooth edges
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = ICON_COLORS.get(state, ICON_COLORS["idle"]) + (255,)
    white = ICON_GLYPH.get(state, ICON_GLYPH["idle"])
    d.ellipse((0, 0, S - 1, S - 1), fill=bg)
    cx, lw = S // 2, max(2, int(S * 0.055))
    d.rounded_rectangle((cx - S * 0.11, S * 0.17, cx + S * 0.11, S * 0.55), radius=int(S * 0.11), fill=white)
    d.arc((cx - S * 0.21, S * 0.27, cx + S * 0.21, S * 0.66), start=0, end=180, fill=white, width=lw)
    d.line((cx, S * 0.66, cx, S * 0.78), fill=white, width=lw)
    d.line((cx - S * 0.13, S * 0.79, cx + S * 0.13, S * 0.79), fill=white, width=lw)
    if state == "muted":
        d.line((S * 0.24, S * 0.20, S * 0.76, S * 0.84), fill=bg, width=int(S * 0.13))
        d.line((S * 0.24, S * 0.20, S * 0.76, S * 0.84), fill=white, width=int(S * 0.06))
    return img.resize((size, size), Image.LANCZOS)


class Tray:
    """Windows notification area icon (pystray). Menu actions are queued for the app's tick thread."""

    def __init__(self):
        self.available = False
        self.icon = None
        self._state = None
        self._title = None
        self._last_change = 0.0
        if sys.platform != "win32" and not os.environ.get("TELSIZ_TRAY"):
            return  # pystray must own the main thread on macOS; Linux trays vary too much
        try:
            import pystray  # raises ImportError, or ValueError when no desktop backend exists
            self._images = {k: make_icon(k) for k in ICON_COLORS}
        except Exception:
            return
        post = UI_EVENTS.put
        self._muted = self._deafened = False
        menu = pystray.Menu(
            pystray.MenuItem("Open Telsiz", lambda: post("show"), default=True),
            pystray.MenuItem("Mute mic", lambda: post("mute"), checked=lambda item: self._muted),
            pystray.MenuItem("Deafen", lambda: post("deafen"), checked=lambda item: self._deafened),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda: post("exit")),
        )
        try:
            self.icon = pystray.Icon(APP_NAME, self._images["idle"], APP_NAME, menu)
            threading.Thread(target=self.icon.run, daemon=True).start()  # documented as safe on Windows
            self.available = True
            self._state = "idle"
        except Exception:
            self.icon = None

    def update(self, state: str, title: str, muted: bool, deafened: bool):
        if not self.available:
            return
        self._muted, self._deafened = muted, deafened
        now = time.monotonic()
        try:
            if state != self._state and now - self._last_change > 0.25:
                self._state, self._last_change = state, now
                self.icon.icon = self._images[state]
            if title != self._title:
                self._title = title
                self.icon.title = title[:120]
        except Exception:
            pass

    def notify(self, message: str):
        if self.available and getattr(self.icon, "HAS_NOTIFICATION", False):
            try:
                self.icon.notify(message, APP_NAME)
            except Exception:
                pass

    def stop(self):
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass


_INSTANCE_SOCK = None  # the listening socket that marks this copy as the running one


def _instance_socket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: nobody may share or steal the port
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:  # elsewhere this only skips old TIME_WAIT leftovers; two listeners are still impossible
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return srv


def release_single_instance():
    """Before an update restarts Telsiz: let the new copy become the running one."""
    global _INSTANCE_SOCK
    if _INSTANCE_SOCK is not None:
        try:  # shutdown wakes the accept() waiting in serve(); on Linux close alone would leave the port taken
            _INSTANCE_SOCK.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            _INSTANCE_SOCK.close()
        except OSError:
            pass
        _INSTANCE_SOCK = None


def claim_single_instance(wait: float = 0.0):
    """'first' (we listen and take requests), 'other' (asked the running copy to show itself), or 'unknown'.
    wait: seconds to keep trying for the port first (after an update the old copy is still closing)."""
    global _INSTANCE_SOCK
    deadline = time.monotonic() + wait
    while True:
        srv = _instance_socket()
        try:
            srv.bind(("127.0.0.1", INSTANCE_PORT))
            srv.listen(4)
            break
        except OSError:
            srv.close()
            if time.monotonic() >= deadline:
                srv = None
                break
            time.sleep(0.2)
    if srv is None:
        try:
            with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1.0) as c:
                c.sendall(b"show\n")
                c.settimeout(1.0)
                if c.recv(16).startswith(b"ok"):
                    return "other"
        except OSError:
            pass
        return "unknown"  # the port belongs to something else: just run

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(1.0)
                    if conn.recv(16).startswith(b"show"):
                        UI_EVENTS.put("show")
                        conn.sendall(b"ok\n")
                        conn.recv(1)  # let the other side close first, so this port is never left in TIME_WAIT
                except OSError:
                    pass
    _INSTANCE_SOCK = srv
    threading.Thread(target=serve, daemon=True).start()
    return "first"


# --------------------------------------------------------------------------
# Updates from GitHub
#
# Telsiz asks GitHub about the newest release once a day. A newer version is
# offered, never installed without asking. How it installs depends on how this
# copy runs:
#   exe     Telsiz.exe on Windows: download the release's Telsiz.exe, rename the
#           running exe to Telsiz.old.exe (Windows lets a running program be
#           renamed, not replaced), put the new one in its place, restart.
#   git     a git clone: git pull --ff-only, which refuses rather than
#           overwrite local changes.
#   source  an unpacked folder: download the release's source zip, swap the
#           files (each replaced file is kept until all of them are in place).
#   manual  anything else (a folder Telsiz can't write to, a frozen build
#           outside Windows): open the release page in the browser.
# Only https addresses on github.com and githubusercontent.com are used, and
# redirects anywhere else are refused.
# --------------------------------------------------------------------------
UPDATE_REPO = "emirbgeo/Telsiz"
UPDATE_API = os.environ.get("TELSIZ_UPDATE_URL") or f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
UPDATE_EVERY = 24 * 3600.0
UPDATE_FIRST_DELAY = float(os.environ.get("TELSIZ_UPDATE_DELAY") or 8.0)
EXE_ASSET = "Telsiz.exe"
_UPDATE_HOSTS = ("github.com", "api.github.com", "codeload.github.com")
_NEVER_REPLACED = {".git", ".github", ".venv", "venv", "__pycache__", "build", "dist"}


def _update_url_ok(url: str) -> bool:
    u = urllib.parse.urlsplit(str(url))
    test = os.environ.get("TELSIZ_UPDATE_URL")
    if test:  # tests: a local stand-in for GitHub at exactly this scheme, host and port
        t = urllib.parse.urlsplit(test)
        if (u.scheme, u.hostname, u.port) == (t.scheme, t.hostname, t.port):
            return True
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host in _UPDATE_HOSTS or host.endswith(".githubusercontent.com"))


class _OffGitHub(urllib.error.URLError):
    """GitHub redirected a download to an address outside GitHub."""


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _update_url_ok(newurl):
            raise _OffGitHub("redirected away from GitHub")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_github(url: str, accept: str, timeout: float):
    if not _update_url_ok(url):
        raise ValueError("That is not a GitHub address.")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION} (update check)",
                                               "Accept": accept})
    return urllib.request.build_opener(_GitHubRedirects).open(req, timeout=timeout)


def _http_get(url: str, accept: str = "application/octet-stream", limit: int = 2 << 20,
              progress=None, timeout: float = 20.0) -> bytes:
    """Whole response in memory, refusing anything over `limit` bytes."""
    with _open_github(url, accept, timeout) as r:
        total = int(r.headers.get("Content-Length") or 0)
        if total > limit:
            raise ValueError("The download is larger than expected.")
        buf = bytearray()
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            buf += chunk
            if len(buf) > limit:
                raise ValueError("The download is larger than expected.")
            if progress is not None and total:
                progress(len(buf) / total)
    return bytes(buf)


def _download_file(url: str, dest: Path, limit: int, progress=None) -> tuple[int, str]:
    """Stream to a file; returns (size, sha256 hex)."""
    h = hashlib.sha256()
    n = 0
    with _open_github(url, "application/octet-stream", 60.0) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        if total > limit:
            raise ValueError("The download is larger than expected.")
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            n += len(chunk)
            if n > limit:
                raise ValueError("The download is larger than expected.")
            h.update(chunk)
            f.write(chunk)
            if progress is not None and total:
                progress(n / total)
    return n, h.hexdigest()


def _version_key(v):
    """'v1.6', '1.6' and '1.6.0' all give (1, 6, 0, 0); None when there is no version number."""
    text = str(v or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    parts = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if ch not in "0123456789":
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 4 or len(digits) != len(piece):
            break
    return tuple(parts + [0] * (4 - len(parts))) if parts else None


def _update_error(e: Exception) -> str:
    if isinstance(e, _OffGitHub):
        return "The download was sent to an address outside GitHub, so Telsiz refused it."
    if isinstance(e, json.JSONDecodeError):
        return "GitHub sent an answer Telsiz doesn't understand. Try again later."
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (403, 429):
            return "GitHub is limiting update checks from this network right now. Try again in an hour."
        if e.code == 404:
            return "No Telsiz release was found on GitHub."
        return f"GitHub answered with error {e.code}. Try again later."
    if isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout)):
        return "Could not reach GitHub. Check your internet connection."
    if isinstance(e, subprocess.TimeoutExpired):
        return "That took too long and was stopped. Try again later."
    if isinstance(e, PermissionError):
        return "Windows refused to replace a Telsiz file. Close other copies of Telsiz and try again."
    return str(e) or e.__class__.__name__


def _writable(folder: Path) -> bool:
    try:
        probe = folder / f".telsiz-write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def install_kind():
    """(kind, folder, why) where kind is exe, git, source or manual; why explains manual."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if sys.platform != "win32" or exe.suffix.lower() != ".exe":
            return "manual", exe.parent, "this build can only be updated from the download page"
        if not _writable(exe.parent):
            return "manual", exe.parent, "Telsiz can't write to the folder it runs from"
        return "exe", exe.parent, ""
    folder = Path(__file__).resolve().parent
    if (folder / ".git").exists():
        if shutil.which("git"):
            return "git", folder, ""
        return "manual", folder, "this folder is a git clone, but git is not installed"
    if not _writable(folder):
        return "manual", folder, "Telsiz can't write to its folder"
    return "source", folder, ""


def _no_window() -> dict:
    """subprocess options that keep a console window from flashing up on Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def _console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


def _requirement_lines(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(" ".join(line.split()))
    return out


def _last_line(text: str) -> str:
    lines = [x.strip() for x in (text or "").strip().splitlines() if x.strip()]
    return lines[-1][:300] if lines else "no details"


def _files_from_zip(data: bytes) -> dict:
    """The files of a GitHub source zip as {relative path: bytes}, without its top folder."""
    out, total = {}, 0
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("The download is not a valid zip file.") from None
    with z:
        names = [i.filename for i in z.infolist() if not i.is_dir()]
        if not names or "/" not in names[0]:
            raise ValueError("The download doesn't look like Telsiz.")
        top = names[0].split("/", 1)[0] + "/"
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.startswith(top):
                raise ValueError("The download doesn't look like Telsiz.")
            rel = name[len(top):]
            parts = rel.split("/")
            if not rel or "\\" in rel or ":" in rel or any(p in ("", ".", "..") for p in parts):
                raise ValueError("The download contains an unsafe file name.")
            if parts[0] in _NEVER_REPLACED or "__pycache__" in parts:
                continue
            total += info.file_size
            if total > 200 << 20:
                raise ValueError("The download is larger than expected.")
            out[rel] = z.read(info)
    if "telsiz.py" not in out or "ui/index.html" not in out:
        raise ValueError("The download doesn't look like Telsiz.")
    return out


def _replace(src: Path, dst: Path):
    """os.replace, retried briefly: on Windows a virus scanner or a reader can hold a file for a moment."""
    for i in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == 19:
                raise
            time.sleep(0.1)


def _swap_in_files(folder: Path, files: dict):
    """Write every file next to its old copy first, then move them all into place; on any failure
    put back the files already moved, so the folder is never left half updated."""
    stage, backup = folder / ".telsiz-update", folder / ".telsiz-backup"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    try:
        for rel, data in files.items():
            p = stage / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            if rel.endswith(".sh"):
                p.chmod(0o755)
        moved = []
        try:
            for rel in files:
                dest = folder / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    keep = backup / rel
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, keep)
                _replace(stage / rel, dest)
                moved.append(rel)
        except Exception:
            for rel in reversed(moved):
                keep = backup / rel
                try:
                    if keep.exists():
                        _replace(keep, folder / rel)
                    else:
                        (folder / rel).unlink()
                except OSError:
                    pass
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)


def _spawn(cmd: list, env: dict, cwd: str):
    """Start the updated Telsiz as an independent process that outlives this one."""
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # leave any job this process belongs to, so closing us can't take the new copy along
            subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True,
                             creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
            return
        except OSError:
            pass
        subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True, creationflags=flags)
    else:
        subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True, start_new_session=True,
                         stdin=subprocess.DEVNULL)


class Updater:
    """The update check, the install, and what the window shows about them."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.kind, self.folder, self.why_manual = install_kind()
        self.lock = threading.Lock()
        self.state = "idle"        # idle, checking, current, available, working, restarting, error
        self.release = None        # the newest release, when it is newer than this copy
        self.step, self.progress, self.message = "", None, ""
        self.manual = False        # the last check was asked for in the window
        self.dismissed = False     # "Later": no more questions until Telsiz starts again
        self.just_updated = os.environ.pop("TELSIZ_UPDATED", "")
        if self.just_updated and _version_key(APP_VERSION) < (_version_key(self.just_updated) or (0,)):
            # the release was tagged with a higher number than the program inside it says: skip that tag,
            # or it would be offered again every day
            ctl.s["update_skip"] = self.just_updated
            self.just_updated = ""
        self._last_check = None
        if self.kind == "exe":
            threading.Thread(target=self._remove_old_exe, daemon=True).start()

    # ---------- what the window sees ----------
    def snapshot(self) -> dict:
        s = self.ctl.s
        with self.lock:
            rel = self.release or {}
            latest = rel.get("version", "")
            prompt = (self.state in ("available", "working", "restarting")
                      or (self.state == "error" and bool(rel))) and not self.dismissed
            if self.state == "available" and not self.manual and latest == s.get("update_skip"):
                prompt = False
            return {"kind": self.kind, "why": self.why_manual, "state": self.state, "version": APP_VERSION,
                    "latest": latest, "notes": rel.get("notes", ""), "page": bool(rel.get("page")),
                    "auto": bool(s.get("update_check", True)), "step": self.step,
                    "progress": None if self.progress is None else round(self.progress, 3),
                    "message": self.message, "prompt": prompt, "just_updated": self.just_updated}

    def _set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    # ---------- checking ----------
    def run(self):
        """Background loop: first check soon after start, then once a day."""
        if self.ctl.done.wait(UPDATE_FIRST_DELAY):
            return
        while not self.ctl.quitting:
            due = self._last_check is None or time.monotonic() - self._last_check >= UPDATE_EVERY
            if due and self.ctl.s.get("update_check", True):
                self.check(manual=False)
            if self.ctl.done.wait(60.0):
                return

    def check(self, manual: bool = False):
        with self.lock:
            if self.state in ("checking", "working", "restarting"):
                return
            self.state, self.manual, self.message = "checking", manual, ""
            if manual:
                self.dismissed = False
        threading.Thread(target=self._check, args=(manual,), daemon=True).start()

    def _check(self, manual: bool):
        self._last_check = time.monotonic()
        try:
            data = json.loads(_http_get(UPDATE_API, accept="application/vnd.github+json").decode("utf-8"))
            rel = self._parse(data)
        except Exception as e:
            self._set(state="error" if manual else "idle", message=_update_error(e), release=None)
            return
        if rel is None or _version_key(rel["version"]) <= _version_key(APP_VERSION):
            self._set(state="current", release=None)
            return
        self._set(state="available", release=rel)
        if not manual and not self.dismissed and rel["version"] != self.ctl.s.get("update_skip"):
            self.ctl.tray.notify(f"Telsiz {rel['version']} is available. Open Telsiz to update.")

    @staticmethod
    def _parse(d):
        if not isinstance(d, dict) or d.get("draft") or d.get("prerelease"):
            return None
        tag = str(d.get("tag_name") or "").strip()
        if _version_key(tag) is None:
            return None
        exe = None
        for a in d.get("assets") or []:
            if isinstance(a, dict) and str(a.get("name") or "").lower() == EXE_ASSET.lower():
                exe = {"url": str(a.get("browser_download_url") or ""), "size": int(a.get("size") or 0),
                       "digest": str(a.get("digest") or "")}
        page = str(d.get("html_url") or "")
        return {"version": tag[1:] if tag[:1] in "vV" else tag,
                "notes": str(d.get("body") or "").strip()[:3000],
                "page": page if _update_url_ok(page) else "",
                "zip": str(d.get("zipball_url") or ""), "exe": exe}

    # ---------- answers from the window ----------
    def later(self):
        self._set(dismissed=True)

    def skip(self) -> str:
        with self.lock:
            self.dismissed = True
            return (self.release or {}).get("version", "")

    def open_page(self):
        rel = self.release or {}
        page = rel.get("page") or f"https://github.com/{UPDATE_REPO}/releases/latest"
        if _update_url_ok(page):
            import webbrowser
            webbrowser.open(page)

    def install(self) -> dict:
        with self.lock:
            if self.release is None or self.state not in ("available", "error"):
                return {"error": "There is no update to install."}
            if self.kind == "manual":
                manual = True
            else:
                manual = False
                self.state, self.step, self.progress, self.message = "working", "Starting…", None, ""
        if manual:
            self.open_page()
            return {"ok": True, "opened": True}
        threading.Thread(target=self._install, daemon=True).start()
        return {"ok": True}

    # ---------- installing ----------
    def _install(self):
        rel = self.release
        try:
            how = {"exe": self._install_exe, "git": self._install_git, "source": self._install_source}[self.kind]
            cmd, env, cwd = how(rel)
            self._set(state="restarting", step="Restarting Telsiz…", progress=None)
            self.ctl.restart_into(cmd, env, cwd, rel["version"])
        except Exception as e:
            self._set(state="error", step="", progress=None, message=_update_error(e))

    def _install_exe(self, rel):
        exe = Path(sys.executable).resolve()
        a = rel.get("exe")
        if not a or not a.get("url"):
            raise ValueError("This release has no Telsiz.exe to download. Use Open download page.")
        new = exe.with_name(exe.name + ".download")
        self._set(step=f"Downloading Telsiz {rel['version']}…", progress=0.0)
        try:
            size, digest = _download_file(a["url"], new, 400 << 20, lambda f: self._set(progress=f))
            if a["size"] and size != a["size"]:
                raise ValueError("The download was incomplete. Try again.")
            want = a.get("digest") or ""
            if want.startswith("sha256:") and want[7:].lower() != digest:
                raise ValueError("The download is damaged (its checksum doesn't match GitHub's). Try again.")
            with open(new, "rb") as f:
                if f.read(2) != b"MZ":
                    raise ValueError("The download is not a Windows program.")
            self._set(step="Installing…", progress=None)
            old = exe.with_name(f"{exe.stem}.old{exe.suffix}")
            try:
                old.unlink()
            except FileNotFoundError:
                pass
            except OSError:  # a previous old copy is still running: use another name
                old = exe.with_name(f"{exe.stem}.old-{int(time.time())}{exe.suffix}")
            _replace(exe, old)  # renaming a running program is allowed on Windows; replacing it is not
            try:
                _replace(new, exe)
            except Exception:
                _replace(old, exe)  # put this version back
                raise
        except Exception:
            try:
                new.unlink()
            except OSError:
                pass
            raise
        # PyInstaller 6.9+: without this the new exe would reuse this exe's unpacked files, which go away when we exit
        return [str(exe)], {"PYINSTALLER_RESET_ENVIRONMENT": "1"}, str(exe.parent)

    def _install_git(self, rel):
        folder = self.folder
        before = (folder / "requirements.txt").read_text("utf-8", "replace") if (folder / "requirements.txt").exists() else ""
        self._set(step="Running git pull…", progress=None)
        r = subprocess.run(["git", "-C", str(folder), "pull", "--ff-only"], capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL, **_no_window())
        if r.returncode != 0:
            out = (r.stderr or "") + "\n" + (r.stdout or "")
            why = next((x.strip() for x in out.splitlines() if x.strip().startswith(("error:", "fatal:"))), _last_line(out))
            hint = " Commit or stash your changes, then try again." if "local changes" in out else ""
            raise ValueError("git pull failed. " + why[:240] + hint)
        after = (folder / "requirements.txt").read_text("utf-8", "replace") if (folder / "requirements.txt").exists() else ""
        self._install_libraries(before, after)
        return [sys.executable, str(folder / "telsiz.py")], {}, str(folder)

    def _install_source(self, rel):
        folder = self.folder
        if not rel.get("zip"):
            raise ValueError("This release has no source download. Use Open download page.")
        self._set(step=f"Downloading Telsiz {rel['version']}…", progress=0.0)
        data = _http_get(rel["zip"], limit=100 << 20, progress=lambda f: self._set(progress=f), timeout=60.0)
        self._set(step="Checking the files…", progress=None)
        files = _files_from_zip(data)
        before = (folder / "requirements.txt").read_text("utf-8", "replace") if (folder / "requirements.txt").exists() else ""
        self._install_libraries(before, files.get("requirements.txt", b"").decode("utf-8", "replace"))
        self._set(step="Replacing files…", progress=None)
        _swap_in_files(folder, files)
        return [sys.executable, str(folder / "telsiz.py")], {}, str(folder)

    def _install_libraries(self, old_text: str, new_text: str):
        """pip install only the requirement lines that are new, before any file changes.
        (Not the whole list: on Linux that would rebuild pynput's optional parts.)"""
        old = set(_requirement_lines(old_text))
        wanted = [x for x in _requirement_lines(new_text) if x not in old]
        if not wanted:
            return
        self._set(step="Installing new libraries…", progress=None)
        r = subprocess.run([_console_python(), "-m", "pip", "install", "--disable-pip-version-check", *wanted],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL, **_no_window())
        if r.returncode != 0:
            raise ValueError("Installing the new libraries failed: " + _last_line(r.stderr or r.stdout))

    def _remove_old_exe(self):
        """After an update: delete Telsiz.old.exe once the old copy has closed."""
        exe = Path(sys.executable).resolve()
        try:  # a download an earlier copy never finished
            exe.with_name(exe.name + ".download").unlink()
        except OSError:
            pass
        for _ in range(60):
            left = list(exe.parent.glob(f"{exe.stem}.old*{exe.suffix}"))
            if not left:
                return
            for p in left:
                try:
                    p.unlink()
                except OSError:
                    pass
            time.sleep(2.0)


# --------------------------------------------------------------------------
# User interface
#
# The window is a web page (ui/index.html) shown by pywebview: Edge WebView2
# on Windows, WebKitGTK on Linux. The page talks to this process over plain
# HTTP on 127.0.0.1, under a random token, so nothing else on the PC (and
# nothing on the network) can read the state or press its buttons.
# Everything below is window logic only; audio, network and screen code above
# is unchanged.
# --------------------------------------------------------------------------
def resource_dir() -> Path:
    """Folder that holds ui/: next to this file, or inside the one-file exe."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


UI_DIR = resource_dir() / "ui"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".png": "image/png",
    ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8",
}
HIDDEN = "•" * 12


class WebViewer:
    """Someone's shared screen: decoded here, handed to viewer.html as JPEG pictures."""

    def __init__(self, ctl, sess, uid: int, name: str):
        from PIL import Image  # ImportError is reported to the user by the caller
        self._Image = Image
        self.ctl, self.sess, self.uid, self.name = ctl, sess, uid, name
        self.viewer = ScreenViewer(uid, lambda: sess.request_keyframe(uid),
                                   lambda seq, pieces: sess.send_nack(uid, seq, pieces))
        sess.viewer = self.viewer
        sess.watch(uid, True)
        self.alive = True
        self.full = False
        self.window = None
        self.jpeg, self.seq = None, 0
        self.cond = threading.Condition()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.alive:
            try:
                rgb = self.viewer.take()
            except Exception:
                rgb = None
            if rgb is None:
                time.sleep(0.008)
                continue
            buf = io.BytesIO()
            self._Image.fromarray(rgb).save(buf, "JPEG", quality=85)
            with self.cond:
                self.jpeg, self.seq = buf.getvalue(), self.seq + 1
                self.cond.notify_all()

    def frame_after(self, seq: int, timeout: float = 1.0):
        """Wait briefly for a picture newer than seq. (seq, jpeg) or None."""
        with self.cond:
            if self.seq <= seq and self.alive:
                self.cond.wait(timeout)
            if self.seq > seq and self.jpeg is not None:
                return self.seq, self.jpeg
        return None

    def resize(self, w, h):
        self.viewer.set_box(max(16, int(w)), max(16, int(h)))

    def close(self, reason: str = ""):
        if not self.alive:
            return
        self.alive = False
        with self.cond:
            self.cond.notify_all()
        self.viewer.close()
        if self.sess.viewer is self.viewer:
            self.sess.viewer = None
        try:
            self.sess.watch(self.uid, False)
        except Exception:
            pass
        w, self.window = self.window, None
        if w is not None:
            threading.Thread(target=_quietly(w.destroy), daemon=True).start()
        if self.ctl.viewer is self:
            self.ctl.viewer = None
        if reason:
            self.ctl._show_notice(reason)


def _quietly(fn, *args):
    def run():
        try:
            fn(*args)
        except Exception:
            pass
    return run


class LocalServer:
    """Serves ui/ and a small JSON API on 127.0.0.1. Every path starts with a random token."""

    def __init__(self, ctl):
        self.ctl = ctl
        self.token = secrets.token_urlsafe(18)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = APP_NAME
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _rest(self):
                parts = self.path.split("?", 1)[0].split("/", 2)
                if len(parts) < 3 or not hmac.compare_digest(parts[1].encode(), outer.token.encode()):
                    return None
                return urllib.parse.unquote(parts[2])

            def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

            def do_GET(self):
                rest = self._rest()
                if rest is None:
                    return self._send(404)
                if rest == "state":
                    return self._json(outer.ctl.state())
                if rest.startswith("frame/"):
                    v = outer.ctl.viewer
                    try:
                        after = int(rest[6:] or 0)
                    except ValueError:
                        after = 0
                    got = v.frame_after(after) if v is not None else None
                    if got is None:
                        return self._send(204)
                    return self._send(200, got[1], "image/jpeg", {"X-Seq": str(got[0])})
                root = UI_DIR.resolve()
                f = (root / (rest or "index.html")).resolve()
                if root not in f.parents or not f.is_file():
                    return self._send(404)
                self._send(200, f.read_bytes(), CONTENT_TYPES.get(f.suffix.lower(), "application/octet-stream"))

            def do_POST(self):
                rest = self._rest()
                if rest is None or not rest.startswith("api/"):
                    return self._send(404)
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    args = json.loads(self.rfile.read(n) or b"{}") if n else {}
                except ValueError:
                    return self._json({"error": "Bad request."}, 400)
                self._json(outer.ctl.call(rest[4:], args if isinstance(args, dict) else {}))

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, page: str = "index.html") -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/{page}"


class Controller:
    """Everything the window shows and does, with no window code in it."""

    ACTIONS = (
        "set_field", "toggle_connection", "reveal", "copy_address",
        "toggle_mute", "toggle_deafen",
        "set_devices", "refresh_devices", "set_gain", "set_mode", "capture_key",
        "set_threshold", "set_loopback", "set_chimes",
        "set_user_volume", "kick", "watch",
        "join_room", "new_room", "delete_room",
        "screens", "start_share", "stop_share",
        "bans", "unban",
        "hide_window", "quit", "first_run_done",
        "viewer_resize", "viewer_fullscreen", "viewer_close",
        "update_check", "update_install", "update_later", "update_skip", "update_auto", "update_page",
    )

    def __init__(self):
        self.lock = threading.RLock()
        self.first_run = not settings_path().exists()
        self.s = load_settings()
        self.engine = AudioEngine()
        self.ptt = PushToTalk(self.engine)
        self.session = None
        self.form = {
            "name": self.s["name"],
            "role": self.s["role"] if self.s["role"] in ("host", "join") else "host",
            "port": str(self.s["port"]),
            "host_password": self.s["host_password"],
            "address": self.s["address"],
            "join_password": self.s["join_password"],
            "remember_address": bool(self.s.get("remember_address", True)),
            "remember_password": bool(self.s.get("remember_password", True)),
        }
        self._names: dict[int, str] = {}
        self._people_sig = None
        self._public_ip = None
        self._revealed = {"internet": False, "lan": False}
        self._share_cache, self._share_at = None, 0.0
        self._save_timer = None
        self._meter_db = METER_MIN_DB
        self.sharer = None             # ScreenSharer while we share our screen
        self.viewer = None             # WebViewer while we watch someone
        self._share_request = None     # (screen, preset, time) between asking the server and its answer
        self._notice_text, self._notice_until = "", 0.0
        self._seen_notice_version = 0
        self._connected_once = False
        self.status = "Not connected"
        self.devices = {"mic": [DEFAULT_DEVICE], "out": [DEFAULT_DEVICE]}
        self._device_error = ""
        self._capturing = False
        self.webview = None            # the pywebview module, once the window exists
        self.window = None
        self.server = None
        self.browser_mode = False
        self.quitting = False
        self._shut = False
        self.done = threading.Event()
        self.tray = Tray()
        self.updater = Updater(self)
        self._apply_engine_settings()
        self._populate_devices()
        self._start_audio()
        self.set_mode(self.engine.mode, save=False)
        if self.form["role"] == "host":
            self._fetch_public_ip()
        self._schedule_save()  # stores a newly created device id (first run or upgrade from 1.0)

    # ---------- plumbing ----------
    def call(self, name: str, args: dict):
        if name not in self.ACTIONS:
            return {"error": "Unknown action."}
        fn = getattr(self, name)
        try:
            inspect.signature(fn).bind(**args)
        except TypeError:
            return {"error": "Bad request."}
        try:
            with self.lock:
                result = fn(**args)
            return result if isinstance(result, dict) else {"ok": True}
        except Exception as e:
            return {"error": friendly_error(e)}

    def run_ticks(self):
        while not self.quitting:
            try:
                with self.lock:
                    self._tick()
            except Exception as e:  # keep the app alive; the next tick usually recovers
                print(f"{APP_NAME}: tick error: {e!r}", file=sys.stderr)
            time.sleep(0.05)

    def _apply_engine_settings(self):
        e, s = self.engine, self.s
        e.in_gain = float(s["in_gain"])
        e.out_gain = float(s["out_gain"])
        e.threshold_db = float(s["threshold"])
        e.mode = s["mode"] if s["mode"] in ("voice", "ptt") else "voice"
        e.ptt_sounds = bool(s.get("ptt_sounds", True))
        self.ptt.key_id = s["ptt_key"]

    # ---------- the picture the page draws ----------
    def state(self) -> dict:
        with self.lock:
            e, sess = self.engine, self.session
            st = {
                "app": {"name": APP_NAME, "version": APP_VERSION, "tray": self.tray.available,
                        "ptt": self.ptt.available, "browser": self.browser_mode, "first_run": self.first_run},
                "form": dict(self.form),
                "status": self.status,
                "meter": {"db": round(self._meter_db, 1), "tx": bool(e.transmitting), "thr": e.threshold_db,
                          "mode": e.mode, "min": METER_MIN_DB, "down": bool(e.ptt_down)},
                "me": {"muted": e.muted, "deafened": e.deafened},
                "ptt": {"label": self.s.get("ptt_label") or "" if self.s.get("ptt_key") else "",
                        "capturing": self._capturing},
                "audio": self._audio_state(),
                "conn": self._conn_state(sess),
                "people": self._people_state(sess) if sess is not None else None,
                "tools": self.form["role"] == "host" or self._admin_session() is not None,
                "moderate": self._can_moderate(),
                "bans": len(self._ban_entries()[1]),
                "watching": ({"uid": self.viewer.uid, "name": self.viewer.name}
                             if self.viewer is not None else None),
                "update": self.updater.snapshot(),
            }
            if self.form["role"] == "host":
                st["share"] = self._share_info()
            return st

    def _audio_state(self):
        e, s = self.engine, self.s
        ins, outs = self.devices["mic"], self.devices["out"]
        errs = [x for x in (self._device_error, e.mic_error, e.out_error) if x]
        return {
            "mics": ins, "outs": outs,
            "mic": s["mic"] if s["mic"] in ins else DEFAULT_DEVICE,
            "out": s["speaker"] if s["speaker"] in outs else DEFAULT_DEVICE,
            "in_gain": round(e.in_gain * 100), "out_gain": round(e.out_gain * 100),
            "loopback": bool(e.loopback), "chimes": bool(e.ptt_sounds),
            "error": "\n".join(errs),
        }

    def _conn_state(self, sess):
        if sess is None:
            return {"kind": "none"}
        if isinstance(sess, HostSession):
            return {"kind": "host", "state": "connected", "port": sess.port, "info": sess.info(),
                    "password": bool(sess.password), "people": len(sess.clients) + 1, "hint": self._rooms_hint()}
        c = {"kind": "client", "state": sess.state, "info": sess.info(), "admin": bool(sess.is_admin),
             "rooms": sess.rooms is not None, "screen": bool(sess.supports_screen),
             "sharing": self.sharer is not None, "pending": self._share_request is not None,
             "room": None, "hint": self._rooms_hint()}
        r = sess.my_room() if sess.rooms is not None else None
        if r is not None:
            c["room"] = {"id": r["id"], "name": r["name"], "people": len(r["members"])}
        else:
            c["people"] = len(sess.roster())
        return c

    def _rooms_hint(self):
        cur = self.session
        if isinstance(cur, ClientSession) and cur.state == "connected" and cur.rooms is None \
                and cur.connected_at is not None and time.monotonic() - cur.connected_at > 2.0:
            return ("Rooms and screen sharing are off here: they need a Telsiz server 1.3 or newer. "
                    "This is an older server, or a room hosted on a PC.")
        if isinstance(cur, HostSession) and cur.alive:
            return "Rooms and screen sharing work on a Telsiz server, not when hosting from this PC."
        return ""

    def _people_state(self, sess):
        my_id = sess.my_id
        rooms = getattr(sess, "rooms", None)
        speaking = self.engine.speaking()
        if self.engine.transmitting and my_id is not None:
            speaking.add(my_id)
        statuses = dict(getattr(sess, "statuses", None) or {})
        if my_id is not None:
            statuses[my_id] = status_flags(self.engine)
        vols = self.s["user_volumes"]

        def member(uid, name, live, here):
            return {"uid": uid, "name": name, "you": uid == my_id, "host": uid == 0, "live": live,
                    "muted": bool(statuses.get(uid, 0) & S_MUTED),
                    "deafened": bool(statuses.get(uid, 0) & S_DEAFENED),
                    "speaking": here and uid in speaking, "vol": int(vols.get(name, 100)),
                    "watchable": self._can_watch(uid), "watching": self.viewer is not None and self.viewer.uid == uid}

        if rooms is not None:
            out = []
            for r in rooms:
                here = r["id"] == sess.room_id
                out.append({"id": r["id"], "name": r["name"], "here": here, "lobby": r["id"] == LOBBY_ID,
                            "members": [member(uid, name, uid == r["sharer"], here) for uid, name in r["members"]]})
            return {"rooms": out}
        return {"rooms": [{"id": None, "name": "In this room", "here": True, "lobby": True,
                           "members": [member(uid, name, False, True) for uid, name in sess.roster()]}]}

    def _share_info(self):
        now = time.monotonic()
        if self._share_cache is not None and now - self._share_at < 5.0:
            return self._share_cache
        port = self.form["port"].strip() or str(DEFAULT_PORT)
        ip = lan_ip()
        lan = f"{ip}:{port}"
        if self._public_ip is None:
            internet, have = "detecting...", False
        elif self._public_ip == "":
            internet, have = "could not detect (no internet?)", False
        else:
            internet, have = f"{self._public_ip}:{port}", True
        others = [a for a in local_ipv4s() if a != ip]
        self._share_cache = {
            "internet": internet if (self._revealed["internet"] or not have) else HIDDEN,
            "internet_ok": have,
            "lan": lan if self._revealed["lan"] else HIDDEN,
            "revealed": dict(self._revealed),
            "port": port,
            "others": others if self._revealed["lan"] else [],
        }
        self._share_at = now
        return self._share_cache

    def _dirty_share(self):
        self._share_cache = None

    # ---------- the room form ----------
    def set_field(self, key, value):
        if key not in self.form or self.session is not None:
            return {"ok": False}
        if key in ("remember_address", "remember_password"):
            self.form[key] = bool(value)
            self._save_now()
            return {"ok": True}
        value = str(value)[:200]
        if key == "name":
            value = value[:MAX_NAME]
        if key == "role":
            if value not in ("host", "join"):
                return {"ok": False}
            self.form["role"] = value
            if value == "host" and self._public_ip == "":
                self._fetch_public_ip()  # retry after an earlier failure
            self._dirty_share()
            self._schedule_save()
            return {"ok": True}
        self.form[key] = value
        if key == "port":
            self._dirty_share()
        self._schedule_save()
        return {"ok": True}

    def reveal(self, which):
        if which in self._revealed:
            self._revealed[which] = not self._revealed[which]
            self._dirty_share()
        return {"ok": True}

    def copy_address(self, which):
        """The real address, for the clipboard only: the page never puts it on screen unless revealed."""
        port = self.form["port"].strip() or str(DEFAULT_PORT)
        if which == "lan":
            return {"text": f"{lan_ip()}:{port}"}
        if self._public_ip:
            return {"text": f"{self._public_ip}:{port}"}
        return {"error": "The Internet address is not known yet."}

    def _fetch_public_ip(self):
        def work():
            try:
                with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
                    ip = r.read().decode("ascii", "replace").strip()
                socket.inet_aton(ip)
                self._public_ip = ip
            except Exception:
                self._public_ip = ""
            self._dirty_share()
        threading.Thread(target=work, daemon=True).start()

    def _room_key(self) -> str:
        """Last room per server, stored under a hash so the address itself isn't written down."""
        return hashlib.sha256(self.form["address"].strip().encode()).hexdigest()[:16]

    def toggle_connection(self):
        if self.session is not None:
            self._end_screens()
            self.session.close()
            self.session = None
            self._session_ended()
            self.status = "Not connected"
            return {"ok": True}
        self._collect_settings()
        name = self.s["name"].strip() or "Player"
        try:
            if self.form["role"] == "host":
                try:
                    port = int(self.form["port"])
                except ValueError:
                    port = 0
                if not 1 <= port <= 65535:
                    raise ValueError("Port must be a number between 1 and 65535.")
                self.session = HostSession(self.engine, name, port, self.form["host_password"], bans=self.s["bans"])
                self._fetch_public_ip()
            else:
                if not self.form["address"].strip():
                    raise ValueError("Type the address of the host or server first.")
                host, port = parse_address(self.form["address"])
                want = int(self.s["last_rooms"].get(self._room_key(), LOBBY_ID))
                self.session = ClientSession(self.engine, name, host, port, self.form["join_password"],
                                             device=self.s["device_id"], want_room=want)
        except Exception as e:
            self.session = None
            return {"error": friendly_error(e)}
        self._seen_notice_version = 0
        self._people_sig = None
        self.status = self.session.status
        self._schedule_save()
        return {"ok": True}

    def _session_ended(self):
        self._end_screens()
        self._people_sig = None
        self._names = {}

    # ---------- people, rooms, moderation ----------
    def _sync_people(self, sess):
        rooms = getattr(sess, "rooms", None)
        if rooms is not None:
            sig = (sess.room_id, tuple((r["id"], tuple(r["members"])) for r in rooms))
            pairs = [m for r in rooms for m in r["members"]]
        else:
            pairs = sess.roster()
            sig = ("flat", tuple(pairs))
        if sig == self._people_sig:
            return
        self._people_sig = sig
        self._names = {uid: name for uid, name in pairs}
        for uid, name in self._names.items():
            if uid != sess.my_id:
                self.engine.volumes[uid] = float(self.s["user_volumes"].get(name, 100)) / 100.0
        if rooms is not None and sess.room_id is not None:
            key = self._room_key()
            if self.s["last_rooms"].get(key) != sess.room_id:
                self.s["last_rooms"][key] = sess.room_id
                self._schedule_save()

    def _other(self, uid):
        """uid if it is someone else in the room, else None."""
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None
        my_id = self.session.my_id if self.session else None
        return uid if uid in self._names and uid != my_id else None

    def set_user_volume(self, uid, value):
        uid = self._other(uid)
        if uid is None:
            return {"ok": False}
        v = min(max(float(value), 0.0), 200.0)
        self.s["user_volumes"][self._names[uid]] = round(v)
        self.engine.volumes[uid] = v / 100.0
        self._schedule_save()
        return {"ok": True}

    def _hosting(self) -> bool:
        return isinstance(self.session, HostSession) and self.session.alive

    def _admin_session(self):
        sess = self.session
        if isinstance(sess, ClientSession) and sess.is_admin and sess.state == "connected":
            return sess
        return None

    def _can_moderate(self) -> bool:
        return self._hosting() or self._admin_session() is not None

    def kick(self, uid, ban=False):
        uid = self._other(uid)
        if uid is None or not self._can_moderate():
            return {"error": "Only the host or a server admin can do that."}
        if self._admin_session() is not None:
            self.session.admin_kick(uid, bool(ban))  # the server answers with new people and ban lists
            return {"ok": True}
        entry = self.session.kick(uid, bool(ban))
        if entry is not None and ban:
            self.s["bans"].append(entry)
            self._schedule_save()
        return {"ok": True}

    def _ban_entries(self):
        admin = self._admin_session()
        if admin is not None:
            return "server", admin.server_bans
        return "local", self.s["bans"]

    def bans(self):
        source, entries = self._ban_entries()
        return {"source": source,
                "title": "Bans on this server" if source == "server" else "Bans for rooms hosted on this PC",
                "entries": [{"name": b.get("name", "?"), "when": b.get("when", "")} for b in entries]}

    def unban(self, index):
        source, entries = self._ban_entries()
        try:
            index = int(index)
        except (TypeError, ValueError):
            return {"ok": False}
        if not 0 <= index < len(entries):
            return {"ok": False}
        if source == "server":
            self._admin_session().admin_unban(entries[index]["id"])  # the list refreshes when the server answers
            return {"ok": True}
        entry = self.s["bans"].pop(index)
        if self._hosting():
            self.session.forgive(entry)
        self._schedule_save()
        return {"ok": True}

    def _rooms_session(self):
        sess = self.session
        if isinstance(sess, ClientSession) and sess.state == "connected" and sess.rooms is not None:
            return sess
        return None

    def join_room(self, rid):
        sess = self._rooms_session()
        if sess is not None and rid is not None and int(rid) != sess.room_id:
            sess.move(int(rid))
        return {"ok": True}

    def new_room(self, name):
        sess = self._rooms_session()
        if sess is None or not sess.is_admin:
            return {"error": "Only a server admin can make rooms."}
        name = " ".join(str(name or "").split())[:MAX_ROOM_NAME]
        if not name:
            return {"error": "Give the room a name."}
        sess.admin_room_new(name)
        return {"ok": True}

    def delete_room(self, rid):
        sess = self._rooms_session()
        if sess is None or not sess.is_admin or rid is None or int(rid) == LOBBY_ID:
            return {"ok": False}
        sess.admin_room_del(int(rid))
        return {"ok": True}

    def _show_notice(self, text: str, seconds: float = 6.0):
        self._notice_text, self._notice_until = text, time.monotonic() + seconds

    # ---------- screen sharing ----------
    def _can_watch(self, uid) -> bool:
        sess = self._rooms_session()
        if sess is None or uid is None or uid == sess.my_id or sess.sharer_here() != uid:
            return False
        return self.viewer is None or self.viewer.uid != uid

    def screens(self):
        sess = self._rooms_session()
        if sess is None or not sess.supports_screen:
            return {"error": "Screen sharing needs a Telsiz server 1.3 or newer."}
        try:
            found = list_screens()
        except Exception as e:
            return {"error": f"Screen sharing needs the mss library ({e}). "
                             f"Start Telsiz with run_windows.bat or run_linux.sh to install it."}
        if not found:
            return {"error": "No screen found to share."}
        return {"screens": [{"index": i, "label": lbl} for i, lbl in found],
                "pick": int(self.s.get("share_monitor", 1)),
                "preset": self.s.get("share_preset", "smooth"),
                "presets": [{"key": k, "label": p["label"]} for k, p in SHARE_PRESETS.items()]}

    def start_share(self, monitor, preset):
        sess = self._rooms_session()
        if sess is None:
            return {"ok": False}
        preset = preset if preset in SHARE_PRESETS else "smooth"
        self.s["share_monitor"], self.s["share_preset"] = int(monitor), preset
        self._schedule_save()
        self._share_request = (int(monitor), preset, time.monotonic())
        sess.set_sharing(True)  # the server answers with a room update (you are sharing) or a notice
        return {"ok": True}

    def stop_share(self):
        sess = self.session
        if isinstance(sess, ClientSession) and sess.alive:
            sess.set_sharing(False)
            sess.on_keyreq = None
        if self.sharer is not None:
            self.sharer.stop()
            self.sharer = None
        self._share_request = None
        return {"ok": True}

    def watch(self, uid):
        sess = self._rooms_session()
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return {"ok": False}
        if sess is None or not self._can_watch(uid):
            return {"error": "That screen is not being shared any more."}
        if self.viewer is not None:
            self.viewer.close()
        try:
            v = WebViewer(self, sess, uid, self._names.get(uid, "Someone"))
        except ImportError:
            return {"error": "Watching needs the Pillow library. Start Telsiz with run_windows.bat or "
                             "run_linux.sh to install it."}
        self.viewer = v
        if self.webview is None or self.window is None:
            return {"open": self.server.url(f"viewer.html#{uid}")}  # browser mode: the page opens a tab
        threading.Thread(target=self._open_viewer_window, args=(v,), daemon=True).start()
        return {"ok": True}

    def _open_viewer_window(self, v):
        try:
            w = self.webview.create_window(f"{v.name}'s screen", self.server.url(f"viewer.html#{v.uid}"),
                                           width=960, height=560, min_size=(320, 200),
                                           background_color="#000000")
        except Exception as e:
            with self.lock:
                v.close(f"Could not open the screen window: {e}")
            return
        v.window = w
        w.events.closed += lambda: self._viewer_window_closed(v)

    def _viewer_window_closed(self, v):
        with self.lock:
            v.window = None  # already gone: don't destroy it again
            v.close()

    def viewer_resize(self, w, h):
        if self.viewer is not None:
            self.viewer.resize(w, h)
        return {"ok": True}

    def viewer_fullscreen(self, on):
        v = self.viewer
        if v is None or v.window is None:
            return {"handled": False}
        if bool(on) != v.full:
            v.full = bool(on)
            threading.Thread(target=_quietly(v.window.toggle_fullscreen), daemon=True).start()
        return {"handled": True}

    def viewer_close(self, uid=None):
        v = self.viewer
        if v is not None and (uid is None or str(uid) == str(v.uid)):  # an old window must not close a newer one
            v.close()
        return {"ok": True}

    def _end_screens(self):
        if self.sharer is not None:
            self.sharer.stop()
            self.sharer = None
        self._share_request = None
        if self.viewer is not None:
            self.viewer.close()

    def _sync_screens(self, sess):
        """Start or stop our share and viewer to match what the server says."""
        rooms_sess = sess if isinstance(sess, ClientSession) and sess.rooms is not None else None
        sharer_here = rooms_sess.sharer_here() if rooms_sess is not None else 0
        me = sess.my_id if sess is not None else None
        if rooms_sess is not None and sharer_here == me and me is not None:
            if self.sharer is None:
                if self._share_request is not None:
                    mon, preset, _ = self._share_request
                    self._share_request = None
                    self.sharer = ScreenSharer(rooms_sess.send_video, mon, preset)
                    rooms_sess.on_keyreq = self.sharer.request_keyframe
                    rooms_sess.on_nack = self.sharer.resend
                else:
                    rooms_sess.set_sharing(False)  # the server still lists us from before: tidy up
        else:
            if self.sharer is not None:
                self.sharer.stop()
                self.sharer = None
                if rooms_sess is not None:
                    rooms_sess.on_keyreq = None
            if self._share_request is not None and time.monotonic() - self._share_request[2] > 5.0:
                self._share_request = None
        if self.sharer is not None and not self.sharer.alive:
            err = self.sharer.error
            self.sharer = None
            if rooms_sess is not None:
                rooms_sess.set_sharing(False)
                rooms_sess.on_keyreq = None
            if err:
                self._show_notice(err, 10.0)
        v = self.viewer
        if v is not None and (v.sess is not sess or sharer_here != v.uid):
            v.close(f"{v.name}'s screen share ended.")

    # ---------- audio ----------
    def _populate_devices(self):
        try:
            ins, outs = list_devices("input"), list_devices("output")
            self._device_error = ""
        except Exception as e:
            ins, outs = [], []
            self._device_error = f"Audio system error: {e}"
        self.devices = {"mic": [DEFAULT_DEVICE] + ins, "out": [DEFAULT_DEVICE] + outs}

    def _start_audio(self):
        try:
            self.engine.start(self.s["mic"], self.s["speaker"])
        except Exception as e:
            self.engine.mic_error = f"Audio could not start: {e}"

    def set_devices(self, mic, out):
        self.s["mic"] = None if mic == DEFAULT_DEVICE else mic
        self.s["speaker"] = None if out == DEFAULT_DEVICE else out
        self._schedule_save()
        self._start_audio()
        return {"ok": True}

    def refresh_devices(self):
        self.engine.stop()
        try:
            sd = get_sd()
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
        self._populate_devices()
        self._start_audio()
        return {"ok": True}

    def set_gain(self, which, value):
        v = min(max(float(value), 0.0), 200.0) / 100.0
        if which == "in":
            self.engine.in_gain = v
            self.s["in_gain"] = round(v, 3)
        elif which == "out":
            self.engine.out_gain = v
            self.s["out_gain"] = round(v, 3)
        self._schedule_save()
        return {"ok": True}

    def set_threshold(self, db):
        if self.engine.mode != "voice":
            return {"ok": False}
        db = round(min(max(float(db), METER_MIN_DB), 0.0))
        self.engine.threshold_db = db
        self.s["threshold"] = db
        self._schedule_save()
        return {"ok": True}

    def set_mode(self, mode, save=True):
        if mode == "ptt" and not self.ptt.available:
            mode = "voice"
        if mode not in ("voice", "ptt"):
            mode = "voice"
        self.engine.mode = self.s["mode"] = mode
        if mode == "ptt":
            self.ptt.key_id = self.s["ptt_key"]
            self.ptt.start()
            if not self.s["ptt_key"]:
                self.capture_key()
        else:
            self.ptt.stop()
            self._capturing = False
        if save:
            self._schedule_save()
        return {"ok": True}

    def capture_key(self):
        if not self.ptt.available:
            return {"error": "Push to talk needs the pynput library."}
        self._capturing = True
        self.ptt.begin_capture()
        return {"ok": True}

    def set_loopback(self, on):
        self.engine.loopback = bool(on)
        if not self.engine.loopback:
            self.engine.local.clear()
        return {"ok": True}

    def set_chimes(self, on):
        self.engine.ptt_sounds = self.s["ptt_sounds"] = bool(on)
        self._schedule_save()
        return {"ok": True}

    def toggle_mute(self):
        self.engine.muted = not self.engine.muted
        if self.engine.muted and self.engine.ptt_down:
            self.engine._ptt_chimed = False  # no "off" chime for a press that was cut by muting
        return {"muted": self.engine.muted}

    def toggle_deafen(self):
        self.engine.deafened = not self.engine.deafened
        return {"deafened": self.engine.deafened}

    # ---------- the tick: everything that changes on its own ----------
    def _tick(self):
        while True:
            try:
                cmd = UI_EVENTS.get_nowait()
            except queue.Empty:
                break
            if cmd == "show":
                self.show_window()
            elif cmd == "mute":
                self.toggle_mute()
            elif cmd == "deafen":
                self.toggle_deafen()
            elif cmd == "exit":
                threading.Thread(target=self.quit, daemon=True).start()
                return
        lvl = min(max(self.engine.level_db, METER_MIN_DB), 0.0)
        self._meter_db = lvl if lvl > self._meter_db else max(lvl, self._meter_db - 3.0)
        self._update_tray()
        sess = self.session
        if sess is not None:
            if sess.state == "failed":
                msg = sess.status
                self.session = None
                self._session_ended()
                self.status = msg
            else:
                nv = getattr(sess, "notice_version", 0)
                if nv != self._seen_notice_version:
                    self._seen_notice_version = nv
                    if sess.notice:
                        self._show_notice(sess.notice)
                    self._share_request = None  # a notice answers a share request we just made
                if time.monotonic() < self._notice_until:
                    self.status = self._notice_text
                elif self.sharer is not None:
                    self.status = "You are sharing your screen."
                else:
                    self.status = sess.status
                self._sync_people(sess)
                self._sync_screens(sess)
        cap = self.ptt.take_captured()
        if cap is not None:
            if cap[0] != "cancel":
                self.s["ptt_key"], self.s["ptt_label"] = cap
                self.ptt.key_id = cap[0]
                self._schedule_save()
            self._capturing = False
            if self.engine.mode != "ptt":
                self.ptt.stop()

    def _update_tray(self):
        e = self.engine
        state = "muted" if (e.muted or e.deafened) else ("live" if e.transmitting else "idle")
        sess = self.session
        if sess is None:
            title = f"{APP_NAME}: not connected"
        elif isinstance(sess, HostSession):
            title = f"{APP_NAME}: hosting, {sess.info()}"
        else:
            title = f"{APP_NAME}: {sess.status}"
        if self.sharer is not None:
            title += " (sharing screen)"
        if e.deafened:
            title += " (deafened)"
        elif e.muted:
            title += " (muted)"
        self.tray.update(state, title, e.muted, e.deafened)

    # ---------- settings ----------
    def _collect_settings(self):
        f = self.form
        self.s["name"] = f["name"].strip()[:MAX_NAME]
        self.s["role"] = f["role"]
        try:
            self.s["port"] = int(f["port"])
        except ValueError:
            pass
        self.s["host_password"] = f["host_password"]
        # stored only when the boxes are ticked; unticking also wipes what was saved before
        self.s["remember_address"] = bool(f["remember_address"])
        self.s["remember_password"] = bool(f["remember_password"])
        self.s["address"] = f["address"].strip() if self.s["remember_address"] else ""
        self.s["join_password"] = f["join_password"] if self.s["remember_password"] else ""

    def _schedule_save(self):
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.8, self._save_from_timer)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _save_from_timer(self):
        with self.lock:
            self._save_now()

    def _save_now(self):
        if self._save_timer is not None:
            self._save_timer.cancel()
            self._save_timer = None
        self._collect_settings()
        save_settings(self.s)

    def first_run_done(self):
        self.first_run = False
        return {"ok": True}

    # ---------- updates ----------
    def update_check(self):
        self.updater.check(manual=True)
        return {"ok": True}

    def update_install(self):
        return self.updater.install()

    def update_later(self):
        self.updater.later()
        return {"ok": True}

    def update_skip(self):
        version = self.updater.skip()
        if version:
            self.s["update_skip"] = version
            self._schedule_save()
        return {"ok": True}

    def update_auto(self, on):
        self.s["update_check"] = bool(on)
        self._schedule_save()
        return {"ok": True}

    def update_page(self):
        self.updater.open_page()
        return {"ok": True}

    def restart_into(self, cmd: list, env: dict, cwd: str, version: str):
        """After an update: start the new copy, then close this one. Runs on the update thread."""
        with self.lock:
            rejoin = "host" if isinstance(self.session, HostSession) else "join" if self.session is not None else ""
            self._save_now()  # the new copy reads the settings as they are now
        full = dict(os.environ, **env)
        full["TELSIZ_UPDATED"] = version
        full.pop("TELSIZ_REJOIN", None)
        if rejoin:
            full["TELSIZ_REJOIN"] = rejoin  # it joins the room again, or starts hosting again
        time.sleep(0.6)  # long enough to read "Restarting"
        release_single_instance()
        try:
            _spawn(cmd, full, cwd)
        except OSError as e:
            claim_single_instance()  # stay the running copy
            raise ValueError("Telsiz was updated but couldn't start the new version. Quit Telsiz and start it again.") from e
        self.quit()

    # ---------- window ----------
    def show_window(self):
        w = self.window
        if w is None:
            if self.browser_mode and self.server is not None:
                import webbrowser
                webbrowser.open(self.server.url())
            return

        def work():
            try:
                w.show()
                w.restore()
                w.on_top = True  # Windows only brings a window forward this way
                time.sleep(0.15)
                w.on_top = False
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def hide_window(self):
        if not self.tray.available or self.window is None:
            return {"ok": False}
        threading.Thread(target=_quietly(self.window.hide), daemon=True).start()
        if not self.s.get("tray_hint_shown"):
            self.s["tray_hint_shown"] = True
            self._schedule_save()
            self.tray.notify("Telsiz is still running. Right-click this icon and choose Exit to quit.")
        return {"ok": True}

    def on_closing(self):
        """The window's close button. Runs on the GUI thread, so it never waits for the lock."""
        if self.quitting or not self.tray.available:
            return True  # let it close: main() quits once the window is gone
        threading.Thread(target=self.hide_window, daemon=True).start()
        return False

    def quit(self):
        self.shutdown()
        if self.webview is not None:
            for w in list(self.webview.windows):
                threading.Thread(target=_quietly(w.destroy), daemon=True).start()
        self.done.set()
        return {"ok": True}

    def shutdown(self):
        with self.lock:
            self.quitting = True
            if self._shut:
                return
            self._shut = True
            try:
                self._end_screens()
                if self.session is not None:
                    self.session.close()
                self.ptt.stop()
                self.engine.stop()
                self._save_now()
            finally:
                self.tray.stop()


def _webview2_present() -> bool:
    """Windows: is the Edge WebView2 Runtime installed? Without it pywebview falls back to the old
    Internet Explorer engine, which can't draw this interface. Elsewhere: always True."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except ImportError:
        return True
    key = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + key),
                       (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + key),
                       (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + key)):
        try:
            with winreg.OpenKey(root, path) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            pass
    return False


def _fatal(message: str):
    """Show an error before any window exists."""
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR
        except Exception:
            pass


def _notice_box(message: str):
    """An information box on Windows (printed elsewhere)."""
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)  # MB_ICONINFORMATION
        except Exception:
            pass


def main():
    missing = []
    for mod in ("numpy", "sounddevice", "av"):
        try:
            __import__(mod)
        except Exception as e:  # sounddevice raises OSError when PortAudio is missing
            missing.append(f"{mod} ({e.__class__.__name__}: {e})")
    if sys.platform == "win32":
        try:
            import ctypes
            try:  # per monitor: sharp text, and screen capture sees real pixels on every monitor
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except OSError:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    after_update = bool(os.environ.get("TELSIZ_UPDATED"))
    if claim_single_instance(wait=15.0 if after_update else 0.0) == "other":
        return  # Telsiz is already running; it has been asked to show its window
    if missing:
        _fatal("Missing components:\n\n" + "\n".join(missing) + "\n\nRun:  pip install -r requirements.txt")
        sys.exit(1)
    if not (UI_DIR / "index.html").is_file():
        _fatal(f"The ui folder is missing next to telsiz.py ({UI_DIR}). Unpack the whole Telsiz folder.")
        sys.exit(1)

    ctl = Controller()
    ctl.server = LocalServer(ctl)
    threading.Thread(target=ctl.run_ticks, daemon=True).start()
    threading.Thread(target=ctl.updater.run, daemon=True).start()
    rejoin = os.environ.pop("TELSIZ_REJOIN", "")
    if rejoin in ("join", "host") and ctl.form["role"] == rejoin:  # just updated while in a room
        threading.Timer(1.0, lambda: ctl.call("toggle_connection", {})).start()

    if os.environ.get("TELSIZ_HEADLESS"):  # tests: no window, print the page address
        print(ctl.server.url(), flush=True)
        try:
            ctl.done.wait()
        except KeyboardInterrupt:
            ctl.shutdown()
        return

    if not _webview2_present():
        _notice_box("Telsiz draws its window with the Microsoft Edge WebView2 Runtime, which is not installed "
                    "on this PC. This time Telsiz opens in your web browser instead; it works the same.\n\n"
                    "To get the Telsiz window back, install the WebView2 Runtime (Evergreen) from Microsoft's "
                    "website, then start Telsiz again.")
        _browser_mode(ctl, "the Edge WebView2 Runtime is not installed")
        return
    try:
        import webview
    except Exception as e:
        _browser_mode(ctl, f"pywebview could not load ({e})")
        return
    ctl.webview = webview
    ctl.window = webview.create_window(f"{APP_NAME} {APP_VERSION}", ctl.server.url("index.html"),
                                       width=440, height=820, min_size=(380, 560),
                                       background_color="#0E1830")
    ctl.window.events.closing += ctl.on_closing
    if os.environ.get("TELSIZ_PRINT_URL"):  # debugging aid: lets a browser or script talk to the window's API
        print(ctl.server.url(), flush=True)
    icon_path = None
    if sys.platform.startswith("linux"):  # Windows takes the icon from the exe; its window engine accepts
        try:                              # only .ico files and crashes outright on a PNG
            p = settings_path().parent / "telsiz-icon.png"
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                make_icon("idle", 256).save(p)
            icon_path = str(p)
        except Exception:
            pass
    if sys.platform.startswith("linux"):
        try:  # WM_CLASS "Telsiz", so the menu entry (StartupWMClass=Telsiz) owns the window
            from gi.repository import GLib
            GLib.set_prgname(APP_NAME.lower())
            GLib.set_application_name(APP_NAME)
        except Exception:
            pass
    try:
        webview.start(private_mode=True, icon=icon_path)
    except Exception as e:
        if not ctl.quitting:
            ctl.webview = ctl.window = None
            _browser_mode(ctl, f"the window engine failed ({e})")
            return
    ctl.shutdown()


def _browser_mode(ctl, why: str):
    """No window engine: show the same page in the default browser and run until Exit or Quit."""
    import webbrowser
    print(f"{APP_NAME}: {why}. Opening the interface in your browser instead.", file=sys.stderr)
    ctl.browser_mode = True
    webbrowser.open(ctl.server.url())
    try:
        ctl.done.wait()
    except KeyboardInterrupt:
        pass
    ctl.shutdown()


if __name__ == "__main__":
    main()
