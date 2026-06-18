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
        """Geological signal counts in the data are only ever 2 or 3."""
        db, _, _ = expedition
        with db._lock:
            row = db._exec(
                "SELECT MAX(count) FROM body_signals "
                "WHERE LOWER(signal_type) LIKE '%geological%'"
            ).fetchone()
        max_val = row[0] if row and row[0] is not None else 0
        assert max_val <= 3, f"Unexpected geo signal count > 3: {max_val}"

    def test_jumponium_rule_all_bodies_ge_66(self, expedition):
        """Jumponium rule (geo≥3 + volcanic + ≥1 jumponium) matches >= 66 bodies
        across ALL scanned bodies (flag_only_first_discoveries=False)."""
        _, stats, _ = expedition
        count = stats.get("rare_finds_jumponium_all", 0)
        assert count >= 66, (
            f"Expected >= 66 jumponium matches across all bodies, got {count}"
        )

    def test_habitable_moon_includes_aidow(self, expedition):
        """Aidow NM-L c21-0 1 a (Water world) must appear as a habitable_moon match."""
        db, _, _ = expedition
        with db._lock:
            row = db._exec(
                "SELECT * FROM rare_finds "
                "WHERE system='Aidow NM-L c21-0' "
                "AND body_name='Aidow NM-L c21-0 1 a' "
                "AND matched_rules LIKE '%habitable_moon%'"
            ).fetchone()
        assert row is not None, (
            "Aidow NM-L c21-0 1 a not found in rare_finds with habitable_moon rule. "
            "Check that the rarity pass ran (call _ingest() which now calls run_rarity_pass())."
        )

    def test_blaed_jumponium_5_materials(self, expedition):
        """Blaed AV-V d3-3 5 a is a first-discovery jumponium target with 5 materials."""
        import json as _json
        db, _, _ = expedition
        with db._lock:
            row = db._exec(
                "SELECT trigger_details FROM rare_finds "
                "WHERE body_name='Blaed AV-V d3-3 5 a' "
                "AND matched_rules LIKE '%jumponium%'"
            ).fetchone()
        assert row is not None, "Blaed AV-V d3-3 5 a not found as jumponium target in rare_finds"
        details = _json.loads(row[0])
        detail_str = details.get("jumponium", "")
        assert "5" in detail_str, f"Expected 5 jumponium materials, got: {detail_str}"
        for mat in ["arsenic", "carbon", "germanium", "niobium", "yttrium"]:
            assert mat in detail_str.lower(), (
                f"Expected {mat} in jumponium details, got: {detail_str}"
            )

    def test_nsp_count_is_zero(self, expedition):
        """
        ⚠️  UNVALIDATED RULE — zero NSP/spaceborne events in current journals.
        Assert no false positives against the existing data.
        TODO: validate this rule on the next live NSP encounter.
        """
        _, stats, _ = expedition
        count = stats.get("rare_finds_nsp", 0)
        assert count == 0, (
            f"NSP alert rule produced {count} matches on current journals — "
            "expected 0 (no NSP events in dataset). Check nsp_mundane_signal_types config."
        )

    def test_ringed_life_bearing_gg_counts(self, expedition):
        """4 water-based-life + 4 ammonia-based-life gas giants have rings (all scanned bodies)."""
        import json as _json
        db, _, _ = expedition
        # Count from raw scan events — includes bodies regardless of WasDiscovered flag.
        with db._lock:
            rows = db._exec(
                "SELECT raw_json FROM events_raw WHERE event='Scan'"
            ).fetchall()
        ringed_water = 0
        ringed_ammonia = 0
        for (rj,) in rows:
            try:
                ev = _json.loads(rj)
            except Exception:
                continue
            pc = ev.get("PlanetClass", "")
            if ev.get("Rings") and pc in (
                "Gas giant with water based life",
                "Gas giant with ammonia based life",
            ):
                if "water" in pc.lower():
                    ringed_water += 1
                else:
                    ringed_ammonia += 1
        assert ringed_water  >= 4, f"Expected >= 4 ringed water-life GGs, got {ringed_water}"
        assert ringed_ammonia >= 4, f"Expected >= 4 ringed ammonia-life GGs, got {ringed_ammonia}"

    def test_anchor_126_jumps_to_phloinn(self, expedition):
        """
        From the expedition cutoff (2026-06-03T00:39:41Z) to the first arrival
        at Phloinn HC-J d10-1 (2026-06-09) must be exactly 126 FSDJumps.
        This is a fixed historical anchor that verifies the cutoff filter
        correctly excludes the ~21-jump pre-expedition shakedown loop.
        """
        db, _, _ = expedition
        n = db.count_jumps_to_first_arrival("Phloinn HC-J d10-1")
        if n == -1:
            pytest.skip("Phloinn HC-J d10-1 not yet reached in current journals.")
        assert n == 126, (
            f"Expected 126 jumps to Phloinn, got {n}. "
            "Check that the expedition_start_timestamp cutoff filter is working."
        )


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
