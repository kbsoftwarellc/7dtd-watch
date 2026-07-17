"""Background poll loop: diff each snapshot against the last, emit what changed.

State is persisted to ~/.config/7dtd-watch/state.json so restarting the watcher
doesn't replay a backlog of alerts for things that already happened.
"""

from __future__ import annotations

import time

from . import gamestate, notify
from .a2s import Snapshot
from .config import Config, Server, load_state, save_state
from .dash import poll_all
from .notify import Event

# One dropped UDP packet is normal. Only call a server down after this many misses.
DOWN_AFTER_FAILURES = 2

# Blood moon warning fires this many game hours before the horde spawns at dusk.
WARN_HOURS_BEFORE_DUSK = 2


def _diff(server: Server, snap: Snapshot, prev: dict) -> tuple[list[Event], dict]:
    """Compare one snapshot to the last-seen state. Returns (events, new state)."""
    events: list[Event] = []
    state = dict(prev)
    label = server.name

    if not snap.online:
        fails = int(prev.get("fail_count", 0)) + 1
        state["fail_count"] = fails
        # Only announce once, on the poll where it crosses the threshold.
        if fails == DOWN_AFTER_FAILURES and prev.get("online", True):
            state["online"] = False
            events.append(
                Event("down", label, "Server is down", f"{server.addr} — {snap.error or 'no response'}")
            )
        elif fails >= DOWN_AFTER_FAILURES:
            state["online"] = False
        return events, state

    state["fail_count"] = 0
    was_online = prev.get("online")
    state["online"] = True

    if was_online is False:
        events.append(Event("up", label, "Server is back up", f"{snap.players}/{snap.max_players} online"))

    # poll_all() has already folded this poll into the measured clock rate, so it's
    # sitting in `prev` by the time we get here.
    gs = gamestate.read(snap, prev.get("tick_rate"))

    # A changed map or version means the world was wiped or the server updated.
    prev_map, prev_ver = prev.get("map"), prev.get("version")
    if prev_map and (prev_map != snap.map or prev_ver != snap.version):
        events.append(
            Event(
                "server_reset",
                label,
                "Server reset or updated",
                f"{prev_map} {prev_ver} \N{RIGHTWARDS ARROW} {snap.map} {snap.version}",
            )
        )
    state["map"] = snap.map
    state["version"] = snap.version

    # Player count. 7DTD gives no names, so the count is all we can report.
    # On the poll where a server comes back, the remembered count is from before the
    # outage — diffing against it would report a phantom "1 player left". The `up`
    # event already carries the current count, so just re-baseline instead.
    recovered = was_online is False
    prev_players = None if recovered else prev.get("players")
    if prev_players is not None and snap.players != prev_players:
        kind = "join" if snap.players > prev_players else "leave"
        verb = "joined" if kind == "join" else "left"
        delta = abs(snap.players - prev_players)
        detail = f"{prev_players} \N{RIGHTWARDS ARROW} {snap.players}/{snap.max_players} players"
        if gs.known:
            detail += f" \N{MIDDLE DOT} Day {gs.day}, {gs.clock}"
        events.append(
            Event(kind, label, f"{delta} player{'s' if delta != 1 else ''} {verb}", detail)
        )
    state["players"] = snap.players

    if gs.known:
        prev_day = prev.get("day")
        if prev_day is not None and gs.day > prev_day:
            events.append(
                Event("day_rollover", label, f"Day {gs.day}", gamestate.blood_moon_line(gs))
            )
        state["day"] = gs.day

        # Blood moon: warn a couple of game hours out, then again when the horde spawns.
        # Keyed on the *spawn* day, not the current day — the horde runs 22:00 -> 04:00
        # and the day counter rolls at midnight underneath it, so keying on gs.day would
        # fire a second "BLOOD MOON" alert at 00:00, halfway through the same horde.
        stage = None
        if gs.horde_active:
            stage = "start"
        elif gs.is_blood_moon_day and gs.hour >= gs.dusk_hour - WARN_HOURS_BEFORE_DUSK:
            stage = "warn"

        if stage:
            marker = f"{gs.horde_day}:{stage}"
            if prev.get("bm_alert") != marker:
                paused = " (paused — server empty)" if gs.frozen else ""
                if stage == "start":
                    left = gamestate.fmt_duration(gs.real_minutes_to_horde_end)
                    title = f"BLOOD MOON — Day {gs.horde_day}"
                    body = f"Horde is out. {snap.players}/{snap.max_players} online, ~{left} real left{paused}."
                else:
                    real = gamestate.fmt_duration(gs.real_minutes_to_horde)
                    game = gamestate.fmt_duration(gs.game_minutes_to_horde)
                    title = f"Blood moon tonight — Day {gs.horde_day}"
                    body = f"Horde at {gs.dusk_hour:02d}:00 — ~{real} real{paused}, {game} game time."
                events.append(Event("blood_moon", label, title, body))
                state["bm_alert"] = marker

    return events, state


def tick(cfg: Config, state: dict, dry_run: bool = False, quiet: bool = False) -> tuple[list[Event], dict]:
    """One poll of every server. Returns the events emitted and the updated state."""
    snaps = poll_all(cfg, state)
    emitted: list[Event] = []

    for server in cfg.servers:
        snap = snaps.get(server.addr)
        if snap is None:
            continue
        events, new_state = _diff(server, snap, state.get(server.addr, {}))
        state[server.addr] = new_state

        for ev in events:
            if ev.kind not in cfg.events:
                continue
            notify.dispatch(ev, cfg, dry_run=dry_run)
            emitted.append(ev)
            if not quiet and not dry_run:
                stamp = time.strftime("%H:%M:%S")
                print(f"[{stamp}] {ev.server}: {ev.title} — {ev.body}".rstrip(" —"))

    return emitted, state


def run(cfg: Config, interval: int, once: bool = False, dry_run: bool = False) -> int:
    if not cfg.servers:
        print("No servers configured. Try: 7dtd-watch servers import")
        return 1

    state = load_state()

    if once:
        events, state = tick(cfg, state, dry_run=dry_run)
        save_state(state)
        if dry_run and not events:
            print("[dry-run] no changes since last poll")
        return 0

    sinks = []
    if cfg.desktop_enabled:
        sinks.append("desktop")
    if cfg.discord_webhook:
        sinks.append("discord")
    print(
        f"watching {len(cfg.servers)} server(s) every {interval}s "
        f"\N{RIGHTWARDS ARROW} {', '.join(sinks) if sinks else 'no sinks enabled'}. Ctrl-C to stop."
    )

    try:
        while True:
            try:
                _, state = tick(cfg, state, dry_run=dry_run)
                save_state(state)
            except OSError as exc:
                # Network dropped out from under us — don't kill the watcher.
                print(f"poll error: {exc}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0
