"""
Unit tests for engine.rarity.evaluate().

Each rule is tested with a crafted scan dict, a geo_count, and the default
rarity config (all rules on, flag_only_first_discoveries=True).  A non-match
control verifies no false positives from an ordinary rocky body.
"""
from __future__ import annotations

import pytest

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.rarity import evaluate, RuleMatch

# ── Base config (all rules on, default thresholds) ─────────────────────────────
_CFG = {
    "flag_only_first_discoveries": True,
    "rule_ringed_habitable":  True,
    "rule_habitable_moon":    True,
    "rule_life_bearing_gg":   True,
    "rule_very_small":        True,
    "rule_tidal_moon":        True,
    "rule_jumponium":         True,
    "rule_exotic_star":       True,
    "rule_high_gravity":      True,
    "rule_fast_rotator":      True,
    "rule_ggg_candidate":     True,
    "rule_nsp_alert":         True,
    "very_small_radius_m":    300_000,
    "high_gravity_g":         3.0,
    "fast_rotator_hours":     3.0,
    "geo_signal_threshold":   3,
    "jumponium_materials":    ["Carbon", "Vanadium", "Germanium", "Arsenic",
                               "Niobium", "Yttrium", "Polonium"],
    "exotic_star_types":      ["N", "H",
                               "DA", "DAB", "DAO", "DAZ", "DAV",
                               "DB", "DBZ", "DBV", "DO", "DOV", "DQ", "DC", "DCV", "DX",
                               "W", "WN", "WNC", "WC", "WO", "AeBe"],
    "ggg_planet_classes":     ["Sudarsky class I gas giant",
                               "Sudarsky class II gas giant",
                               "Sudarsky class III gas giant",
                               "Sudarsky class IV gas giant",
                               "Sudarsky class V gas giant",
                               "Helium-rich gas giant",
                               "Helium gas giant"],
    "nsp_mundane_signal_types": [],
}

# ── Helper: build a minimal first-discovery scan ───────────────────────────────

def _scan(**kw) -> dict:
    base = {
        "event":         "Scan",
        "BodyName":      "Test A 1",
        "BodyID":        1,
        "StarSystem":    "Test System",
        "SystemAddress": 99999,
        "WasDiscovered": False,  # first discovery
        "WasMapped":     False,
        "DistanceFromArrivalLS": 100.0,
    }
    base.update(kw)
    return base


def _tags(matches: list[RuleMatch]) -> set[str]:
    return {m.tag for m in matches}


# ── Non-match control ──────────────────────────────────────────────────────────

def test_ordinary_rocky_body_no_match():
    scan = _scan(PlanetClass="Rocky body", Landable=True,
                 Radius=500_000, SurfaceGravity=2.0 * 9.80665,
                 RotationPeriod=86400, TidalLock=False,
                 Volcanism="", Materials=[])
    assert evaluate(scan, 0, _CFG) == []


# ── First-discovery gate ───────────────────────────────────────────────────────

def test_already_discovered_body_skipped():
    scan = _scan(PlanetClass="Earthlike body", WasDiscovered=True,
                 Rings=[{"Name": "ring"}])
    assert evaluate(scan, 0, _CFG) == []


def test_flag_only_false_evaluates_known_body():
    cfg = dict(_CFG, flag_only_first_discoveries=False)
    scan = _scan(PlanetClass="Earthlike body", WasDiscovered=True,
                 Rings=[{"Name": "ring"}])
    assert "ringed_habitable" in _tags(evaluate(scan, 0, cfg))


# ── Rule 1: Ringed habitable ───────────────────────────────────────────────────

@pytest.mark.parametrize("planet_class", [
    "Earthlike body", "Water world", "Ammonia world"
])
def test_ringed_habitable(planet_class):
    scan = _scan(PlanetClass=planet_class, Rings=[{"Name": "A Ring"}])
    assert "ringed_habitable" in _tags(evaluate(scan, 0, _CFG))


def test_ringed_habitable_no_rings():
    scan = _scan(PlanetClass="Earthlike body")
    assert "ringed_habitable" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule 2: Habitable moon ─────────────────────────────────────────────────────

@pytest.mark.parametrize("planet_class", [
    "Earthlike body", "Water world", "Ammonia world"
])
def test_habitable_moon(planet_class):
    scan = _scan(PlanetClass=planet_class, Parents=[{"Planet": 5}, {"Star": 0}])
    assert "habitable_moon" in _tags(evaluate(scan, 0, _CFG))


def test_habitable_moon_orbiting_star_not_flagged():
    scan = _scan(PlanetClass="Water world", Parents=[{"Star": 0}])
    assert "habitable_moon" not in _tags(evaluate(scan, 0, _CFG))


def test_habitable_moon_no_parents_not_flagged():
    scan = _scan(PlanetClass="Water world")
    assert "habitable_moon" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule 3: Life-bearing gas giant ────────────────────────────────────────────

@pytest.mark.parametrize("planet_class,expect_ringed", [
    ("Gas giant with water based life",   False),
    ("Gas giant with ammonia based life", False),
    ("Gas giant with water based life",   True),
])
def test_life_bearing_gg(planet_class, expect_ringed):
    rings = [{"Name": "A Ring"}] if expect_ringed else []
    scan  = _scan(PlanetClass=planet_class, Rings=rings)
    ms    = evaluate(scan, 0, _CFG)
    assert "life_bearing_gg" in _tags(ms)
    m = next(x for x in ms if x.tag == "life_bearing_gg")
    assert m.attrs["rings"] == expect_ringed


# ── Rule 4: Very small body ────────────────────────────────────────────────────

def test_very_small_below_threshold():
    scan = _scan(PlanetClass="Rocky body", Radius=212_000)  # ~212 km
    assert "very_small" in _tags(evaluate(scan, 0, _CFG))


def test_very_small_above_threshold():
    scan = _scan(PlanetClass="Rocky body", Radius=301_000)
    assert "very_small" not in _tags(evaluate(scan, 0, _CFG))


def test_very_small_exactly_at_threshold_not_flagged():
    scan = _scan(PlanetClass="Rocky body", Radius=300_000)
    assert "very_small" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule 5: Tidally-heated moon ────────────────────────────────────────────────

def test_tidal_moon_volcanic_moon():
    scan = _scan(
        PlanetClass="Rocky body",
        Parents=[{"Planet": 2}],
        Volcanism="minor silicate vapour geysers",
    )
    assert "tidal_moon" in _tags(evaluate(scan, 0, _CFG))


def test_tidal_moon_no_volcanism():
    scan = _scan(PlanetClass="Rocky body", Parents=[{"Planet": 2}], Volcanism="")
    assert "tidal_moon" not in _tags(evaluate(scan, 0, _CFG))


def test_tidal_moon_orbiting_star_not_flagged():
    scan = _scan(
        PlanetClass="Rocky body",
        Parents=[{"Star": 0}],
        Volcanism="minor silicate vapour geysers",
    )
    assert "tidal_moon" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule 6: Jumponium synthesis target ────────────────────────────────────────

def _jumponium_scan(materials: list[str], geo_count: int = 3,
                    volcanism: str = "major silicate vapour geysers") -> dict:
    mats = [{"Name": m, "Name_Localised": m, "Percent": 1.0} for m in materials]
    return _scan(PlanetClass="Rocky body", Landable=True,
                 Volcanism=volcanism, Materials=mats)


def test_jumponium_full_set_5_materials():
    scan = _jumponium_scan(["Arsenic", "Carbon", "Germanium", "Niobium", "Yttrium"])
    ms   = evaluate(scan, 3, _CFG)
    assert "jumponium" in _tags(ms)
    m = next(x for x in ms if x.tag == "jumponium")
    assert m.attrs["jumponium_count"] == 5
    assert "arsenic" in m.details


def test_jumponium_single_material():
    scan = _jumponium_scan(["Carbon"])
    assert "jumponium" in _tags(evaluate(scan, 3, _CFG))


def test_jumponium_zero_materials_not_flagged():
    scan = _jumponium_scan(["Iron", "Nickel"])  # non-jumponium
    assert "jumponium" not in _tags(evaluate(scan, 3, _CFG))


def test_jumponium_geo_count_below_threshold():
    scan = _jumponium_scan(["Carbon", "Germanium"])
    assert "jumponium" not in _tags(evaluate(scan, 2, _CFG))


def test_jumponium_no_volcanism_not_flagged():
    scan = _jumponium_scan(["Carbon"], volcanism="")
    assert "jumponium" not in _tags(evaluate(scan, 3, _CFG))


# ── Rule 7: Exotic stars ──────────────────────────────────────────────────────

@pytest.mark.parametrize("star_type", ["N", "H", "DA", "DB", "DC", "W", "WN", "WC", "AeBe"])
def test_exotic_star(star_type):
    scan = _scan(StarType=star_type)
    assert "exotic_star" in _tags(evaluate(scan, 0, _CFG))


def test_main_sequence_star_not_flagged():
    for st in ["G", "K", "M", "F", "A", "B"]:
        scan = _scan(StarType=st)
        assert "exotic_star" not in _tags(evaluate(scan, 0, _CFG)), f"Star type {st} should not be exotic"


# ── Rule 8: High-gravity landable ─────────────────────────────────────────────

def test_high_gravity_above_threshold():
    sg_si = 4.0 * 9.80665  # 4 g
    scan  = _scan(PlanetClass="High metal content body", Landable=True,
                  SurfaceGravity=sg_si)
    assert "high_gravity" in _tags(evaluate(scan, 0, _CFG))


def test_high_gravity_below_threshold():
    sg_si = 2.5 * 9.80665
    scan  = _scan(PlanetClass="High metal content body", Landable=True,
                  SurfaceGravity=sg_si)
    assert "high_gravity" not in _tags(evaluate(scan, 0, _CFG))


def test_high_gravity_not_landable_not_flagged():
    sg_si = 5.0 * 9.80665
    scan  = _scan(PlanetClass="Gas giant", Landable=False, SurfaceGravity=sg_si)
    assert "high_gravity" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule 9: Fast rotator ──────────────────────────────────────────────────────

def test_fast_rotator_below_threshold():
    scan = _scan(PlanetClass="Rocky body", RotationPeriod=7200.0, TidalLock=False)  # 2h
    assert "fast_rotator" in _tags(evaluate(scan, 0, _CFG))


def test_fast_rotator_retrograde_also_caught():
    scan = _scan(PlanetClass="Rocky body", RotationPeriod=-3599.0, TidalLock=False)
    assert "fast_rotator" in _tags(evaluate(scan, 0, _CFG))


def test_fast_rotator_above_threshold():
    scan = _scan(PlanetClass="Rocky body", RotationPeriod=14400.0, TidalLock=False)  # 4h
    assert "fast_rotator" not in _tags(evaluate(scan, 0, _CFG))


def test_fast_rotator_tidal_lock_excluded():
    scan = _scan(PlanetClass="Rocky body", RotationPeriod=3600.0, TidalLock=True)
    assert "fast_rotator" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule 10: GGG candidate ────────────────────────────────────────────────────

def test_ggg_candidate_class_i_with_atmosphere():
    scan = _scan(
        PlanetClass="Sudarsky class I gas giant",
        AtmosphereComposition=[{"Name": "Hydrogen", "Percent": 80.0}],
    )
    assert "ggg_candidate" in _tags(evaluate(scan, 0, _CFG))


def test_ggg_candidate_no_atmosphere_not_flagged():
    scan = _scan(PlanetClass="Sudarsky class I gas giant", AtmosphereComposition=[])
    assert "ggg_candidate" not in _tags(evaluate(scan, 0, _CFG))


def test_ggg_candidate_non_gg_class_not_flagged():
    scan = _scan(
        PlanetClass="Water world",
        AtmosphereComposition=[{"Name": "Water", "Percent": 100.0}],
    )
    assert "ggg_candidate" not in _tags(evaluate(scan, 0, _CFG))


# ── Rule toggle: disabling a rule produces no match ───────────────────────────

def test_rule_disable_ringed_habitable():
    cfg  = dict(_CFG, rule_ringed_habitable=False)
    scan = _scan(PlanetClass="Earthlike body", Rings=[{"Name": "ring"}])
    assert "ringed_habitable" not in _tags(evaluate(scan, 0, cfg))


def test_rule_disable_exotic_star():
    cfg  = dict(_CFG, rule_exotic_star=False)
    scan = _scan(StarType="N")
    assert "exotic_star" not in _tags(evaluate(scan, 0, cfg))


# ── Multiple rules can fire together ──────────────────────────────────────────

def test_multiple_rules_on_same_body():
    # A very small, fast-rotating, volcanic moon with jumponium
    mats = [{"Name": m, "Name_Localised": m, "Percent": 1.0}
            for m in ["Carbon", "Germanium"]]
    scan = _scan(
        PlanetClass="Rocky body",
        Landable=True,
        Radius=200_000,
        RotationPeriod=3000.0,
        TidalLock=False,
        Parents=[{"Planet": 5}],
        Volcanism="major silicate vapour geysers",
        Materials=mats,
    )
    tags = _tags(evaluate(scan, 3, _CFG))
    assert "very_small"   in tags
    assert "fast_rotator" in tags
    assert "tidal_moon"   in tags
    assert "jumponium"    in tags
