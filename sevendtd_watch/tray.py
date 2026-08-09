"""System-tray front end: the answer to "is anything happening on the server?"

Everything else in this tool has to be *asked*. `status` is a command you run, `dash`
is a window you leave open, `watch` only speaks when something changes — and if you
walk away from the desk, the toast it fired is gone by the time you get back. The tray
is the piece that is always there and always answers, at a glance and after the fact:

  * **The icon is the answer.** It shows the number of players online across every
    server. No players means a pause glyph, because the 7DTD world clock stops dead
    when a server is empty — a paused icon and a running number are literally "is game
    time moving?". A horde night turns it red.
  * **Hover for the detail** — per-server day/clock, horde ETA, whether the clock is
    frozen, and how stale the reading is.
  * **A badge for what you missed.** Every event is kept, and the icon carries a dot
    until you look. Coming back to the desk to a green "2" with a dot on it means
    somebody logged on while you were gone, whether or not you saw the toast.
  * **Screen-lock aware.** If the session locks and something happens, unlocking gets
    a single summary notification instead of nothing.

Qt (PyQt6) is the only non-stdlib dependency in the project and it is confined to this
file — the import happens inside `run()`, so every other command still works on a box
without it. Qt is used rather than libappindicator because AppIndicator has no tooltip:
hovering is half the point here.

The poll loop is `watch.tick()`, the same one `watch` uses, sharing the same state file.
That keeps the measured clock rate, the event de-duplication, and the alert wording
identical between the two, and means the tray costs no extra network traffic. It also
means running `watch` and `tray` at once is pointless and fights over state.json — the
lock file only guards against two *trays*, so don't do that.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import clock, config, gamestate, notify
from .a2s import Snapshot
from .config import Config, Server

# Panel icons are small; these are drawn large and scaled down by the panel.
ICON_PX = 128

# Colours, matched to the Discord embed colours in notify.py so an alert and the icon
# agree about what state the server is in.
COL_LIVE = "#43B581"  # players on, clock running
COL_PAUSED = "#5A6270"  # reachable but empty, clock frozen
COL_DOWN = "#F04747"  # no response
COL_HORDE = "#E01E1E"  # blood moon horde is out
COL_BADGE = "#F0A53C"  # unseen events
COL_TEXT = "#FFFFFF"
# Labels and small print. A mid grey rather than a palette lookup, because it has to
# stay readable against both a light and a dark window without being recomputed.
COL_DIM = "#8A8F98"

# The details window. Wide enough that a horde line never wraps, which is the whole
# reason it exists — a panel tooltip cannot give a line that much room.
WINDOW_WIDTH = 560

# Colours for each server state, keyed the same way Overview.state is.
STATE_COLOURS = {
    "horde": COL_HORDE,
    "live": COL_LIVE,
    "paused": COL_PAUSED,
    "down": COL_DOWN,
    "unknown": COL_PAUSED,
}


def server_state(snap: Snapshot | None, gs=None) -> str:
    """One server's state, in the same vocabulary `Overview.state` uses."""
    if snap is None:
        return "unknown"
    if not snap.online:
        return "down"
    if gs is not None and gs.known and gs.horde_active and snap.players > 0:
        return "horde"
    return "live" if snap.players > 0 else "paused"

AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
AUTOSTART_PATH = AUTOSTART_DIR / "7dtd-watch-tray.desktop"
LOCK_PATH = config.CONFIG_DIR / "tray.lock"

# Terminals to try for "Open dashboard", and the flag each one wants before a command.
TERMINALS = [
    ("konsole", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("gnome-terminal", ["--"]),
    ("xfce4-terminal", ["-x"]),
    ("xterm", ["-e"]),
]

# How often the tooltip is rebuilt, so its "updated Ns ago" line stays honest between
# polls. Cheap — it's a string built from snapshots already in memory.
TOOLTIP_REFRESH_MS = 2000

# Human labels for the alert toggles in the settings submenu, most useful first —
# ALL_EVENTS is ordered by when each kind was added, which is not a useful menu.
EVENT_ORDER = [
    "clock_start",
    "clock_stop",
    "blood_moon",
    "down",
    "up",
    "join",
    "leave",
    "day_rollover",
    "server_reset",
]

EVENT_LABELS = {
    "clock_start": "Someone logged on (clock starts)",
    "clock_stop": "Server emptied (clock pauses)",
    "join": "Player joined",
    "leave": "Player left",
    "up": "Server came back",
    "down": "Server went down",
    "blood_moon": "Blood moon",
    "day_rollover": "New day",
    "server_reset": "Server reset or updated",
}


@dataclass
class Overview:
    """Everything the icon and tooltip need, reduced from one poll of every server."""

    players: int = 0
    reachable: int = 0
    offline: int = 0
    horde: bool = False
    polled_at: float = 0.0

    @property
    def known(self) -> bool:
        return self.reachable > 0 or self.offline > 0

    @property
    def state(self) -> str:
        """`horde` / `live` / `paused` / `down` / `unknown` — picks the icon colour."""
        if not self.known:
            return "unknown"
        if self.horde:
            return "horde"
        if self.players > 0:
            return "live"
        if self.reachable > 0:
            return "paused"
        return "down"


def summarise(cfg: Config, snaps: dict[str, Snapshot], rates: dict[str, float | None]) -> Overview:
    ov = Overview()
    for server in cfg.servers:
        snap = snaps.get(server.addr)
        if snap is None:
            continue
        ov.polled_at = max(ov.polled_at, snap.polled_at)
        if not snap.online:
            ov.offline += 1
            continue
        ov.reachable += 1
        ov.players += snap.players
        gs = gamestate.read(snap, rates.get(server.addr))
        # A horde on an empty server isn't news — the clock is frozen, so it will still
        # be there whenever somebody logs in. Only colour the icon red if it's live.
        if gs.known and gs.horde_active and snap.players > 0:
            ov.horde = True
    return ov


def server_lines(
    server: Server, snap: Snapshot | None, rate: float | None, compact: bool = False
) -> list[str]:
    """The per-server detail block, shared by the tooltip, the menu and the window.

    `compact` is for the tooltip. A panel tooltip is a narrow column that hard-wraps,
    and a wrapped line reads far worse than a short one — so the compact form takes the
    short name from the config rather than the server's own advertised name (which runs
    to things like "Some Server | PVE | Experimental"), says "PAUSED" rather than
    "CLOCK PAUSED (server empty)", and drops the asides entirely. The full form keeps
    them, because the window and the menu have the room.
    """
    name = server.name if compact else (snap.name if snap and snap.name else server.name)

    if snap is None:
        return [f"\N{MEDIUM WHITE CIRCLE} {name} \N{EM DASH} not polled yet"]

    if not snap.online:
        return [
            f"\N{CROSS MARK} {name} \N{EM DASH} OFFLINE",
            f"   {snap.error or 'no response'}",
        ]

    gs = gamestate.read(snap, rate)
    live = snap.players > 0
    mark = "\N{BLACK RIGHT-POINTING TRIANGLE}" if live else "\N{DOUBLE VERTICAL BAR}"

    if compact:
        lines = [f"{mark} {name}   {snap.players}/{snap.max_players}"]
        if not gs.known:
            lines.append("   clock unknown")
            return lines
        lines.append(
            f"   Day {gs.day} \N{MIDDLE DOT} {gs.clock} {gs.phase_icon} \N{MIDDLE DOT} "
            f"{'running' if live else 'PAUSED'}"
        )
        if gs.blood_moon_freq > 0:
            if gs.horde_active:
                left = gamestate.fmt_duration(gs.real_minutes_to_horde_end)
                lines.append(f"   HORDE OUT \N{MIDDLE DOT} ~{left} left")
            else:
                eta = gamestate.fmt_duration(gs.real_minutes_to_horde)
                lines.append(f"   horde ~{eta} real \N{MIDDLE DOT} day {gs.horde_day}")
        return lines

    lines = [f"{mark} {name} \N{EM DASH} {snap.players}/{snap.max_players} players"]
    if gs.known:
        moving = "clock running" if live else "CLOCK PAUSED (server empty)"
        lines.append(f"    Day {gs.day} \N{MIDDLE DOT} {gs.clock} {gs.phase_icon} \N{MIDDLE DOT} {moving}")
        lines.append(f"    horde: {gamestate.blood_moon_line(gs)}")
        if not gs.rate_measured:
            lines.append("    (clock speed not yet measured \N{EM DASH} ETA is from server config)")
    else:
        lines.append("    clock unknown \N{EM DASH} server did not report one")

    return lines


# Nothing in the tooltip may exceed this, or the panel wraps it and the whole column
# turns into a wall. Every tooltip line is clipped to it as a backstop.
TOOLTIP_WIDTH = 42

# Compact stand-ins for event titles, for the "since you looked" list in the tooltip.
# The full titles are what the notifications say; these are what fits.
SHORT_EVENTS = {
    "clock_start": "clock running",
    "clock_stop": "clock paused",
    "server_reset": "reset/updated",
    "down": "offline",
    "up": "back up",
    "blood_moon": "BLOOD MOON",
}


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def short_event(ev: notify.Event) -> str:
    """What an event is called when there is no room to say it properly."""
    return SHORT_EVENTS.get(ev.kind) or clip(ev.title, 20)


def headline(ov: Overview) -> str:
    """The one-line answer, short enough for a panel tooltip."""
    if not ov.known:
        return "7dtd-watch \N{MIDDLE DOT} no readings yet"

    if ov.players:
        head = f"{ov.players} player{'' if ov.players == 1 else 's'} online"
    elif ov.reachable:
        head = "clocks paused"
    else:
        return "7dtd-watch \N{MIDDLE DOT} all servers offline"

    if ov.horde:
        head = f"BLOOD MOON \N{MIDDLE DOT} {head}"
    # An offline server must never hide behind a headline about the others.
    if ov.offline:
        head += f" \N{MIDDLE DOT} {ov.offline} offline"
    return f"7dtd-watch \N{MIDDLE DOT} {head}"


def meta_line(snap: Snapshot) -> str:
    """Map / version / region / ping / flags — the small print under a server card."""
    meta = [snap.map or "?", snap.version or "?"]
    region = snap.rules.get("Region")
    if region:
        meta.append(region)
    meta.append(f"{snap.ping_ms:.0f}ms")
    flags = []
    if snap.password:
        flags.append("password")
    if snap.rule_bool("EACEnabled"):
        flags.append("EAC")
    if snap.rule_bool("ModdedConfig"):
        flags.append("modded")
    if flags:
        meta.append("/".join(flags))
    return "  \N{MIDDLE DOT}  ".join(meta)


def ago(seconds: float, short: bool = False) -> str:
    """`11s ago`, or `11s` when `short` — a tooltip has no room for the suffix."""
    if seconds < 0:
        return "now" if short else "just now"
    if seconds < 60:
        out = f"{int(seconds)}s"
    elif seconds < 3600:
        out = f"{int(seconds // 60)}m"
    elif seconds < 86400:
        out = f"{int(seconds // 3600)}h"
    else:
        out = f"{int(seconds // 86400)}d"
    return out if short else f"{out} ago"


def away_summary(events: list[notify.Event], since: float) -> str | None:
    """One line per server describing what happened while the screen was locked."""
    missed = [e for e in events if e.at >= since]
    if not missed:
        return None

    by_server: dict[str, list[notify.Event]] = {}
    for ev in missed:
        by_server.setdefault(ev.server, []).append(ev)

    lines = []
    for server, evs in by_server.items():
        # The last event of each kind is the one that still describes reality; a
        # join/leave/join churn should read as "someone is on", not three lines.
        latest: dict[str, notify.Event] = {}
        for ev in evs:
            latest[ev.kind] = ev
        headline = ", ".join(f"{e.icon} {e.title}" for e in latest.values())
        lines.append(f"{server}: {headline}")
    return "\n".join(lines)


def _launch_argv() -> list[str]:
    """How to start this tray again — for the autostart .desktop file."""
    exe = shutil.which("7dtd-watch")
    if exe:
        return [exe, "tray"]
    shim = Path(__file__).resolve().parent.parent / "7dtd-watch"
    if shim.exists() and os.access(shim, os.X_OK):
        return [sys.executable, str(shim), "tray"]
    return [sys.executable, "-m", "sevendtd_watch", "tray"]


def _quote(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def autostart_enabled() -> bool:
    return AUTOSTART_PATH.exists()


def set_autostart(enabled: bool) -> None:
    if not enabled:
        AUTOSTART_PATH.unlink(missing_ok=True)
        return
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    AUTOSTART_PATH.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=7dtd-watch tray\n"
        "Comment=7 Days to Die server status in the system tray\n"
        f"Exec={_quote(_launch_argv())}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def open_dashboard() -> bool:
    """Spawn `dash` in whatever terminal this box has. False if it has none."""
    root = Path(__file__).resolve().parent.parent
    inner = [sys.executable, "-m", "sevendtd_watch", "dash"]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), env.get("PYTHONPATH", "")]))

    for name, flags in TERMINALS:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            subprocess.Popen([exe, *flags, *inner], cwd=str(root), env=env, start_new_session=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _acquire_lock():
    """Hold an exclusive lock for the lifetime of the process. None if one is held."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle  # deliberately kept alive by the caller; closing releases the lock


def run(cfg: Config, interval: int, install_autostart: bool | None = None) -> int:
    """Start the tray. Qt is imported here so the rest of the tool stays dependency-free."""
    if install_autostart is not None:
        set_autostart(install_autostart)
        where = AUTOSTART_PATH
        print(f"{'wrote' if install_autostart else 'removed'} {where}")
        return 0

    if not cfg.servers:
        print("No servers configured. Try: 7dtd-watch servers import")
        return 1

    try:
        from PyQt6 import QtWidgets  # noqa: F401
    except ImportError:
        print("The tray needs PyQt6, the tool's only optional dependency.")
        print("  Fedora:  sudo dnf install python3-pyqt6")
        print("  Debian:  sudo apt install python3-pyqt6")
        print("  pip:     pip install --user PyQt6")
        print("\nEverything else (status / dash / watch) works without it.")
        return 1

    lock = _acquire_lock()
    if lock is None:
        print(f"A 7dtd-watch tray is already running (lock: {LOCK_PATH}).")
        return 1

    try:
        from ._trayqt import launch

        return launch(cfg, interval)
    finally:
        lock.close()
