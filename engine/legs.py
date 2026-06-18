"""
Leg state machine.

A "leg" is the stretch of travel between two waypoints (or from expedition
start to the first waypoint, or from the last waypoint to Parrot's Head).

Auto-close rules
----------------
* FSDJump AWAY FROM an unvisited compass-point waypoint (on_depart)
  → close current leg (name = waypoint label), open the next leg.
  Triggered on departure so that any cartographic / exo-bio sales made
  aboard the carrier in the waypoint system are included in the closing leg.
* FSDJump / Location arriving at expedition_end_system (on_arrive)
  → close current leg, mark expedition COMPLETE.
* Revisiting a waypoint (already has a closed leg named after it)
  → do nothing.
* CarrierJump events are NOT fed to on_arrive() or on_depart(); they never
  trigger leg closes.
* Manual close (tray menu or --cli command) closes the active leg with an
  arbitrary name and opens the next.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .db import Database

log = logging.getLogger(__name__)


class LegManager:
    def __init__(self, db: Database, cfg: dict) -> None:
        self.db = db
        self.cfg = cfg
        self._waypoint_map: dict[str, str] = cfg["waypoint_map"]
        self._end_norm: str = cfg["expedition_end_system_norm"]
        self._expedition_complete = False

    # ── Startup ────────────────────────────────────────────────────────────────

    def ensure_first_leg(self) -> None:
        """Open Leg 1 if no legs exist yet."""
        if self.db.get_leg_count() == 0:
            start_ts = self.cfg["expedition_start_timestamp"]
            start_sys = self.cfg.get("expedition_start_system", "")
            leg_id = self.db.open_leg(1, None, start_sys, start_ts)
            log.info("Opened Leg 1 (start_ts=%s, start_sys=%s)", start_ts, start_sys)
        else:
            # Restore expedition_complete flag from DB
            all_legs = self.db.get_all_legs()
            for leg in all_legs:
                # If the most recent leg is closed and there's no open leg, expedition is done.
                pass
            active = self.db.get_active_leg()
            if active is None:
                self._expedition_complete = True
                log.info("Expedition already marked complete (no open leg found).")

    # ── Event entry point ──────────────────────────────────────────────────────

    def on_arrive(self, system_name: str, ts: str) -> dict:
        """
        Called for every FSDJump destination or Location event (never CarrierJump).

        Handles only the expedition-end trigger.  Waypoint leg closes are now
        handled by on_depart() so that sales made in the waypoint system are
        captured in the closing leg before the export runs.

        Returns a result dict:
          {"action": "none"|"expedition_complete",
           "waypoint_label": None,
           "leg_id_closed": int | None}
        """
        if self._expedition_complete:
            return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

        norm = system_name.lower().strip()
        leg = self.db.get_active_leg()
        if leg is None:
            return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

        # ── Expedition end (arrival-triggered — you don't depart from the end) ─
        if norm == self._end_norm:
            self.db.close_leg(leg["leg_id"], system_name, ts)
            self._expedition_complete = True
            log.info(
                "EXPEDITION COMPLETE: arrived at %s — Leg %d closed.",
                system_name,
                leg["ordinal"],
            )
            return {
                "action": "expedition_complete",
                "waypoint_label": None,
                "leg_id_closed": leg["leg_id"],
            }

        return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

    def on_depart(self, from_system: str, ts: str) -> dict:
        """
        Called when the commander FSDJumps AWAY from a system.

        If from_system is an unvisited compass-point waypoint, close the current
        leg (naming it after that waypoint) and open the next one.  This ensures
        that any cartographic / exo-bio sales transacted in the waypoint system
        are attributed to the leg that ends there, not the leg that follows.

        Returns a result dict:
          {"action": "none"|"waypoint_close",
           "waypoint_label": str | None,
           "leg_id_closed": int | None}
        """
        if self._expedition_complete:
            return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

        norm = from_system.lower().strip()
        if norm not in self._waypoint_map:
            return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

        label   = self._waypoint_map[norm]
        visited = self.db.get_visited_waypoints()
        if label in visited:
            log.debug("Departing already-visited waypoint '%s' — no leg change.", label)
            return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

        leg = self.db.get_active_leg()
        if leg is None:
            return {"action": "none", "waypoint_label": None, "leg_id_closed": None}

        old_leg_id = leg["leg_id"]
        self.db.close_leg(old_leg_id, from_system, ts, name=label)
        self.db.mark_waypoint_visited(label, from_system, ts)
        next_ordinal = leg["ordinal"] + 1
        self.db.open_leg(next_ordinal, None, from_system, ts)
        log.info(
            "Waypoint '%s' — departing: Leg %d closed, Leg %d opened.",
            label,
            leg["ordinal"],
            next_ordinal,
        )
        return {
            "action": "waypoint_close",
            "waypoint_label": label,
            "leg_id_closed": old_leg_id,
        }

    # ── Manual close ───────────────────────────────────────────────────────────

    def manual_close(self, ts: str | None = None, label: str | None = None) -> int | None:
        """
        Manually close the active leg and open the next one.
        Returns the closed leg_id, or None if no active leg.
        """
        leg = self.db.get_active_leg()
        if leg is None:
            log.warning("manual_close: no active leg to close.")
            return None

        if ts is None:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        last_jump = self.db.get_last_jump(leg["leg_id"])
        end_sys = last_jump["to_system"] if last_jump else (leg["start_system"] or "Unknown")
        name = label or f"Leg {leg['ordinal']} (manual)"

        old_id = leg["leg_id"]
        self.db.close_leg(old_id, end_sys, ts, name=name)
        next_ord = leg["ordinal"] + 1
        self.db.open_leg(next_ord, None, end_sys, ts)
        log.info("Manual close: Leg %d → '%s', Leg %d opened.", leg["ordinal"], name, next_ord)
        return old_id

    # ── Status summary ─────────────────────────────────────────────────────────

    def status_text(self) -> str:
        if self._expedition_complete:
            return "Expedition complete!"
        leg = self.db.get_active_leg()
        if leg is None:
            return "No active leg."
        stats = self.db.stats_for_leg(leg["leg_id"])
        last = self.db.get_last_jump(leg["leg_id"])
        last_sys = last["to_system"] if last else (leg["start_system"] or "—")
        return (
            f"Leg {leg['ordinal']}  |  {stats['commander_jumps']} jumps  "
            f"|  {stats['distance_ly']:.1f} LY  |  Last: {last_sys}"
        )

    @property
    def is_complete(self) -> bool:
        return self._expedition_complete
