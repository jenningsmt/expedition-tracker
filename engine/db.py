"""
SQLite database layer.

All tables are created here.  Every public method acquires a threading.Lock
before touching the connection so the watcher thread and the tray/CLI thread
can safely share one DB object.

Idempotency guarantee: insert_event() returns False when the dedupe_key already
exists, letting callers skip handler dispatch for re-seen events.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


# ── Schema ─────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS events_raw (
    id         INTEGER PRIMARY KEY,
    dedupe_key TEXT    UNIQUE NOT NULL,
    ts         TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    raw_json   TEXT    NOT NULL
);

-- Tracks how many bytes of each journal file have been fully processed.
-- Prevents double-counting on restart.
CREATE TABLE IF NOT EXISTS file_progress (
    file_path       TEXT PRIMARY KEY,
    bytes_processed INTEGER NOT NULL DEFAULT 0,
    last_updated    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS legs (
    leg_id      INTEGER PRIMARY KEY,
    ordinal     INTEGER NOT NULL,
    name        TEXT,                         -- waypoint label, or NULL until closed
    start_system TEXT,
    start_ts    TEXT,
    end_system  TEXT,
    end_ts      TEXT,
    status      TEXT NOT NULL DEFAULT 'open'  -- 'open' | 'closed'
);

-- Waypoints that have already triggered a leg auto-close.
CREATE TABLE IF NOT EXISTS waypoints_visited (
    label      TEXT PRIMARY KEY,
    system     TEXT NOT NULL,
    visited_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jumps (
    id             INTEGER PRIMARY KEY,
    ts             TEXT    NOT NULL,
    from_system    TEXT,
    to_system      TEXT    NOT NULL,
    jump_dist_ly   REAL,
    fuel_used      REAL,
    star_pos_x     REAL,
    star_pos_y     REAL,
    star_pos_z     REAL,
    leg_id         INTEGER REFERENCES legs(leg_id)
);
CREATE INDEX IF NOT EXISTS idx_jumps_leg ON jumps(leg_id);

CREATE TABLE IF NOT EXISTS carrier_jumps (
    id         INTEGER PRIMARY KEY,
    ts         TEXT    NOT NULL,
    to_system  TEXT    NOT NULL,
    star_pos_x REAL,
    star_pos_y REAL,
    star_pos_z REAL,
    leg_id     INTEGER REFERENCES legs(leg_id)
);

-- One row per system, first visit wins for leg_id.
CREATE TABLE IF NOT EXISTS systems (
    id              INTEGER PRIMARY KEY,
    system_name     TEXT    NOT NULL UNIQUE,
    system_address  INTEGER,
    star_pos_x      REAL,
    star_pos_y      REAL,
    star_pos_z      REAL,
    first_visit_ts  TEXT,
    body_count      INTEGER,
    fully_scanned   INTEGER NOT NULL DEFAULT 0,
    leg_id          INTEGER REFERENCES legs(leg_id)
);

-- One row per body per system; first scan wins.
CREATE TABLE IF NOT EXISTS bodies (
    id                       INTEGER PRIMARY KEY,
    ts                       TEXT    NOT NULL,
    system                   TEXT    NOT NULL,
    body_name                TEXT,
    body_id                  INTEGER,
    body_class               TEXT,
    terraform_state          TEXT,
    landable                 INTEGER NOT NULL DEFAULT 0,
    distance_from_arrival_ls REAL,
    was_discovered           INTEGER NOT NULL DEFAULT 1,  -- 0 = first discovery
    was_mapped               INTEGER NOT NULL DEFAULT 1,
    surface_mapped           INTEGER NOT NULL DEFAULT 0,
    leg_id                   INTEGER REFERENCES legs(leg_id),
    UNIQUE(system, body_id)
);
CREATE INDEX IF NOT EXISTS idx_bodies_leg ON bodies(leg_id);

CREATE TABLE IF NOT EXISTS body_signals (
    id          INTEGER PRIMARY KEY,
    system      TEXT NOT NULL,
    body_name   TEXT,
    signal_type TEXT,
    count       INTEGER,
    leg_id      INTEGER REFERENCES legs(leg_id)
);

-- Each row is one scan-stage for one organism on one body.
-- Variant is populated at stage 3 (Analyse); earlier stages may have it too.
CREATE TABLE IF NOT EXISTS organic_scans (
    id         INTEGER PRIMARY KEY,
    ts         TEXT    NOT NULL,
    system     TEXT    NOT NULL,
    body_id    INTEGER,
    genus      TEXT,
    species    TEXT,
    variant    TEXT,
    scan_stage INTEGER NOT NULL,  -- 1=Log 2=Sample 3=Analyse
    leg_id     INTEGER REFERENCES legs(leg_id),
    UNIQUE(system, body_id, genus, scan_stage)
);

-- One row per notable body (or per system for NSP alerts, body_id=-1).
-- Idempotent on (system, body_id) via ON CONFLICT DO UPDATE.
CREATE TABLE IF NOT EXISTS rare_finds (
    id              INTEGER PRIMARY KEY,
    system          TEXT    NOT NULL,
    system_address  INTEGER,
    body_id         INTEGER NOT NULL DEFAULT -1,
    body_name       TEXT,
    body_class      TEXT,
    leg_id          INTEGER REFERENCES legs(leg_id),
    matched_rules   TEXT    NOT NULL DEFAULT '[]',
    trigger_details TEXT    NOT NULL DEFAULT '{}',
    trigger_attrs   TEXT    NOT NULL DEFAULT '{}',
    was_discovered  INTEGER NOT NULL DEFAULT 1,
    was_mapped      INTEGER NOT NULL DEFAULT 1,
    was_footfalled  INTEGER NOT NULL DEFAULT 0,
    distance_ls     REAL,
    UNIQUE(system, body_id)
);
CREATE INDEX IF NOT EXISTS idx_rare_finds_leg ON rare_finds(leg_id);

CREATE TABLE IF NOT EXISTS codex_entries (
    id           INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,
    name         TEXT,
    region       TEXT,
    system       TEXT,
    is_new_entry INTEGER NOT NULL DEFAULT 0,
    leg_id       INTEGER REFERENCES legs(leg_id)
);

CREATE TABLE IF NOT EXISTS sales_cartographics (
    id             INTEGER PRIMARY KEY,
    ts             TEXT    NOT NULL,
    total_earnings INTEGER,
    base_value     INTEGER,
    bonus          INTEGER,
    systems_count  INTEGER,
    leg_id         INTEGER REFERENCES legs(leg_id)
);

CREATE TABLE IF NOT EXISTS sales_cartographics_systems (
    id          INTEGER PRIMARY KEY,
    sale_id     INTEGER NOT NULL REFERENCES sales_cartographics(id),
    system_name TEXT,
    num_bodies  INTEGER
);

CREATE TABLE IF NOT EXISTS sales_organic (
    id     INTEGER PRIMARY KEY,
    ts     TEXT    NOT NULL,
    leg_id INTEGER REFERENCES legs(leg_id)
);

CREATE TABLE IF NOT EXISTS sales_organic_items (
    id      INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales_organic(id),
    genus   TEXT,
    species TEXT,
    variant TEXT,
    value   INTEGER,
    bonus   INTEGER
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(_DDL)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Low-level helpers ──────────────────────────────────────────────────────

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def _execmany(self, sql: str, rows: list) -> None:
        self._conn.executemany(sql, rows)

    # ── events_raw ─────────────────────────────────────────────────────────────

    def insert_event(self, dedupe_key: str, ts: str, event: str, raw_json: str) -> bool:
        """Returns True if the event was new and inserted; False if duplicate."""
        with self._lock:
            try:
                self._exec(
                    "INSERT INTO events_raw(dedupe_key,ts,event,raw_json) VALUES(?,?,?,?)",
                    (dedupe_key, ts, event, raw_json),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # ── file_progress ──────────────────────────────────────────────────────────

    def get_file_offset(self, path: str) -> int:
        with self._lock:
            row = self._exec(
                "SELECT bytes_processed FROM file_progress WHERE file_path=?", (path,)
            ).fetchone()
        return row[0] if row else 0

    def set_file_offset(self, path: str, offset: int, ts: str = "") -> None:
        with self._lock:
            self._exec(
                """INSERT INTO file_progress(file_path,bytes_processed,last_updated)
                   VALUES(?,?,?)
                   ON CONFLICT(file_path) DO UPDATE
                   SET bytes_processed=excluded.bytes_processed,
                       last_updated=excluded.last_updated""",
                (path, offset, ts),
            )

    # ── legs ───────────────────────────────────────────────────────────────────

    def open_leg(self, ordinal: int, name: str | None, start_system: str, start_ts: str) -> int:
        with self._lock:
            cur = self._exec(
                """INSERT INTO legs(ordinal,name,start_system,start_ts,status)
                   VALUES(?,?,?,?,'open')""",
                (ordinal, name, start_system, start_ts),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def close_leg(
        self,
        leg_id: int,
        end_system: str,
        end_ts: str,
        name: str | None = None,
    ) -> None:
        with self._lock:
            if name is not None:
                self._exec(
                    """UPDATE legs SET status='closed', end_system=?, end_ts=?, name=?
                       WHERE leg_id=?""",
                    (end_system, end_ts, name, leg_id),
                )
            else:
                self._exec(
                    """UPDATE legs SET status='closed', end_system=?, end_ts=?
                       WHERE leg_id=?""",
                    (end_system, end_ts, leg_id),
                )

    def get_active_leg(self) -> sqlite3.Row | None:
        with self._lock:
            return self._exec(
                "SELECT * FROM legs WHERE status='open' ORDER BY leg_id DESC LIMIT 1"
            ).fetchone()

    def get_all_legs(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                "SELECT * FROM legs ORDER BY ordinal"
            ).fetchall()

    def get_leg(self, leg_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._exec(
                "SELECT * FROM legs WHERE leg_id=?", (leg_id,)
            ).fetchone()

    def get_leg_count(self) -> int:
        with self._lock:
            row = self._exec("SELECT COUNT(*) FROM legs").fetchone()
            return row[0] if row else 0

    # ── waypoints_visited ──────────────────────────────────────────────────────

    def get_visited_waypoints(self) -> set[str]:
        with self._lock:
            rows = self._exec("SELECT label FROM waypoints_visited").fetchall()
        return {r[0] for r in rows}

    def mark_waypoint_visited(self, label: str, system: str, ts: str) -> None:
        with self._lock:
            self._exec(
                """INSERT OR IGNORE INTO waypoints_visited(label,system,visited_ts)
                   VALUES(?,?,?)""",
                (label, system, ts),
            )

    # ── jumps ──────────────────────────────────────────────────────────────────

    def insert_jump(
        self,
        ts: str,
        from_system: str | None,
        to_system: str,
        jump_dist_ly: float | None,
        fuel_used: float | None,
        star_pos: list,
        leg_id: int | None,
    ) -> None:
        x, y, z = (star_pos + [None, None, None])[:3]
        with self._lock:
            self._exec(
                """INSERT INTO jumps
                   (ts,from_system,to_system,jump_dist_ly,fuel_used,
                    star_pos_x,star_pos_y,star_pos_z,leg_id)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (ts, from_system, to_system, jump_dist_ly, fuel_used, x, y, z, leg_id),
            )

    def get_last_jump(self, leg_id: int | None = None) -> sqlite3.Row | None:
        with self._lock:
            if leg_id is not None:
                return self._exec(
                    "SELECT * FROM jumps WHERE leg_id=? ORDER BY ts DESC LIMIT 1",
                    (leg_id,),
                ).fetchone()
            return self._exec(
                "SELECT * FROM jumps ORDER BY ts DESC LIMIT 1"
            ).fetchone()

    # ── carrier_jumps ──────────────────────────────────────────────────────────

    def insert_carrier_jump(
        self, ts: str, to_system: str, star_pos: list, leg_id: int | None
    ) -> None:
        x, y, z = (star_pos + [None, None, None])[:3]
        with self._lock:
            self._exec(
                """INSERT INTO carrier_jumps(ts,to_system,star_pos_x,star_pos_y,star_pos_z,leg_id)
                   VALUES(?,?,?,?,?,?)""",
                (ts, to_system, x, y, z, leg_id),
            )

    # ── systems ────────────────────────────────────────────────────────────────

    def upsert_system(
        self,
        system_name: str,
        system_address: int | None,
        star_pos: list,
        visit_ts: str,
        leg_id: int | None,
    ) -> None:
        x, y, z = (star_pos + [None, None, None])[:3]
        with self._lock:
            self._exec(
                """INSERT INTO systems
                   (system_name,system_address,star_pos_x,star_pos_y,star_pos_z,
                    first_visit_ts,leg_id)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(system_name) DO NOTHING""",
                (system_name, system_address, x, y, z, visit_ts, leg_id),
            )

    def update_system_body_count(self, system_name: str, body_count: int) -> None:
        with self._lock:
            self._exec(
                "UPDATE systems SET body_count=? WHERE system_name=?",
                (body_count, system_name),
            )

    def set_system_fully_scanned(self, system_name: str) -> None:
        with self._lock:
            self._exec(
                "UPDATE systems SET fully_scanned=1 WHERE system_name=?",
                (system_name,),
            )

    # ── bodies ─────────────────────────────────────────────────────────────────

    def insert_body(
        self,
        ts: str,
        system: str,
        body_name: str,
        body_id: int | None,
        body_class: str | None,
        terraform_state: str | None,
        landable: bool,
        distance_ls: float | None,
        was_discovered: bool,
        was_mapped: bool,
        leg_id: int | None,
    ) -> None:
        with self._lock:
            self._exec(
                """INSERT OR IGNORE INTO bodies
                   (ts,system,body_name,body_id,body_class,terraform_state,landable,
                    distance_from_arrival_ls,was_discovered,was_mapped,leg_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts, system, body_name, body_id, body_class,
                    terraform_state or "", int(landable),
                    distance_ls, int(was_discovered), int(was_mapped), leg_id,
                ),
            )

    def set_body_surface_mapped(self, system: str, body_id: int) -> None:
        with self._lock:
            self._exec(
                "UPDATE bodies SET surface_mapped=1 WHERE system=? AND body_id=?",
                (system, body_id),
            )

    # ── body_signals ───────────────────────────────────────────────────────────

    def insert_body_signal(
        self, system: str, body_name: str, signal_type: str, count: int, leg_id: int | None
    ) -> None:
        with self._lock:
            self._exec(
                """INSERT INTO body_signals(system,body_name,signal_type,count,leg_id)
                   VALUES(?,?,?,?,?)""",
                (system, body_name, signal_type, count, leg_id),
            )

    # ── organic_scans ──────────────────────────────────────────────────────────

    def upsert_organic_scan(
        self,
        ts: str,
        system: str,
        body_id: int | None,
        genus: str,
        species: str,
        variant: str | None,
        scan_stage: int,
        leg_id: int | None,
    ) -> None:
        with self._lock:
            self._exec(
                """INSERT INTO organic_scans
                   (ts,system,body_id,genus,species,variant,scan_stage,leg_id)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(system,body_id,genus,scan_stage)
                   DO UPDATE SET variant=COALESCE(excluded.variant, variant),
                                 ts=excluded.ts""",
                (ts, system, body_id, genus, species, variant, scan_stage, leg_id),
            )

    # ── codex_entries ──────────────────────────────────────────────────────────

    def insert_codex_entry(
        self,
        ts: str,
        name: str,
        region: str,
        system: str,
        is_new_entry: bool,
        leg_id: int | None,
    ) -> None:
        with self._lock:
            self._exec(
                """INSERT INTO codex_entries(ts,name,region,system,is_new_entry,leg_id)
                   VALUES(?,?,?,?,?,?)""",
                (ts, name, region, system, int(is_new_entry), leg_id),
            )

    # ── sales ──────────────────────────────────────────────────────────────────

    def insert_carto_sale(
        self,
        ts: str,
        total_earnings: int,
        base_value: int,
        bonus: int,
        systems_count: int,
        leg_id: int | None,
    ) -> int:
        with self._lock:
            cur = self._exec(
                """INSERT INTO sales_cartographics
                   (ts,total_earnings,base_value,bonus,systems_count,leg_id)
                   VALUES(?,?,?,?,?,?)""",
                (ts, total_earnings, base_value, bonus, systems_count, leg_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def insert_carto_sale_system(
        self, sale_id: int, system_name: str, num_bodies: int
    ) -> None:
        with self._lock:
            self._exec(
                "INSERT INTO sales_cartographics_systems(sale_id,system_name,num_bodies) VALUES(?,?,?)",
                (sale_id, system_name, num_bodies),
            )

    def insert_organic_sale(self, ts: str, leg_id: int | None) -> int:
        with self._lock:
            cur = self._exec(
                "INSERT INTO sales_organic(ts,leg_id) VALUES(?,?)",
                (ts, leg_id),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def insert_organic_sale_item(
        self,
        sale_id: int,
        genus: str,
        species: str,
        variant: str,
        value: int,
        bonus: int,
    ) -> None:
        with self._lock:
            self._exec(
                """INSERT INTO sales_organic_items(sale_id,genus,species,variant,value,bonus)
                   VALUES(?,?,?,?,?,?)""",
                (sale_id, genus, species, variant, value, bonus),
            )

    # ── rare_finds ─────────────────────────────────────────────────────────────

    def upsert_rare_find(
        self,
        system: str,
        system_address: "int | None",
        body_id: int,
        body_name: "str | None",
        body_class: "str | None",
        leg_id: "int | None",
        matches: list,          # list[RuleMatch] from engine.rarity
        was_discovered: int,
        was_mapped: int,
        was_footfalled: int,
        distance_ls: "float | None",
    ) -> None:
        import json as _json
        tags     = [m.tag for m in matches]
        details  = {m.tag: m.details for m in matches}
        attrs    = {m.tag: m.attrs    for m in matches}
        with self._lock:
            self._exec(
                """INSERT INTO rare_finds
                   (system, system_address, body_id, body_name, body_class, leg_id,
                    matched_rules, trigger_details, trigger_attrs,
                    was_discovered, was_mapped, was_footfalled, distance_ls)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(system, body_id) DO UPDATE SET
                       matched_rules   = excluded.matched_rules,
                       trigger_details = excluded.trigger_details,
                       trigger_attrs   = excluded.trigger_attrs,
                       leg_id          = COALESCE(excluded.leg_id, leg_id),
                       system_address  = COALESCE(excluded.system_address, system_address),
                       body_name       = COALESCE(excluded.body_name, body_name),
                       body_class      = COALESCE(excluded.body_class, body_class),
                       was_discovered  = excluded.was_discovered,
                       was_mapped      = excluded.was_mapped,
                       was_footfalled  = excluded.was_footfalled,
                       distance_ls     = COALESCE(excluded.distance_ls, distance_ls)""",
                (
                    system, system_address, body_id, body_name, body_class, leg_id,
                    _json.dumps(tags), _json.dumps(details), _json.dumps(attrs),
                    was_discovered, was_mapped, was_footfalled, distance_ls,
                ),
            )

    def get_rare_finds_for_leg(self, leg_id: int) -> "list[sqlite3.Row]":
        with self._lock:
            return self._exec(
                "SELECT * FROM rare_finds WHERE leg_id=? ORDER BY system, body_id",
                (leg_id,),
            ).fetchall()

    def get_rare_finds_stats(self, leg_id: "int | None" = None) -> "dict[str, Any]":
        """Return count and per-rule breakdown for rare_finds."""
        import json as _json
        where = "WHERE leg_id=?" if leg_id is not None else ""
        p: tuple = (leg_id,) if leg_id is not None else ()
        with self._lock:
            rows = self._exec(
                f"SELECT matched_rules FROM rare_finds {where}", p
            ).fetchall()
        total    = len(rows)
        breakdown: dict[str, int] = {}
        for (mj,) in rows:
            try:
                for tag in _json.loads(mj):
                    breakdown[tag] = breakdown.get(tag, 0) + 1
            except Exception:
                pass
        return {"rare_finds_count": total, "rare_finds_by_rule": breakdown}

    def count_all_rare_finds(self) -> int:
        with self._lock:
            return self._exec("SELECT COUNT(*) FROM rare_finds").fetchone()[0]

    # ── rarity-pass helpers ────────────────────────────────────────────────────

    def get_scan_events_raw(self) -> "list[tuple]":
        """Return list of (raw_json,) for all Scan events (not belt clusters)."""
        with self._lock:
            return self._exec(
                "SELECT raw_json FROM events_raw WHERE event='Scan'"
            ).fetchall()

    def get_events_raw_by_type(self, event_type: str) -> "list[tuple]":
        with self._lock:
            return self._exec(
                "SELECT raw_json FROM events_raw WHERE event=?", (event_type,)
            ).fetchall()

    def get_geo_signal_count(self, system: str, body_name: str) -> int:
        """Return geological signal count for this body (0 if none recorded)."""
        with self._lock:
            row = self._exec(
                """SELECT count FROM body_signals
                   WHERE system=? AND body_name=?
                   AND LOWER(signal_type) LIKE '%geological%'
                   LIMIT 1""",
                (system, body_name),
            ).fetchone()
        return row[0] if row else 0

    def get_body_leg_id(self, system: str, body_id: int) -> "int | None":
        with self._lock:
            row = self._exec(
                "SELECT leg_id FROM bodies WHERE system=? AND body_id=?",
                (system, body_id),
            ).fetchone()
        return row[0] if row else None

    def get_system_leg_id(self, system_name: str) -> "int | None":
        with self._lock:
            row = self._exec(
                "SELECT leg_id FROM systems WHERE system_name=? LIMIT 1",
                (system_name,),
            ).fetchone()
        return row[0] if row else None

    # ── aggregate queries (used by exporter + validate) ────────────────────────

    def stats_for_leg(self, leg_id: int | None = None) -> dict[str, Any]:
        """
        Return headline metrics for one leg (or all legs if leg_id is None).

        All body counts here are DISTINCT per (system, body_id) — the bodies
        table enforces INSERT OR IGNORE on that pair, so duplicate scan events
        (revisited systems, re-scan on login) do not inflate these totals.
        Use raw_scan_stats() for the raw event-count versions.
        """
        where = "WHERE leg_id=?" if leg_id is not None else ""
        p: tuple = (leg_id,) if leg_id is not None else ()
        # Helper: attach an AND/WHERE connector after an existing clause
        and_ = "AND" if where else "WHERE"

        # Barycentres (from ScanBaryCentre events) are stored in the bodies table
        # for the class breakdown, but excluded from scan/discovery/mapping counts
        # because raw_scan_stats counts only Scan events (not ScanBaryCentre).
        # This keeps the invariant   distinct <= raw   satisfied.
        no_bc = f"COALESCE(body_class,'') != 'Barycentre'"

        with self._lock:
            jumps = self._exec(
                f"SELECT COUNT(*), COALESCE(SUM(jump_dist_ly),0) FROM jumps {where}", p
            ).fetchone()
            carrier = self._exec(
                f"SELECT COUNT(*) FROM carrier_jumps {where}", p
            ).fetchone()
            # Distinct FSDJump destinations only — excludes Location-on-login
            # arrivals in carrier-jump destination systems.
            systems = self._exec(
                f"SELECT COUNT(DISTINCT to_system) FROM jumps {where}", p
            ).fetchone()
            bodies = self._exec(
                f"SELECT COUNT(*) FROM bodies {where} {and_} {no_bc}", p
            ).fetchone()
            first_disc = self._exec(
                f"SELECT COUNT(*) FROM bodies {where} {and_} {no_bc} AND was_discovered=0", p,
            ).fetchone()
            surface_map = self._exec(
                f"SELECT COUNT(*) FROM bodies {where} {and_} {no_bc} AND surface_mapped=1", p,
            ).fetchone()
            # Terraformable: any non-empty TerraformState, regardless of planet class.
            terra = self._exec(
                f"SELECT COUNT(*) FROM bodies {where} {and_} {no_bc} "
                f"AND terraform_state IS NOT NULL AND terraform_state != ''", p,
            ).fetchone()
            hmc_terra = self._exec(
                f"SELECT COUNT(*) FROM bodies {where} {and_} "
                f"body_class='High metal content body' "
                f"AND terraform_state IS NOT NULL AND terraform_state != ''", p,
            ).fetchone()
            organics = self._exec(
                f"""SELECT COUNT(DISTINCT variant) FROM organic_scans
                    {where} {and_} scan_stage=3 AND variant IS NOT NULL""",
                p,
            ).fetchone()
            codex_new = self._exec(
                f"SELECT COUNT(*) FROM codex_entries {where} {and_} is_new_entry=1", p,
            ).fetchone()
            carto_sum = self._exec(
                f"SELECT COUNT(*), COALESCE(SUM(total_earnings),0) FROM sales_cartographics {where}", p
            ).fetchone()
            exobio_where = where.replace("leg_id", "so.leg_id")
            exobio_and   = "AND" if exobio_where else "WHERE"
            exobio_q = self._exec(
                f"""SELECT COUNT(DISTINCT so.id), COALESCE(SUM(soi.value+soi.bonus),0)
                    FROM sales_organic so
                    JOIN sales_organic_items soi ON soi.sale_id=so.id
                    {exobio_where}""",
                p,
            ).fetchone()

        return {
            "commander_jumps":   jumps[0],
            "distance_ly":       round(jumps[1], 2),
            "carrier_jumps":     carrier[0],
            "systems_visited":   systems[0],
            "bodies_scanned":    bodies[0],      # DISTINCT (system, body_id)
            "first_discovered":  first_disc[0],  # DISTINCT
            "bodies_mapped":     surface_map[0], # DISTINCT
            "terraformable_count":   terra[0],     # DISTINCT, any class
            "hmc_terraformable":     hmc_terra[0], # DISTINCT HMC with non-empty TerraformState
            "organic_variants":  organics[0],    # DISTINCT variant strings at stage 3
            "new_codex":         codex_new[0],
            "carto_sales_count": carto_sum[0],
            "carto_earnings":    carto_sum[1],
            "exobio_sales_count":exobio_q[0],
            "exobio_earnings":   exobio_q[1],
        }

    def raw_scan_stats(self) -> dict[str, int]:
        """
        Raw event-count versions of body-scan metrics from events_raw.

        Unlike stats_for_leg() (which counts DISTINCT bodies), these count
        every qualifying journal event — including duplicate scans of revisited
        systems and bodies mapped without a prior DSS scan.

        Use these exclusively for the validation/snapshot path, never for
        user-facing leg reports.

        Notes on notable-class raw counts:
          • elw_raw / ww_raw / aw_raw — raw Scan events for that planet class
          • hmc_terraformable_raw      — raw Scan events for terraformable HMC
                                         specifically (not all HMC; ~79 out of ~485)
        """
        with self._lock:
            bodies_scanned_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='Scan'"
            ).fetchone()[0]
            first_discovered_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='Scan' "
                "AND raw_json LIKE '%\"WasDiscovered\":false%'"
            ).fetchone()[0]
            bodies_mapped_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='SAAScanComplete'"
            ).fetchone()[0]
            elw_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='Scan' "
                "AND raw_json LIKE '%Earthlike body%'"
            ).fetchone()[0]
            ww_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='Scan' "
                "AND raw_json LIKE '%Water world%'"
            ).fetchone()[0]
            aw_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='Scan' "
                "AND raw_json LIKE '%Ammonia world%'"
            ).fetchone()[0]
            hmc_terraformable_raw = self._exec(
                "SELECT COUNT(*) FROM events_raw WHERE event='Scan' "
                "AND raw_json LIKE '%High metal content body%' "
                "AND raw_json LIKE '%Terraformable%'"
            ).fetchone()[0]
        return {
            "bodies_scanned_raw":       bodies_scanned_raw,
            "first_discovered_raw":     first_discovered_raw,
            "bodies_mapped_raw":        bodies_mapped_raw,
            "elw_raw":                  elw_raw,
            "ww_raw":                   ww_raw,
            "aw_raw":                   aw_raw,
            "hmc_terraformable_raw":    hmc_terraformable_raw,
        }

    def get_body_class_breakdown(self, leg_id: int | None = None) -> dict[str, int]:
        """
        Count bodies per planet/star class for a leg (or all legs).
        Returns {class_name: count} sorted by count descending.
        Planet classes are full strings ('Water world', 'Icy body', …);
        star types are short spectral codes ('G', 'M', 'K', …).
        """
        if leg_id is not None:
            with self._lock:
                rows = self._exec(
                    "SELECT COALESCE(body_class,'(none)'), COUNT(*) FROM bodies "
                    "WHERE leg_id=? GROUP BY body_class ORDER BY COUNT(*) DESC",
                    (leg_id,),
                ).fetchall()
        else:
            with self._lock:
                rows = self._exec(
                    "SELECT COALESCE(body_class,'(none)'), COUNT(*) FROM bodies "
                    "GROUP BY body_class ORDER BY COUNT(*) DESC"
                ).fetchall()
        return {r[0]: r[1] for r in rows}

    def count_jumps_to_first_arrival(self, system_name: str) -> int:
        """
        Count FSDJumps (from DB start = expedition cutoff) up to and including
        the first arrival at system_name.  Returns -1 if the system has not
        been reached yet.
        """
        norm = system_name.lower()
        with self._lock:
            first = self._exec(
                "SELECT ts FROM jumps WHERE LOWER(to_system)=? ORDER BY ts LIMIT 1",
                (norm,),
            ).fetchone()
            if not first:
                return -1
            count = self._exec(
                "SELECT COUNT(*) FROM jumps WHERE ts <= ?", (first[0],)
            ).fetchone()[0]
        return count

    def min_event_ts(self) -> str | None:
        """Return the earliest timestamp in events_raw, or None if empty."""
        with self._lock:
            row = self._exec("SELECT MIN(ts) FROM events_raw").fetchone()
        return row[0] if row else None

    def get_systems_for_leg(self, leg_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                "SELECT * FROM systems WHERE leg_id=? ORDER BY first_visit_ts",
                (leg_id,),
            ).fetchall()

    def get_bodies_for_leg(self, leg_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                "SELECT * FROM bodies WHERE leg_id=? ORDER BY system, body_id",
                (leg_id,),
            ).fetchall()

    def get_organics_for_leg(self, leg_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                """SELECT * FROM organic_scans
                   WHERE leg_id=? AND scan_stage=3
                   ORDER BY system, body_id, genus""",
                (leg_id,),
            ).fetchall()

    def get_codex_for_leg(self, leg_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                "SELECT * FROM codex_entries WHERE leg_id=? AND is_new_entry=1 ORDER BY ts",
                (leg_id,),
            ).fetchall()

    def get_carto_sales_for_leg(self, leg_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                "SELECT * FROM sales_cartographics WHERE leg_id=? ORDER BY ts",
                (leg_id,),
            ).fetchall()

    def get_organic_sales_for_leg(self, leg_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._exec(
                """SELECT so.*, soi.genus, soi.species, soi.variant, soi.value, soi.bonus
                   FROM sales_organic so
                   JOIN sales_organic_items soi ON soi.sale_id=so.id
                   WHERE so.leg_id=? ORDER BY so.ts""",
                (leg_id,),
            ).fetchall()
