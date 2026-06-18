"""
Configuration loader.  Reads config.toml using stdlib tomllib (Python 3.11+)
and normalises values so the rest of the engine never touches raw config again.
"""
from __future__ import annotations

import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def _expand(path: str, root: Path) -> Path:
    """Expand %USERPROFILE% / ~ and resolve relative to the script root."""
    p = os.path.expandvars(os.path.expanduser(path))
    result = Path(p)
    if not result.is_absolute():
        result = root / result
    return result


def load(config_path: Path) -> dict:
    root = config_path.parent

    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)

    # ── Paths ──────────────────────────────────────────────────────────────────
    raw["journal_dir"] = _expand(raw["journal_dir"], root)
    raw["output_dir"]  = _expand(raw["output_dir"],  root)
    raw["db_path"]     = _expand(raw["db_path"],      root)

    raw["output_dir"].mkdir(parents=True, exist_ok=True)

    # ── Timestamp cutoff ───────────────────────────────────────────────────────
    raw["expedition_start_dt"] = datetime.fromisoformat(
        raw["expedition_start_timestamp"].replace("Z", "+00:00")
    )

    # ── Normalised system names for fast matching ──────────────────────────────
    for wp in raw.get("waypoints", []):
        wp["system_norm"] = wp["system"].lower().strip()

    raw["expedition_end_system_norm"] = raw["expedition_end_system"].lower().strip()
    raw["expedition_start_system_norm"] = raw.get(
        "expedition_start_system", ""
    ).lower().strip()

    # Quick-lookup dict: normalised_system_name → waypoint label
    raw["waypoint_map"] = {
        wp["system_norm"]: wp["label"]
        for wp in raw.get("waypoints", [])
    }

    return raw
