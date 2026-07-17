"""Turning A2S rules into the things a player actually wants to know.

Pure functions over a Snapshot — no sockets, no I/O — so the math is testable
without a live server. Run `python3 -m sevendtd_watch.gamestate --selftest` to
check it against values measured from real servers.

The load-bearing facts, all verified against live 7DTD servers:

  * `CurrentServerTime` is a tick counter. 24000 ticks = one game day,
    1000 ticks = one game hour. This is the only source of the game date.
  * The `DayCount` rule is NOT the current day — it read 3 on two servers whose
    real days were 16 and 23. It is deliberately ignored.
  * The clock stops dead when nobody is online, which is also why a real-world
    horde ETA is only meaningful while the server is populated.
  * A game day starts at 04:00. `DayLightLength` hours of light puts dusk at
    04:00 + 18h = 22:00, which is also when the blood moon horde spawns.
  * The horde runs from 22:00 until 04:00 *the next day* — it straddles the day
    counter, which rolls at 00:00. So `day % freq == 0` alone cannot tell you
    whether the horde is out: by 01:00 the day has advanced past the blood moon
    day while the zombies are still coming.
"""

from __future__ import annotations

from dataclasses import dataclass

from .a2s import Snapshot

TICKS_PER_DAY = 24000
TICKS_PER_HOUR = 1000
DAY_STARTS_AT_HOUR = 4
GAME_MINUTES_PER_DAY = 24 * 60


@dataclass
class GameState:
    known: bool  # False when the server didn't give us a clock
    day: int
    hour: int
    minute: int
    is_night: bool
    dusk_hour: int
    frozen: bool  # clock is stopped because the server is empty
    blood_moon_freq: int
    is_blood_moon_day: bool
    days_to_blood_moon: int
    game_minutes_to_dusk: int  # 0 once dusk has passed for the day

    # The horde, in the terms the question actually gets asked in.
    horde_active: bool  # zombies are out right now
    horde_day: int  # the day the current-or-next horde spawns on (0 = never)
    game_minutes_to_horde: int  # 0 while the horde is active
    real_minutes_to_horde: float  # only counts down while somebody is online
    real_minutes_to_horde_end: float  # how much longer it lasts, while active

    # Which clock speed the real-world numbers above were computed with.
    ticks_per_second: float
    rate_measured: bool  # False = derived from DayNightLength rather than observed
    day_night_length: int  # real minutes per full game day, per the server config

    @property
    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def phase_icon(self) -> str:
        if self.horde_active:
            return "\N{LARGE RED CIRCLE}"
        return "\N{CRESCENT MOON}" if self.is_night else "\N{BLACK SUN WITH RAYS}"


def elements(ticks: int) -> tuple[int, int, int]:
    """Split a `CurrentServerTime` tick count into (day, hour, minute).

    Verified: 372072 -> Day 16, 12:04.
    """
    day = ticks // TICKS_PER_DAY + 1
    into_day = ticks % TICKS_PER_DAY
    hour = into_day // TICKS_PER_HOUR
    minute = into_day % TICKS_PER_HOUR * 60 // TICKS_PER_HOUR
    return day, hour, minute


def blood_moon(day: int, freq: int) -> tuple[bool, int]:
    """(is tonight a horde night, game days until the next one)."""
    if freq <= 0:
        return False, 0
    is_today = day % freq == 0
    return is_today, (freq - day % freq) % freq


def config_ticks_per_second(day_night_length: int) -> float:
    """The clock speed the server's config implies: 24000 ticks over DayNightLength minutes."""
    if day_night_length <= 0:
        day_night_length = 60
    return TICKS_PER_DAY / (day_night_length * 60)


def real_minutes(game_minutes: float, ticks_per_second: float) -> float:
    """Game minutes -> real-world minutes, at a given clock speed."""
    if ticks_per_second <= 0:
        return 0.0
    ticks = game_minutes * TICKS_PER_HOUR / 60
    return ticks / ticks_per_second / 60


def fmt_duration(minutes: float) -> str:
    """`45m` / `4h 25m` / `3d 23h` — same shape for game time and real time."""
    total = int(round(minutes))
    if total < 1:
        return "<1m"
    if total < 60:
        return f"{total}m"
    if total < GAME_MINUTES_PER_DAY:
        return f"{total // 60}h {total % 60:02d}m"
    days, rem = divmod(total, GAME_MINUTES_PER_DAY)
    return f"{days}d {rem // 60}h"


def _horde(day: int, hour: int, minute: int, freq: int, dusk_hour: int) -> tuple[bool, int, int, int]:
    """(horde_active, horde_day, game_minutes_to_spawn, game_minutes_to_end).

    Everything in absolute game minutes since day 1 00:00 — the only sane way to reason
    about a horde that spawns at 22:00 on day D and ends at 04:00 on day D+1.
    """
    if freq <= 0:
        return False, 0, 0, 0

    now = (day - 1) * GAME_MINUTES_PER_DAY + hour * 60 + minute

    def spawn_at(d: int) -> int:
        return (d - 1) * GAME_MINUTES_PER_DAY + dusk_hour * 60

    def ends_at(d: int) -> int:
        return d * GAME_MINUTES_PER_DAY + DAY_STARTS_AT_HOUR * 60

    # Only two horde nights can possibly be in progress: the one that spawned today, and
    # the one that spawned yesterday and hasn't ended yet.
    for candidate in (day, day - 1):
        if candidate >= 1 and candidate % freq == 0 and spawn_at(candidate) <= now < ends_at(candidate):
            return True, candidate, 0, ends_at(candidate) - now

    _, days_ahead = blood_moon(day, freq)
    target = day + days_ahead
    if spawn_at(target) <= now:  # today's horde has already come and gone
        target += freq
    return False, target, spawn_at(target) - now, 0


def read(snap: Snapshot, rate: float | None = None) -> GameState:
    """Derive the game state from a snapshot. Safe on a partial or offline one.

    `rate` is the observed ticks/sec from `clock.observe()`. Without one, the real-world
    numbers fall back to what DayNightLength implies — which runs optimistic on a server
    whose clock is slower than its config claims.
    """
    ticks = snap.rule_int("CurrentServerTime", -1)
    freq = snap.rule_int("BloodMoonFrequency", 7)
    daylight = snap.rule_int("DayLightLength", 18)
    day_night_length = snap.rule_int("DayNightLength", 60)

    measured = bool(rate and rate > 0)
    tps = float(rate) if measured else config_ticks_per_second(day_night_length)

    if ticks < 0:
        return GameState(
            known=False, day=0, hour=0, minute=0, is_night=False,
            dusk_hour=DAY_STARTS_AT_HOUR + daylight, frozen=False,
            blood_moon_freq=freq, is_blood_moon_day=False, days_to_blood_moon=0,
            game_minutes_to_dusk=0,
            horde_active=False, horde_day=0, game_minutes_to_horde=0,
            real_minutes_to_horde=0.0, real_minutes_to_horde_end=0.0,
            ticks_per_second=tps, rate_measured=measured,
            day_night_length=day_night_length,
        )

    day, hour, minute = elements(ticks)
    dusk_hour = DAY_STARTS_AT_HOUR + daylight
    is_night = hour >= dusk_hour or hour < DAY_STARTS_AT_HOUR
    is_bm, days_to_bm = blood_moon(day, freq)

    to_dusk = max(0, dusk_hour * 60 - (hour * 60 + minute))
    active, horde_day, to_horde, to_end = _horde(day, hour, minute, freq, dusk_hour)

    return GameState(
        known=True,
        day=day,
        hour=hour,
        minute=minute,
        is_night=is_night,
        dusk_hour=dusk_hour,
        frozen=snap.players == 0,
        blood_moon_freq=freq,
        is_blood_moon_day=is_bm,
        days_to_blood_moon=days_to_bm,
        game_minutes_to_dusk=to_dusk,
        horde_active=active,
        horde_day=horde_day,
        game_minutes_to_horde=to_horde,
        real_minutes_to_horde=real_minutes(to_horde, tps),
        real_minutes_to_horde_end=real_minutes(to_end, tps),
        ticks_per_second=tps,
        rate_measured=measured,
        day_night_length=day_night_length,
    )


def blood_moon_line(gs: GameState) -> str:
    """The horde line — real-world time first, because that is the question being asked.

    The clock only runs while somebody is online, so on an empty server the real figure
    is how long it will take *once people are on*, and the line says so.
    """
    if not gs.known:
        return "horde: unknown (server did not report a clock)"
    if gs.blood_moon_freq <= 0:
        return "blood moons disabled"

    paused = " (paused — server empty)" if gs.frozen else ""

    if gs.horde_active:
        left = fmt_duration(gs.real_minutes_to_horde_end)
        return f"BLOOD MOON — horde is out, ~{left} real left{paused}"

    real = fmt_duration(gs.real_minutes_to_horde)
    game = fmt_duration(gs.game_minutes_to_horde)
    return f"~{real} real{paused}  \N{MIDDLE DOT}  day {gs.horde_day}, in {game} game time"


def _selftest() -> int:
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    # Measured live from a running server.
    check("elements(372072)", elements(372072), (16, 12, 4))
    check("elements(371277)", elements(371277), (16, 11, 16))
    # Measured live from a second server (clock frozen, 0 players).
    check("elements(545701)", elements(545701), (23, 17, 42))

    check("elements(0)", elements(0), (1, 0, 0))
    check("elements(24000)", elements(24000), (2, 0, 0))
    check("elements(23999)", elements(23999), (1, 23, 59))

    # One server runs BloodMoonFrequency=10, another runs 7.
    check("bm day16 freq10", blood_moon(16, 10), (False, 4))
    check("bm day20 freq10", blood_moon(20, 10), (True, 0))
    check("bm day23 freq7", blood_moon(23, 7), (False, 5))
    check("bm day21 freq7", blood_moon(21, 7), (True, 0))
    check("bm day1 freq7", blood_moon(1, 7), (False, 6))
    check("bm freq0", blood_moon(5, 0), (False, 0))

    # A game hour is DayNightLength/24 real minutes: 60/24 = 2.5 at default, i.e.
    # 24000 ticks / 3600s = 6.667 ticks/sec.
    check("config rate @60", round(config_ticks_per_second(60), 3), 6.667)
    check("config rate @0 (bad rule)", round(config_ticks_per_second(0), 3), 6.667)

    check("fmt 0.4m", fmt_duration(0.4), "<1m")
    check("fmt 45m", fmt_duration(45), "45m")
    check("fmt 264.8m", fmt_duration(264.8), "4h 25m")
    check("fmt 5721m", fmt_duration(5721), "3d 23h")

    snap = Snapshot(host="x", port=1, online=True, players=1)
    snap.rules = {
        "CurrentServerTime": "372072",  # day 16, 12:04
        "BloodMoonFrequency": "10",
        "DayLightLength": "18",
        "DayNightLength": "60",
    }
    gs = read(snap)
    check("read day", gs.day, 16)
    check("read clock", gs.clock, "12:04")
    check("read dusk_hour", gs.dusk_hour, 22)
    check("read is_night", gs.is_night, False)
    check("read frozen", gs.frozen, False)
    check("read days_to_bm", gs.days_to_blood_moon, 4)
    # 12:04 -> 22:00 is 9h56m = 596 game minutes.
    check("read to_dusk", gs.game_minutes_to_dusk, 596)

    # Day 16 12:04 -> the day-20 horde at 22:00 is 4 days + 9h56m = 6356 game minutes.
    check("horde day", gs.horde_day, 20)
    check("horde not active", gs.horde_active, False)
    check("game minutes to horde", gs.game_minutes_to_horde, 6356)
    # At the config rate (6.667 t/s): 6356 * 60/1440 = 264.8 real minutes.
    check("real minutes to horde", round(gs.real_minutes_to_horde, 1), 264.8)
    check("rate not measured", gs.rate_measured, False)

    # The same countdown at the rate actually measured on the live server. A slower
    # clock puts the horde *further* away in real time — by 43 minutes here, which is
    # the whole reason the rate is measured rather than trusted.
    gs_measured = read(snap, rate=5.74)
    check("measured rate used", gs_measured.rate_measured, True)
    check("real minutes at 5.74 t/s", round(gs_measured.real_minutes_to_horde, 1), 307.6)
    check("measured is later than config", gs_measured.real_minutes_to_horde > gs.real_minutes_to_horde, True)

    # Empty server -> clock reported frozen, not treated as an error.
    snap.players = 0
    check("frozen when empty", read(snap).frozen, True)
    check("frozen line says paused", "paused — server empty" in blood_moon_line(read(snap)), True)

    # Horde night: 23:00 on day 20 at freq 10. Out, and running until 04:00 on day 21.
    snap.players = 2
    snap.rules["CurrentServerTime"] = str(19 * TICKS_PER_DAY + 23 * TICKS_PER_HOUR)  # day 20, 23:00
    gs = read(snap)
    check("night day20", (gs.day, gs.clock, gs.is_night, gs.is_blood_moon_day), (20, "23:00", True, True))
    check("horde active day20 23:00", gs.horde_active, True)
    check("horde day while active", gs.horde_day, 20)
    check("5 game hours of horde left", gs.real_minutes_to_horde_end, real_minutes(300, gs.ticks_per_second))
    check("bm line active", blood_moon_line(gs), "BLOOD MOON — horde is out, ~12m real left")

    # 02:00 on day 21 — the day counter has rolled, the horde has NOT ended. This is the
    # case the old `day % freq == 0` test got wrong: it reported the next blood moon as
    # 9 days out while the base was actively being eaten.
    snap.rules["CurrentServerTime"] = str(20 * TICKS_PER_DAY + 2 * TICKS_PER_HOUR)  # day 21, 02:00
    gs = read(snap)
    check("day21 02:00 reads as day 21", gs.day, 21)
    check("horde still active after midnight", gs.horde_active, True)
    check("horde day is the spawn day", gs.horde_day, 20)
    check("no countdown while active", gs.game_minutes_to_horde, 0)

    # 05:00 on day 21 — over. The next one is day 30.
    snap.rules["CurrentServerTime"] = str(20 * TICKS_PER_DAY + 5 * TICKS_PER_HOUR)  # day 21, 05:00
    gs = read(snap)
    check("horde over by 05:00", gs.horde_active, False)
    check("next horde is day 30", gs.horde_day, 30)

    # Blood moon day, before the spawn: a countdown, not "active".
    snap.rules["CurrentServerTime"] = str(19 * TICKS_PER_DAY + 21 * TICKS_PER_HOUR)  # day 20, 21:00
    gs = read(snap)
    check("pre-spawn not active", gs.horde_active, False)
    check("pre-spawn horde day", gs.horde_day, 20)
    check("pre-spawn 1 game hour out", gs.game_minutes_to_horde, 60)

    # Blood moons disabled.
    snap.rules["BloodMoonFrequency"] = "0"
    gs = read(snap)
    check("freq 0: no horde", (gs.horde_active, gs.horde_day, gs.game_minutes_to_horde), (False, 0, 0))
    check("freq 0 line", blood_moon_line(gs), "blood moons disabled")

    # Missing clock -> known=False, no crash.
    check("no clock", read(Snapshot(host="x", port=1)).known, False)

    if failures:
        print("SELFTEST FAILED")
        for f in failures:
            print("  -", f)
        return 1
    print("selftest ok")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
