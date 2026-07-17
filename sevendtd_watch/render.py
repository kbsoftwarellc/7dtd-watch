"""Formatting shared by `status` and `dash`."""

from __future__ import annotations

import os
import sys
import time

from . import gamestate
from .a2s import Snapshot
from .config import Server

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
GREY = "\033[90m"
BRED = "\033[91m"


def color_enabled(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return stream.isatty()


class Paint:
    """Colorize, or don't — one switch instead of an `if` at every call site."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled else text


def player_bar(players: int, maximum: int, width: int = 12) -> str:
    if maximum <= 0:
        return ""
    filled = min(width, round(players / maximum * width))
    return "\N{FULL BLOCK}" * filled + "\N{LIGHT SHADE}" * (width - filled)


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def block(
    server: Server, snap: Snapshot, p: Paint, now: float | None = None, rate: float | None = None
) -> list[str]:
    """The per-server panel, as a list of lines. `rate` is the measured ticks/sec, if known."""
    now = now if now is not None else time.time()
    lines: list[str] = []

    if not snap.online:
        dot = p("\N{BLACK CIRCLE}", RED)
        lines.append(f"{dot} {p(server.name, BOLD)}  {p(server.addr, GREY)}")
        lines.append(f"    {p('OFFLINE', RED)} — {snap.error or 'no response'}")
        return lines

    gs = gamestate.read(snap, rate)

    live = snap.players > 0
    dot = p("\N{BLACK CIRCLE}", GREEN if live else YELLOW)
    header = f"{dot} {p(snap.name or server.name, BOLD)}  {p(snap.addr, GREY)}"
    lines.append(header)

    bar = player_bar(snap.players, snap.max_players)
    count = f"{snap.players}/{snap.max_players}"
    count_c = p(count, GREEN if live else GREY)
    bar_c = p(bar, GREEN if live else GREY)
    lines.append(f"    players  {count_c} {bar_c}")

    if gs.known:
        clock = f"Day {gs.day} \N{MIDDLE DOT} {gs.clock} {gs.phase_icon}"
        if gs.frozen:
            clock += p("  (clock frozen — server empty)", DIM)
        lines.append(f"    time     {p(clock, BOLD if not gs.frozen else '')}")

        bm = gamestate.blood_moon_line(gs)
        hot = gs.horde_active or gs.is_blood_moon_day
        lines.append(f"    horde    {p(bm, BRED + BOLD) if hot else p(bm, YELLOW)}")
    else:
        lines.append(f"    time     {p('unknown (server did not report a clock)', GREY)}")

    if snap.sessions:
        longest = max(s.minutes for s in snap.sessions)
        newest = min(s.minutes for s in snap.sessions)
        lines.append(
            f"    sessions {p(f'longest {longest:.0f}m, newest {newest:.0f}m', GREY)}"
            + p("  (7DTD hides player names)", DIM)
        )

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
    lines.append(f"    {p(' \N{MIDDLE DOT} '.join(meta), GREY)}")

    age = now - snap.polled_at
    if age > 2:
        lines.append(f"    {p(ago(age), DIM)}")

    return lines


def to_dict(server: Server, snap: Snapshot, rate: float | None = None) -> dict:
    """Machine-readable snapshot for `status --json` (waybar, polybar, scripts)."""
    gs = gamestate.read(snap, rate)
    out = {
        "name": server.name,
        "host": snap.host,
        "port": snap.port,
        "online": snap.online,
        "polled_at": snap.polled_at,
    }
    if not snap.online:
        out["error"] = snap.error
        return out

    out.update(
        {
            "server_name": snap.name,
            "map": snap.map,
            "players": snap.players,
            "max_players": snap.max_players,
            "version": snap.version,
            "ping_ms": round(snap.ping_ms, 1),
            "password": snap.password,
            "eac": snap.rule_bool("EACEnabled"),
            "modded": snap.rule_bool("ModdedConfig"),
            "region": snap.rules.get("Region", ""),
            "sessions_minutes": [round(s.minutes, 1) for s in snap.sessions],
        }
    )
    if gs.known:
        out["game"] = {
            "day": gs.day,
            "clock": gs.clock,
            "is_night": gs.is_night,
            "clock_frozen": gs.frozen,
            "blood_moon_frequency": gs.blood_moon_freq,
            "is_blood_moon_day": gs.is_blood_moon_day,
            "days_to_blood_moon": gs.days_to_blood_moon,
            "horde_active": gs.horde_active,
            "horde_day": gs.horde_day,
            "game_minutes_to_horde": gs.game_minutes_to_horde,
            # The headline number. It only counts down while the server has players on it.
            "real_minutes_to_horde": round(gs.real_minutes_to_horde, 1),
            "real_eta": gamestate.fmt_duration(gs.real_minutes_to_horde),
            "ticks_per_second": round(gs.ticks_per_second, 3),
            "rate_measured": gs.rate_measured,
            "summary": gamestate.blood_moon_line(gs),
        }
    return out
