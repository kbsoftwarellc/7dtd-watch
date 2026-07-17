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

PREFS_PATH = Path.home() / ".config" / "unity3d" / "The Fun Pimps" / "7 Days To Die" / "prefs"

MIN_POLL_INTERVAL = 5
DEFAULT_POLL_INTERVAL = 15

ALL_EVENTS = ["join", "leave", "down", "up", "blood_moon", "day_rollover", "server_reset"]


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
            "poll_interval": self.poll_interval,
            "servers": [asdict(s) for s in self.servers],
            "notify": self.notify,
            "events": self.events,
        }


def load() -> Config:
    """Load config, writing a seeded default on first run."""
    if not CONFIG_PATH.exists():
        # Ship with no servers — each user adds their own, either with
        # `servers import` (read from the game's history) or `servers add HOST:PORT`.
        cfg = Config(servers=[])
        save(cfg)
        return cfg

    raw = json.loads(CONFIG_PATH.read_text())
    cfg = Config(
        poll_interval=max(MIN_POLL_INTERVAL, int(raw.get("poll_interval", DEFAULT_POLL_INTERVAL))),
        servers=[Server(name=s["name"], host=s["host"], port=int(s["port"])) for s in raw.get("servers", [])],
        notify=raw.get("notify", {"desktop": True, "discord_webhook": ""}),
        events=raw.get("events", list(ALL_EVENTS)),
    )
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
