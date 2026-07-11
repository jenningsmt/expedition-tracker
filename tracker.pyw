"""
Elite Dangerous Expedition Tracker

Entry point.  Run via:
  pythonw.exe tracker.pyw            # system-tray mode (no console)
  python     tracker.pyw --cli       # headless / console mode
  python     tracker.pyw --validate  # invariants + baseline comparison
  python     tracker.pyw --snapshot  # regenerate validation_baseline.json

The .pyw extension means Windows will use pythonw.exe by default when
double-clicked (no console window).
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

BASELINE_PATH = ROOT / "validation_baseline.json"

# ── Logging setup ──────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool = False, to_console: bool = False) -> None:
    level   = logging.DEBUG if verbose else logging.INFO
    fmt     = "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            ROOT / "tracker.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
    ]
    if to_console:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


# ── Shared ingestion helper ────────────────────────────────────────────────────

def _ingest(cfg: dict, db) -> None:
    """Backfill all journals into db, then run the rarity detector."""
    from engine.legs   import LegManager
    from engine.parser import EventParser
    from engine.watcher import JournalWatcher
    from engine.rarity import run_rarity_pass

    legs    = LegManager(db, cfg)
    legs.ensure_first_leg()
    parser  = EventParser(db, legs, cfg)
    watcher = JournalWatcher(Path(cfg["journal_dir"]), db, parser)
    watcher.backfill()
    run_rarity_pass(db, cfg.get("rarity", {}))


def _collect_stats(db, cfg: dict | None = None) -> dict[str, Any]:
    """
    Merge DISTINCT and RAW metrics into a single flat dict for snapshot / validate.

    Keys ending in _distinct come from stats_for_leg (deduplicated bodies table).
    Keys ending in _raw come from raw_scan_stats (raw event counts from events_raw).
    Rare-find keys are computed fresh each time (no DB writes).
    """
    from engine.rarity import count_rule_matches_all_bodies

    s = db.stats_for_leg(None)
    r = db.raw_scan_stats()
    rarity_cfg = (cfg or {}).get("rarity", {})

    # All-bodies jumponium count (flag_only_first_discoveries overridden to False)
    all_matches = count_rule_matches_all_bodies(db, rarity_cfg)

    return {
        # Journey totals
        "commander_jumps":            s["commander_jumps"],
        "distance_ly":                s["distance_ly"],
        "carrier_jumps":              s["carrier_jumps"],
        "systems_visited":            s["systems_visited"],
        # Body counts -- both views
        "bodies_scanned_distinct":    s["bodies_scanned"],
        "bodies_scanned_raw":         r["bodies_scanned_raw"],
        "first_discovered_distinct":  s["first_discovered"],
        "first_discovered_raw":       r["first_discovered_raw"],
        "bodies_mapped_distinct":     s["bodies_mapped"],
        "bodies_mapped_raw":          r["bodies_mapped_raw"],
        # High-value bodies (raw scan events; see db.raw_scan_stats())
        "elw_raw":                    r["elw_raw"],
        "ww_raw":                     r["ww_raw"],
        "aw_raw":                     r["aw_raw"],
        "hmc_terraformable_raw":      r["hmc_terraformable_raw"],
        # Terraformable (distinct bodies, any class)
        "terraformable_distinct":     s["terraformable_count"],
        # Biology, codex, sales
        "organic_variants_distinct":  s["organic_variants"],
        "new_codex":                  s["new_codex"],
        "carto_sales":                s["carto_sales_count"],
        "carto_earnings":             s["carto_earnings"],
        "exobio_sales":               s["exobio_sales_count"],
        "exobio_earnings":            s["exobio_earnings"],
        # Rare finds (first-discoveries only, as stored in rare_finds table)
        "rare_finds_total":           db.count_all_rare_finds(),
        # Jumponium across ALL scanned bodies (flag_only_first_discoveries overridden)
        "rare_finds_jumponium_all":   all_matches.get("jumponium", 0),
        "rare_finds_nsp":             all_matches.get("nsp_alert", 0),
    }


# ── Mode runners ───────────────────────────────────────────────────────────────

def _run_tray(cfg: dict) -> None:
    from engine.db import Database
    from tray.app  import TrackerTray
    db   = Database(cfg["db_path"])
    tray = TrackerTray(cfg, db)
    tray.run()


def _run_cli(cfg: dict) -> None:
    import threading
    from engine.db       import Database
    from engine.legs     import LegManager
    from engine.parser   import EventParser
    from engine.watcher  import JournalWatcher
    from engine.exporter import export_leg, export_master_rollup

    log = logging.getLogger("cli")
    db  = Database(cfg["db_path"])

    legs   = LegManager(db, cfg)
    legs.ensure_first_leg()

    def on_leg_close(leg_id: int) -> None:
        from engine.rarity import run_rarity_pass
        log.info("Leg %d closed -- running rarity pass ...", leg_id)
        run_rarity_pass(db, cfg.get("rarity", {}))
        log.info("Leg %d -- exporting ...", leg_id)
        export_leg(leg_id, db, cfg["output_dir"])
        export_master_rollup(db, cfg["output_dir"])

    parser  = EventParser(db, legs, cfg, on_leg_close=on_leg_close)
    parser.restore_state()
    watcher = JournalWatcher(Path(cfg["journal_dir"]), db, parser)

    log.info("Starting backfill from %s ...", cfg["journal_dir"])
    n = watcher.backfill()
    log.info("Backfill done (%d new events).  Status: %s", n, legs.status_text())

    watcher.start()
    log.info("Live-tailing journals.  Press Ctrl-C to stop.")

    stop_event = threading.Event()
    def _sigint(sig, frame):
        log.info("Interrupt received -- shutting down ...")
        stop_event.set()
    signal.signal(signal.SIGINT, _sigint)

    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        watcher.stop()
        db.close()
        log.info("CLI session ended.")


def _run_snapshot(cfg: dict) -> None:
    """Ingest all journals, compute metrics, write validation_baseline.json."""
    from engine.db import Database
    log = logging.getLogger("snapshot")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        db = Database(tmp_path)
        log.info("Snapshot: ingesting journals from %s ...", cfg["journal_dir"])
        _ingest(cfg, db)
        stats = _collect_stats(db, cfg)
        db.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    baseline = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cutoff":       cfg["expedition_start_timestamp"],
        "metrics":      stats,
    }
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"Snapshot written to {BASELINE_PATH}")
    print()
    print("Metrics captured:")
    for k, v in sorted(stats.items()):
        fmt = f"{v:,}" if isinstance(v, (int, float)) else str(v)
        print(f"  {k:<32} {fmt}")


def _run_validate(cfg: dict) -> None:
    """Assert invariants, then verify all metrics are >= the stored baseline."""
    from engine.db import Database
    log = logging.getLogger("validate")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    exit_code = 0
    try:
        db = Database(tmp_path)
        log.info("Validate: ingesting journals from %s ...", cfg["journal_dir"])
        _ingest(cfg, db)
        stats = _collect_stats(db, cfg)

        # ── Invariant checks ───────────────────────────────────────────────────
        print("=== Invariant Checks ===")
        inv_failures = _check_invariants(db, cfg, stats)
        if inv_failures:
            for desc, detail in inv_failures:
                print(f"  [FAIL] {desc}: {detail}")
            exit_code = 1
        else:
            print(f"  All {len(_INVARIANTS)} invariants passed.")

        db.close()

        # ── Baseline comparison ────────────────────────────────────────────────
        print()
        print("=== Baseline Comparison ===")
        if not BASELINE_PATH.exists():
            print(f"  No baseline found at {BASELINE_PATH}.")
            print("  Run  python tracker.pyw --snapshot  to create one.")
            print("  (Only invariant results above count for this run.)")
        else:
            bl_failures = _check_baseline(stats, BASELINE_PATH)
            if bl_failures:
                for line in bl_failures:
                    print(f"  {line}")
                exit_code = 1
            else:
                baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
                n = len(baseline["metrics"])
                print(f"  All {n} metrics meet or exceed baseline "
                      f"(generated {baseline['generated_at']}).")

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    print()
    if exit_code == 0:
        print("PASSED.")
    else:
        print("FAILED.")
        sys.exit(exit_code)


# ── Invariants ─────────────────────────────────────────────────────────────────

def _check_geo_signal_max(db) -> tuple[bool, str]:
    with db._lock:
        row = db._exec(
            "SELECT MAX(count) FROM body_signals "
            "WHERE LOWER(signal_type) LIKE '%geological%'"
        ).fetchone()
    max_val = row[0] if row and row[0] is not None else 0
    return max_val <= 3, f"max geo signals = {max_val} (expected <= 3)"


# Each entry: (description, lambda(db, cfg, stats) -> (ok: bool, detail: str))
_INVARIANTS: list[tuple[str, Any]] = [
    (
        "No pre-cutoff events in DB",
        lambda db, cfg, s: (
            (True, "events_raw is empty") if db.min_event_ts() is None
            else (db.min_event_ts() >= cfg["expedition_start_timestamp"],
                  f"min ts = {db.min_event_ts()} vs cutoff {cfg['expedition_start_timestamp']}")
        ),
    ),
    (
        "systems_visited <= commander_jumps",
        lambda db, cfg, s: (
            s["systems_visited"] <= s["commander_jumps"],
            f"{s['systems_visited']} vs {s['commander_jumps']}",
        ),
    ),
    (
        "bodies_scanned: distinct <= raw",
        lambda db, cfg, s: (
            s["bodies_scanned_distinct"] <= s["bodies_scanned_raw"],
            f"{s['bodies_scanned_distinct']} distinct, {s['bodies_scanned_raw']} raw",
        ),
    ),
    (
        "first_discovered: distinct <= raw",
        lambda db, cfg, s: (
            s["first_discovered_distinct"] <= s["first_discovered_raw"],
            f"{s['first_discovered_distinct']} distinct, {s['first_discovered_raw']} raw",
        ),
    ),
    (
        "bodies_mapped: distinct <= raw",
        lambda db, cfg, s: (
            s["bodies_mapped_distinct"] <= s["bodies_mapped_raw"],
            f"{s['bodies_mapped_distinct']} distinct, {s['bodies_mapped_raw']} raw",
        ),
    ),
    (
        "first_discovered_distinct <= bodies_scanned_distinct",
        lambda db, cfg, s: (
            s["first_discovered_distinct"] <= s["bodies_scanned_distinct"],
            f"{s['first_discovered_distinct']} <= {s['bodies_scanned_distinct']}",
        ),
    ),
    (
        "bodies_mapped_distinct <= bodies_scanned_distinct",
        lambda db, cfg, s: (
            s["bodies_mapped_distinct"] <= s["bodies_scanned_distinct"],
            f"{s['bodies_mapped_distinct']} <= {s['bodies_scanned_distinct']}",
        ),
    ),
    (
        "carto_earnings >= 0",
        lambda db, cfg, s: (s["carto_earnings"] >= 0, str(s["carto_earnings"])),
    ),
    (
        "exobio_earnings >= 0",
        lambda db, cfg, s: (s["exobio_earnings"] >= 0, str(s["exobio_earnings"])),
    ),
    (
        "commander_jumps >= 0 and distance_ly >= 0",
        lambda db, cfg, s: (
            s["commander_jumps"] >= 0 and s["distance_ly"] >= 0,
            f"jumps={s['commander_jumps']}, dist={s['distance_ly']}",
        ),
    ),
    (
        "Rare finds: geo signal counts are only 2 or 3",
        lambda db, cfg, s: _check_geo_signal_max(db),
    ),
]


def _check_invariants(db, cfg: dict, stats: dict) -> list[tuple[str, str]]:
    """Run all invariants. Returns list of (description, detail) for failures."""
    failures = []
    for desc, fn in _INVARIANTS:
        ok, detail = fn(db, cfg, stats)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {desc}")
        if not ok:
            failures.append((desc, detail))
    return failures


def _check_baseline(current: dict, path: Path) -> list[str]:
    """
    Compare current metrics against the stored baseline.
    Any metric that has DECREASED is a regression -> failure.
    Returns list of failure lines (empty = all good).
    """
    baseline = json.loads(path.read_text(encoding="utf-8"))
    bl_metrics: dict = baseline.get("metrics", {})
    failures = []
    for key, bl_val in sorted(bl_metrics.items()):
        cur_val = current.get(key)
        if cur_val is None:
            failures.append(f"[FAIL] {key}: metric missing from current run")
            continue
        if isinstance(bl_val, (int, float)) and isinstance(cur_val, (int, float)):
            if cur_val < bl_val:
                drop = bl_val - cur_val
                failures.append(
                    f"[FAIL] {key}: DROPPED by {drop:,} "
                    f"(baseline={bl_val:,}, current={cur_val:,})"
                )
            else:
                delta = cur_val - bl_val
                tag   = f"+{delta:,}" if delta else "no change"
                print(f"  [OK]   {key:<32} baseline={bl_val:,}  current={cur_val:,}  ({tag})")
    return failures


# ── Main ───────────────────────────────────────────────────────────────────────

def _run_reset() -> None:
    """Interactively wipe all expedition data."""
    from engine.reset import reset_expedition_data

    print()
    print("This will permanently delete:")
    print("  • tracker.db       — all jump, scan, and organic data")
    print("  • output/          — all exported xlsx and csv files")
    print("  • validation_baseline.json  — if present")
    print()
    print("config.toml will NOT be changed.")
    print()
    answer = input("Type YES to confirm reset: ").strip()
    if answer != "YES":
        print()
        print("Reset cancelled.")
        return

    try:
        removed = reset_expedition_data(ROOT)
    except OSError as exc:
        print()
        print(f"Reset failed: {exc}")
        print("If the tracker is running, stop it first (tray → Stop & exit).")
        sys.exit(1)

    print()
    if removed["db"]:
        print("  Deleted tracker.db")
    if removed["exports"]:
        print(f"  Deleted {removed['exports']} file(s) from output/")
    if removed["baseline"]:
        print("  Deleted validation_baseline.json")
    if not any(removed.values()):
        print("  Nothing to delete — already clean.")
    print()
    print("Reset complete. Configure a new expedition in config.toml,")
    print("then launch tracker.pyw to start fresh.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Elite Dangerous Expedition Tracker"
    )
    ap.add_argument("--cli",      action="store_true", help="Run headless (console/log output)")
    ap.add_argument("--validate", action="store_true", help="Assert invariants + check baseline")
    ap.add_argument("--snapshot", action="store_true", help="Regenerate validation_baseline.json")
    ap.add_argument("--reset",    action="store_true", help="Delete all expedition data (keeps config.toml)")
    ap.add_argument("--config",   default=str(ROOT / "config.toml"), help="Path to config.toml")
    ap.add_argument("--verbose",  action="store_true", help="Debug-level logging")
    args = ap.parse_args()

    to_console = args.cli or args.validate or args.snapshot or args.reset
    _setup_logging(verbose=args.verbose, to_console=to_console)

    if args.reset:
        _run_reset()
        return

    from engine.config import load as load_cfg
    cfg = load_cfg(Path(args.config))

    if args.snapshot:
        _run_snapshot(cfg)
    elif args.validate:
        _run_validate(cfg)
    elif args.cli:
        _run_cli(cfg)
    else:
        _run_tray(cfg)


if __name__ == "__main__":
    main()
