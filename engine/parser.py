"""
Journal event parser.

Each line of an ED journal is a JSON object.  This module:
  1. Parses the JSON.
  2. Filters by timestamp cutoff and commander name.
  3. Calls db.insert_event() for deduplication — returns False if seen before.
  4. Dispatches to the appropriate private handler.

Parsing quirks documented inline where non-obvious.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from .db import Database
from .legs import LegManager

log = logging.getLogger(__name__)

# Scan-stage constants
_STAGE = {"Log": 1, "Sample": 2, "Analyse": 3}

# Body classes flagged as "notable"
_NOTABLE = {
    "Earthlike body",
    "Water world",
    "Ammonia world",
    "High metal content body",
}


def _loc(d: dict, *keys: str, default: Any = None) -> Any:
    """Prefer _Localised variant of a key, then the raw key, then default."""
    for k in keys:
        if (v := d.get(k + "_Localised")) is not None:
            return v
        if (v := d.get(k)) is not None:
            return v
    return default


class EventParser:
    def __init__(
        self,
        db: Database,
        legs: LegManager,
        cfg: dict,
        on_leg_close: "Callable[[int], None] | None" = None,
    ) -> None:
        self.db = db
        self.legs = legs
        self._cutoff: datetime = cfg["expedition_start_dt"]
        self._commander: str = cfg["commander"]
        self._on_leg_close = on_leg_close  # callback for tray/CLI notifications

        # Current system is maintained across calls so we can fill from_system
        # on FSDJump events (the event itself only carries the destination).
        # Restored from the DB on startup via restore_state().
        self._current_system: str | None = None

    def restore_state(self) -> None:
        """Restore in-memory state from the DB after a restart."""
        last = self.db.get_last_jump()
        self._current_system = last["to_system"] if last else None
        log.debug("Parser state restored: current_system=%s", self._current_system)

    # ── Public entry point ─────────────────────────────────────────────────────

    def process_line(self, line: str) -> bool:
        """
        Parse one journal line.
        Returns True if the event was new and fully processed, False otherwise.
        """
        line = line.strip()
        if not line or line[0] != "{":
            return False

        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            log.debug("Skipping non-JSON line: %.80s", line)
            return False

        ts_str = ev.get("timestamp", "")
        if not ts_str:
            return False

        # ── Timestamp cutoff ───────────────────────────────────────────────────
        try:
            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            return False
        if ts_dt < self._cutoff:
            return False

        event_type = ev.get("event", "")

        # ── Dedupe ─────────────────────────────────────────────────────────────
        # Hash the normalised JSON so the same logical event from two files
        # (Continued scenario) only counts once.
        raw_norm = json.dumps(ev, sort_keys=True, separators=(",", ":"))
        dedupe_key = hashlib.sha256(raw_norm.encode()).hexdigest()
        if not self.db.insert_event(dedupe_key, ts_str, event_type, line):
            return False  # Already processed

        # ── Dispatch ───────────────────────────────────────────────────────────
        handler = getattr(self, f"_on_{event_type}", None)
        if handler:
            try:
                handler(ev, ts_str)
            except Exception:
                log.exception("Error handling %s event at %s", event_type, ts_str)

        return True

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _active_leg_id(self) -> int | None:
        leg = self.db.get_active_leg()
        return leg["leg_id"] if leg else None

    def _arrive(self, system_name: str, ts_str: str) -> None:
        """Update current-system state and feed the leg manager."""
        self._current_system = system_name
        result = self.legs.on_arrive(system_name, ts_str)
        if result["action"] in ("waypoint_close", "expedition_complete"):
            closed_id = result["leg_id_closed"]
            if self._on_leg_close and closed_id is not None:
                self._on_leg_close(closed_id)

    # ── Event handlers ─────────────────────────────────────────────────────────

    def _on_FSDJump(self, ev: dict, ts: str) -> None:
        to_sys = ev.get("StarSystem", "")
        if not to_sys:
            return

        # Departure check BEFORE recording the jump: if we're leaving an unvisited
        # waypoint, close the old leg now so that every sale made in the waypoint
        # system is captured in the export, and the jump away opens the new leg.
        if self._current_system:
            result = self.legs.on_depart(self._current_system, ts)
            if result["action"] == "waypoint_close":
                closed_id = result["leg_id_closed"]
                if self._on_leg_close and closed_id is not None:
                    self._on_leg_close(closed_id)

        # leg_id is now the active leg — either unchanged or the freshly opened one.
        leg_id = self._active_leg_id()
        self.db.insert_jump(
            ts=ts,
            from_system=self._current_system,
            to_system=to_sys,
            jump_dist_ly=ev.get("JumpDist"),
            fuel_used=ev.get("FuelUsed"),
            star_pos=ev.get("StarPos", []),
            leg_id=leg_id,
        )
        self.db.upsert_system(
            system_name=to_sys,
            system_address=ev.get("SystemAddress"),
            star_pos=ev.get("StarPos", []),
            visit_ts=ts,
            leg_id=leg_id,
        )
        self._arrive(to_sys, ts)

    def _on_CarrierJump(self, ev: dict, ts: str) -> None:
        # Tracked separately; does NOT advance leg logic or jump distance totals.
        to_sys = ev.get("StarSystem", "")
        if not to_sys:
            return
        self.db.insert_carrier_jump(
            ts=ts,
            to_system=to_sys,
            star_pos=ev.get("StarPos", []),
            leg_id=self._active_leg_id(),
        )
        # The commander is aboard the carrier → update current location.
        self._current_system = to_sys

    def _on_Location(self, ev: dict, ts: str) -> None:
        # Fires on login/respawn.  Treat as a system arrival but NOT a jump.
        sys_name = ev.get("StarSystem", "")
        if not sys_name:
            return
        leg_id = self._active_leg_id()
        self.db.upsert_system(
            system_name=sys_name,
            system_address=ev.get("SystemAddress"),
            star_pos=ev.get("StarPos", []),
            visit_ts=ts,
            leg_id=leg_id,
        )
        self._arrive(sys_name, ts)

    def _on_FSSDiscoveryScan(self, ev: dict, ts: str) -> None:
        sys_name = ev.get("SystemName", "")
        body_count = ev.get("BodyCount")
        if sys_name and body_count is not None:
            self.db.update_system_body_count(sys_name, body_count)

    def _on_FSSAllBodiesFound(self, ev: dict, ts: str) -> None:
        sys_name = ev.get("SystemName", "")
        if sys_name:
            self.db.set_system_fully_scanned(sys_name)

    def _on_Scan(self, ev: dict, ts: str) -> None:
        # Skip auto-scans (ScanType="AutoScan" are stars you pass through);
        # we want Detailed scans for the body counts.  But actually, we want
        # all scans because the validation counts include auto-scans.
        # Skip belt-cluster pseudo-bodies — they have BodyType="Belt Cluster".
        if ev.get("BodyType") == "Belt Cluster":
            return

        body_name = ev.get("BodyName", "")
        body_id   = ev.get("BodyID")
        system    = ev.get("StarSystem") or self._current_system or ""

        # Determine class: planet or star
        if "PlanetClass" in ev:
            body_class = ev["PlanetClass"]
        elif "StarType" in ev:
            body_class = ev["StarType"]
        else:
            body_class = None

        self.db.insert_body(
            ts=ts,
            system=system,
            body_name=body_name,
            body_id=body_id,
            body_class=body_class,
            terraform_state=ev.get("TerraformState"),
            landable=bool(ev.get("Landable", False)),
            distance_ls=ev.get("DistanceFromArrivalLS"),
            was_discovered=bool(ev.get("WasDiscovered", True)),
            was_mapped=bool(ev.get("WasMapped", True)),
            leg_id=self._active_leg_id(),
        )

    def _on_ScanBaryCentre(self, ev: dict, ts: str) -> None:
        # Barycentre of a binary pair — no PlanetClass/StarType, not landable.
        # Counted in body-scan totals alongside regular Scan events.
        body_name = ev.get("BodyName", "")
        body_id   = ev.get("BodyID")
        system    = ev.get("StarSystem") or self._current_system or ""
        if not system:
            return
        self.db.insert_body(
            ts=ts, system=system, body_name=body_name, body_id=body_id,
            body_class="Barycentre", terraform_state=None, landable=False,
            distance_ls=None, was_discovered=True, was_mapped=True,
            leg_id=self._active_leg_id(),
        )

    def _on_SAAScanComplete(self, ev: dict, ts: str) -> None:
        body_id = ev.get("BodyID")
        system  = ev.get("SystemName") or self._current_system or ""
        if system and body_id is not None:
            self.db.set_body_surface_mapped(system, body_id)

    def _on_FSSSignalDiscovered(self, ev: dict, ts: str) -> None:
        # Stored in events_raw by the generic dedupe path above.
        # The rarity pass queries events_raw by type to check for NSP signals.
        pass

    def _on_FSSBodySignals(self, ev: dict, ts: str) -> None:
        system    = ev.get("SystemName") or self._current_system or ""
        body_name = ev.get("BodyName", "")
        leg_id    = self._active_leg_id()
        for sig in ev.get("Signals", []):
            sig_type = sig.get("Type_Localised") or sig.get("Type", "")
            count    = sig.get("Count", 0)
            if sig_type:
                self.db.insert_body_signal(system, body_name, sig_type, count, leg_id)

    def _on_ScanOrganic(self, ev: dict, ts: str) -> None:
        scan_type = ev.get("ScanType", "")
        stage = _STAGE.get(scan_type, 0)
        if stage == 0:
            return

        system  = ev.get("SystemName") or self._current_system or ""
        body_id = ev.get("Body")

        genus   = _loc(ev, "Genus")   or ev.get("Genus",   "")
        species = _loc(ev, "Species") or ev.get("Species", "")
        variant = _loc(ev, "Variant") or ev.get("Variant")

        self.db.upsert_organic_scan(
            ts=ts,
            system=system,
            body_id=body_id,
            genus=genus,
            species=species,
            variant=variant,
            scan_stage=stage,
            leg_id=self._active_leg_id(),
        )

    def _on_CodexEntry(self, ev: dict, ts: str) -> None:
        name    = _loc(ev, "Name")   or ev.get("Name", "")
        region  = _loc(ev, "Region") or ev.get("Region", "")
        system  = ev.get("System", "") or self._current_system or ""
        is_new  = bool(ev.get("IsNewEntry", False))
        self.db.insert_codex_entry(ts, name, region, system, is_new, self._active_leg_id())

    def _on_MultiSellExplorationData(self, ev: dict, ts: str) -> None:
        discovered = ev.get("Discovered", [])
        sale_id = self.db.insert_carto_sale(
            ts=ts,
            total_earnings=ev.get("TotalEarnings", 0),
            base_value=ev.get("BaseValue", 0),
            bonus=ev.get("Bonus", 0),
            systems_count=len(discovered),
            leg_id=self._active_leg_id(),
        )
        for entry in discovered:
            sys_name   = entry.get("SystemName") or entry.get("System", "")
            num_bodies = entry.get("NumBodies", 0)
            self.db.insert_carto_sale_system(sale_id, sys_name, num_bodies)

    def _on_SellExplorationData(self, ev: dict, ts: str) -> None:
        # Older single-sell event; Discovered[] is a flat list of system names.
        discovered = ev.get("Discovered", [])
        sale_id = self.db.insert_carto_sale(
            ts=ts,
            total_earnings=ev.get("TotalEarnings", 0),
            base_value=ev.get("BaseValue", 0),
            bonus=ev.get("Bonus", 0),
            systems_count=len(discovered),
            leg_id=self._active_leg_id(),
        )
        for sys_name in discovered:
            if isinstance(sys_name, str):
                self.db.insert_carto_sale_system(sale_id, sys_name, 0)

    def _on_SellOrganicData(self, ev: dict, ts: str) -> None:
        leg_id  = self._active_leg_id()
        sale_id = self.db.insert_organic_sale(ts, leg_id)
        for item in ev.get("BioData", []):
            genus   = _loc(item, "Genus")   or item.get("Genus",   "")
            species = _loc(item, "Species") or item.get("Species", "")
            variant = _loc(item, "Variant") or item.get("Variant", "")
            value   = item.get("Value", 0)
            bonus   = item.get("Bonus", 0)
            self.db.insert_organic_sale_item(sale_id, genus, species, variant, value, bonus)
