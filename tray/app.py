"""
System-tray application (pystray + Pillow).

The icon is generated programmatically as a compass rose so there's no
external asset dependency.  The tray runs in pystray's own thread; the
journal watcher runs in its daemon thread.  All state mutation goes through
the engine objects which handle their own locking.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item, Menu

from engine.db       import Database
from engine.legs     import LegManager
from engine.parser   import EventParser
from engine.watcher  import JournalWatcher
from engine.exporter import export_leg, export_master_rollup

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# ── Icon generation ────────────────────────────────────────────────────────────

def _make_icon(size: int = 64) -> Image.Image:
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    c    = size // 2
    r    = size // 2 - 4

    # Compass circle
    draw.ellipse([c - r, c - r, c + r, c + r], outline="#4FC3F7", width=2)

    # Cardinal points (N/S/E/W triangles)
    tri_h = r - 6
    tri_w = 7
    # North – blue
    draw.polygon([(c, c - tri_h), (c - tri_w, c - 4), (c + tri_w, c - 4)], fill="#1565C0")
    # South – red
    draw.polygon([(c, c + tri_h), (c - tri_w, c + 4), (c + tri_w, c + 4)], fill="#C62828")
    # East
    draw.polygon([(c + tri_h, c), (c + 4, c - tri_w), (c + 4, c + tri_w)], fill="#BDBDBD")
    # West
    draw.polygon([(c - tri_h, c), (c - 4, c - tri_w), (c - 4, c + tri_w)], fill="#BDBDBD")

    # Centre dot
    draw.ellipse([c - 3, c - 3, c + 3, c + 3], fill="#FFF176")

    return img


# ── Tray application ───────────────────────────────────────────────────────────

class TrackerTray:
    def __init__(self, cfg: dict, db: Database) -> None:
        self._cfg  = cfg
        self._db   = db
        self._legs = LegManager(db, cfg)
        self._parser = EventParser(
            db, self._legs, cfg,
            on_leg_close=self._on_leg_close,
        )
        self._watcher = JournalWatcher(
            journal_dir=Path(cfg["journal_dir"]),
            db=db,
            parser=self._parser,
        )
        self._icon: pystray.Icon | None = None
        self._lock = threading.Lock()

    # ── pystray entry ──────────────────────────────────────────────────────────

    def run(self) -> None:
        self._legs.ensure_first_leg()
        self._parser.restore_state()

        # Backfill in a background thread so the tray icon appears quickly.
        bf_thread = threading.Thread(
            target=self._backfill_then_start, name="backfill", daemon=True
        )
        bf_thread.start()

        icon_img = _make_icon()
        self._icon = pystray.Icon(
            name="ED Tracker",
            icon=icon_img,
            title="ED Expedition Tracker",
            menu=Menu(
                Item("Status",                self._show_status),
                Item("Close leg & export now", self._manual_close),
                Menu.SEPARATOR,
                Item("Configure expedition…", self._open_config),
                Menu.SEPARATOR,
                Item("Stop & exit",           self._stop_and_exit),
            ),
        )
        log.info("Starting tray icon.")
        self._icon.run()

    # ── Background startup ─────────────────────────────────────────────────────

    def _backfill_then_start(self) -> None:
        try:
            self._watcher.backfill()
            self._watcher.start()
            self._update_tooltip()
        except Exception:
            log.exception("Error during backfill/start.")

    # ── Tray callbacks ─────────────────────────────────────────────────────────

    def _show_status(self, icon, item) -> None:
        status = self._legs.status_text()
        log.info("Status: %s", status)
        # pystray doesn't have a built-in popup; update tooltip and log.
        self._update_tooltip(status)
        # Show a Windows balloon notification if possible.
        try:
            icon.notify(status, title="ED Tracker")
        except Exception:
            pass

    def _manual_close(self, icon, item) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            closed_id = self._legs.manual_close(ts)
        if closed_id is not None:
            self._export_leg(closed_id)
            msg = f"Leg {closed_id} closed and exported."
            log.info(msg)
            try:
                icon.notify(msg, title="ED Tracker")
            except Exception:
                pass
        self._update_tooltip()

    def _open_config(self, icon, item) -> None:
        config_script = Path(__file__).parent.parent / "ui" / "config_window.py"
        subprocess.Popen([sys.executable, str(config_script)])

    def _stop_and_exit(self, icon, item) -> None:
        log.info("Stop & exit requested.")
        self._shutdown()
        icon.stop()

    # ── Leg-close callback (called from parser thread) ─────────────────────────

    def _on_leg_close(self, leg_id: int) -> None:
        self._export_leg(leg_id)
        self._update_tooltip()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _export_leg(self, leg_id: int) -> None:
        try:
            out = Path(self._cfg["output_dir"])
            export_leg(leg_id, self._db, out)
            export_master_rollup(self._db, out)
        except Exception:
            log.exception("Export failed for leg %d.", leg_id)

    def _update_tooltip(self, text: str | None = None) -> None:
        if text is None:
            text = self._legs.status_text()
        if self._icon:
            try:
                self._icon.title = f"ED Tracker — {text}"
            except Exception:
                pass

    def _shutdown(self) -> None:
        log.info("Flushing and shutting down …")
        self._watcher.stop()
        self._db.close()
        log.info("Clean shutdown complete.")
