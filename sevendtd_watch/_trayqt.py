"""The Qt half of the tray. Imported only once PyQt6 is known to be present.

Kept apart from `tray.py` so that everything decidable without a display — what the
icon should say, what the tooltip reads, what you missed while the screen was locked —
stays importable and testable on a box with no Qt and no session bus.

Two threads. Qt owns the main one and never blocks on a socket; a `Poller` QThread runs
`watch.tick()` on the configured interval and hands back snapshots over a signal, which
Qt queues onto the main thread for us.
"""

from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QObject, QRect, Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import clock, config, gamestate, notify, tray, watch
from .config import Config

# Where the screen-lock signal comes from. KDE registers the freedesktop name on both
# of these paths and GNOME on the first; connecting to both and de-duplicating by value
# is cheaper than probing which one this desktop uses.
SCREENSAVER_PATHS = ["/org/freedesktop/ScreenSaver", "/ScreenSaver"]
SCREENSAVER_IFACE = "org.freedesktop.ScreenSaver"

CENTER = Qt.AlignmentFlag.AlignCenter.value


def _purge(menu: QMenu) -> None:
    """Empty a menu completely, submenus included.

    QMenu.clear() deletes only actions whose parent is the menu itself. A submenu's
    action is parented to the *submenu*, so addMenu() leaves both that action and the
    submenu QMenu behind on every rebuild -- and the tray menu is rebuilt whenever the
    server lines change. Measured on a 15s poll interval over five days: 57,381 live
    QMenu and 516,143 live QAction objects, 2.0 GB of RSS. Reparenting first means a
    deferred delete that has not landed yet can never be found twice.
    """
    for sub in menu.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly):
        sub.setParent(None)
        sub.deleteLater()
    menu.clear()


def _paint_icon(state: str, players: int, unseen: int) -> QIcon:
    """Draw the panel icon: a colour for the state, a number for the players, a dot for news."""
    size = tray.ICON_PX
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))

    fills = {
        "horde": tray.COL_HORDE,
        "live": tray.COL_LIVE,
        "paused": tray.COL_PAUSED,
        "down": tray.COL_DOWN,
        "unknown": tray.COL_PAUSED,
    }

    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(0, 0, 0, 0))
    p.setBrush(QColor(fills.get(state, tray.COL_PAUSED)))
    inset = size * 0.06
    p.drawRoundedRect(
        QRect(int(inset), int(inset), int(size - 2 * inset), int(size - 2 * inset)),
        int(size * 0.22),
        int(size * 0.22),
    )

    p.setBrush(QColor(tray.COL_TEXT))
    if state == "paused":
        # The pause glyph *is* the message: an empty server means the world clock has
        # stopped, so nothing is progressing until somebody logs in.
        bar_w, bar_h = size * 0.15, size * 0.42
        gap = size * 0.11
        top = (size - bar_h) / 2
        for dx in (-gap / 2 - bar_w, gap / 2):
            p.drawRoundedRect(
                QRect(int(size / 2 + dx), int(top), int(bar_w), int(bar_h)),
                int(bar_w * 0.35),
                int(bar_w * 0.35),
            )
    else:
        text = {"down": "!", "unknown": "?"}.get(state) or (
            str(players) if players <= 99 else "99+"
        )
        sizes = {1: 0.62, 2: 0.50, 3: 0.36}
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * sizes.get(len(text), 0.36)))
        p.setFont(font)
        p.setPen(QColor(tray.COL_TEXT))
        # The badge sits in the top-right corner. Lean the number away from it so the
        # two stay legible together at the ~24px a panel actually renders.
        nudge = int(size * 0.05) if unseen > 0 else 0
        p.drawText(QRect(-nudge, nudge, size, size), CENTER, text)

    if unseen > 0:
        r = size * 0.30
        cx, cy = size - r * 0.85, r * 0.85
        p.setPen(QColor(0, 0, 0, 170))
        p.setBrush(QColor(tray.COL_BADGE))
        p.drawEllipse(int(cx - r / 2), int(cy - r / 2), int(r), int(r))
        label = str(unseen) if unseen <= 9 else "+"
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(r * 0.78))
        p.setFont(font)
        p.setPen(QColor("#1B1B1B"))
        p.drawText(QRect(int(cx - r / 2), int(cy - r / 2), int(r), int(r)), CENTER, label)

    p.end()
    return QIcon(pix)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _stat(value: str, label: str, colour: str | None = None) -> str:
    """One big number over a small caption — the unit the window is built out of."""
    tint = f" color:{colour};" if colour else ""
    return (
        f'<td style="padding-right:22px">'
        f'<div style="font-size:15pt; font-weight:600;{tint}">{value}</div>'
        f'<div style="font-size:7pt; color:{tray.COL_DIM}; letter-spacing:1px">{label}</div>'
        f"</td>"
    )


def _card_html(server, snap, rate) -> str:
    """One server, as rich text. A card is a single QLabel — Qt lays HTML out better
    than a hand-built grid of widgets would, and it is far less code to keep aligned."""
    name = _esc(snap.name if snap is not None and snap.name else server.name)
    gs = gamestate.read(snap, rate) if snap is not None and snap.online else None
    state = tray.server_state(snap, gs)
    accent = tray.STATE_COLOURS[state]

    head = (
        f'<div><span style="color:{accent}; font-size:14pt">\N{BLACK CIRCLE}</span>'
        f'<span style="font-size:12pt; font-weight:600"> {name}</span>'
        f'<span style="color:{tray.COL_DIM}; font-size:9pt">&nbsp;&nbsp;{_esc(server.addr)}</span></div>'
    )

    if snap is None:
        return head + f'<div style="color:{tray.COL_DIM}; padding-top:6px">not polled yet</div>'

    if not snap.online:
        return head + (
            f'<div style="padding-top:6px"><span style="color:{tray.COL_DOWN}; font-weight:600">'
            f"OFFLINE</span> <span style='color:{tray.COL_DIM}'>\N{EM DASH} "
            f"{_esc(snap.error or 'no response')}</span></div>"
        )

    live = snap.players > 0
    cells = [
        _stat(
            f"{snap.players}<span style='font-size:9pt; color:{tray.COL_DIM}'>/{snap.max_players}</span>",
            "PLAYERS",
            accent if live else None,
        )
    ]
    if gs is not None and gs.known:
        cells.append(_stat(str(gs.day), "GAME DAY"))
        cells.append(_stat(f"{gs.clock} {gs.phase_icon}", "TIME"))
        cells.append(
            _stat(
                "RUNNING" if live else "PAUSED",
                "WORLD CLOCK",
                tray.COL_LIVE if live else tray.COL_BADGE,
            )
        )
    body = f'<table cellspacing="0" cellpadding="0" style="margin-top:10px"><tr>{"".join(cells)}</tr></table>'

    extra = ""
    if gs is not None and gs.known:
        hot = gs.horde_active or gs.is_blood_moon_day
        colour = tray.COL_HORDE if hot else tray.COL_DIM
        extra += (
            f'<div style="margin-top:10px">'
            f'<span style="font-size:7pt; color:{tray.COL_DIM}; letter-spacing:1px">HORDE&nbsp;&nbsp;</span>'
            f'<span style="color:{colour}{"; font-weight:600" if hot else ""}">'
            f"{_esc(gamestate.blood_moon_line(gs))}</span></div>"
        )
        if not gs.rate_measured:
            extra += (
                f'<div style="color:{tray.COL_DIM}; font-size:8pt; margin-top:2px">'
                f"clock speed not yet measured \N{EM DASH} this ETA is from the server config</div>"
            )
    if snap.sessions:
        longest = max(s.minutes for s in snap.sessions)
        newest = min(s.minutes for s in snap.sessions)
        extra += (
            f'<div style="color:{tray.COL_DIM}; font-size:8pt; margin-top:6px">'
            f"sessions: longest {longest:.0f}m, newest {newest:.0f}m "
            f"\N{EM DASH} 7DTD hides player names</div>"
        )

    meta = (
        f'<div style="color:{tray.COL_DIM}; font-size:8pt; margin-top:6px">'
        f"{_esc(tray.meta_line(snap))}</div>"
    )
    return head + body + extra + meta


class DetailsWindow(QWidget):
    """The roomy view. Everything the tooltip has to abbreviate, laid out properly.

    Rebuilt wholesale on every refresh — the content is a few hundred bytes of rich
    text, and diffing it would cost more than redrawing it.
    """

    def __init__(self, ui: "Tray"):
        super().__init__()
        self.ui = ui
        self.setWindowTitle("7dtd-watch")
        self.setMinimumWidth(tray.WINDOW_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        self.header = QLabel()
        self.header.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(self.header)

        self.cards = QVBoxLayout()
        self.cards.setSpacing(10)
        outer.addLayout(self.cards)

        self.events = QLabel()
        self.events.setTextFormat(Qt.TextFormat.RichText)
        self.events.setWordWrap(True)
        outer.addWidget(self.events)
        # Collects any slack in one place if the window is enlarged, so growing it does
        # not smear gaps between the cards.
        outer.addStretch(1)

        row = QHBoxLayout()
        self.stamp = QLabel()
        row.addWidget(self.stamp)
        row.addStretch(1)
        for label, slot in [
            ("Refresh", self.ui.poller.refresh),
            ("Open dash", self.ui._open_dash),
            ("Close", self.hide),
        ]:
            button = QPushButton(label)
            button.clicked.connect(slot)
            row.addWidget(button)
        outer.addLayout(row)

        self._card_widgets: list[QLabel] = []
        self._sized = False

    def _card(self, index: int) -> QLabel:
        """Reuse the QLabel for a given server so the window doesn't flicker on refresh."""
        while len(self._card_widgets) <= index:
            label = QLabel()
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet(
                "background: rgba(128,128,128,0.10); border-radius: 10px; padding: 12px;"
            )
            self.cards.addWidget(label)
            self._card_widgets.append(label)
        return self._card_widgets[index]

    def refresh(self) -> None:
        ui = self.ui
        self.header.setText(
            f'<div style="font-size:13pt; font-weight:600">{_esc(ui.headline())}</div>'
        )

        for i, server in enumerate(ui.cfg.servers):
            card = self._card(i)
            card.setText(_card_html(server, ui.snaps.get(server.addr), ui.rates.get(server.addr)))
            card.show()
        for extra in self._card_widgets[len(ui.cfg.servers) :]:
            extra.hide()

        recent = list(reversed(ui.events[-8:]))
        if recent:
            rows = "".join(
                f'<tr><td style="color:{tray.COL_DIM}; padding-right:10px; font-size:8pt">'
                f'{time.strftime("%H:%M", time.localtime(e.at)) if e.at else "--:--"}</td>'
                f'<td style="font-size:9pt">{e.icon} {_esc(e.server)}: {_esc(e.title)}'
                f'{f"<span style=color:{tray.COL_BADGE}> \N{BULLET}</span>" if e.at > ui.seen_at else ""}'
                f"</td></tr>"
                for e in recent
            )
            self.events.setText(
                f'<div style="font-size:7pt; color:{tray.COL_DIM}; letter-spacing:1px">RECENT</div>'
                f'<table cellspacing="0" cellpadding="2" style="margin-top:4px">{rows}</table>'
            )
            self.events.show()
        else:
            self.events.hide()

        age = tray.ago(time.time() - ui.overview.polled_at) if ui.overview.polled_at else "no reading yet"
        self.stamp.setText(f'<span style="color:{tray.COL_DIM}; font-size:8pt">updated {age}</span>')

    def showEvent(self, event):  # noqa: N802 - Qt naming
        # Opening the window is looking at it, so it clears the badge like the menu does.
        self.refresh()
        self.ui._mark_seen()
        if not self._sized:
            # Hug the content the first time. After that the size is the user's, and
            # reopening must not undo a resize they chose.
            #
            # heightForWidth, not sizeHint: these cards are word-wrapped rich text, and
            # sizeHint guesses their height from a width they are not being given, so it
            # over-reports and leaves dead space under the last card.
            self._sized = True
            self.layout().activate()
            wanted = self.heightForWidth(tray.WINDOW_WIDTH)
            self.resize(tray.WINDOW_WIDTH, wanted if wanted > 0 else self.sizeHint().height())
        super().showEvent(event)


class Poller(QThread):
    """Runs `watch.tick()` forever. One poll's worth of results per `polled` emission."""

    polled = pyqtSignal(object, object, object)  # snaps, rates, [(Event, sinks)]

    def __init__(self, cfg: Config, interval: int):
        super().__init__()
        self.cfg = cfg
        self.interval = interval
        self._wake = threading.Event()
        self._stopping = False

    def refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def run(self) -> None:  # noqa: D102 - QThread entry point
        state = config.load_state()
        while not self._stopping:
            batch: list[tuple[notify.Event, list[str]]] = []
            snaps: dict = {}
            try:
                _, state, snaps = watch.tick(
                    self.cfg,
                    state,
                    quiet=True,
                    on_event=lambda ev, sinks: batch.append((ev, sinks)),
                )
                config.save_state(state)
            except OSError as exc:
                # Network down, DNS gone, laptop suspended mid-poll — never kill the tray.
                print(f"poll error: {exc}", flush=True)

            rates = {s.addr: clock.rate_for(state, s.addr) for s in self.cfg.servers}
            self.polled.emit(snaps, rates, batch)

            self._wake.wait(self.interval)
            self._wake.clear()


class Tray(QObject):
    """The tray icon, its menu, and the small amount of state behind the badge.

    A QObject because the screen-lock handler is a D-Bus slot, and Qt will only bind one
    to a pyqtSlot on a real QObject.
    """

    def __init__(self, app: QApplication, cfg: Config, interval: int):
        super().__init__()
        self.app = app
        self.cfg = cfg
        self.snaps: dict = {}
        self.rates: dict = {}
        self.overview = tray.Overview()

        limit = cfg.history_limit
        self.events = [notify.Event.from_dict(e) for e in config.load_events(limit)]
        self.seen_at = config.load_seen()
        self.away_since: float | None = None
        self.locked = False

        self.icon = QSystemTrayIcon()
        self.icon.setToolTip("7dtd-watch \N{EM DASH} starting up\N{HORIZONTAL ELLIPSIS}")
        self.menu = QMenu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self.menu.aboutToShow.connect(self._mark_seen)

        self.poller = Poller(cfg, interval)
        self.poller.polled.connect(self._on_polled)

        # Keeps the "updated Ns ago" line honest between polls.
        self.ticker = QTimer()
        self.ticker.timeout.connect(self._refresh_tooltip)
        self.ticker.start(tray.TOOLTIP_REFRESH_MS)

        self._menu_shown: tuple = ()
        self.window = DetailsWindow(self)
        self._connect_screensaver()
        self._rebuild_menu()
        self._refresh_icon()
        self.icon.show()
        self.poller.start()

    # ---------------------------------------------------------------- lifecycle

    def _connect_screensaver(self) -> None:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return
        for path in SCREENSAVER_PATHS:
            # Empty service = accept the signal from whichever process owns the name.
            bus.connect("", path, SCREENSAVER_IFACE, "ActiveChanged", self._on_lock_changed)

    def quit(self) -> None:
        self.window.close()
        self.poller.stop()
        self.poller.wait(3000)
        self.icon.hide()
        self.app.quit()

    # ------------------------------------------------------------------- events

    @pyqtSlot(bool)
    def _on_lock_changed(self, active: bool) -> None:
        # Both paths are connected, so the same change usually arrives twice.
        if active == self.locked:
            return
        self.locked = active

        if active:
            self.away_since = time.time()
            return

        since, self.away_since = self.away_since, None
        if since is None or not self.cfg.tray_opt("summary_on_return"):
            return
        summary = tray.away_summary(self.events, since)
        if not summary:
            return
        gone = tray.ago(time.time() - since).replace(" ago", "")
        ev = notify.Event("away", "7dtd-watch", f"While you were away ({gone})", summary)
        if not notify.dispatch(ev, self.cfg):
            self.icon.showMessage(ev.title, summary, QSystemTrayIcon.MessageIcon.Information, 10000)

    def _on_polled(self, snaps: dict, rates: dict, batch: list) -> None:
        self.snaps = snaps or self.snaps
        self.rates = rates
        self.overview = tray.summarise(self.cfg, self.snaps, self.rates)

        for ev, sinks in batch:
            self.events.append(ev)
            # notify-send missing or the desktop sink turned off — say it ourselves
            # rather than dropping the alert on the floor.
            if not sinks:
                self.icon.showMessage(
                    f"{ev.icon} {ev.server}".strip(),
                    f"{ev.title}\n{ev.body}".strip(),
                    QSystemTrayIcon.MessageIcon.Critical
                    if ev.urgent
                    else QSystemTrayIcon.MessageIcon.Information,
                    10000,
                )
        if batch:
            limit = self.cfg.history_limit
            self.events = self.events[-limit:]
            config.save_events([e.to_dict() for e in self.events], limit)

        self._refresh_icon()
        self._refresh_tooltip()
        # Rebuilding pushes a whole new menu over DBus to the panel. Only do it when
        # something in it changed, and never while it is on screen under the cursor.
        if self._menu_key() != self._menu_shown and not self.menu.isVisible():
            self._rebuild_menu()

    def _on_activated(self, reason) -> None:
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        self.toggle_window()

    def toggle_window(self) -> None:
        if self.window.isVisible():
            self.window.hide()
            return
        self.window.refresh()
        self.window.show()
        # Wayland gives a tray app no say over stacking, so ask twice and settle for
        # whatever the compositor grants.
        self.window.raise_()
        self.window.activateWindow()

    # ------------------------------------------------------------------ unseen

    @property
    def unseen(self) -> list[notify.Event]:
        return [e for e in self.events if e.at > self.seen_at]

    def _mark_and_rebuild(self) -> None:
        self._mark_seen()
        self._rebuild_menu()

    def _mark_seen(self) -> None:
        if not self.unseen:
            return
        self.seen_at = time.time()
        config.save_seen(self.seen_at)
        self._refresh_icon()
        self._refresh_tooltip()

    # ------------------------------------------------------------------ display

    def headline(self) -> str:
        return tray.headline(self.overview)

    def detail_lines(self, compact: bool = False) -> list[str]:
        out: list[str] = []
        for server in self.cfg.servers:
            if out:
                out.append("")
            out.extend(
                tray.server_lines(
                    server, self.snaps.get(server.addr), self.rates.get(server.addr), compact=compact
                )
            )
        return out

    def _refresh_icon(self) -> None:
        unseen = len(self.unseen) if self.cfg.tray_opt("badge_unseen") else 0
        self.icon.setIcon(_paint_icon(self.overview.state, self.overview.players, unseen))

    def _refresh_tooltip(self) -> None:
        # Every line here is written to survive a narrow panel tooltip without wrapping:
        # short names, short durations, no trailing asides. The window is where the long
        # form lives.
        lines = [self.headline()]

        unseen = self.unseen
        if unseen:
            lines.append("")
            lines.append(f"\N{WARNING SIGN} {len(unseen)} new since you looked")
            for ev in unseen[-3:]:
                when = tray.ago(time.time() - ev.at, short=True)
                lines.append(f"   {ev.icon} {tray.clip(ev.server, 14)} \N{MIDDLE DOT} {tray.short_event(ev)} ({when})")

        lines.append("")
        lines.extend(self.detail_lines(compact=True))

        if self.overview.polled_at:
            lines.append("")
            lines.append(f"updated {tray.ago(time.time() - self.overview.polled_at)} \N{MIDDLE DOT} click for detail")
        # Backstop: whatever the strings above did, nothing wraps the panel column.
        self.icon.setToolTip("\n".join(tray.clip(ln, tray.TOOLTIP_WIDTH) for ln in lines))

        if self.window.isVisible():
            self.window.refresh()

    # --------------------------------------------------------------------- menu

    def _menu_key(self) -> tuple:
        """Fingerprint of everything the menu displays."""
        return (
            self.headline(),
            tuple(self.detail_lines()),
            len(self.events),
            len(self.unseen),
        )

    def _rebuild_menu(self) -> None:
        self._menu_shown = self._menu_key()
        _purge(self.menu)

        head = self.menu.addAction(self.headline())
        head.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Details window\N{HORIZONTAL ELLIPSIS}").triggered.connect(self.toggle_window)
        self.menu.addSeparator()

        for server in self.cfg.servers:
            lines = tray.server_lines(server, self.snaps.get(server.addr), self.rates.get(server.addr))
            sub = self.menu.addMenu(lines[0])
            for line in lines[1:]:
                act = sub.addAction(line.strip())
                act.setEnabled(False)
            sub.addSeparator()
            addr = sub.addAction(f"Copy {server.addr}")
            addr.triggered.connect(lambda _=False, a=server.addr: self.app.clipboard().setText(a))

        self.menu.addSeparator()
        self._add_recent_menu()

        unseen = self.unseen
        if unseen:
            mark = self.menu.addAction(f"Mark {len(unseen)} as seen")
            # Deferred: rebuilding the menu from inside its own handler would pull the
            # QAction out from under the signal that is still being delivered.
            mark.triggered.connect(lambda: QTimer.singleShot(0, self._mark_and_rebuild))

        self.menu.addSeparator()
        self.menu.addAction("Refresh now").triggered.connect(self.poller.refresh)
        self.menu.addAction("Open dashboard\N{HORIZONTAL ELLIPSIS}").triggered.connect(self._open_dash)

        self.menu.addSeparator()
        self._add_settings_menu()

        self.menu.addSeparator()
        self.menu.addAction("Quit").triggered.connect(self.quit)

    def _add_recent_menu(self) -> None:
        sub = self.menu.addMenu("Recent events")
        if not self.events:
            act = sub.addAction("nothing yet")
            act.setEnabled(False)
            return

        for ev in reversed(self.events[-20:]):
            stamp = time.strftime("%H:%M", time.localtime(ev.at)) if ev.at else "--:--"
            new = " \N{BULLET}" if ev.at > self.seen_at else ""
            act = sub.addAction(f"{stamp}  {ev.icon} {ev.server}: {ev.title}{new}")
            act.setEnabled(False)

        sub.addSeparator()
        sub.addAction("Clear history").triggered.connect(self._clear_history)

    def _add_settings_menu(self) -> None:
        sub = self.menu.addMenu("Settings")

        alerts = sub.addMenu("Alert me about")
        ordered = tray.EVENT_ORDER + [k for k in config.ALL_EVENTS if k not in tray.EVENT_ORDER]
        for kind in ordered:
            act = alerts.addAction(tray.EVENT_LABELS.get(kind, kind))
            act.setCheckable(True)
            act.setChecked(kind in self.cfg.events)
            act.toggled.connect(lambda on, k=kind: self._toggle_event(k, on))

        sub.addSeparator()
        badge = sub.addAction("Badge for unseen events")
        badge.setCheckable(True)
        badge.setChecked(bool(self.cfg.tray_opt("badge_unseen")))
        badge.toggled.connect(lambda on: self._toggle_tray("badge_unseen", on))

        summary = sub.addAction("Summary when I unlock the screen")
        summary.setCheckable(True)
        summary.setChecked(bool(self.cfg.tray_opt("summary_on_return")))
        summary.toggled.connect(lambda on: self._toggle_tray("summary_on_return", on))

        sub.addSeparator()
        auto = sub.addAction("Start at login")
        auto.setCheckable(True)
        auto.setChecked(tray.autostart_enabled())
        auto.toggled.connect(tray.set_autostart)

    # ------------------------------------------------------------------ actions

    def _toggle_event(self, kind: str, on: bool) -> None:
        if on and kind not in self.cfg.events:
            self.cfg.events.append(kind)
        elif not on and kind in self.cfg.events:
            self.cfg.events.remove(kind)
        config.save(self.cfg)

    def _toggle_tray(self, key: str, on: bool) -> None:
        self.cfg.tray[key] = bool(on)
        config.save(self.cfg)
        self._refresh_icon()

    def _clear_history(self) -> None:
        self.events = []
        config.save_events([], self.cfg.history_limit)
        self._mark_seen()
        self._refresh_icon()
        self._rebuild_menu()

    def _open_dash(self) -> None:
        if not tray.open_dashboard():
            self.icon.showMessage(
                "No terminal found",
                "Install konsole/kitty/xterm, or run `7dtd-watch dash` yourself.",
                QSystemTrayIcon.MessageIcon.Warning,
                8000,
            )


def launch(cfg: Config, interval: int) -> int:
    app = QApplication(["7dtd-watch"])
    app.setApplicationName("7dtd-watch")
    app.setApplicationDisplayName("7dtd-watch")
    app.setDesktopFileName("7dtd-watch-tray")
    # No windows are ever shown, so the default "quit when the last one closes" would
    # tear the process down the moment a menu popped and closed.
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray on this desktop session.")
        print("KDE/GNOME/XFCE all provide one; GNOME needs the AppIndicator extension.")
        return 1

    ui = Tray(app, cfg, interval)
    print(f"tray running \N{EM DASH} {len(cfg.servers)} server(s), polling every {interval}s.")
    try:
        return app.exec()
    finally:
        ui.poller.stop()
        ui.poller.wait(3000)
