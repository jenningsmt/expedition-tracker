"""
Unit tests for engine.parser — event handling, cutoff filter, deduplication.
"""
from __future__ import annotations

import json

import pytest

from tests.conftest import make_jump, make_loadgame, make_scan, CUTOFF


# ── Cutoff filter ──────────────────────────────────────────────────────────────

def test_event_before_cutoff_is_ignored(parser, db):
    line = make_jump(ts="2024-12-31T23:59:59Z", star_system="Pre-Cutoff System")
    result = parser.process_line(line)
    assert not result
    # No jumps should be in the DB
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 0


def test_event_at_cutoff_is_included(parser, db):
    line = make_jump(ts=CUTOFF, star_system="Cutoff System")
    result = parser.process_line(line)
    assert result
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 1


def test_event_after_cutoff_is_included(parser, db):
    line = make_jump(ts="2026-06-04T00:00:00Z", star_system="Post-Cutoff System")
    assert parser.process_line(line)


# ── Deduplication ──────────────────────────────────────────────────────────────

def test_duplicate_event_not_double_counted(parser, db):
    line = make_jump(ts="2026-06-03T02:00:00Z", star_system="Dup System", jump_dist=5.0)
    assert parser.process_line(line)   # first time: new
    assert not parser.process_line(line)  # second time: duplicate
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 1


def test_same_event_different_whitespace_not_deduplicated_separately(parser, db):
    """The dedupe key is based on normalised JSON, not raw bytes."""
    ev = {
        "timestamp": "2026-06-03T03:00:00Z",
        "event": "FSDJump",
        "StarSystem": "WS System",
        "StarPos": [1.0, 2.0, 3.0],
        "JumpDist": 7.5,
        "FuelUsed": 0.5,
        "FuelLevel": 9.5,
    }
    line1 = json.dumps(ev)
    line2 = json.dumps(ev, indent=2)  # extra whitespace
    assert parser.process_line(line1)
    assert not parser.process_line(line2)  # same logical event → same hash


# ── FSDJump handling ───────────────────────────────────────────────────────────

def test_fsdjump_inserts_jump_and_system(parser, db):
    line = make_jump(
        ts="2026-06-03T04:00:00Z",
        star_system="Test Star",
        jump_dist=12.3,
        star_pos=[10.0, 20.0, 30.0],
    )
    parser.process_line(line)
    with db._lock:
        jump = db._exec("SELECT * FROM jumps WHERE to_system='Test Star'").fetchone()
        sys  = db._exec("SELECT * FROM systems WHERE system_name='Test Star'").fetchone()
    assert jump is not None
    assert abs(jump["jump_dist_ly"] - 12.3) < 0.01
    assert sys is not None
    assert abs(sys["star_pos_x"] - 10.0) < 0.01


def test_fsdjump_from_system_tracks_previous(parser, db):
    parser.process_line(make_jump(ts="2026-06-03T05:00:00Z", star_system="Alpha"))
    parser.process_line(make_jump(ts="2026-06-03T05:10:00Z", star_system="Beta"))
    with db._lock:
        row = db._exec("SELECT from_system FROM jumps WHERE to_system='Beta'").fetchone()
    assert row["from_system"] == "Alpha"


# ── Body scan handling ─────────────────────────────────────────────────────────

def test_scan_inserts_body(parser, db):
    parser.process_line(make_jump(ts="2026-06-03T06:00:00Z", star_system="ScanSys"))
    parser.process_line(make_scan(
        ts="2026-06-03T06:05:00Z",
        body_name="ScanSys 1",
        body_id=1,
        planet_class="Water world",
        was_discovered=False,
        star_system="ScanSys",
    ))
    with db._lock:
        body = db._exec("SELECT * FROM bodies WHERE body_name='ScanSys 1'").fetchone()
    assert body is not None
    assert body["body_class"] == "Water world"
    assert body["was_discovered"] == 0  # first discovery


def test_scan_duplicate_body_not_double_inserted(parser, db):
    parser.process_line(make_jump(ts="2026-06-03T07:00:00Z", star_system="DupScan"))
    s = make_scan(ts="2026-06-03T07:05:00Z", body_id=1, star_system="DupScan")
    parser.process_line(s)
    parser.process_line(s)  # duplicate line
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM bodies WHERE system='DupScan'").fetchone()[0]
    assert n == 1


def test_saa_scan_complete_sets_surface_mapped(parser, db):
    parser.process_line(make_jump(ts="2026-06-03T08:00:00Z", star_system="MapSys"))
    parser.process_line(make_scan(
        ts="2026-06-03T08:05:00Z",
        body_id=3,
        star_system="MapSys",
        body_name="MapSys 3",
    ))
    saa = json.dumps({
        "timestamp": "2026-06-03T08:10:00Z",
        "event": "SAAScanComplete",
        "BodyName": "MapSys 3",
        "BodyID": 3,
        "SystemName": "MapSys",
    })
    parser.process_line(saa)
    with db._lock:
        body = db._exec("SELECT surface_mapped FROM bodies WHERE body_id=3 AND system='MapSys'").fetchone()
    assert body["surface_mapped"] == 1


# ── Organic scans ──────────────────────────────────────────────────────────────

def test_organic_scan_three_stages(parser, db):
    parser.process_line(make_jump(ts="2026-06-03T09:00:00Z", star_system="BioSys"))

    for stage, scan_type in [(1, "Log"), (2, "Sample"), (3, "Analyse")]:
        line = json.dumps({
            "timestamp": f"2026-06-03T09:{stage:02d}:00Z",
            "event": "ScanOrganic",
            "ScanType": scan_type,
            "SystemName": "BioSys",
            "Body": 2,
            "Genus_Localised": "Fumerola",
            "Species_Localised": "Fumerola Aquatis",
            "Variant_Localised": "Fumerola Aquatis - Aquamarine",
        })
        parser.process_line(line)

    with db._lock:
        rows = db._exec("SELECT * FROM organic_scans WHERE system='BioSys'").fetchall()
    assert len(rows) == 3
    assert {r["scan_stage"] for r in rows} == {1, 2, 3}


# ── Carrier jump ──────────────────────────────────────────────────────────────

def test_carrier_jump_goes_to_carrier_jumps_not_jumps(parser, db):
    line = json.dumps({
        "timestamp": "2026-06-03T10:00:00Z",
        "event": "CarrierJump",
        "StarSystem": "Carrier Dest",
        "StarPos": [1.0, 2.0, 3.0],
    })
    parser.process_line(line)
    with db._lock:
        j  = db._exec("SELECT COUNT(*) FROM jumps WHERE to_system='Carrier Dest'").fetchone()[0]
        cj = db._exec("SELECT COUNT(*) FROM carrier_jumps WHERE to_system='Carrier Dest'").fetchone()[0]
    assert j == 0
    assert cj == 1


# ── Sales ──────────────────────────────────────────────────────────────────────

def test_multi_sell_exploration_data(parser, db):
    line = json.dumps({
        "timestamp": "2026-06-03T11:00:00Z",
        "event": "MultiSellExplorationData",
        "Discovered": [
            {"SystemName": "Sold Sys A", "NumBodies": 3},
            {"SystemName": "Sold Sys B", "NumBodies": 7},
        ],
        "BaseValue": 1_000_000,
        "Bonus": 100_000,
        "TotalEarnings": 1_100_000,
    })
    parser.process_line(line)
    with db._lock:
        sale  = db._exec("SELECT * FROM sales_cartographics").fetchone()
        systs = db._exec("SELECT * FROM sales_cartographics_systems").fetchall()
    assert sale["total_earnings"] == 1_100_000
    assert len(systs) == 2


def test_sell_organic_data(parser, db):
    line = json.dumps({
        "timestamp": "2026-06-03T12:00:00Z",
        "event": "SellOrganicData",
        "MarketID": 99999,
        "BioData": [
            {
                "Genus_Localised": "Fumerola",
                "Species_Localised": "Fumerola Aquatis",
                "Variant_Localised": "Fumerola Aquatis - Aquamarine",
                "Value": 3_819_975,
                "Bonus": 11_459_925,
            }
        ],
    })
    parser.process_line(line)
    with db._lock:
        sale  = db._exec("SELECT * FROM sales_organic").fetchone()
        items = db._exec("SELECT * FROM sales_organic_items").fetchall()
    assert sale is not None
    assert len(items) == 1
    assert items[0]["value"] == 3_819_975


# ── Commander filtering ────────────────────────────────────────────────────────

def test_loadgame_sets_session_commander(parser):
    parser.process_line(make_loadgame(commander="ExampleCMDR"))
    assert parser._session_commander == "examplecmdr"


def test_events_before_any_loadgame_pass_through(parser, db):
    """No LoadGame seen yet → _session_commander is None → events are not gated."""
    assert parser._session_commander is None
    assert parser.process_line(make_jump(ts="2026-06-03T01:00:00Z", star_system="Pre-LG"))
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 1


def test_correct_commander_events_processed(parser, db):
    parser.process_line(make_loadgame(commander="ExampleCMDR"))
    assert parser.process_line(make_jump(ts="2026-06-03T01:01:00Z", star_system="My Jump"))
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 1


def test_wrong_commander_events_dropped(parser, db):
    parser.process_line(make_loadgame(commander="OtherCMDR"))
    result = parser.process_line(make_jump(ts="2026-06-03T01:02:00Z", star_system="Alt Jump"))
    assert not result
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 0


def test_commander_matching_is_case_insensitive(parser, db):
    """Config commander stored as lowercase; journal may use any capitalisation."""
    parser.process_line(make_loadgame(commander="EXAMPLECMDR"))
    assert parser.process_line(make_jump(ts="2026-06-03T01:03:00Z", star_system="Case Jump"))
    with db._lock:
        n = db._exec("SELECT COUNT(*) FROM jumps").fetchone()[0]
    assert n == 1


def test_commander_switch_mid_session(parser, db):
    """
    First session: correct commander → jumps recorded.
    Second session (different commander): jumps dropped.
    Third session: correct commander again → jumps recorded.
    """
    parser.process_line(make_loadgame(ts="2026-06-03T01:00:00Z", commander="ExampleCMDR"))
    parser.process_line(make_jump(ts="2026-06-03T01:05:00Z", star_system="Jump A"))

    parser.process_line(make_loadgame(ts="2026-06-03T02:00:00Z", commander="AltCMDR"))
    parser.process_line(make_jump(ts="2026-06-03T02:05:00Z", star_system="Jump B"))

    parser.process_line(make_loadgame(ts="2026-06-03T03:00:00Z", commander="ExampleCMDR"))
    parser.process_line(make_jump(ts="2026-06-03T03:05:00Z", star_system="Jump C"))

    with db._lock:
        systems = [
            r[0] for r in db._exec("SELECT to_system FROM jumps ORDER BY ts").fetchall()
        ]
    assert systems == ["Jump A", "Jump C"]
    assert "Jump B" not in systems


# ── Change 1: Terraformable vs body-class separation ─────────────────────────

def test_terraformable_count_distinct(parser, db):
    """terraformable_count in stats_for_leg is distinct bodies with non-empty TerraformState."""
    parser.process_line(make_jump(ts="2026-06-03T13:00:00Z", star_system="TerSys"))

    # One terraformable body
    terra_scan = json.dumps({
        "timestamp": "2026-06-03T13:05:00Z",
        "event": "Scan", "ScanType": "Detailed",
        "BodyName": "TerSys 1", "BodyID": 1, "StarSystem": "TerSys",
        "PlanetClass": "High metal content body",
        "TerraformState": "Terraformable",
        "Landable": False, "DistanceFromArrivalLS": 100.0,
        "WasDiscovered": True, "WasMapped": False,
    })
    # One non-terraformable body
    plain_scan = json.dumps({
        "timestamp": "2026-06-03T13:06:00Z",
        "event": "Scan", "ScanType": "Detailed",
        "BodyName": "TerSys 2", "BodyID": 2, "StarSystem": "TerSys",
        "PlanetClass": "Icy body",
        "TerraformState": "",
        "Landable": False, "DistanceFromArrivalLS": 200.0,
        "WasDiscovered": True, "WasMapped": False,
    })
    parser.process_line(terra_scan)
    parser.process_line(plain_scan)

    s = db.stats_for_leg(None)
    assert s["terraformable_count"] == 1, (
        f"Expected 1 terraformable body, got {s['terraformable_count']}"
    )
    assert s["bodies_scanned"] == 2


def test_body_class_breakdown_separates_stars_and_planets(parser, db):
    """get_body_class_breakdown returns dict including both planet classes and star types."""
    parser.process_line(make_jump(ts="2026-06-03T14:00:00Z", star_system="ClsBrkSys"))

    planet_scan = json.dumps({
        "timestamp": "2026-06-03T14:05:00Z",
        "event": "Scan", "ScanType": "Detailed",
        "BodyName": "ClsBrkSys 1", "BodyID": 1, "StarSystem": "ClsBrkSys",
        "PlanetClass": "Water world",
        "TerraformState": "", "Landable": False,
        "DistanceFromArrivalLS": 100.0,
        "WasDiscovered": False, "WasMapped": False,
    })
    star_scan = json.dumps({
        "timestamp": "2026-06-03T14:06:00Z",
        "event": "Scan", "ScanType": "AutoScan",
        "BodyName": "ClsBrkSys A", "BodyID": 0, "StarSystem": "ClsBrkSys",
        "StarType": "G",
        "Landable": False, "DistanceFromArrivalLS": 0.0,
        "WasDiscovered": False, "WasMapped": False,
    })
    parser.process_line(planet_scan)
    parser.process_line(star_scan)

    bkd = db.get_body_class_breakdown(None)
    assert "Water world" in bkd
    assert "G" in bkd
    # Stars and planets have different names — no conflation
    assert bkd["Water world"] >= 1
    assert bkd["G"] >= 1


# ── Change 2: DISTINCT vs RAW counts ─────────────────────────────────────────

def test_distinct_vs_raw_body_counts(parser, db):
    """
    The same body scanned twice (same system + body_id, different events) counts
    as 1 in stats_for_leg (DISTINCT) but 2 in raw_scan_stats (raw event count).
    """
    parser.process_line(make_jump(ts="2026-06-03T15:00:00Z", star_system="RevSys"))

    scan_args = dict(
        body_name="RevSys 1", body_id=10,
        planet_class="High metal content body",
        was_discovered=False, star_system="RevSys"
    )
    # First scan
    parser.process_line(make_scan(ts="2026-06-03T15:05:00Z", **scan_args))
    # Second scan — different timestamp -> different hash -> new event in events_raw
    # but same (system, body_id) -> INSERT OR IGNORE -> no new row in bodies
    parser.process_line(make_scan(ts="2026-06-03T15:10:00Z", **scan_args))

    s = db.stats_for_leg(None)
    r = db.raw_scan_stats()

    # DISTINCT: body appears once in bodies table
    # Find specifically this body
    with db._lock:
        body_count = db._exec(
            "SELECT COUNT(*) FROM bodies WHERE system='RevSys' AND body_id=10"
        ).fetchone()[0]
    assert body_count == 1

    # RAW: 2 scan events processed (two different timestamps)
    with db._lock:
        raw_count = db._exec(
            "SELECT COUNT(*) FROM events_raw WHERE event='Scan'"
            " AND raw_json LIKE '%RevSys 1%'"
        ).fetchone()[0]
    assert raw_count == 2

    # The raw >= distinct invariant holds
    assert s["bodies_scanned"] <= r["bodies_scanned_raw"]


def test_stats_for_leg_key_names(db, legs):
    """Smoke-test that stats_for_leg returns the expected set of keys."""
    s = db.stats_for_leg(None)
    required = {
        "commander_jumps", "distance_ly", "carrier_jumps", "systems_visited",
        "bodies_scanned", "first_discovered", "bodies_mapped",
        "terraformable_count", "organic_variants", "new_codex",
        "carto_sales_count", "carto_earnings",
        "exobio_sales_count", "exobio_earnings",
    }
    missing = required - s.keys()
    assert not missing, f"Missing keys: {missing}"
    # Old conflated keys must be gone
    removed = {"jump_count", "total_dist_ly", "body_scans", "surface_mapped",
               "distinct_organics", "elw_count", "ww_count", "aw_count", "hmc_count"}
    still_present = removed & s.keys()
    assert not still_present, f"Old keys still present: {still_present}"


def test_raw_scan_stats_key_names(db):
    """Smoke-test that raw_scan_stats returns the expected set of keys."""
    r = db.raw_scan_stats()
    required = {
        "bodies_scanned_raw", "first_discovered_raw", "bodies_mapped_raw",
        "elw_raw", "ww_raw", "aw_raw", "hmc_terraformable_raw",
    }
    missing = required - r.keys()
    assert not missing, f"Missing raw keys: {missing}"
