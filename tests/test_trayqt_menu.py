#!/usr/bin/env python3
"""Qt-layer test: rebuilding the tray menu must not pile up submenus.

The tray menu is rebuilt every time the server lines change. `QMenu.clear()` deletes
only actions whose parent is the menu itself; a submenu's action is parented to the
submenu, so `addMenu()` leaves both behind. Five days at a 15s interval left 57,381
QMenu and 516,143 QAction objects alive and 2.0 GB of RSS. `_purge()` is the fix and
this is the test that would have caught it.

Needs PyQt6; runs headless. Run: python3 tests/test_trayqt_menu.py
"""

import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="7dtd-watch-test-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6 import sip
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import QApplication, QMenu
except ImportError:  # pragma: no cover - box without Qt, same as the tray itself
    print("PyQt6 missing, skipping Qt menu tests")
    sys.exit(0)

from sevendtd_watch._trayqt import _purge  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


app = QApplication([])


def build(menu):
    """The shape _rebuild_menu makes: two server submenus, recent, settings, nested."""
    for name in ("Navezgane", "Pregen10k"):
        sub = menu.addMenu(name)
        sub.addAction("day 14")
        sub.addAction("Copy addr")
    menu.addMenu("Recent events").addAction("nothing yet")
    settings = menu.addMenu("Settings")
    settings.addMenu("Alert me about").addAction("join")
    menu.addAction("Quit")


# --------------------------------------------------- the bug, so it stays documented

leaky = QMenu()
for _ in range(5):
    leaky.clear()
    build(leaky)
check("QMenu.clear() alone strands submenus", len(leaky.findChildren(QMenu)) > 5, True)

# ------------------------------------------------------------------------- the fix

menu = QMenu()
kept = []
for i in range(50):
    _purge(menu)
    build(menu)
    kept.append(len(menu.findChildren(QMenu)))

check("one rebuild makes 5 submenus", kept[0], 5)
check("50 rebuilds make no more", set(kept), {5})

# The submenus are not merely unparented, they are destroyed once the loop turns.
menu_before = QMenu()
build(menu_before)
doomed = menu_before.findChildren(QMenu)
_purge(menu_before)
# deleteLater() lands when the event loop next turns; app.exec() does this for the tray.
QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
check("purged submenus are really freed", all(sip.isdeleted(m) for m in doomed), True)

# A purged menu is empty, not merely childless.
check("purged menu has no actions", menu_before.actions(), [])

if failures:
    print("TRAYQT MENU TESTS FAILED")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("trayqt menu tests ok")
