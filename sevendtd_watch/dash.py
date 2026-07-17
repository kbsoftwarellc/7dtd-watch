"""The live dashboard — the thing that replaces reopening the in-game server search."""

from __future__ import annotations

import select
import shutil
import sys
import termios
import time
import tty
from concurrent.futures import ThreadPoolExecutor

from . import a2s, clock, render
from .config import Config, load_state, save_state

HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
HOME = "\033[H"
CLEAR_LINE = "\033[K"
CLEAR_BELOW = "\033[J"
CLEAR_SCREEN = "\033[2J"


def poll_all(cfg: Config, state: dict | None = None) -> dict[str, a2s.Snapshot]:
    """Poll every server at once — one slow server shouldn't stall the others.

    Pass `state` to fold each snapshot into that server's measured clock rate. The
    caller owns loading and saving it, so `watch` keeps its single load/save and can't
    stomp its own event state.
    """
    if not cfg.servers:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, len(cfg.servers))) as pool:
        futures = {s.addr: pool.submit(a2s.query, s.host, s.port) for s in cfg.servers}
        snaps = {addr: f.result() for addr, f in futures.items()}

    if state is not None:
        for snap in snaps.values():
            clock.observe(state, snap)
    return snaps


class _Keys:
    """Read single keypresses without blocking, restoring the terminal on the way out."""

    def __init__(self):
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.saved = None

    def __enter__(self):
        if self.fd is not None:
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.fd is not None and self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def get(self, timeout: float) -> str | None:
        """Wait up to `timeout` for a keypress. Returns None if the time ran out."""
        if self.fd is None:
            time.sleep(timeout)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        return sys.stdin.read(1)


def _frame(
    cfg: Config,
    snaps: dict[str, a2s.Snapshot],
    p: render.Paint,
    next_poll: float,
    state: dict | None = None,
) -> str:
    now = time.time()
    width = shutil.get_terminal_size((80, 24)).columns

    out = [p("7dtd-watch", render.BOLD) + p(f"  \N{MIDDLE DOT}  {len(cfg.servers)} server(s)", render.GREY)]
    out.append(p("\N{BOX DRAWINGS LIGHT HORIZONTAL}" * min(width, 64), render.GREY))

    for server in cfg.servers:
        snap = snaps.get(server.addr)
        if snap is None:
            continue
        rate = clock.rate_for(state, server.addr) if state is not None else None
        out.extend(render.block(server, snap, p, now=now, rate=rate))
        out.append("")

    remaining = max(0, next_poll - now)
    out.append(
        p(f"refresh in {remaining:.0f}s  \N{MIDDLE DOT}  [r] refresh now  [q] quit", render.DIM)
    )
    return "\n".join(out)


def run(cfg: Config, interval: int) -> int:
    if not cfg.servers:
        print("No servers configured. Try: 7dtd-watch servers import")
        return 1

    p = render.Paint(render.color_enabled())

    # Not a terminal (piped, redirected)? Fall back to plain repeated snapshots.
    # flush=True because a pipe is block-buffered and would otherwise show nothing.
    state = load_state()

    if not sys.stdout.isatty():
        while True:
            snaps = poll_all(cfg, state)
            save_state(state)
            print(_frame(cfg, snaps, p, time.time() + interval, state), flush=True)
            time.sleep(interval)

    sys.stdout.write(HIDE_CURSOR + CLEAR_SCREEN)
    try:
        with _Keys() as keys:
            snaps: dict[str, a2s.Snapshot] = {}
            next_poll = 0.0
            while True:
                if time.time() >= next_poll:
                    snaps = poll_all(cfg, state)
                    save_state(state)
                    next_poll = time.time() + interval

                # Redraw in place (home + clear-to-EOL per line) rather than clearing
                # the whole screen, which would flicker on every tick.
                body = _frame(cfg, snaps, p, next_poll, state)
                sys.stdout.write(HOME + CLEAR_LINE.join(line + "\n" for line in body.split("\n")) + CLEAR_BELOW)
                sys.stdout.flush()

                # Tick once a second so the countdown moves, but poll only on `interval`.
                key = keys.get(min(1.0, max(0.05, next_poll - time.time())))
                if key in ("q", "Q"):
                    return 0
                if key in ("r", "R"):
                    next_poll = 0.0
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write(SHOW_CURSOR + "\n")
        sys.stdout.flush()
