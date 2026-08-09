#!/usr/bin/env python3
"""Tray tests: the icon/tooltip/away logic, and the events that drive it.

None of this needs Qt or a display — `tray.py` deliberately keeps every decision out of
the Qt layer so it can be checked here.

Run: python3 tests/test_tray.py
"""

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Point the config at a scratch dir *before* importing it — its paths are module-level
# constants, and a test must never touch the real ~/.config/7dtd-watch.
_TMP = tempfile.mkdtemp(prefix="7dtd-watch-test-")
os.environ["XDG_CONFIG_HOME"] = _TMP

from sevendtd_watch import config, tray  # noqa: E402
from sevendtd_watch.a2s import Session, Snapshot  # noqa: E402
from sevendtd_watch.config import Config, Server  # noqa: E402
from sevendtd_watch.gamestate import TICKS_PER_DAY, TICKS_PER_HOUR  # noqa: E402
from sevendtd_watch.notify import Event  # noqa: E402
from sevendtd_watch.watch import _diff  # noqa: E402

SERVER = Server(name="Test", host="203.0.113.10", port=26900)
OTHER = Server(name="Other", host="198.51.100.20", port=26900)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def snap(players=1, day=16, hour=12, minute=0, freq=10, online=True, server=SERVER):
    ticks = (day - 1) * TICKS_PER_DAY + hour * TICKS_PER_HOUR + minute * TICKS_PER_HOUR // 60
    s = Snapshot(host=server.host, port=server.port, online=online, polled_at=1000.0)
    if not online:
        s.error = "timed out"
        return s
    s.name, s.map, s.version = "Test Server", "Navezgane", "01.03.00"
    s.players, s.max_players = players, 12
    s.sessions = [Session("", 0, 10.0)] * players
    s.rules = {
        "CurrentServerTime": str(ticks),
        "BloodMoonFrequency": str(freq),
        "DayLightLength": "18",
        "DayNightLength": "60",
    }
    return s


def kinds(events):
    return [e.kind for e in events]


# --------------------------------------------------------------- clock crossing
# The whole point of the feature: 0 <-> non-zero is what starts and stops the world
# clock, and it is reported as its own kind rather than as a join/leave.

_, empty_state = _diff(SERVER, snap(players=0), {})
check("baseline records zero", empty_state["players"], 0)

events, state = _diff(SERVER, snap(players=1), empty_state)
check("0 -> 1 is clock_start", kinds(events), ["clock_start"])
check("clock_start title", events[0].title, "Someone is on \N{EM DASH} clock running")
check("clock_start body has count", "0 \N{RIGHTWARDS ARROW} 1/12 players" in events[0].body, True)
check("clock_start body has day", "Day 16, 12:00" in events[0].body, True)
check("clock_start is stamped", events[0].at > 0, True)

# A jump straight from empty to several is still one clock_start, not a join as well.
events, _ = _diff(SERVER, snap(players=4), empty_state)
check("0 -> 4 is one clock_start", kinds(events), ["clock_start"])

events, stopped = _diff(SERVER, snap(players=0), state)
check("1 -> 0 is clock_stop", kinds(events), ["clock_stop"])
check("clock_stop title", events[0].title, "Server empty \N{EM DASH} clock paused")
check("clock_stop says where it froze", "frozen at Day 16, 12:00" in events[0].body, True)

# Non-zero to non-zero is unchanged: still join/leave, no clock event.
events, _ = _diff(SERVER, snap(players=3), state)
check("1 -> 3 stays a join", kinds(events), ["join"])
events, _ = _diff(SERVER, snap(players=1), {"players": 3, "online": True, "day": 16})
check("3 -> 1 stays a leave", kinds(events), ["leave"])

# A server coming back from an outage re-baselines rather than inventing a crossing.
down_state = {"players": 2, "online": False, "fail_count": 2, "day": 16}
events, _ = _diff(SERVER, snap(players=0), down_state)
check("recovery does not fake a clock_stop", kinds(events), ["up"])

# ------------------------------------------------------------------- migration

legacy = {
    "poll_interval": 15,
    "servers": [{"name": "Test", "host": "1.2.3.4", "port": 26900}],
    "notify": {"desktop": True, "discord_webhook": ""},
    "events": ["join", "leave", "down", "up", "blood_moon", "day_rollover", "server_reset"],
}
config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
config.CONFIG_PATH.write_text(json.dumps(legacy))
cfg = config.load()
check("migration enables clock_start", "clock_start" in cfg.events, True)
check("migration enables clock_stop", "clock_stop" in cfg.events, True)
check("migration stamps the version", cfg.version, config.CONFIG_VERSION)
check("migration was written back", json.loads(config.CONFIG_PATH.read_text())["version"], config.CONFIG_VERSION)

# An explicit opt-out survives a reload — migration only runs once, on the old version.
cfg.events.remove("clock_stop")
config.save(cfg)
check("opt-out is not re-added", "clock_stop" in config.load().events, False)

# Tray defaults fill in for a config that has never heard of them.
check("tray default badge", config.load().tray_opt("badge_unseen"), True)
check("tray history floor", Config(tray={"history": 2}).history_limit, 10)
check("tray history bad value", Config(tray={"history": "nope"}).history_limit, config.DEFAULT_HISTORY)

# ------------------------------------------------------------------- event log

log = [Event("clock_start", "Test", "t1", "b1", at=100.0), Event("down", "Test", "t2", "b2", at=200.0)]
config.save_events([e.to_dict() for e in log])
back = [Event.from_dict(e) for e in config.load_events()]
check("event log round-trips", back, log)

config.EVENTS_PATH.write_text(json.dumps(log[0].to_dict()) + "\n{ this is torn")
check("torn last line is skipped", len(config.load_events()), 1)

config.save_events([{"kind": "join", "server": "s", "title": "t", "body": "", "at": i} for i in range(50)], limit=10)
check("event log is capped", len(config.load_events()), 10)
check("event log keeps the newest", config.load_events()[-1]["at"], 49)

# --------------------------------------------------------------------- overview

two = Config(servers=[SERVER, OTHER])


def overview(**by_addr):
    snaps = {addr: s for addr, s in by_addr.items()}
    return tray.summarise(two, snaps, {})


ov = overview(**{SERVER.addr: snap(players=2), OTHER.addr: snap(players=1, server=OTHER)})
check("players are summed across servers", ov.players, 3)
check("state live", ov.state, "live")

ov = overview(**{SERVER.addr: snap(players=0), OTHER.addr: snap(players=0, server=OTHER)})
check("all empty reads as paused", ov.state, "paused")
check("paused has no players", ov.players, 0)

ov = overview(**{SERVER.addr: snap(players=0), OTHER.addr: snap(players=2, server=OTHER)})
check("one populated server wins over an empty one", ov.state, "live")

ov = overview(**{SERVER.addr: snap(online=False), OTHER.addr: snap(online=False, server=OTHER)})
check("all offline reads as down", ov.state, "down")
check("offline counted", ov.offline, 2)

ov = overview(**{SERVER.addr: snap(online=False), OTHER.addr: snap(players=1, server=OTHER)})
check("a reachable server outranks a dead one", ov.state, "live")

check("no snapshots at all is unknown", tray.summarise(two, {}, {}).state, "unknown")

# Horde night with players on -> red. The same night with nobody on is not urgent: the
# clock is frozen, so the horde is going nowhere until somebody logs in.
horde = snap(players=2, day=20, hour=23, freq=10)
check("live horde colours the icon", tray.summarise(two, {SERVER.addr: horde}, {}).state, "horde")
frozen_horde = snap(players=0, day=20, hour=23, freq=10)
check("frozen horde does not", tray.summarise(two, {SERVER.addr: frozen_horde}, {}).state, "paused")

# ------------------------------------------------------------------ detail text

lines = tray.server_lines(SERVER, snap(players=2), rate=5.74)
check("detail leads with the play glyph", lines[0].startswith("\N{BLACK RIGHT-POINTING TRIANGLE}"), True)
check("detail has the count", "2/12 players" in lines[0], True)
check("detail says the clock runs", "clock running" in lines[1], True)
check("detail has a horde line", lines[2].strip().startswith("horde:"), True)

lines = tray.server_lines(SERVER, snap(players=0), rate=5.74)
check("empty detail leads with pause", lines[0].startswith("\N{DOUBLE VERTICAL BAR}"), True)
check("empty detail shouts about the clock", "CLOCK PAUSED" in lines[1], True)

lines = tray.server_lines(SERVER, snap(online=False), rate=None)
check("offline detail", "OFFLINE" in lines[0], True)
check("offline detail has the error", "timed out" in lines[1], True)

check("un-polled server", "not polled yet" in tray.server_lines(SERVER, None, None)[0], True)

# An unmeasured clock rate is called out, because the ETA is then a config guess.
check("unmeasured rate is flagged", any("not yet measured" in ln for ln in tray.server_lines(SERVER, snap(), None)), True)
check("measured rate is not flagged", any("not yet measured" in ln for ln in tray.server_lines(SERVER, snap(), 5.74)), False)

check("ago seconds", tray.ago(12), "12s ago")
check("ago minutes", tray.ago(600), "10m ago")
check("ago hours", tray.ago(7200), "2h ago")
check("ago days", tray.ago(200000), "2d ago")
check("ago short drops the suffix", tray.ago(12, short=True), "12s")
check("ago short hours", tray.ago(7200, short=True), "2h")

# ------------------------------------------------------------- compact (tooltip)
# A panel tooltip is a narrow column. Every compact line has to fit without wrapping,
# which is the one property worth asserting: no line runs long, and the long server
# name the server advertises is replaced by the short one from the config.

LONG = Server(name="Short Name", host="203.0.113.10", port=26900)
wordy = snap(players=2, server=LONG)
wordy.name = "Some Server | PVE | Experimental Long Advertised Name"

compact = tray.server_lines(LONG, wordy, rate=5.74, compact=True)
full = tray.server_lines(LONG, wordy, rate=5.74)
check("compact uses the config name", "Short Name" in compact[0], True)
check("compact drops the advertised name", "Experimental" in "".join(compact), False)
check("full keeps the advertised name", "Experimental" in full[0], True)
check("compact fits a narrow tooltip", max(len(ln) for ln in compact) <= 40, True)
check("compact is three lines", len(compact), 3)
check("compact says running", "running" in compact[1], True)
check("compact drops the rate aside", any("not yet measured" in ln for ln in compact), False)

paused_compact = tray.server_lines(LONG, snap(players=0, server=LONG), rate=5.74, compact=True)
check("compact shouts PAUSED", "PAUSED" in paused_compact[1], True)
check("compact paused still fits", max(len(ln) for ln in paused_compact) <= 40, True)

horde_compact = tray.server_lines(LONG, snap(players=2, day=20, hour=23, server=LONG), 5.74, compact=True)
check("compact horde is loud", "HORDE OUT" in horde_compact[2], True)
check("compact horde fits", max(len(ln) for ln in horde_compact) <= 40, True)

off_compact = tray.server_lines(LONG, snap(online=False, server=LONG), None, compact=True)
check("compact offline", "OFFLINE" in off_compact[0], True)

# Blood moons off — no horde line to print, and nothing should blow up.
check("compact with no blood moons", len(tray.server_lines(LONG, snap(freq=0, server=LONG), 5.74, True)), 2)

# ---------------------------------------------------- per-server state and meta

check("state live", tray.server_state(snap(players=2), None), "live")
check("state paused", tray.server_state(snap(players=0), None), "paused")
check("state down", tray.server_state(snap(online=False), None), "down")
check("state unknown", tray.server_state(None, None), "unknown")
import sevendtd_watch.gamestate as _gs  # noqa: E402

live_horde = snap(players=2, day=20, hour=23)
check("state horde", tray.server_state(live_horde, _gs.read(live_horde, 5.74)), "horde")
dead_horde = snap(players=0, day=20, hour=23)
check("frozen horde is only paused", tray.server_state(dead_horde, _gs.read(dead_horde, 5.74)), "paused")
check("every state has a colour", all(s in tray.STATE_COLOURS for s in
      ["live", "paused", "down", "horde", "unknown"]), True)

meta = tray.meta_line(snap())
check("meta has the map", "Navezgane" in meta, True)
check("meta has the version", "01.03.00" in meta, True)
check("meta has the ping", "ms" in meta, True)

# ------------------------------------------------------------------- headline

def ov_of(**by_addr):
    return tray.summarise(two, by_addr, {})


busy = ov_of(**{SERVER.addr: snap(players=2), OTHER.addr: snap(players=1, server=OTHER)})
check("headline counts players", tray.headline(busy), "7dtd-watch \N{MIDDLE DOT} 3 players online")
one = ov_of(**{SERVER.addr: snap(players=1)})
check("headline singular", tray.headline(one), "7dtd-watch \N{MIDDLE DOT} 1 player online")
quiet = ov_of(**{SERVER.addr: snap(players=0), OTHER.addr: snap(players=0, server=OTHER)})
check("headline paused", tray.headline(quiet), "7dtd-watch \N{MIDDLE DOT} clocks paused")
dead = ov_of(**{SERVER.addr: snap(online=False), OTHER.addr: snap(online=False, server=OTHER)})
check("headline all offline", tray.headline(dead), "7dtd-watch \N{MIDDLE DOT} all servers offline")
check("headline no readings", tray.headline(tray.Overview()), "7dtd-watch \N{MIDDLE DOT} no readings yet")

# An offline server must not hide behind a headline about the ones that answered.
mixed = ov_of(**{SERVER.addr: snap(online=False), OTHER.addr: snap(players=0, server=OTHER)})
check("headline surfaces an offline server", "1 offline" in tray.headline(mixed), True)
mixed_live = ov_of(**{SERVER.addr: snap(online=False), OTHER.addr: snap(players=2, server=OTHER)})
check("headline keeps both facts", tray.headline(mixed_live), "7dtd-watch \N{MIDDLE DOT} 2 players online \N{MIDDLE DOT} 1 offline")

hordey = ov_of(**{SERVER.addr: snap(players=2, day=20, hour=23)})
check("headline shouts blood moon", "BLOOD MOON" in tray.headline(hordey), True)

# Every headline has to fit the tooltip column without being clipped.
for label, o in [("busy", busy), ("quiet", quiet), ("dead", dead), ("mixed", mixed), ("horde", hordey)]:
    check(f"headline fits ({label})", len(tray.headline(o)) <= tray.TOOLTIP_WIDTH, True)

# ------------------------------------------------------------- clip and labels

check("clip leaves short text alone", tray.clip("abc", 10), "abc")
check("clip at the boundary", tray.clip("abcde", 5), "abcde")
check("clip trims and marks", tray.clip("abcdefghij", 5), "abcd\N{HORIZONTAL ELLIPSIS}")
check("clip never returns empty", len(tray.clip("abcdef", 1)) > 0, True)

check("short event: clock start", tray.short_event(Event("clock_start", "s", "Someone is on — clock running", "")), "clock running")
check("short event: clock stop", tray.short_event(Event("clock_stop", "s", "Server empty — clock paused", "")), "clock paused")
check("short event falls back to the title", tray.short_event(Event("day_rollover", "s", "Day 387", "")), "Day 387")
check("short event clips a long title", len(tray.short_event(Event("join", "s", "x" * 80, ""))), 20)

# ------------------------------------------------------------------ away summary

history = [
    Event("clock_stop", "Test", "Server empty", "b", at=50.0),
    Event("clock_start", "Test", "Someone is on", "b", at=150.0),
    Event("join", "Test", "1 player joined", "b", at=160.0),
    Event("down", "Other", "Server is down", "b", at=170.0),
]
check("nothing missed before the lock", tray.away_summary(history, since=500.0), None)

summary = tray.away_summary(history, since=100.0)
check("away summary skips what you saw", "Server empty" in summary, False)
check("away summary groups by server", summary.count("\n"), 1)
check("away summary keeps the latest per kind", "Someone is on" in summary and "1 player joined" in summary, True)
check("away summary covers every server", "Other:" in summary, True)

churn = [
    Event("clock_start", "Test", "Someone is on", "b", at=10.0),
    Event("clock_stop", "Test", "Server empty", "b", at=20.0),
    Event("clock_start", "Test", "Someone is on", "b", at=30.0),
]
check("churn collapses to one line per kind", tray.away_summary(churn, since=0.0).count("\n"), 0)

# --------------------------------------------------------------------- autostart

tray.AUTOSTART_DIR = pathlib.Path(_TMP) / "autostart"
tray.AUTOSTART_PATH = tray.AUTOSTART_DIR / "7dtd-watch-tray.desktop"
check("autostart starts off", tray.autostart_enabled(), False)
tray.set_autostart(True)
check("autostart on", tray.autostart_enabled(), True)
desktop = tray.AUTOSTART_PATH.read_text()
check("autostart is an application", "Type=Application" in desktop, True)
check("autostart launches the tray", desktop.strip().splitlines()[-2].startswith("Exec=") or "tray" in desktop, True)
check("autostart is not a terminal app", "Terminal=false" in desktop, True)
tray.set_autostart(False)
check("autostart off", tray.autostart_enabled(), False)
tray.set_autostart(False)  # removing twice must not raise

# ------------------------------------------------------------------------ done

if failures:
    print("TRAY TESTS FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("tray tests ok")
