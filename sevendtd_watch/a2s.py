"""Steam A2S query protocol, as spoken by 7 Days to Die dedicated servers.

Three queries, all UDP, all unauthenticated — the same ones the in-game server
browser sends:

    A2S_INFO    header 'T'  -> name, map, player count, version
    A2S_RULES   header 'V'  -> ~91 key/value rules, including the game clock
    A2S_PLAYER  header 'U'  -> per-player score + connected duration

7DTD returns *blank* player names in A2S_PLAYER, so `sessions` carries durations
only. Names would require telnet/RCON on a server you administer.
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field

WHOLE = b"\xff\xff\xff\xff"  # single-packet reply prefix
SPLIT = b"\xff\xff\xff\xfe"  # multi-packet reply prefix

INFO_PAYLOAD = WHOLE + b"TSource Engine Query\x00"

DEFAULT_TIMEOUT = 3.0


@dataclass
class Session:
    """One connected player. 7DTD leaves `name` empty; `minutes` is the real one."""

    name: str
    score: int
    minutes: float


@dataclass
class Snapshot:
    host: str
    port: int
    online: bool = False
    error: str = ""
    name: str = ""
    map: str = ""
    # Player count comes from A2S_INFO: the CurrentPlayers *rule* lags by a poll or two.
    players: int = 0
    max_players: int = 0
    bots: int = 0
    version: str = ""
    password: bool = False
    ping_ms: float = 0.0
    rules: dict[str, str] = field(default_factory=dict)
    sessions: list[Session] = field(default_factory=list)
    polled_at: float = 0.0

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    def rule_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.rules[key])
        except (KeyError, ValueError):
            return default

    def rule_bool(self, key: str, default: bool = False) -> bool:
        v = self.rules.get(key)
        if v is None:
            return default
        return v.strip().lower() == "true"


class _Reader:
    """Cursor over a response body. A2S packs C strings and little-endian scalars."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def short(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 2], "little")
        self.pos += 2
        return v

    def long(self) -> int:
        v = int.from_bytes(self.data[self.pos : self.pos + 4], "little", signed=True)
        self.pos += 4
        return v

    def float32(self) -> float:
        v = struct.unpack("<f", self.data[self.pos : self.pos + 4])[0]
        self.pos += 4
        return v

    def cstr(self) -> str:
        end = self.data.index(0, self.pos)
        s = self.data[self.pos : end].decode("utf-8", "replace")
        self.pos = end + 1
        return s

    def remaining(self) -> int:
        return len(self.data) - self.pos


def _exchange(host: str, port: int, payload: bytes, timeout: float) -> bytes:
    """Send one request, collect the reply, reassembling it if the server split it.

    Returns the reply body with the 4-byte prefix stripped (so body[0] is the header).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, (host, port))
        first, _ = sock.recvfrom(65535)

        if first[:4] == WHOLE:
            return first[4:]

        if first[:4] != SPLIT:
            raise ValueError(f"unknown reply prefix {first[:4]!r}")

        # Split reply: header is id(long) total(byte) number(byte) splitsize(short).
        # Collect every fragment, then order by `number`.
        total = first[8]
        parts = {first[9]: first[12:]}
        while len(parts) < total:
            pkt, _ = sock.recvfrom(65535)
            if pkt[:4] != SPLIT:
                raise ValueError("mixed split and whole packets in one reply")
            parts[pkt[9]] = pkt[12:]
        joined = b"".join(parts[i] for i in sorted(parts))
        return joined[4:]  # reassembled payload carries its own WHOLE prefix
    finally:
        sock.close()


def _query(host: str, port: int, header: bytes, timeout: float) -> bytes:
    """Send a RULES/PLAYER query, satisfying the challenge handshake.

    These two carry a 4-byte challenge slot. The first request sends a placeholder
    of -1; the server answers with header 'A' and the real challenge, which *replaces*
    the placeholder on the re-send. (Appending instead of replacing gets you silence.)
    A2S_INFO is the odd one out — see query_info, where the challenge is appended.
    """
    reply = _exchange(host, port, WHOLE + header + b"\xff\xff\xff\xff", timeout)
    if reply[:1] == b"A":
        challenge = reply[1:5]
        reply = _exchange(host, port, WHOLE + header + challenge, timeout)
    return reply


def query_info(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> tuple[dict, float]:
    """A2S_INFO. Returns (fields, round-trip milliseconds)."""
    started = time.monotonic()
    # A2S_INFO's challenge, unlike the others', appends to the full payload.
    reply = _exchange(host, port, INFO_PAYLOAD, timeout)
    if reply[:1] == b"A":
        reply = _exchange(host, port, INFO_PAYLOAD + reply[1:5], timeout)
    ping_ms = (time.monotonic() - started) * 1000

    r = _Reader(reply)
    if r.byte() != ord("I"):
        raise ValueError("not an A2S_INFO reply")
    r.byte()  # protocol version
    info = {
        "name": r.cstr(),
        "map": r.cstr(),
        "folder": r.cstr(),
        "game": r.cstr(),
    }
    r.short()  # steam app id
    info["players"] = r.byte()
    info["max_players"] = r.byte()
    info["bots"] = r.byte()
    r.byte()  # server type
    r.byte()  # environment
    info["password"] = r.byte() == 1
    r.byte()  # vac
    info["version"] = r.cstr()
    return info, ping_ms


def query_rules(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> dict[str, str]:
    """A2S_RULES. 7DTD packs the game clock and every gameplay setting in here."""
    reply = _query(host, port, b"V", timeout)
    r = _Reader(reply)
    if r.byte() != ord("E"):
        raise ValueError("not an A2S_RULES reply")
    count = r.short()
    rules: dict[str, str] = {}
    for _ in range(count):
        if r.remaining() < 2:
            break  # truncated reply — keep what we parsed rather than blowing up
        key = r.cstr()
        value = r.cstr()
        if key:
            rules[key] = value
    return rules


def query_players(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> list[Session]:
    """A2S_PLAYER. On 7DTD the names are blank; the connected duration is the payload."""
    reply = _query(host, port, b"U", timeout)
    r = _Reader(reply)
    if r.byte() != ord("D"):
        raise ValueError("not an A2S_PLAYER reply")
    count = r.byte()
    sessions: list[Session] = []
    for _ in range(count):
        if r.remaining() < 10:
            break
        r.byte()  # index — always 0 on 7DTD
        name = r.cstr()
        score = r.long()
        seconds = r.float32()
        sessions.append(Session(name=name, score=score, minutes=seconds / 60.0))
    return sessions


def query(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> Snapshot:
    """Poll one server. Never raises: an unreachable server comes back `online=False`,
    so a single dead entry can't take down the dashboard."""
    snap = Snapshot(host=host, port=port, polled_at=time.time())
    try:
        info, ping_ms = query_info(host, port, timeout)
    except (OSError, ValueError, IndexError) as exc:
        snap.error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, socket.timeout) else "timed out"
        return snap

    snap.online = True
    snap.ping_ms = ping_ms
    snap.name = info["name"]
    snap.map = info["map"]
    snap.players = info["players"]
    snap.max_players = info["max_players"]
    snap.bots = info["bots"]
    snap.version = info["version"]
    snap.password = info["password"]

    # Rules carry the clock, so they can never be cached between polls.
    try:
        snap.rules = query_rules(host, port, timeout)
    except (OSError, ValueError, IndexError):
        pass  # partial snapshot still beats none — the dashboard degrades a field at a time

    if snap.players > 0:
        try:
            snap.sessions = query_players(host, port, timeout)
        except (OSError, ValueError, IndexError):
            pass

    return snap
