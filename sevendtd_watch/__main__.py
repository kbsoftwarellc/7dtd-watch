"""CLI entry point: status / dash / watch / servers / test-notify."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, a2s, clock, config, dash, notify, render, watch
from .config import Config, Server


def cmd_status(cfg: Config, args) -> int:
    servers = cfg.find(args.server) if args.server else cfg.servers
    if not servers:
        target = f"matching {args.server!r}" if args.server else "configured"
        print(f"No servers {target}. Try: 7dtd-watch servers import")
        return 1

    scoped = Config(poll_interval=cfg.poll_interval, servers=servers, notify=cfg.notify, events=cfg.events)

    # Polling through the shared state keeps the measured clock rate current, so a
    # one-shot `status` benefits from whatever `dash`/`watch` have already measured.
    state = config.load_state()
    snaps = dash.poll_all(scoped, state)
    config.save_state(state)

    if args.json:
        out = [
            render.to_dict(s, snaps[s.addr], clock.rate_for(state, s.addr))
            for s in servers
            if s.addr in snaps
        ]
        json.dump(out if len(out) != 1 else out[0], sys.stdout, indent=2)
        print()
        return 0

    p = render.Paint(render.color_enabled())
    for i, server in enumerate(servers):
        snap = snaps.get(server.addr)
        if snap is None:
            continue
        if i:
            print()
        print("\n".join(render.block(server, snap, p, rate=clock.rate_for(state, server.addr))))

    return 0 if any(snaps[s.addr].online for s in servers if s.addr in snaps) else 2


def cmd_dash(cfg: Config, args) -> int:
    return dash.run(cfg, max(config.MIN_POLL_INTERVAL, args.interval or cfg.poll_interval))


def cmd_watch(cfg: Config, args) -> int:
    return watch.run(
        cfg,
        max(config.MIN_POLL_INTERVAL, args.interval or cfg.poll_interval),
        once=args.once,
        dry_run=args.dry_run,
    )


def cmd_test_notify(cfg: Config, args) -> int:
    ev = notify.Event(
        kind="blood_moon",
        server="7dtd-watch",
        title="Test notification",
        body="If you can read this, the sink works.",
    )
    sent = notify.dispatch(ev, cfg)
    if not sent:
        print("No sink accepted the event.")
        if not cfg.desktop_enabled:
            print("  desktop: disabled in config")
        if not cfg.discord_webhook:
            print("  discord: no webhook set (config notify.discord_webhook or $SEVENDTD_DISCORD_WEBHOOK)")
        return 1
    print(f"sent via: {', '.join(sent)}")
    return 0


def cmd_servers(cfg: Config, args) -> int:
    action = args.action or "list"

    if action == "list":
        if not cfg.servers:
            print("No servers configured. Try: 7dtd-watch servers import")
            return 1
        for s in cfg.servers:
            print(f"  {s.name:<24} {s.addr}")
        print(f"\nconfig: {config.CONFIG_PATH}")
        return 0

    if action == "add":
        if not args.address:
            print("usage: 7dtd-watch servers add HOST:PORT [--name NAME]")
            return 1
        host, _, port = args.address.rpartition(":")
        if not host or not port.isdigit():
            print(f"bad address {args.address!r} — expected HOST:PORT")
            return 1

        print(f"probing {host}:{port} ...")
        snap = a2s.query(host, int(port))
        if not snap.online:
            print(f"  no response ({snap.error}). Adding anyway.")
        else:
            print(f"  {snap.name} — {snap.players}/{snap.max_players} online")

        name = args.name or snap.name or args.address
        cfg.servers.append(Server(name=name, host=host, port=int(port)))
        config.save(cfg)
        print(f"added {name}")
        return 0

    if action == "remove":
        if not args.address:
            print("usage: 7dtd-watch servers remove NAME_OR_ADDRESS")
            return 1
        matches = cfg.find(args.address)
        if not matches:
            print(f"nothing matches {args.address!r}")
            return 1
        for m in matches:
            cfg.servers.remove(m)
            print(f"removed {m.name} ({m.addr})")
        config.save(cfg)
        return 0

    if action == "import":
        entries = config.import_history()
        if not entries:
            print(f"No server history found in {config.PREFS_PATH}")
            print("(Join a server in-game at least once, then try again.)")
            return 1

        known = {s.addr for s in cfg.servers}
        added = 0
        print(f"found {len(entries)} server(s) in the game's history — probing...\n")
        for host, port in entries:
            addr = f"{host}:{port}"
            if addr in known:
                print(f"  {addr:<24} already configured")
                continue
            snap = a2s.query(host, port, timeout=2.0)
            if not snap.online:
                print(f"  {addr:<24} no response — skipped")
                continue
            print(f"  {addr:<24} {snap.name} ({snap.players}/{snap.max_players})")
            cfg.servers.append(Server(name=snap.name or addr, host=host, port=port))
            added += 1

        if added:
            config.save(cfg)
            print(f"\nadded {added} server(s) to {config.CONFIG_PATH}")
        else:
            print("\nnothing new to add.")
        return 0

    print(f"unknown action {action!r}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="7dtd-watch",
        description="Monitor 7 Days to Die servers without launching the game.",
    )
    ap.add_argument("--version", action="version", version=f"7dtd-watch {__version__}")
    sub = ap.add_subparsers(dest="command")

    s = sub.add_parser("status", help="one-shot snapshot of every server, then exit")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.add_argument("--server", help="only servers matching this name or address")
    s.set_defaults(func=cmd_status)

    d = sub.add_parser("dash", help="live self-refreshing dashboard")
    d.add_argument("--interval", type=int, help=f"seconds between polls (min {config.MIN_POLL_INTERVAL})")
    d.set_defaults(func=cmd_dash)

    w = sub.add_parser("watch", help="background poll loop, sends notifications on changes")
    w.add_argument("--interval", type=int, help=f"seconds between polls (min {config.MIN_POLL_INTERVAL})")
    w.add_argument("--once", action="store_true", help="poll once and exit (for cron)")
    w.add_argument("--dry-run", action="store_true", help="print events instead of sending them")
    w.set_defaults(func=cmd_watch)

    sv = sub.add_parser("servers", help="list / add / remove / import servers")
    sv.add_argument("action", nargs="?", choices=["list", "add", "remove", "import"], default="list")
    sv.add_argument("address", nargs="?", help="HOST:PORT to add, or name/address to remove")
    sv.add_argument("--name", help="display name when adding")
    sv.set_defaults(func=cmd_servers)

    t = sub.add_parser("test-notify", help="fire a sample event through every enabled sink")
    t.set_defaults(func=cmd_test_notify)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    # Bare `7dtd-watch` is the thing you want most often.
    if not args.command:
        args = ap.parse_args(["status"])

    cfg = config.load()
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
