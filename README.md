# 7dtd-watch

Monitor a 7 Days to Die server without launching the game.

Instead of opening the game → **Join Game** → searching for the server → closing and
reopening the search to force a refresh, you get:

```
$ 7dtd-watch status

● My Community Server  203.0.113.10:26900
    players  3/12 ███░░░░░░░░░
    time     Day 16 · 22:39 🌙
    horde    ~4h 09m real  ·  day 20, in 3d 23h game time
    sessions longest 40m, newest 16m  (7DTD hides player names)
    Navezgane · 01.03.00 · NorthAmericaWest · 123ms · password/modded

● Friends' Server  198.51.100.20:26900
    players  0/8 ░░░░░░░░░░░░
    time     Day 23 · 17:42 ☀  (clock frozen — server empty)
    horde    ~5h 11m real (paused — server empty)  ·  day 28, in 5d 4h game time
    Pregen10k · 01.03.00 · NorthAmericaEast · 56ms · password/modded
```

It reads the **Steam A2S query protocol** over UDP — the exact same public status
query the in-game server browser sends. No admin access, no RCON, no telnet, no
mods on the server. It works on any server you can see in the browser.

## Requirements

Python 3.10+. **No dependencies** — standard library only. No venv, no pip.

The optional `tray` command is the one exception: it needs PyQt6
(`sudo dnf install python3-pyqt6` / `sudo apt install python3-pyqt6`). Everything
else works without it, and the tray tells you what to install if it's missing.

## Usage

```bash
./7dtd-watch                      # same as `status`
./7dtd-watch status               # one-shot snapshot of every server, then exit
./7dtd-watch status --json        # machine-readable (waybar, polybar, scripts)
./7dtd-watch status --server pseudo   # just the one you care about

./7dtd-watch dash                 # live self-refreshing panel; [r] refresh, [q] quit
./7dtd-watch dash --interval 10

./7dtd-watch watch                # background loop, notifies you when things change
./7dtd-watch watch --once         # single poll, for cron
./7dtd-watch watch --dry-run      # print events instead of sending them

./7dtd-watch tray                 # tray icon: hover for detail, click for the window
./7dtd-watch tray --install-autostart   # ...and start it at login
./7dtd-watch tray --remove-autostart

./7dtd-watch servers list
./7dtd-watch servers add 203.0.113.10:26900 --name "My Server"
./7dtd-watch servers remove "My Server"
./7dtd-watch servers import       # read the servers you've joined, out of the game itself

./7dtd-watch test-notify          # check the notification sinks work
```

### Discovering servers

On first run the server list is empty — you add the servers you care about.
`servers import` reads 7DTD's own Unity prefs
(`~/.config/unity3d/The Fun Pimps/7 Days To Die/prefs`) and decodes the
`ServerHistoryCache` key — a base64 blob wrapped around another base64 blob —
into the list of servers you've actually joined. Join a new server in-game once,
run `servers import`, and it shows up here. No IP typing.

### Notifications

`watch` diffs each poll against the last and emits:

| event | when |
|---|---|
| `clock_start` | the server went from empty to occupied — **the world clock is running again** |
| `clock_stop` | the last player left — **the world clock is frozen** |
| `join` / `leave` | player count changed, without crossing zero |
| `down` / `up` | server stopped/started answering (after 2 consecutive misses, so one dropped UDP packet doesn't cry wolf) |
| `blood_moon` | 2 game-hours before the horde spawns, and again when it does |
| `day_rollover` | a new in-game day started |
| `server_reset` | the map or version changed — a wipe or an update |

A change that crosses zero is reported as `clock_start`/`clock_stop` *instead of* a
`join`/`leave`, never as both — crossing zero is the bigger news, and one thing
happening should not put two toasts on screen.

Sinks are **desktop toasts** (`notify-send`), a **Discord webhook**, and the tray.
All are optional and fail soft. Set the webhook in the config, or via
`$SEVENDTD_DISCORD_WEBHOOK` so it never has to be committed anywhere.

State lives in `~/.config/7dtd-watch/state.json`, so restarting the watcher doesn't
replay a backlog of alerts. The first sight of a server is always silent.

### Running the watcher in the background

```bash
# systemd user service
systemd-run --user --unit=7dtd-watch ~/Documents/7dtd-watch/7dtd-watch watch

# or from cron, every 5 minutes
*/5 * * * * ~/Documents/7dtd-watch/7dtd-watch watch --once
```

## The tray

```bash
./7dtd-watch tray
```

`status` is a command you have to run, `dash` is a window you have to leave open, and
`watch` only speaks when something changes — so if you're away from the desk, the toast
it fired is gone before you get back. The tray is the piece that is always there.

**The icon is the answer.** It carries the number of players online across every server,
so "is anybody on?" is answered without hovering, clicking, or remembering to check.

| icon | meaning |
|---|---|
| green **`3`** | 3 players on. The world clock is running. |
| grey **`⏸`** | reachable, but empty — **the world clock is frozen**, nothing is progressing |
| red **`4`** | blood moon horde is out, right now, with players on |
| red **`!`** | no server is answering |
| amber dot | events have happened that you haven't looked at yet |

**Hover for the short version.** A panel tooltip is a narrow column, so it gets the
short form: player count, day and clock, whether that clock is moving, horde ETA. Every
line is written — and hard-clipped — to fit without wrapping.

```
7dtd-watch · 3 players online

⚠ 2 new since you looked
   ☀ My Community Server · Day 387 (5m)
   ▶ My Community Server · clock running (1m)

▶ My Community Server   3/12
   Day 16 · 22:39 🌙 · running
   horde ~4h 37m real · day 20

⏸ Friends' Server   0/12
   Day 23 · 17:42 ☀ · PAUSED
   horde ~5h 11m real · day 28

updated 7s ago · click for detail
```

**Click for the long version.** Left-click opens a details window with room to breathe:
a card per server with player count, game day, clock and world-clock state as labelled
stats, the full horde line, session lengths, map/version/ping/flags, and the recent
event log underneath. It refreshes itself while open, and sizes to its content the first
time so there's no dead space.

**A badge for what you missed.** Every event is kept in
`~/.config/7dtd-watch/events.jsonl`, and the icon wears a numbered amber dot until you
look. Walk away, come back to a green **`2`** with a dot on it, and you know somebody
logged on and the clock is moving again — whether or not you saw the toast, and even if
the tray was restarted or the machine rebooted in between. Hovering lists what they
were; opening the menu or the window clears the badge.

**Screen-lock aware.** If the session locks and things happen, unlocking gets one
summary notification — *"While you were away (37m): My Community Server: ▶ Someone is
on — clock running"* — instead of nothing. This rides KDE/GNOME's
`org.freedesktop.ScreenSaver` signal; where that isn't available the badge still does
the job on its own.

Right-click for the menu: the details window, per-server detail, recent events, refresh
now, open the `dash` in a terminal, per-event alert toggles, and start-at-login.
Everything toggled there is written straight back to the config file.

Autostart writes `~/.config/autostart/7dtd-watch-tray.desktop`, either from the menu or
with `./7dtd-watch tray --install-autostart`.

Run the tray *or* `watch`, not both — they share `state.json` and would fight over it.
The tray runs the same poll loop, so it costs no extra traffic and gives the same
alerts. A lock file at `~/.config/7dtd-watch/tray.lock` stops two trays starting.

## Config

`~/.config/7dtd-watch/config.json` is written on first run with an **empty** server
list — add your own with `servers import` / `servers add`, or edit the file directly.
The shape:

```json
{
  "poll_interval": 15,
  "servers": [
    {"name": "My Community Server", "host": "203.0.113.10", "port": 26900}
  ],
  "notify": {"desktop": true, "discord_webhook": ""},
  "events": ["join", "leave", "down", "up", "blood_moon", "day_rollover",
             "server_reset", "clock_start", "clock_stop"],
  "tray": {"badge_unseen": true, "summary_on_return": true, "history": 100}
}
```

A config written by an older version is missing the event kinds added since, which
would silently mute those alerts. `version` tracks that: loading an old config enables
the new kinds once and writes the file back. Removing a kind by hand after that sticks —
the migration only runs on the older version.

Poll interval floors at 5s. Each poll is three small UDP round-trips per server —
about what the in-game browser does while it's open. These are public status queries.

## The horde countdown

Every server line leads with **how long, in real-world time, until the horde spawns** —
on any day, not just the blood moon itself. "Blood moon in 4 days" is a game-time answer
to a real-world question; `~4h 09m real` is the answer you can plan an evening around.

Two things make that number honest:

* **The clock speed is measured, not assumed.** `DayNightLength=60` implies the game
  clock runs at 24000 ticks / 3600s = **6.67 ticks/sec**, but a live server was measured
  running at **5.74** — 14% slow, which is worth over half an hour on a four-hour
  countdown. The tool samples the real rate between polls (rejecting anything it can't
  trust: an empty server in the window, a restart, a window too short or too long), and
  falls back to the config-derived rate only until it has a sample. `dash` and `watch`
  poll often enough to calibrate within a couple of polls; `status` then reuses whatever
  they measured.
* **A paused clock is labelled paused.** The world doesn't tick with nobody in it, so on
  an empty server the ETA is what it will take *once somebody logs on*, and says
  `(paused — server empty)` rather than quietly counting down against a stopped clock.

While the horde is out, the line switches to how much longer it lasts:

```
horde    BLOOD MOON — horde is out, ~12m real left
```

## What this can and can't tell you

**It can't give you player names.** 7DTD returns *blank* names in its A2S_PLAYER
reply — the protocol carries the field, the game just doesn't fill it in. You get the
player count and how long each session has been connected, which is why the tool
reports `longest 40m, newest 16m` instead of a name list. Names would require telnet
or RCON on a server you administer.

**The game clock freezes when the server is empty.** This is real 7DTD behavior, not a
stale reading — the world stops ticking with nobody in it. The tool says so explicitly
rather than showing a time that quietly stopped being true.

## How the game clock works

Worth writing down, because two of these are traps:

* **`CurrentServerTime` is a tick counter.** 24000 ticks = one game day,
  1000 ticks = one game hour. That is the only source of the in-game date:
  ```
  day    = t // 24000 + 1
  hour   = (t % 24000) // 1000
  minute = (t % 24000 % 1000) * 60 // 1000
  ```
* **The `DayCount` rule is NOT the current day.** It read `3` on two different
  servers whose real days were 16 and 23. It is deliberately ignored. Trusting it
  gives you confidently wrong output.
* **`CurrentPlayers` (a rule) lags the count in A2S_INFO** by a poll or two. The
  player count always comes from A2S_INFO.
* A game day starts at **04:00**; `DayLightLength` (18h) puts dusk at **22:00**,
  which is also when the blood moon horde spawns.
* A blood moon falls on every `BloodMoonFrequency`-th day (7 or 10, depending on the
  server), and the horde runs **22:00 → 04:00 the next day**. It straddles the day
  counter, which rolls at midnight: at 01:00 the game says "Day 21" while the day-20
  horde is still eating your base. Testing `day % freq == 0` alone will tell you the next
  blood moon is 9 days away in the middle of one.
* `DayNightLength` is *real* minutes per full game day (60), so one game hour is 2.5
  real minutes — but only while somebody is online to make the clock tick, and only if
  the server's clock actually keeps up with its own config (the one measured above does
  not).

## Tests

```bash
python3 -m sevendtd_watch.gamestate --selftest   # clock, horde countdown, blood-moon math
python3 tests/test_clock.py                      # tick-rate measurement (mostly: what it rejects)
python3 tests/test_events.py                     # notification event logic
python3 tests/test_tray.py                       # tray icon state, tooltip, away summary, migration
```

`test_tray.py` needs no Qt and no display — `tray.py` keeps every decision (what the
icon should say, what fits in a tooltip, what you missed) out of the Qt layer
precisely so it can be tested.

The clock assertions are pinned to values measured off live servers, so if the tick
math ever drifts, the selftest fails rather than the dashboard lying.
