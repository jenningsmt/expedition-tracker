"""
Journal file watcher.

Two responsibilities:
  1. Backfill — on startup, scan all Journal.*.log files in journal_dir in
     chronological order and process any lines not yet recorded (tracks byte
     offsets in the DB so a restart resumes where it left off).
  2. Live tail — after backfill, use watchdog to detect file modifications and
     new journal files (game spawns a new file each launch).

Threading model
---------------
The watchdog handler runs in watchdog's internal thread.  It posts file-path
events onto a queue.  The processing thread drains the queue.  This keeps SQLite
access on one thread (the processor) even though notifications arrive from another.

A threading.Event (_stop) signals both threads to exit cleanly.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
from watchdog.observers import Observer

from .db import Database
from .parser import EventParser

log = logging.getLogger(__name__)

_JOURNAL_RE = re.compile(r"^Journal\.\d{4}-\d{2}-\d{2}T\d{6}\.\d{2}\.log$", re.IGNORECASE)

# Sentinel pushed onto the queue to stop the processor thread.
_STOP = object()


class _WatchdogHandler(FileSystemEventHandler):
    def __init__(self, journal_dir: Path, work_queue: "queue.Queue[object]") -> None:
        self._dir = journal_dir
        self._q   = work_queue

    def _is_journal(self, path: str) -> bool:
        return _JOURNAL_RE.match(Path(path).name) is not None

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory and self._is_journal(event.src_path):
            self._q.put(("process", Path(event.src_path)))

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory and self._is_journal(event.src_path):
            log.info("New journal file detected: %s", Path(event.src_path).name)
            self._q.put(("process", Path(event.src_path)))


class JournalWatcher:
    def __init__(
        self,
        journal_dir: Path,
        db: Database,
        parser: EventParser,
    ) -> None:
        self._dir    = journal_dir
        self._db     = db
        self._parser = parser
        self._q: "queue.Queue[object]" = queue.Queue()
        self._stop   = threading.Event()
        self._observer: Observer | None = None
        self._proc_thread: threading.Thread | None = None

    # ── Backfill ───────────────────────────────────────────────────────────────

    def backfill(self) -> int:
        """
        Process all journal files in chronological order.
        Returns the total number of new events processed.
        """
        files = sorted(
            [f for f in self._dir.glob("Journal.*.log") if _JOURNAL_RE.match(f.name)],
            key=lambda f: f.name,
        )
        log.info("Backfilling %d journal files from %s …", len(files), self._dir)
        total = 0
        for f in files:
            total += self._process_file(f)
        log.info("Backfill complete: %d new events ingested.", total)
        return total

    def _process_file(self, path: Path) -> int:
        """Read new content from path (from stored byte offset). Returns new event count."""
        offset = self._db.get_file_offset(str(path))
        new_events = 0
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                buf = b""
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    lines = buf.split(b"\n")
                    buf = lines[-1]  # possibly incomplete trailing line
                    for raw in lines[:-1]:
                        line = raw.decode("utf-8", errors="replace")
                        if self._parser.process_line(line):
                            new_events += 1
                # Don't count the bytes in the incomplete buffer
                new_offset = fh.tell() - len(buf)
        except OSError as exc:
            log.warning("Could not read %s: %s", path.name, exc)
            return 0

        if new_offset > offset:
            self._db.set_file_offset(
                str(path), new_offset,
                ts=str(path.stat().st_mtime),
            )
        return new_events

    # ── Live tailing ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog observer and the processor thread."""
        self._proc_thread = threading.Thread(
            target=self._run_processor, name="journal-processor", daemon=True
        )
        self._proc_thread.start()

        self._observer = Observer()
        handler = _WatchdogHandler(self._dir, self._q)
        self._observer.schedule(handler, str(self._dir), recursive=False)
        self._observer.start()
        log.info("Live journal watch started on %s", self._dir)

    def stop(self) -> None:
        """Signal both threads to stop and wait for clean shutdown."""
        self._stop.set()
        self._q.put(_STOP)
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._proc_thread:
            self._proc_thread.join(timeout=10)
        log.info("Watcher stopped.")

    def _run_processor(self) -> None:
        """Drain the work queue until the stop sentinel arrives."""
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=1)
            except queue.Empty:
                continue
            if item is _STOP:
                break
            kind, path = item
            if kind == "process":
                self._process_file(path)
