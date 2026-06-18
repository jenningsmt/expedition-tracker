"""
Unit tests for engine.legs — leg state machine logic.
"""
from __future__ import annotations

import json
import pytest

from tests.conftest import make_jump, CUTOFF


# ── First-leg creation ─────────────────────────────────────────────────────────

def test_ensure_first_leg_creates_leg(db, cfg):
    from engine.legs import LegManager
    lm = LegManager(db, cfg)
    lm.ensure_first_leg()
    leg = db.get_active_leg()
    assert leg is not None
    assert leg["ordinal"] == 1
    assert leg["status"] == "open"


def test_ensure_first_leg_idempotent(db, cfg):
    from engine.legs import LegManager
    lm = LegManager(db, cfg)
    lm.ensure_first_leg()
    lm.ensure_first_leg()  # second call must not open a second leg
    assert db.get_leg_count() == 1


# ── Waypoint auto-close (departure-triggered) ─────────────────────────────────

def test_arriving_at_waypoint_does_not_close_leg(legs, db):
    """Arrival at a waypoint no longer triggers leg close — departure does."""
    result = legs.on_arrive("HD 6428", "2026-06-05T10:00:00Z")
    assert result["action"] == "none"
    assert db.get_leg_count() == 1        # still just the one open leg
    assert db.get_active_leg() is not None


def test_departing_from_waypoint_closes_leg_and_opens_next(legs, db):
    legs.on_arrive("HD 6428", "2026-06-05T10:00:00Z")   # arrive (no-op)
    result = legs.on_depart("HD 6428", "2026-06-05T12:00:00Z")  # depart → close

    assert result["action"] == "waypoint_close"
    assert result["waypoint_label"] == "Nadir"

    closed = db.get_leg(result["leg_id_closed"])
    assert closed["status"] == "closed"
    assert closed["name"] == "Nadir"
    assert closed["end_system"] == "HD 6428"

    active = db.get_active_leg()
    assert active is not None
    assert active["ordinal"] == 2
    assert active["status"] == "open"


def test_case_insensitive_waypoint_match(legs):
    result = legs.on_depart("hd 6428", "2026-06-05T11:00:00Z")
    assert result["action"] == "waypoint_close"
    assert result["waypoint_label"] == "Nadir"


def test_revisiting_waypoint_does_not_re_close(legs, db):
    legs.on_depart("HD 6428", "2026-06-05T10:00:00Z")   # first departure
    result2 = legs.on_depart("HD 6428", "2026-06-10T10:00:00Z")  # revisit depart
    assert result2["action"] == "none"
    # Still only 2 legs (original + one opened after Nadir)
    assert db.get_leg_count() == 2


def test_non_waypoint_system_does_nothing(legs, db):
    result = legs.on_depart("Some Random System", "2026-06-04T00:00:00Z")
    assert result["action"] == "none"
    assert db.get_leg_count() == 1  # only the first leg


def test_arriving_at_non_waypoint_does_nothing(legs, db):
    result = legs.on_arrive("Some Random System", "2026-06-04T00:00:00Z")
    assert result["action"] == "none"
    assert db.get_leg_count() == 1


# ── Expedition end (still arrival-triggered) ───────────────────────────────────

def test_arriving_at_end_system_marks_expedition_complete(legs, db):
    result = legs.on_arrive("Parrot's Head Sector EL-Y d70", "2026-06-20T00:00:00Z")
    assert result["action"] == "expedition_complete"
    assert legs.is_complete

    active = db.get_active_leg()
    assert active is None  # no open leg


def test_case_insensitive_end_system_match(legs):
    result = legs.on_arrive("PARROT'S HEAD SECTOR EL-Y D70", "2026-06-20T00:00:00Z")
    assert result["action"] == "expedition_complete"


def test_no_action_after_expedition_complete(legs):
    legs.on_arrive("Parrot's Head Sector EL-Y d70", "2026-06-20T00:00:00Z")
    result = legs.on_depart("HD 6428", "2026-06-20T01:00:00Z")
    assert result["action"] == "none"


# ── Manual close ──────────────────────────────────────────────────────────────

def test_manual_close_closes_and_opens_next(legs, db, parser):
    # Make a jump so get_last_jump works
    parser.process_line(make_jump(ts="2026-06-03T01:00:00Z", star_system="SystemA"))
    old_id = legs.manual_close(ts="2026-06-03T02:00:00Z", label="Midpoint")
    assert old_id is not None

    closed = db.get_leg(old_id)
    assert closed["status"] == "closed"
    assert closed["name"] == "Midpoint"

    active = db.get_active_leg()
    assert active is not None
    assert active["ordinal"] == 2


def test_manual_close_no_active_leg_returns_none(db, cfg):
    from engine.legs import LegManager
    lm = LegManager(db, cfg)
    # Don't call ensure_first_leg
    result = lm.manual_close()
    assert result is None


# ── Multi-waypoint sequence ────────────────────────────────────────────────────

def test_visiting_all_six_waypoints_creates_seven_legs(legs, db):
    waypoints = [
        ("HD 6428",             "2026-06-05T00:00:00Z"),
        ("HIP 58832",           "2026-06-07T00:00:00Z"),
        ("Sphiesi HX-L d7-0",   "2026-06-09T00:00:00Z"),
        ("Ood Fleau ZJ-I d9-0",  "2026-06-11T00:00:00Z"),
        ("Lyed YJ-I d9-0",      "2026-06-13T00:00:00Z"),
        ("Oevasy SG-Y d0",      "2026-06-15T00:00:00Z"),
    ]
    for sys_name, ts in waypoints:
        result = legs.on_depart(sys_name, ts)
        assert result["action"] == "waypoint_close", (
            f"Expected waypoint_close on depart from {sys_name}, got {result['action']}"
        )

    # 6 closed + 1 open = 7 legs total
    assert db.get_leg_count() == 7
    active = db.get_active_leg()
    assert active["ordinal"] == 7


# ── Sales are attributed to the correct leg ───────────────────────────────────

def test_sales_before_departure_land_in_old_leg(legs, db, parser):
    """
    Carto/exobio sales made in the waypoint system belong to the closing leg,
    not the next one.  Verify by processing a sale event between waypoint
    arrival and departure.
    """
    import json as _json

    # Jump into the waypoint system
    parser.process_line(make_jump(ts="2026-06-05T10:00:00Z", star_system="HD 6428"))

    # Sell carto data while parked at the waypoint
    sale_line = _json.dumps({
        "timestamp": "2026-06-05T11:00:00Z",
        "event": "MultiSellExplorationData",
        "Discovered": [{"SystemName": "Some Deep Space System", "NumBodies": 5}],
        "BaseValue": 1_000_000,
        "Bonus": 50_000,
        "TotalEarnings": 1_050_000,
    })
    parser.process_line(sale_line)

    # Depart the waypoint — leg 1 closes here
    parser.process_line(make_jump(ts="2026-06-05T12:00:00Z", star_system="Next System"))

    with db._lock:
        leg1_id = db._exec(
            "SELECT leg_id FROM legs WHERE name='Nadir'"
        ).fetchone()[0]
        sale = db._exec(
            "SELECT * FROM sales_cartographics WHERE leg_id=?", (leg1_id,)
        ).fetchone()

    assert sale is not None, "Carto sale should be in Leg 1 (closed at waypoint departure)"
    assert sale["total_earnings"] == 1_050_000
