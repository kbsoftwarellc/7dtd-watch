"""How fast is this server's game clock *actually* running?

`DayNightLength` (real minutes per game day) implies a tick rate — at the default 60,
24000 ticks / 3600 s = 6.67 ticks/sec. One live server was measured running at **5.74**.
That 14% gap is worth over half an hour on a four-hour horde countdown, so the ETA is
based on the rate we observe, and falls back to the config-derived one only when there
is no sample yet.

Samples fold into a per-server EMA kept in the existing state.json, alongside whatever
`watch` stores there (`watch._diff` copies the dict, so these keys survive). The caller
owns loading and saving that state — see `dash.poll_all`.
"""

from __future__ import annotations

from .a2s import Snapshot
from .gamestate import TICKS_PER_DAY

# One laggy poll shouldn't swing the ETA; a genuinely different rate should still win
# within a few polls.
EMA_ALPHA = 0.3

# Too short a window and poll jitter dominates. Too long and the server may have sat
# empty (clock frozen) for part of it, dragging the rate toward zero. 15s dash/watch
# polls land comfortably inside; two `status` runs an hour apart do not, and are simply
# not sampled.
MIN_SAMPLE_SECONDS = 5
MAX_SAMPLE_SECONDS = 120

# A rate this far off what the server's own config implies isn't a slow clock, it's
# junk (a restart, a rollback, a misread). The band is wide enough that a modded
# DayNightLength still calibrates normally.
MIN_RATE_FACTOR = 0.25
MAX_RATE_FACTOR = 2.0


def config_rate(snap: Snapshot) -> float:
    """Ticks per second implied by the server's own DayNightLength rule."""
    day_night_length = snap.rule_int("DayNightLength", 60)
    if day_night_length <= 0:
        day_night_length = 60
    return TICKS_PER_DAY / (day_night_length * 60)


def rate_for(state: dict, addr: str) -> float | None:
    """The measured rate for one server, or None if there's never been a clean sample."""
    rate = state.get(addr, {}).get("tick_rate")
    return float(rate) if rate else None


def observe(state: dict, snap: Snapshot) -> float | None:
    """Fold one poll into a server's rate estimate. Mutates `state` in place.

    Returns the rate to use now — the EMA if we have one, else None, meaning the caller
    should fall back to the config rate.
    """
    if not snap.online:
        return rate_for(state, snap.addr)

    entry = state.setdefault(snap.addr, {})
    ticks = snap.rule_int("CurrentServerTime", -1)
    if ticks < 0:
        return rate_for(state, snap.addr)

    prev_ticks = entry.get("tick_ticks")
    prev_at = entry.get("tick_at")
    prev_players = entry.get("tick_players", 0)

    # This poll becomes the new baseline whatever happens below.
    entry["tick_ticks"] = ticks
    entry["tick_at"] = snap.polled_at
    entry["tick_players"] = snap.players

    if prev_ticks is None or prev_at is None:
        return rate_for(state, snap.addr)

    # The clock only advances while somebody is online, so a window that touches an
    # empty server says nothing about how fast it runs when populated.
    if int(prev_players) <= 0 or snap.players <= 0:
        return rate_for(state, snap.addr)

    dt = snap.polled_at - float(prev_at)
    if not MIN_SAMPLE_SECONDS <= dt <= MAX_SAMPLE_SECONDS:
        return rate_for(state, snap.addr)

    delta = ticks - int(prev_ticks)
    if delta <= 0:
        # 0 = frozen despite players (rare); negative = the server restarted or rolled
        # back. Neither is a rate, and the baseline above has already re-anchored us.
        return rate_for(state, snap.addr)

    rate = delta / dt
    implied = config_rate(snap)
    if not implied * MIN_RATE_FACTOR <= rate <= implied * MAX_RATE_FACTOR:
        return rate_for(state, snap.addr)

    previous = entry.get("tick_rate")
    entry["tick_rate"] = rate if not previous else EMA_ALPHA * rate + (1 - EMA_ALPHA) * float(previous)
    entry["tick_samples"] = int(entry.get("tick_samples", 0)) + 1
    return float(entry["tick_rate"])
