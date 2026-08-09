"""Where events go: desktop toasts and/or a Discord webhook.

Both sinks are optional and fail soft. No webhook configured means Discord is
simply skipped; no `notify-send` on the box means the desktop sink is skipped.
Neither ever raises into the poll loop.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import Config

# Blood moon is the one event worth interrupting someone for.
URGENT = {"blood_moon"}

ICONS = {
    "join": "\N{BUSTS IN SILHOUETTE}",
    "leave": "\N{BUSTS IN SILHOUETTE}",
    "up": "\N{WHITE HEAVY CHECK MARK}",
    "down": "\N{CROSS MARK}",
    "blood_moon": "\N{LARGE RED CIRCLE}",
    "day_rollover": "\N{BLACK SUN WITH RAYS}",
    "server_reset": "\N{ANTICLOCKWISE OPEN CIRCLE ARROW}",
    "clock_start": "\N{BLACK RIGHT-POINTING TRIANGLE}",
    "clock_stop": "\N{DOUBLE VERTICAL BAR}",
    # Not a poll event — the tray raises it by hand when the screen unlocks.
    "away": "\N{BELL}",
}

COLORS = {
    "join": 0x43B581,
    "leave": 0x747F8D,
    "up": 0x43B581,
    "down": 0xF04747,
    "blood_moon": 0xE01E1E,
    "day_rollover": 0xFAA61A,
    "server_reset": 0x7289DA,
    "clock_start": 0x43B581,
    "clock_stop": 0x747F8D,
    "away": 0xF0A53C,
}


@dataclass
class Event:
    kind: str  # one of config.ALL_EVENTS
    server: str  # display name
    title: str
    body: str
    # Wall-clock time the event was raised. The tray reports events as "22m ago", and
    # after a restart it reads them back from disk, so the time has to travel with them.
    at: float = field(default_factory=time.time)

    @property
    def urgent(self) -> bool:
        return self.kind in URGENT

    @property
    def icon(self) -> str:
        return ICONS.get(self.kind, "")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "server": self.server, "title": self.title, "body": self.body, "at": self.at}

    @classmethod
    def from_dict(cls, raw: dict) -> "Event":
        return cls(
            kind=str(raw.get("kind", "")),
            server=str(raw.get("server", "")),
            title=str(raw.get("title", "")),
            body=str(raw.get("body", "")),
            at=float(raw.get("at", 0.0)),
        )


def _notify_send(ev: Event) -> bool:
    if not shutil.which("notify-send"):
        return False
    icon = ICONS.get(ev.kind, "")
    summary = f"{icon} {ev.server}".strip()
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=7dtd-watch",
                f"--urgency={'critical' if ev.urgent else 'normal'}",
                summary,
                f"{ev.title}\n{ev.body}" if ev.body else ev.title,
            ],
            check=False,
            timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _discord(ev: Event, webhook: str) -> bool:
    payload = {
        "username": "7dtd-watch",
        "embeds": [
            {
                "title": f"{ICONS.get(ev.kind, '')} {ev.server}".strip(),
                "description": f"**{ev.title}**\n{ev.body}" if ev.body else f"**{ev.title}**",
                "color": COLORS.get(ev.kind, 0x99AAB5),
            }
        ],
    }
    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "7dtd-watch"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, OSError):
        return False


def dispatch(ev: Event, cfg: Config, dry_run: bool = False) -> list[str]:
    """Send an event to every enabled sink. Returns the sinks that accepted it."""
    if dry_run:
        print(f"[dry-run] {ev.kind}: {ev.server} — {ev.title} {ev.body}".rstrip())
        return ["dry-run"]

    sent = []
    if cfg.desktop_enabled and _notify_send(ev):
        sent.append("desktop")
    webhook = cfg.discord_webhook
    if webhook and _discord(ev, webhook):
        sent.append("discord")
    return sent
