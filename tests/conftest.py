"""
Shared pytest fixtures for the expedition-tracker test suite.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make the project root importable regardless of how pytest is invoked.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.db     import Database
from engine.config import load as load_cfg
from engine.legs   import LegManager
from engine.parser import EventParser

# Cutoff timestamp used across unit tests — well before any synthetic event timestamps.
CUTOFF = "2025-01-01T00:00:00Z"

# Minimal config dict that the engine modules accept (no real files needed).
# Waypoint systems below are example values used by test_legs.py to exercise
# waypoint logic; they are not tied to any real expedition.
_BASE_CFG = {
    "expedition_start_timestamp": CUTOFF,
    "expedition_start_dt": datetime.fromisoformat(CUTOFF.replace("Z", "+00:00")),
    "expedition_start_system": "Sol",
    "expedition_start_system_norm": "sol",
    "expedition_end_system": "Parrot's Head Sector EL-Y d70",
    "expedition_end_system_norm": "parrot's head sector el-y d70",
    "commander": "ExampleCMDR",
    "waypoints": [
        {"label": "Nadir",  "system": "HD 6428",            "system_norm": "hd 6428"},
        {"label": "Zenith", "system": "HIP 58832",           "system_norm": "hip 58832"},
        {"label": "West",   "system": "Sphiesi HX-L d7-0",  "system_norm": "sphiesi hx-l d7-0"},
        {"label": "East",   "system": "Ood Fleau ZJ-I d9-0", "system_norm": "ood fleau zj-i d9-0"},
        {"label": "South",  "system": "Lyed YJ-I d9-0",     "system_norm": "lyed yj-i d9-0"},
        {"label": "North",  "system": "Oevasy SG-Y d0",     "system_norm": "oevasy sg-y d0"},
    ],
    "waypoint_map": {
        "hd 6428":             "Nadir",
        "hip 58832":           "Zenith",
        "sphiesi hx-l d7-0":   "West",
        "ood fleau zj-i d9-0": "East",
        "lyed yj-i d9-0":      "South",
        "oevasy sg-y d0":      "North",
    },
}


@pytest.fixture
def cfg():
    return dict(_BASE_CFG)


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-ish database for each test."""
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def legs(db, cfg):
    lm = LegManager(db, cfg)
    lm.ensure_first_leg()
    return lm


@pytest.fixture
def parser(db, legs, cfg):
    return EventParser(db, legs, cfg)


# ── Journal line helpers ───────────────────────────────────────────────────────

def make_loadgame(
    ts: str = "2026-06-03T00:30:00Z",
    commander: str = "ExampleCMDR",
) -> str:
    return json.dumps({
        "timestamp": ts,
        "event": "LoadGame",
        "Commander": commander,
        "FID": "F0000000",
        "Horizons": True,
        "Odyssey": True,
    })


def make_jump(
    ts: str = "2026-06-03T01:00:00Z",
    star_system: str = "Alpha Centauri",
    jump_dist: float = 10.0,
    star_pos: list | None = None,
) -> str:
    return json.dumps({
        "timestamp": ts,
        "event": "FSDJump",
        "StarSystem": star_system,
        "SystemAddress": 12345,
        "StarPos": star_pos or [0.0, 0.0, 0.0],
        "JumpDist": jump_dist,
        "FuelUsed": 1.0,
        "FuelLevel": 10.0,
    })


def make_scan(
    ts: str = "2026-06-03T01:05:00Z",
    body_name: str = "Alpha Centauri A 1",
    body_id: int = 1,
    planet_class: str = "High metal content body",
    was_discovered: bool = True,
    was_mapped: bool = True,
    star_system: str = "Alpha Centauri",
) -> str:
    return json.dumps({
        "timestamp": ts,
        "event": "Scan",
        "ScanType": "Detailed",
        "BodyName": body_name,
        "BodyID": body_id,
        "StarSystem": star_system,
        "PlanetClass": planet_class,
        "TerraformState": "",
        "Landable": False,
        "DistanceFromArrivalLS": 100.0,
        "WasDiscovered": was_discovered,
        "WasMapped": was_mapped,
    })
