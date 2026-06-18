"""
Rare / notable first-discovery detector.

Public API
----------
evaluate(scan, geo_count, rarity_cfg) -> list[RuleMatch]
    Pure function — no DB access.  Evaluates one Scan event dict against the
    ruleset and returns every rule that matched.

run_rarity_pass(db, rarity_cfg) -> int
    Iterates every Scan event in events_raw, calls evaluate(), and upserts
    results into the rare_finds table.  Also checks for NSP system alerts via
    CodexEntry and FSSSignalDiscovered events.  Idempotent — safe to re-run.
    Returns total rare-find count after the pass.

count_rule_matches_all_bodies(db, rarity_cfg) -> dict[str, int]
    Like run_rarity_pass but counts only (no DB writes), and overrides
    flag_only_first_discoveries=False so all scanned bodies are evaluated.
    Used by the validate/snapshot path to capture the jumponium-all-bodies
    metric.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)

_G_TO_SI     = 9.80665   # 1 g in m/s²
_HOURS_TO_S  = 3600.0

_HABITABLE   = {"Earthlike body", "Water world", "Ammonia world"}
_LIFE_GG     = {"Gas giant with water based life", "Gas giant with ammonia based life"}

_DEFAULT_JUMPONIUM = [
    "Carbon", "Vanadium", "Germanium", "Arsenic",
    "Niobium", "Yttrium", "Polonium",
]
_DEFAULT_EXOTIC = [
    "N", "H",
    "DA", "DAB", "DAO", "DAZ", "DAV",
    "DB", "DBZ", "DBV", "DO", "DOV", "DQ", "DC", "DCV", "DX",
    "W", "WN", "WNC", "WC", "WO", "AeBe",
]
_DEFAULT_GGG_CLASSES = [
    "Sudarsky class I gas giant",
    "Sudarsky class II gas giant",
    "Sudarsky class III gas giant",
    "Sudarsky class IV gas giant",
    "Sudarsky class V gas giant",
    "Helium-rich gas giant",
    "Helium gas giant",
]

# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class RuleMatch:
    tag:     str
    details: str
    attrs:   dict = field(default_factory=dict)


# ── Scan-level helpers ─────────────────────────────────────────────────────────

def _has_rings(scan: dict) -> bool:
    return bool(scan.get("Rings"))


def _is_planet_parent(scan: dict) -> bool:
    """True when the first Parent entry is a Planet (body orbits a planet, not a star)."""
    parents = scan.get("Parents", [])
    if not parents:
        return False
    return "Planet" in parents[0]


def _materials(scan: dict) -> dict[str, float]:
    """Return {name: percent} from the Materials list (landable bodies only)."""
    out: dict[str, float] = {}
    for m in scan.get("Materials", []):
        name = m.get("Name_Localised") or m.get("Name", "")
        if name:
            out[name] = m.get("Percent", 0.0)
    return out


# ── Core evaluator ─────────────────────────────────────────────────────────────

def evaluate(scan: dict, geo_count: int, rarity_cfg: dict) -> list[RuleMatch]:
    """
    Evaluate one Scan event dict against every enabled rule.

    scan        : parsed journal Scan event dict
    geo_count   : geological signal count from FSSBodySignals (0 when absent)
    rarity_cfg  : the [rarity] section of config.toml

    Returns a (possibly empty) list of RuleMatch objects.
    """
    cfg = rarity_cfg
    matches: list[RuleMatch] = []

    # First-discovery gate
    if cfg.get("flag_only_first_discoveries", True) and scan.get("WasDiscovered", True):
        return []

    planet_class = scan.get("PlanetClass", "")
    star_type    = scan.get("StarType", "")
    volcanism    = (scan.get("Volcanism") or "").strip()
    landable     = bool(scan.get("Landable", False))
    rings        = _has_rings(scan)
    is_moon      = _is_planet_parent(scan)

    # ── Rule 1: Ringed ELW / WW / AW ──────────────────────────────────────────
    if cfg.get("rule_ringed_habitable", True) and planet_class in _HABITABLE and rings:
        matches.append(RuleMatch(
            tag="ringed_habitable",
            details=f"Ringed {planet_class}",
            attrs={"planet_class": planet_class, "rings": True},
        ))

    # ── Rule 2: ELW / WW / AW as a moon ───────────────────────────────────────
    if cfg.get("rule_habitable_moon", True) and planet_class in _HABITABLE and is_moon:
        matches.append(RuleMatch(
            tag="habitable_moon",
            details=f"{planet_class} orbiting a planet (moon)",
            attrs={"planet_class": planet_class},
        ))

    # ── Rule 3: Life-bearing gas giant ────────────────────────────────────────
    if cfg.get("rule_life_bearing_gg", True) and planet_class in _LIFE_GG:
        detail = planet_class + (" (ringed)" if rings else "")
        matches.append(RuleMatch(
            tag="life_bearing_gg",
            details=detail,
            attrs={"planet_class": planet_class, "rings": rings},
        ))

    # ── Rule 4: Very small body ────────────────────────────────────────────────
    if cfg.get("rule_very_small", True):
        radius_m  = scan.get("Radius")
        threshold = cfg.get("very_small_radius_m", 300_000)
        if radius_m is not None and radius_m < threshold:
            matches.append(RuleMatch(
                tag="very_small",
                details=f"Radius {radius_m/1000:.1f} km (threshold {threshold/1000:.0f} km)",
                attrs={"radius_m": radius_m, "threshold_m": threshold},
            ))

    # ── Rule 5: Tidally-heated moon ────────────────────────────────────────────
    if cfg.get("rule_tidal_moon", True) and is_moon and volcanism:
        matches.append(RuleMatch(
            tag="tidal_moon",
            details=f"Volcanic moon: {volcanism}",
            attrs={"volcanism": volcanism},
        ))

    # ── Rule 6: Jumponium synthesis target ─────────────────────────────────────
    if cfg.get("rule_jumponium", True):
        geo_thr = cfg.get("geo_signal_threshold", 3)
        jset    = {m.lower() for m in cfg.get("jumponium_materials", _DEFAULT_JUMPONIUM)}
        if geo_count >= geo_thr and volcanism:
            mats  = _materials(scan)
            found = sorted(name for name in mats if name.lower() in jset)
            if found:
                matches.append(RuleMatch(
                    tag="jumponium",
                    details=f"jumponium: {len(found)} — {','.join(f.lower() for f in found)}",
                    attrs={
                        "geo_count":       geo_count,
                        "volcanism":       volcanism,
                        "jumponium_found": found,
                        "jumponium_count": len(found),
                    },
                ))

    # ── Rule 7: Exotic star ────────────────────────────────────────────────────
    if cfg.get("rule_exotic_star", True) and star_type:
        exotic = cfg.get("exotic_star_types", _DEFAULT_EXOTIC)
        if star_type in exotic:
            matches.append(RuleMatch(
                tag="exotic_star",
                details=f"Exotic star: {star_type}",
                attrs={"star_type": star_type},
            ))

    # ── Rule 8: High-gravity landable ─────────────────────────────────────────
    if cfg.get("rule_high_gravity", True) and landable:
        sg_si     = scan.get("SurfaceGravity")
        thr_g     = cfg.get("high_gravity_g", 3.0)
        thr_si    = thr_g * _G_TO_SI
        if sg_si is not None and sg_si > thr_si:
            matches.append(RuleMatch(
                tag="high_gravity",
                details=f"{sg_si/_G_TO_SI:.2f} g (threshold {thr_g:.1f} g)",
                attrs={"surface_gravity_g": sg_si / _G_TO_SI, "threshold_g": thr_g},
            ))

    # ── Rule 9: Fast rotator (non-tidal-locked) ───────────────────────────────
    if cfg.get("rule_fast_rotator", True) and not scan.get("TidalLock", False):
        rot_s  = scan.get("RotationPeriod")
        thr_h  = cfg.get("fast_rotator_hours", 3.0)
        thr_s  = thr_h * _HOURS_TO_S
        if rot_s is not None and abs(rot_s) < thr_s:
            rot_h = abs(rot_s) / _HOURS_TO_S
            matches.append(RuleMatch(
                tag="fast_rotator",
                details=f"Rotation {rot_h:.2f} h (threshold {thr_h:.1f} h)",
                attrs={"rotation_hours": rot_h, "threshold_hours": thr_h},
            ))

    # ── Rule 10: GGG candidate ─────────────────────────────────────────────────
    if cfg.get("rule_ggg_candidate", True):
        ggg_cls = cfg.get("ggg_planet_classes", _DEFAULT_GGG_CLASSES)
        if planet_class in ggg_cls and scan.get("AtmosphereComposition"):
            matches.append(RuleMatch(
                tag="ggg_candidate",
                details=f"GGG candidate ({planet_class}) — VERIFY VISUALLY",
                attrs={"planet_class": planet_class},
            ))

    # Rule 11 (NSP) is system-level; handled by _check_nsp() in run_rarity_pass.

    return matches


# ── DB-backed pass ─────────────────────────────────────────────────────────────

def run_rarity_pass(db: "Database", rarity_cfg: dict) -> int:
    """
    Evaluate all Scan events in events_raw and upsert matches into rare_finds.
    Also runs the NSP system-level check.
    Idempotent — safe to call multiple times.
    Returns total rare-find row count after the pass.
    """
    scan_rows = db.get_scan_events_raw()
    processed = 0
    for (raw_json,) in scan_rows:
        try:
            scan = json.loads(raw_json)
        except Exception:
            continue
        if scan.get("BodyType") == "Belt Cluster":
            continue
        system    = scan.get("StarSystem", "")
        body_id   = scan.get("BodyID")
        body_name = scan.get("BodyName", "")
        if not system or body_id is None:
            continue

        geo_count = db.get_geo_signal_count(system, body_name)
        matches   = evaluate(scan, geo_count, rarity_cfg)
        if not matches:
            continue

        leg_id = db.get_body_leg_id(system, body_id)
        db.upsert_rare_find(
            system         = system,
            system_address = scan.get("SystemAddress"),
            body_id        = body_id,
            body_name      = body_name,
            body_class     = scan.get("PlanetClass") or scan.get("StarType"),
            leg_id         = leg_id,
            matches        = matches,
            was_discovered = int(bool(scan.get("WasDiscovered", True))),
            was_mapped     = int(bool(scan.get("WasMapped", True))),
            was_footfalled = int(bool(scan.get("WasFootFalled", True))),
            distance_ls    = scan.get("DistanceFromArrivalLS"),
        )
        processed += 1

    if rarity_cfg.get("rule_nsp_alert", True):
        _check_nsp(db, rarity_cfg)

    total = db.count_all_rare_finds()
    log.info("Rarity pass: %d bodies matched, %d total rare finds in DB.", processed, total)
    return total


def count_rule_matches_all_bodies(db: "Database", rarity_cfg: dict) -> dict[str, int]:
    """
    Count rule matches across ALL scanned bodies (flag_only_first_discoveries=False).
    No DB writes — used only by the snapshot/validate stats path.
    Returns {rule_tag: count}.
    """
    cfg_all = dict(rarity_cfg)
    cfg_all["flag_only_first_discoveries"] = False

    counts: dict[str, int] = {}
    for (raw_json,) in db.get_scan_events_raw():
        try:
            scan = json.loads(raw_json)
        except Exception:
            continue
        if scan.get("BodyType") == "Belt Cluster":
            continue
        system    = scan.get("StarSystem", "")
        body_id   = scan.get("BodyID")
        body_name = scan.get("BodyName", "")
        if not system or body_id is None:
            continue
        geo_count = db.get_geo_signal_count(system, body_name)
        for m in evaluate(scan, geo_count, cfg_all):
            counts[m.tag] = counts.get(m.tag, 0) + 1
    return counts


# ── NSP system-level check ─────────────────────────────────────────────────────

def _check_nsp(db: "Database", rarity_cfg: dict) -> None:
    """
    ⚠️  UNVALIDATED — zero NSP events in the current journal set.
    Check for Notable Stellar Phenomena via:
      1. CodexEntry with Category == "$Codex_Category_StellarPhenomena;"
      2. FSSSignalDiscovered with non-mundane SignalType

    Any match stores a system-level rare_find row with body_id=-1.
    """
    mundane = {s.lower() for s in rarity_cfg.get("nsp_mundane_signal_types", [])}

    # ── CodexEntry check ──────────────────────────────────────────────────────
    for (raw_json,) in db.get_events_raw_by_type("CodexEntry"):
        try:
            ev = json.loads(raw_json)
        except Exception:
            continue
        cat = ev.get("Category", "")
        if cat == "$Codex_Category_StellarPhenomena;":
            system  = ev.get("System") or ev.get("StarSystem", "")
            sys_addr = ev.get("SystemAddress")
            leg_id  = db.get_system_leg_id(system)
            name    = ev.get("Name_Localised") or ev.get("Name", "NSP CodexEntry")
            db.upsert_rare_find(
                system=system, system_address=sys_addr, body_id=-1,
                body_name=name, body_class="NSP",
                leg_id=leg_id,
                matches=[RuleMatch(
                    tag="nsp_alert",
                    details=f"NSP codex entry: {name}",
                    attrs={"source": "CodexEntry", "category": cat},
                )],
                was_discovered=0, was_mapped=1, was_footfalled=1,
                distance_ls=None,
            )

    # ── FSSSignalDiscovered check ─────────────────────────────────────────────
    for (raw_json,) in db.get_events_raw_by_type("FSSSignalDiscovered"):
        try:
            ev = json.loads(raw_json)
        except Exception:
            continue
        sig_type = (ev.get("SignalType_Localised") or ev.get("SignalType") or "").strip()
        if sig_type.lower() not in mundane and sig_type:
            system   = ev.get("StarSystem", "")
            sys_addr = ev.get("SystemAddress")
            leg_id   = db.get_system_leg_id(system)
            db.upsert_rare_find(
                system=system, system_address=sys_addr, body_id=-1,
                body_name=sig_type, body_class="NSP",
                leg_id=leg_id,
                matches=[RuleMatch(
                    tag="nsp_alert",
                    details=f"Non-mundane FSS signal: {sig_type}",
                    attrs={"source": "FSSSignalDiscovered", "signal_type": sig_type},
                )],
                was_discovered=0, was_mapped=1, was_footfalled=1,
                distance_ls=None,
            )
