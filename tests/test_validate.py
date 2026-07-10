"""
Full-expedition validation tests.

Two layers:
  1. Invariants -- always computed fresh; never need editing as the expedition grows.
  2. Baseline comparison -- metrics must be >= validation_baseline.json.
     Skipped if no baseline exists (run `python tracker.pyw --snapshot` to create one).

Both layers are also exercised by `python tracker.pyw --validate`.

Run:
  pytest tests/test_validate.py -v          # requires journal files
  pytest tests/test_validate.py -v -k inv   # only the invariants
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

BASELINE_PATH = ROOT / "validation_baseline.json"

# ── Skip gate ─────────────────────────────────────────────────────────────────

def _journal_dir() -> Path:
    from engine.config import load as load_cfg
    cfg_path = ROOT / "config.toml"
    if cfg_path.exists():
        try:
            return Path(load_cfg(cfg_path)["journal_dir"])
        except Exception:
            pass
    return Path(os.path.expandvars(
        r"%USERPROFILE%\Saved Games\Frontier Developments\Elite Dangerous"
    ))


_JOURNAL_DIR = _journal_dir()
_JOURNALS_EXIST = _JOURNAL_DIR.is_dir() and any(_JOURNAL_DIR.glob("Journal.*.log"))

pytestmark = pytest.mark.skipif(
    not _JOURNALS_EXIST,
    reason=f"Journal files not found at {_JOURNAL_DIR}",
)


# ── Shared fixture: full-expedition DB (ingested once per module) ──────────────

@pytest.fixture(scope="module")
def expedition(tmp_path_factory):
    """
    Ingest all journals into a temp DB; return (db, stats, cfg).
    'stats' is the merged flat dict from tracker._collect_stats().
    _ingest() also runs the rarity pass, populating the rare_finds table.
    """
    from engine.config  import load as load_cfg
    from engine.db      import Database
    from tracker        import _ingest, _collect_stats

    cfg     = load_cfg(ROOT / "config.toml")
    tmp_db  = tmp_path_factory.mktemp("validate") / "expedition.db"
    db      = Database(tmp_db)
    _ingest(cfg, db)
    stats   = _collect_stats(db, cfg)
    yield db, stats, cfg
    db.close()


# ── Invariant tests ────────────────────────────────────────────────────────────

class TestInvariants:
    """Invariants that must always hold regardless of expedition progress."""

    def test_no_pre_cutoff_events(self, expedition):
        db, stats, cfg = expedition
        min_ts = db.min_event_ts()
        cutoff = cfg["expedition_start_timestamp"]
        if min_ts is not None:
            assert min_ts >= cutoff, (
                f"Pre-cutoff event found: {min_ts} < {cutoff}"
            )

    def test_systems_visited_le_commander_jumps(self, expedition):
        _, stats, _ = expedition
        assert stats["systems_visited"] <= stats["commander_jumps"], (
            f"systems_visited ({stats['systems_visited']}) > "
            f"commander_jumps ({stats['commander_jumps']})"
        )

    @pytest.mark.parametrize("kind", [
        "bodies_scanned", "first_discovered", "bodies_mapped"
    ])
    def test_distinct_le_raw(self, expedition, kind):
        _, stats, _ = expedition
        d = stats[f"{kind}_distinct"]
        r = stats[f"{kind}_raw"]
        assert d <= r, f"{kind}: distinct ({d}) > raw ({r})"

    def test_first_discovered_le_bodies_scanned(self, expedition):
        _, stats, _ = expedition
        assert stats["first_discovered_distinct"] <= stats["bodies_scanned_distinct"]

    def test_bodies_mapped_le_bodies_scanned(self, expedition):
        _, stats, _ = expedition
        assert stats["bodies_mapped_distinct"] <= stats["bodies_scanned_distinct"]

    def test_credit_totals_nonnegative(self, expedition):
        _, stats, _ = expedition
        assert stats["carto_earnings"]  >= 0
        assert stats["exobio_earnings"] >= 0

    def test_journey_metrics_nonnegative(self, expedition):
        _, stats, _ = expedition
        assert stats["commander_jumps"] >= 0
        assert stats["distance_ly"]     >= 0
        assert stats["carrier_jumps"]   >= 0
        assert stats["systems_visited"] >= 0

    # ── Rare-finds invariants ──────────────────────────────────────────────────

    def test_geo_signal_counts_at_most_3(self, expedition):
        """Geological signal counts are never greater than 3 (ED game constraint)."""
        db, _, _ = expedition
        with db._lock:
            row = db._exec(
                "SELECT MAX(count) FROM body_signals "
                "WHERE LOWER(signal_type) LIKE '%geological%'"
            ).fetchone()
        max_val = row[0] if row and row[0] is not None else 0
        assert max_val <= 3, f"Unexpected geo signal count > 3: {max_val}"


# ── Baseline comparison tests ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def baseline():
    if not BASELINE_PATH.exists():
        pytest.skip(
            f"No baseline at {BASELINE_PATH}. "
            "Run `python tracker.pyw --snapshot` to create one."
        )
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


class TestBaseline:
    """Each metric in the baseline must be <= the current value (no regressions)."""

    @pytest.mark.parametrize("key", [
        "commander_jumps", "distance_ly", "carrier_jumps", "systems_visited",
        "bodies_scanned_distinct", "bodies_scanned_raw",
        "first_discovered_distinct", "first_discovered_raw",
        "bodies_mapped_distinct", "bodies_mapped_raw",
        "organic_variants_distinct", "new_codex",
        "carto_sales", "carto_earnings", "exobio_sales", "exobio_earnings",
        "terraformable_distinct", "elw_raw", "ww_raw", "aw_raw",
        "hmc_terraformable_raw",
        # Rare-finds metrics (monotonic >=)
        "rare_finds_total",
        "rare_finds_jumponium_all",
        "rare_finds_nsp",
    ])
    def test_metric_not_below_baseline(self, expedition, baseline, key):
        _, stats, _ = expedition
        bl_val  = baseline["metrics"].get(key)
        cur_val = stats.get(key)

        if bl_val is None:
            pytest.skip(f"{key} not in baseline (run --snapshot to update)")
        if cur_val is None:
            pytest.fail(f"{key} missing from current stats")

        assert cur_val >= bl_val, (
            f"{key} DROPPED: baseline={bl_val:,}, current={cur_val:,} "
            f"(regression by {bl_val - cur_val:,})"
        )
