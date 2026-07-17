#!/usr/bin/env python3
"""Tick-rate measurement: feed clock.observe() synthetic polls, assert what it accepts.

The whole point of this module is to be *skeptical* — a bad sample silently poisons
every horde ETA the tool prints, so most of these tests are about what gets rejected.

Run: python3 tests/test_clock.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sevendtd_watch import clock  # noqa: E402
from sevendtd_watch.a2s import Snapshot  # noqa: E402

ADDR = "203.0.113.10:26900"
MEASURED_RATE = 5.74  # the rate a live server was actually measured running at
CONFIG_RATE = 24000 / (60 * 60)  # 6.667 t/s, what DayNightLength=60 claims

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def snap(ticks, at, players=2):
    s = Snapshot(host="203.0.113.10", port=26900, online=True, players=players)
    s.polled_at = at
    s.rules = {"CurrentServerTime": str(int(ticks)), "DayNightLength": "60"}
    return s


def sample(state, t0=1000.0, dt=15.0, rate=MEASURED_RATE, players=2, prev_players=2, ticks0=372072):
    """Two polls dt apart with the clock running at `rate`. Returns what observe() gives."""
    clock.observe(state, snap(ticks0, t0, prev_players))
    return clock.observe(state, snap(ticks0 + rate * dt, t0 + dt, players))


check("config rate", round(clock.config_rate(snap(0, 0)), 3), 6.667)
check("no rate to start", clock.rate_for({}, ADDR), None)

# The first poll can only establish a baseline — there's nothing to measure against.
state = {}
check("first poll has no rate", clock.observe(state, snap(372072, 1000.0)), None)
check("first poll stores a baseline", state[ADDR]["tick_ticks"], 372072)

# A clean 15s window on a populated server: measured, and close to the truth. Ticks come
# back as whole numbers, so a 15s window quantizes to ~0.07 t/s — hence the tolerance.
state = {}
rate = sample(state)
check("good sample accepted", abs(rate - MEASURED_RATE) < 0.1, True)
check("sample counted", state[ADDR]["tick_samples"], 1)
check("rate_for reads it back", abs(clock.rate_for(state, ADDR) - MEASURED_RATE) < 0.1, True)

# An empty server at either end of the window tells us nothing: the clock is frozen for
# some unknown part of it, so the apparent rate would be too slow.
state = {}
check("empty now -> no rate", sample(state, players=0), None)
state = {}
check("empty before -> no rate", sample(state, prev_players=0), None)

# Windows too short (jitter dominates) or too long (the server may have emptied mid-way).
state = {}
check("2s window rejected", sample(state, dt=2.0), None)
state = {}
check("300s window rejected", sample(state, dt=300.0), None)

# A frozen clock despite players, and a server that restarted and rolled the clock back.
state = {}
clock.observe(state, snap(372072, 1000.0))
check("zero delta rejected", clock.observe(state, snap(372072, 1015.0)), None)
check("restart rejected", clock.observe(state, snap(500, 1030.0)), None)
check("restart re-baselines", state[ADDR]["tick_ticks"], 500)

# Junk rates outside [0.25x, 2x] of what the config implies get dropped rather than
# poisoning the ETA.
state = {}
check("absurdly fast rejected", sample(state, rate=CONFIG_RATE * 3), None)
state = {}
check("absurdly slow rejected", sample(state, rate=CONFIG_RATE * 0.1), None)
# ...but a modded DayNightLength still calibrates: 1.5x config is inside the band.
state = {}
check("1.5x config accepted", abs(sample(state, rate=CONFIG_RATE * 1.5) - CONFIG_RATE * 1.5) < 0.1, True)

# The EMA damps a single odd sample instead of letting it swing the countdown.
state = {ADDR: {"tick_rate": 5.7, "tick_samples": 4}}
smoothed = sample(state, rate=6.6)
check("EMA pulls toward the new sample", 5.7 < smoothed < 6.6, True)
check("EMA stays closer to history", abs(smoothed - (0.3 * 6.6 + 0.7 * 5.7)) < 0.05, True)

# An offline server must not disturb the rate we already have.
state = {ADDR: {"tick_rate": 5.74}}
off = Snapshot(host="203.0.113.10", port=26900, online=False)
check("offline keeps the rate", clock.observe(state, off), 5.74)

# A server that reports no clock at all (some modded ones) mustn't crash or invent one.
state = {}
noclock = Snapshot(host="203.0.113.10", port=26900, online=True, players=1)
noclock.polled_at = 1000.0
check("no clock -> no rate", clock.observe(state, noclock), None)

if failures:
    print("FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("clock tests ok")
