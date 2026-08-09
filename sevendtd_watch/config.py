"""Config at ~/.config/7dtd-watch/config.json, plus server discovery from the game itself.

7DTD records every server you've joined in its Unity prefs, under a
`ServerHistoryCache` key that is base64 wrapped around base64:

    b64(b64("ip$port:timestamp$bool;ip$port:timestamp$bool;..."))

`import_history()` decodes that, which is how this tool found the servers to
monitor without anyone typing an IP. Join a new server in-game, run
`7dtd-watch servers import`, and it shows up here.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "7dtd-watch"
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"
EVENTS_PATH = CONFIG_DIR / "events.jsonl"
# Kept out of state.json: the tray writes it on a click while the poll loop is
# rewriting state.json on its own schedule, and a lost update would resurrect a
# badge that was already dismissed.
SEEN_PATH = CONFIG_DIR / "tray-seen"

PREFS_PATH = Path.home() / ".config" / "unity3d" / "The Fun Pimps" / "7 Days To Die" / "prefs"

MIN_POLL_INTERVAL = 5
DEFAULT_POLL_INTERVAL = 15

# Bumped whenever a release adds event kinds. A config written by an older version is
# missing those keys entirely, which would silently mute the new alerts — `load()`
# grandfathers them in rather than making anyone hand-edit JSON. See _migrate().
CONFIG_VERSION = 2

ALL_EVENTS = [
    "join",
    "leave",
    "down",
    "up",
    "blood_moon",
    "day_rollover",
    "server_reset",
    # v2. The 0 <-> non-zero crossing, which is what actually starts and stops the
    # world clock — `join`/`leave` only tell you the count moved.
    "clock_start",
    "clock_stop",
]

# Which kinds each config version introduced, so a migration knows what to add.
EVENTS_ADDED_IN = {2: ["clock_start", "clock_stop"]}

DEFAULT_TRAY = {
    # Show a badge on the tray icon while events have gone unseen.
    "badge_unseen": True,
    # On unlocking the screen, summarise what happened while it was locked.
    "summary_on_return": True,
    # Keep this many events in events.jsonl for the "recent" menu.
    "history": 100,
}

# How many events the tray keeps in memory / on disk, absent a config override.
DEFAULT_HISTORY = 100


@dataclass
class Server:
    name: str
    host: str
    port: int

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Config:
    poll_interval: int = DEFAULT_POLL_INTERVAL
    servers: list[Server] = field(default_factory=list)
    notify: dict = field(default_factory=lambda: {"desktop": True, "discord_webhook": ""})
    events: list[str] = field(default_factory=lambda: list(ALL_EVENTS))
    tray: dict = field(default_factory=lambda: dict(DEFAULT_TRAY))
    version: int = CONFIG_VERSION

    def tray_opt(self, key: str):
        return self.tray.get(key, DEFAULT_TRAY.get(key))

    @property
    def history_limit(self) -> int:
        try:
            return max(10, int(self.tray_opt("history")))
        except (TypeError, ValueError):
            return DEFAULT_HISTORY

    @property
    def discord_webhook(self) -> str:
        # Env var wins, so a webhook never has to be committed to a config file.
        return os.environ.get("SEVENDTD_DISCORD_WEBHOOK") or self.notify.get("discord_webhook", "")

    @property
    def desktop_enabled(self) -> bool:
        return bool(self.notify.get("desktop", True))

    def find(self, needle: str) -> list[Server]:
        n = needle.lower()
        return [s for s in self.servers if n in s.name.lower() or n in s.addr]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "poll_interval": self.poll_interval,
            "servers": [asdict(s) for s in self.servers],
            "notify": self.notify,
            "events": self.events,
            "tray": self.tray,
        }


def _migrate(cfg: Config, from_version: int) -> bool:
    """Grandfather in event kinds added since `from_version`. True if anything changed."""
    changed = False
    for version, kinds in sorted(EVENTS_ADDED_IN.items()):
        if from_version >= version:
            continue
        for kind in kinds:
            if kind not in cfg.events:
                cfg.events.append(kind)
                changed = True
    if cfg.version != CONFIG_VERSION:
        cfg.version = CONFIG_VERSION
        changed = True
    return changed


def load() -> Config:
    """Load config, writing a seeded default on first run."""
    if not CONFIG_PATH.exists():
        # Ship with no servers — each user adds their own, either with
        # `servers import` (read from the game's history) or `servers add HOST:PORT`.
        cfg = Config(servers=[])
        save(cfg)
        return cfg

    raw = json.loads(CONFIG_PATH.read_text())
    tray = dict(DEFAULT_TRAY)
    tray.update(raw.get("tray") or {})
    cfg = Config(
        poll_interval=max(MIN_POLL_INTERVAL, int(raw.get("poll_interval", DEFAULT_POLL_INTERVAL))),
        servers=[Server(name=s["name"], host=s["host"], port=int(s["port"])) for s in raw.get("servers", [])],
        notify=raw.get("notify", {"desktop": True, "discord_webhook": ""}),
        events=raw.get("events", list(ALL_EVENTS)),
        tray=tray,
        version=int(raw.get("version", 1)),
    )
    # A config that predates an event kind never opted *out* of it — it just didn't
    # know about it. Enable it and write the file back so this happens once.
    if _migrate(cfg, int(raw.get("version", 1))):
        save(cfg)
    return cfg


def save(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), indent=2) + "\n")


def load_state() -> dict:
    """Last-seen state per server, so `watch` can survive a restart without re-alerting."""
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def load_seen() -> float:
    """When the tray was last looked at. Everything newer counts as unseen."""
    try:
        return float(SEEN_PATH.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def save_seen(when: float) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(f"{when}\n")


def load_events(limit: int = DEFAULT_HISTORY) -> list[dict]:
    """The most recent events, oldest first. Empty list if the log is missing or junk."""
    if not EVENTS_PATH.exists():
        return []
    try:
        lines = EVENTS_PATH.read_text().splitlines()
    except OSError:
        return []

    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn last line from a kill mid-write — skip it, don't lose the rest
    return out


def save_events(events: list[dict], limit: int = DEFAULT_HISTORY) -> None:
    """Rewrite the log with the last `limit` entries. Small file, so no rotation dance."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(e) + "\n" for e in events[-limit:])
    EVENTS_PATH.write_text(body)


def _decode_history(blob: str) -> list[tuple[str, int]]:
    """Unwrap the doubly-base64'd ServerHistoryCache into (host, port) pairs."""
    try:
        once = base64.b64decode(blob).decode("utf-8", "replace")
        twice = base64.b64decode(once).decode("utf-8", "replace")
    except Exception:
        return []

    out: list[tuple[str, int]] = []
    for entry in twice.split(";"):
        # Each entry is "ip$port:timestamp$favorite".
        m = re.match(r"^([^$]+)\$(\d+):", entry.strip())
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def import_history(prefs_path: Path = PREFS_PATH) -> list[tuple[str, int]]:
    """Read the servers this machine has joined, newest last. Empty list if unreadable."""
    if not prefs_path.exists():
        return []
    try:
        text = prefs_path.read_text(errors="replace")
    except OSError:
        return []

    m = re.search(r'<pref name="ServerHistoryCache"[^>]*>([^<]*)<', text)
    if not m:
        return []
    return _decode_history(m.group(1).strip())
