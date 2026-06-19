# Galactic Six Points Expedition

A long-haul exploration voyage across the Milky Way — and the toolkit that records it.

---

## The Sandbox

Elite Dangerous is a space-flight simulator built around a full-scale, 1:1 recreation of our Milky Way galaxy: roughly 400 billion star systems, the overwhelming majority of which no human player has ever visited. One of the game's core pursuits is exploration — flying out into uncharted space, scanning the stars, planets, moons, and lifeforms you find, and being the first commander to put your name on a new discovery.

The Galactic Six Points Expedition is one such voyage. It's organized around six destination systems — one for each direction a starship can travel in the galaxy: North, South, East, West, Zenith (up), and Nadir (down).

## Why explorers do this

Exploration in Elite Dangerous is part science expedition, part personal odyssey. The science is real: the game is a 1:1 simulation of the Milky Way, so a commander dropping into an untouched system is the first to chart its stars, measure the mass and chemistry of its worlds, and document the lifeforms found on them — and your name stays attached to every body you discover, a permanent entry in humanity's shared map of the galaxy. The odyssey is the rest of it — weeks alone in the dark, tens of thousands of light-years from another soul, navigating star to star for the singular thrill of arriving somewhere no one has ever been, and cataloguing the strange and beautiful things that turn up out there: ringed Earth-like worlds, moons glowing with volcanism from the giant they orbit, gas giants tinged green with life.

## Scope

This is not a quick trip. An expedition like this likely means weeks or months of game time, the survey of thousands of star systems, and several hundreds of thousands of light-years of travel crisscrossing the galaxy to venture into the farthest reaches — far enough that a single navigational mistake can strand a ship with no help for hundreds of jumps in any direction.

## What's in this repository

This toolkit watches the game's own session journals as the expedition unfolds, extracts the discoveries, jumps, distances, and earnings into a local database, and produces a summary for each leg of the journey — plus a running tally of the rare and notable finds along the way. (See the sections below for setup and usage.)

---

# Elite Dangerous Expedition Tracker — "Galactic Six Points"

A real-time journal watcher for the **Galactic Six Points** expedition.  
Reads ED journal files as you play, stores everything in a local SQLite database,
and generates per-leg Excel + CSV summaries automatically when you reach a waypoint.

---

## Contents

| Path | What it is |
|---|---|
| `tracker.pyw` | Main entry point (tray + CLI + validate modes) |
| `engine/` | Core engine — fully testable without the tray |
| `tray/app.py` | Thin pystray shell over the engine |
| `config.toml` | All user-editable settings |
| `requirements.txt` | Pinned Python dependencies |
| `make_shortcut.py` | Creates a Desktop .lnk shortcut |
| `tests/` | pytest suite (unit + full-expedition validation) |
| `output/` | Generated leg exports land here (auto-created) |
| `tracker.db` | SQLite database (auto-created) |
| `tracker.log` | Rotating log (auto-created) |

---

## Install

1. **Python 3.11+** required (uses `tomllib` from stdlib).

2. Clone / copy this folder anywhere — e.g. `C:\Tools\expedition-tracker\`.

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Edit `config.toml` if your ED journals are not in the default Saved Games path,
   or if you want exports somewhere specific.

---

## Create the Desktop shortcut

Run once (needs Pillow, which is already in requirements):

```
python make_shortcut.py
```

This creates `%USERPROFILE%\Desktop\ED Tracker.lnk` pointing to
`pythonw.exe tracker.pyw` with a compass-rose icon and the correct working directory.

---

## How to use

### Session start

Double-click the Desktop shortcut (or run `pythonw tracker.pyw`).
A compass-rose icon appears in the system tray.  The tracker immediately:

1. Backfills all journal files from the `expedition_start_timestamp` cutoff.
2. Starts live-tailing the active journal file.
3. Detects new journal files if the game restarts mid-session.

### During a session

- **Hover** over the tray icon to see current leg / jump count / distance / last system.
- Right-click for the full menu:

| Menu item | What it does |
|---|---|
| **Status** | Shows current leg stats as a balloon notification |
| **Close current leg & export now** | Manually closes the active leg, exports it to xlsx + CSVs, and opens a new leg |
| **Stop & exit** | Flushes the DB, stops the watcher, removes the tray icon |

### Session end

Right-click → **Stop & exit**.  Never just kill the process — this ensures the
DB is cleanly closed and no partial writes are left.

---

## How legs work

| Trigger | Effect |
|---|---|
| Expedition start timestamp | Leg 1 opens automatically |
| FSDJump to an **unvisited** Galactic Six Points waypoint | Current leg closes (named after that waypoint), next leg opens |
| FSDJump to `Parrot's Head Sector EL-Y d70` | Final leg closes, expedition marked **complete** |
| Manual "Close current leg & export now" | Closes active leg with an ordinal name, opens the next |

**Waypoints are unordered** — visit them in any sequence.  
**Revisiting** a waypoint you've already reached does nothing.  
**Carrier jumps** are tracked separately and never trigger a leg change.

### The Galactic Six Points

| Label | System |
|---|---|
| Nadir | HD 6428 |
| Zenith | HIP 58832 |
| West | Sphiesi HX-L d7-0 |
| East | Ood Fleau ZJ-I d9-0 |
| South | Lyed YJ-I d9-0 |
| North | Oevasy SG-Y d0 |

Final destination: **Parrot's Head Sector EL-Y d70**

---

## Editing waypoints

Open `config.toml` and edit the `[[waypoints]]` blocks.  System names are
matched **case-insensitively** against `FSDJump.StarSystem`, so capitalization
doesn't matter.  Restart the tracker for changes to take effect.

---

## CLI / headless mode

Useful for debugging or running on a server without a display:

```
python tracker.pyw --cli
```

Prints events to stdout and writes the same rotating `tracker.log`.
Press Ctrl-C to stop cleanly.

---

## Validation

The validation system has two layers that require **no manual editing** as the expedition grows.

### Invariants

Always-true structural assertions computed fresh from the current journals every run.
Never need updating.  Examples:
- No event has a timestamp before the expedition cutoff.
- `systems_visited <= commander_jumps`
- `distinct_bodies_scanned <= raw_scan_events` (dedup ≤ raw)
- `first_discovered ≤ bodies_scanned`
- **Anchor**: exactly 126 FSDJumps from the cutoff to the first arrival at Phloinn HC-J d10-1.

### Snapshot baseline (`validation_baseline.json`)

A regenerable JSON file that captures current cumulative metrics.
`--validate` checks that every metric is **≥ the baseline** — a decrease is a real
regression.  Growth is expected and always passes.

### Workflow after each play session

```
python tracker.pyw --validate    # catches any regression / parser bug
# eyeball the numbers look right
python tracker.pyw --snapshot    # advance the baseline to current state
```

No code edits required.

### Creating the baseline for the first time

```
python tracker.pyw --snapshot
```

### Running validate

```
python tracker.pyw --validate
```

```
=== Invariant Checks ===
  [PASS] No pre-cutoff events in DB
  [PASS] systems_visited <= commander_jumps
  [PASS] bodies_scanned: distinct <= raw
  ...
  [PASS] Anchor: 126 FSDJumps to first arrival at Phloinn HC-J d10-1
  [PASS] Rare finds: jumponium rule (all bodies) >= 66
  [PASS] Rare finds: geo signal counts are only 2 or 3
  [PASS] Rare finds: NSP alert count == 0 on current journals (UNVALIDATED rule)
  All 14 invariants passed.

=== Baseline Comparison ===
  [OK]   commander_jumps   baseline=215  current=220  (+5)
  [OK]   distance_ly       baseline=15149.91  current=15500.12  (+350.21)
  ...
  All 21 metrics meet or exceed baseline.

PASSED.
```

Exit code 0 = all checks pass.  Exit code 1 = invariant failure or metric regression.

### Running as pytest

```
pytest tests/test_validate.py -v
```

Skipped automatically when journal files are not present.  The `expedition` fixture
is module-scoped so journals are ingested only once for the full class.

---

## Output files

Each leg export lands in `output/` (configurable in `config.toml`):

```
output/
  Leg01_Nadir_2026-06-03.xlsx
  Leg01_Nadir_2026-06-03_systems.csv
  Leg01_Nadir_2026-06-03_bodies.csv
  Leg01_Nadir_2026-06-03_terraformable.csv   ← terraformable + ELW/WW/AW
  Leg01_Nadir_2026-06-03_body_classes.csv    ← per-class count breakdown
  Leg01_Nadir_2026-06-03_organics.csv
  Leg01_Nadir_2026-06-03_codex.csv
  Leg01_Nadir_2026-06-03_rare_finds.csv  ← notable first discoveries
  Leg01_Nadir_2026-06-03_sales.csv
  Leg02_Zenith_2026-06-05.xlsx
  ...
  master_rollup.xlsx         ← regenerated on every leg close
```

The xlsx has sheets: **Summary**, Systems, Bodies, Terraformable & Notable,
Body Classes, Organics, Codex Firsts, Rare Finds, Sales.

### Metric definitions

| Metric | Definition |
|---|---|
| **Bodies scanned** | Distinct `(system, body_id)` pairs from `Scan` events. A body revisited in a later session counts once. |
| **First-discovered** | Distinct bodies where `WasDiscovered == false` in the first `Scan` event. |
| **Surface-mapped** | Distinct bodies with a `SAAScanComplete` event. |
| **Terraformable bodies** | Distinct bodies where `TerraformState` is non-empty, **regardless of planet class**. This is the primary high-value exploration metric. |
| **Body-class counts** | Independent per-`PlanetClass` tallies (Water world, Earthlike body, …) and per-`StarType` tallies (G, M, K, …). Reported separately from terraformable so a terraformable HMC body is counted in both. |
| **Organic variants** | Distinct `Variant_Localised` strings where `ScanOrganic.ScanType == Analyse` (stage 3). |

The `master_rollup.xlsx` aggregates all legs with a per-leg breakdown table and
a whole-expedition summary sheet.

---

## Rare / notable first-discovery detector

The tracker evaluates every body scan against a configurable ruleset and
flags structurally notable objects.  Results appear in a **Rare Finds** sheet
in each per-leg xlsx, a matching `.csv`, and a rollup breakdown in
`master_rollup.xlsx`.

### Design note — why not edastro's GEC "rare" list?

edastro's GEC "rare" catalogue lists already-discovered community POIs.
You cannot be the **first discoverer** of something already on that list, so
it is the wrong source for this feature.  Rarity here is **structural**: it is
computed deterministically from journal fields (`Scan`, `FSSBodySignals`),
not by matching external instances.

### Ruleset

All rules are individually toggle-able in `config.toml` under `[rarity]`.
Setting `flag_only_first_discoveries = false` evaluates all scanned bodies
and records the WasDiscovered/WasMapped/WasFootFalled flags alongside.

| # | Rule tag | Trigger |
|---|---|---|
| 1 | `ringed_habitable` | ELW / WW / AW with rings present |
| 2 | `habitable_moon` | ELW / WW / AW whose first Parent is a Planet |
| 3 | `life_bearing_gg` | Gas giant with water-based or ammonia-based life; marked ringed if applicable |
| 4 | `very_small` | Radius < `very_small_radius_m` (default 300 km) |
| 5 | `tidal_moon` | Orbits a planet (`Parents[0]` is Planet) **and** Volcanism non-empty |
| 6 | `jumponium` | Geological signals ≥ `geo_signal_threshold` (default 3) **and** Volcanism non-empty **and** ≥ 1 material in the jumponium set |
| 7 | `exotic_star` | StarType in configurable set: N, H, D\*, W\*, AeBe |
| 8 | `high_gravity` | Landable body with SurfaceGravity > `high_gravity_g` × 9.80665 m/s² (default 3 g) |
| 9 | `fast_rotator` | `|RotationPeriod|` < `fast_rotator_hours` × 3600 s (default 3 h), excluding tidal-lock |
| 10 | `ggg_candidate` | Gas giant in `ggg_planet_classes` with non-empty `AtmosphereComposition` — **best-effort heuristic, verify visually; will have false positives** |
| 11 | `nsp_alert` | ⚠️ **UNVALIDATED** — system-level alert for Notable Stellar Phenomena via CodexEntry category `$Codex_Category_StellarPhenomena;` or non-mundane `FSSSignalDiscovered` signal. Zero NSP events exist in the current journals; rule produces 0 matches today. Validate on the next live NSP encounter. |

### Jumponium set (configurable)

`Carbon`, `Vanadium`, `Germanium`, `Arsenic`, `Niobium`, `Yttrium`, `Polonium`.
Materials are matched case-insensitively against the journal `Name_Localised` field.
The `trigger_details` column records the count and names found (e.g., `jumponium: 5 — arsenic,carbon,germanium,niobium,yttrium`).

### Data joins

- **Materials** and **Volcanism** come from the `Scan` event directly.
- **Geological signal count** comes from `FSSBodySignals` events (stored in the `body_signals` table),
  matched by `(system, body_name)`.  The rarity pass runs **after** all events are ingested
  so the geo count is always available regardless of event ordering in the journal.

### Configurable thresholds (all in `[rarity]` of `config.toml`)

| Key | Default | Meaning |
|---|---|---|
| `flag_only_first_discoveries` | `true` | Only flag bodies where `WasDiscovered == false` |
| `very_small_radius_m` | `300000` | 300 km in metres |
| `high_gravity_g` | `3.0` | Surface gravity threshold in g |
| `fast_rotator_hours` | `3.0` | Rotation period threshold in hours |
| `geo_signal_threshold` | `3` | Minimum geological signals for jumponium rule |
| `jumponium_materials` | (list) | Material names to check for jumponium synthesis |
| `exotic_star_types` | (list) | Exact StarType codes considered exotic |
| `ggg_planet_classes` | (list) | PlanetClass values considered GGG candidates |
| `nsp_mundane_signal_types` | (list) | FSSSignalDiscovered signal types **not** flagged as NSP |

---

## Parsing quirks

- **Cutoff filter**: every event with `timestamp < 2026-06-03T00:39:41Z` is silently
  discarded.  This excludes the 1–2 June shakedown loop (~21 hops) AND the carrier
  moves before the expedition proper.

- **CarrierJump vs FSDJump**: `CarrierJump` events go into a separate table and are
  excluded from jump/distance totals.  The carrier can move independently; only
  commander FSD jumps count.

- **Organic scans — 3 stages**: `ScanOrganic` fires for `Log` (approach), `Sample`
  (first sample), and `Analyse` (complete).  All 3 rows are stored.  The "distinct
  organic variants" metric counts only stage-3 (Analyse) rows with a non-null variant.

- **Carto sales — scan-vs-sale timing**: `MultiSellExplorationData` fires when you
  sell at a station.  Credits are attributed to the leg active *at sale time*, not
  when the systems were scanned.

- **Idempotent ingestion**: every event is hashed (SHA-256 of normalised JSON) and
  stored in `events_raw` with a UNIQUE constraint.  Reprocessing the same file is
  safe.  Byte offsets are also tracked per file so backfill skips already-processed
  content on restart.

- **Continued journals**: when a journal file reaches ~15 MB the game creates a new
  file and emits a `Continued` event.  The tracker handles this naturally by
  processing all files in chronological order with hash deduplication.

---

## Running unit tests

```
pytest tests/ -v --ignore=tests/test_validate.py
```

The unit tests use in-memory databases and synthetic journal lines — no real
journal files needed.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tray icon doesn't appear | Run `python tracker.pyw --cli` to see error output |
| Wrong jump counts | Check `tracker.log` — look for "Skipping" or "Error handling" lines |
| Waypoint not auto-closing | Confirm exact `StarSystem` string in-game; update `config.toml` |
| `tomllib` import error | You're running Python < 3.11; upgrade or `pip install tomli` |
| Shortcut won't launch | Re-run `make_shortcut.py`; check pythonw.exe path in the output |
