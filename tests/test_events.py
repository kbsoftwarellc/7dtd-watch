#!/usr/bin/env python3
"""Event-diff tests: feed watch._diff synthetic snapshots and assert what it emits.

Run: python3 tests/test_events.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sevendtd_watch.a2s import Session, Snapshot  # noqa: E402
from sevendtd_watch.config import Server  # noqa: E402
from sevendtd_watch.gamestate import TICKS_PER_DAY, TICKS_PER_HOUR  # noqa: E402
from sevendtd_watch.watch import DOWN_AFTER_FAILURES, _diff  # noqa: E402

SERVER = Server(name="Test", host="203.0.113.10", port=26900)

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def snap(players=1, day=16, hour=12, minute=0, freq=10, map_="Navezgane", version="01.03.00", online=True):
    ticks = (day - 1) * TICKS_PER_DAY + hour * TICKS_PER_HOUR + minute * TICKS_PER_HOUR // 60
    s = Snapshot(host=SERVER.host, port=SERVER.port, online=online)
    if not online:
        s.error = "timed out"
        return s
    s.name, s.map, s.version = "Test Server", map_, version
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


# First sight of a server must be silent — no alert storm on startup.
events, state = _diff(SERVER, snap(players=2), {})
check("first poll is silent", kinds(events), [])
check("first poll records players", state["players"], 2)
check("first poll records day", state["day"], 16)

# Player count changes.
events, s2 = _diff(SERVER, snap(players=3), state)
check("join fires", kinds(events), ["join"])
check("join title", events[0].title, "1 player joined")
check("join body has count", "2 \N{RIGHTWARDS ARROW} 3/12 players" in events[0].body, True)

events, s3 = _diff(SERVER, snap(players=1), s2)
check("leave fires", kinds(events), ["leave"])
check("leave title", events[0].title, "2 players left")

events, _ = _diff(SERVER, snap(players=3), s2)
check("no event when count is unchanged", kinds(events), [])

# Going down: one dropped packet must not alert; the threshold must.
state_up = {"online": True, "players": 2, "day": 16, "map": "Navezgane", "version": "01.03.00"}
events, s = _diff(SERVER, snap(online=False), state_up)
check("single failure is silent", kinds(events), [])
check("failure counted", s["fail_count"], 1)

for _ in range(DOWN_AFTER_FAILURES - 1):
    events, s = _diff(SERVER, snap(online=False), s)
check("down fires at threshold", kinds(events), ["down"])
check("marked offline", s["online"], False)

events, s = _diff(SERVER, snap(online=False), s)
check("down does not repeat", kinds(events), [])

events, s = _diff(SERVER, snap(players=1), s)
check("up fires on recovery", kinds(events), ["up"])
check("fail count reset", s["fail_count"], 0)

# Day rollover.
base = {"online": True, "players": 1, "day": 16, "map": "Navezgane", "version": "01.03.00"}
events, _ = _diff(SERVER, snap(players=1, day=17), base)
check("day rollover fires", kinds(events), ["day_rollover"])

# Blood moon: warn before dusk, fire at dusk, never repeat within the same night.
bm = {"online": True, "players": 1, "day": 20, "map": "Navezgane", "version": "01.03.00"}

events, _ = _diff(SERVER, snap(players=1, day=20, hour=12, freq=10), bm)
check("no warning at midday", kinds(events), [])

events, s = _diff(SERVER, snap(players=1, day=20, hour=20, freq=10), bm)
check("warning fires 2h before dusk", kinds(events), ["blood_moon"])
check("warning is a warning", events[0].title, "Blood moon tonight — Day 20")

events, s = _diff(SERVER, snap(players=1, day=20, hour=21, freq=10), s)
check("warning does not repeat", kinds(events), [])

events, s = _diff(SERVER, snap(players=1, day=20, hour=22, freq=10), s)
check("horde fires at dusk", kinds(events), ["blood_moon"])
check("horde is the start", events[0].title, "BLOOD MOON — Day 20")

events, s = _diff(SERVER, snap(players=1, day=20, hour=23, freq=10), s)
check("horde does not repeat", kinds(events), [])

# 01:00 on day 21. The horde spawned at 22:00 on day 20 and runs until 04:00, but the
# day counter rolled at midnight underneath it. The alert is keyed on the spawn day, so
# this must NOT fire a second "BLOOD MOON — Day 21" halfway through the same horde.
events, s = _diff(SERVER, snap(players=1, day=21, hour=1, freq=10), s)
check("midnight does not re-alert mid-horde", kinds(events), ["day_rollover"])
check("rollover line knows the horde is out", "horde is out" in events[0].body, True)

# 05:00 on day 21: the horde is over, and nothing more is said about it.
events, s = _diff(SERVER, snap(players=1, day=21, hour=5, freq=10), s)
check("quiet once the horde ends", kinds(events), [])

# Non-blood-moon day at dusk stays quiet.
events, _ = _diff(SERVER, snap(players=1, day=16, hour=22, freq=10), base)
check("dusk on a normal day is quiet", kinds(events), [])

# A wipe / update changes the map or version.
events, _ = _diff(SERVER, snap(players=1, map_="Pregen10k"), base)
check("map change fires reset", kinds(events), ["server_reset"])
events, _ = _diff(SERVER, snap(players=1, version="01.04.00"), base)
check("version change fires reset", kinds(events), ["server_reset"])

if failures:
    print("FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("event tests ok")
