# Swiss Meteo Shade — project handoff

Context for continuing work in Claude Code. Written after a long series of
review/fix rounds; the point of this document is so you don't re-derive things
that were settled by probing live data, and don't "fix" things that are already
correct.

## Repository layout

```
<repo root>/                  = https://github.com/CHfish-ch/ha-addons (public)
├── repository.yaml          name/url/maintainer shown in the Add-on Store
├── swiss_meteo_shade/       THE ADD-ON — one top-level folder per add-on in this repo
│   ├── config.yaml          manifest, 17 options
│   ├── Dockerfile           COPY paths are relative to this dir; no change needed
│   ├── run.py               entrypoint
│   ├── shade.py             orchestrator + MQTT discovery
│   ├── forecast.py          gust/sun/temp, three sources, conditional-GET cache
│   ├── logic.py             pure decision function
│   ├── radar.py             RZC composite → rain nowcast
│   ├── events.py            error/warning recorder
│   ├── translations/en.yaml option names + descriptions for the config screen
│   ├── README.md            shown on the install page
│   └── DOCS.md              symlink → README.md; shown in the Documentation tab
├── tests/                   conftest.py + test_logic.py, test_events.py, test_radar.py
├── tools/                   *_probe.py, rain_forensics.py — never shipped
└── HANDOFF.md               this file
```

Distributed as a Home Assistant **Add-on Store repository** (Settings → Add-ons
→ Add-on Store → ⋮ → Repositories → paste the repo URL), not through HACS —
HACS covers integrations/frontend, not Supervisor add-ons. The Supervisor scans
the repo's **top level** for folders containing `config.yaml`; `tests/`,
`tools/`, and `HANDOFF.md` have none, so they're ignored automatically and stay
out of the built Docker image (each add-on folder is its own build context).
A second add-on, if one is ever added, would be a new sibling top-level folder.

**The add-on directory name is load-bearing: it must equal the `slug` in
`config.yaml` (`swiss_meteo_shade`, underscores).** If they diverge the add-on
still installs and runs, but the Supervisor silently fails to load
`translations/en.yaml` and the config screen shows raw option keys. Don't rename
the `swiss_meteo_shade/` folder without also updating `config.yaml`'s `slug`.

Tests and tools import app modules (`logic`, `events`, `radar`), so the add-on
directory is put on `sys.path` two ways: `tests/conftest.py` covers pytest, and
each file carries the same inline shim so the standalone `__main__` runners work
too. Verified working when invoked from the repo root, from inside `tests/`, and
via pytest. Only `rain_forensics.py` among the tools imports app code; the
`*_probe.py` scripts hit the APIs directly and stand alone.

The test files import only what they need — `logic.py`, `forecast.py` and
`events.py` pull in neither numpy nor h5py, and `radar.py` guards both imports,
so no module stubbing is required anywhere. `test_radar.py` needs real numpy.

## What it is

A Home Assistant **add-on** (not an integration, not HACS-installable) that turns
MeteoSwiss open data into awning/blind recommendations published over MQTT
discovery. Slug `swiss_meteo_shade`, version 1.0.1. Runs on the user's own HA OS
box; Switzerland only.

Three signals feed one decision:

| Signal | Source | Resolution |
| --- | --- | --- |
| Rain now → +10 min | MeteoSwiss radar RZC composite | 1 km grid, 5 min |
| Gust outlook | MeteoSwiss local forecast | point (postcode/station), hourly |
| Gust outlook | Open-Meteo ICON-CH1 | 1–2 km grid, hourly |
| Sun outlook | MeteoSwiss local forecast sunshine | point, hourly |

Decision surface (`logic.py`, pure, no I/O):

```
sun_<output> = sunshine_minutes >= sun_min_<output>  AND temperature gate passes
wind_high    = hysteresis band around gust_limit / gust_release
retract      = wind_high OR rain OR (all gust sources failed)

awning_extend            = sun_awning      AND NOT retract
backup_blinds_close      = sun_backup      AND retract
independent_blinds_close = sun_independent
```

`awning_extend` and `backup_blinds_close` can never both be true. *Exactly* one
fires only when `sun_min_awning == sun_min_backup`; with different thresholds
there are conditions where neither fires (intended).

## What each module is responsible for

- `run.py` — options parsing (safe casters that warn and fall back rather than
  crashing the container), MQTT connect/loop, Supervisor credential refresh,
  SIGTERM/SIGINT, watchdog exit after repeated failures, hysteresis persistence
  to `/data`.
- `shade.py` — calls radar + forecast, applies the three sun thresholds, builds
  the state dict, publishes MQTT discovery for 20 entities.
- `forecast.py` — gust/sun/temp from official OGD + app fallback + Open-Meteo,
  plus the conditional-GET cache.
- `logic.py` — pure decision function, no I/O, fully unit-testable.
- `radar.py` — RZC composite → rain now/+5/+10 via Lagrangian persistence.
- `events.py` — records latest error/warning for two diagnostic sensors.

## Verified facts — probed against live endpoints, do NOT re-guess

These cost real effort to establish. Each was wrong-guessed at least once.

**Radar (`ch.meteoschweiz.ogd-radar-precip`)**
- Items `YYYYMMDD-ch`, assets lowercase `rzc...h5`, ~30 MB.
- Values are **already mm/h** (gain=1, offset=0, quantity=RATE). Nodata = NaN,
  mask with `isnan`.
- Grid 710×640 1 km cells, origin LV95 E2255000/N1480000, EPSG:2056.
- ODIM `UL_lat`/`UL_lon` **are present** and are the NW corner (all four corners
  verified to ~4 m). A reviewer doubted this; the doubt was unfounded.
- STAC listing lags ~10 min behind publication, hence `probe_newer` walking
  forward. HEAD returns 200 for existing objects and **403 (not 404)** for ones
  that don't exist yet — verified. A 403 in a manual `curl -sI` means "no such
  file", not "HEAD is blocked".

**Official forecast (`ch.meteoschweiz.ogd-local-forecasting`)**
- Items `YYYYMMDD-ch`. Today's item fills in progressively after ~00:07 UTC, so
  always pick the newest item that actually **has** the files you need.
- Each file is **32 MB, 1,232,313 rows**, LONG format
  `point_id;point_type_id;Date;<param>`.
- **The hour in the filename is the model RUN hour, not a per-hour slice.** Every
  file contains the full 219-hour (9-day) forecast as rows. Verified by counting
  distinct dates. There is no smaller per-hour asset — a bandwidth win was looked
  for and does not exist.
- `Date` is **YYYYMMDDHH in UTC** (verified via sunshine vs sunrise).
- Params: gust `fu3010h1` (1 s peak, km/h), sunshine `sre000h0`
  (**minutes per hour**, values 0–60 — confirmed by live values of 11/30/55),
  temp `tre200h0`.
- Encoding is **latin-1 only**. utf-8 *crashes* on byte 0xe9. Verified.
- `point_id` is **not globally unique** — 89 collisions in 5,629 points. 85 are
  cross-type (e.g. id 436 = Couvet type 1 *and* Cabane Arpitettaz type 3), so
  you must match on `(point_id, point_type_id)` together. The remaining 4 are
  exact-duplicate rows, so first-match is safe.
- Conditional GET works: `If-None-Match` → **304, 0 bytes**. Verified on both the
  forecast and radar buckets.

**App fallback (`app-prod-ws.meteoswiss-app.ch/v1/plzDetail`)**
- Undocumented. Fields `gustSpeed1h`, `sunshine1h`, `temperatureMean1h`, each 144
  hourly values.
- Anchored to `graph['start']` (local midnight), **not** `startLowResolution`.
- Missing-value sentinel is 32767.

**Open-Meteo**
- Model `meteoswiss_icon_ch1`, field `wind_gusts_10m`, `forecast_days=2`,
  timezone UTC. Gust only. Joined into the gust set via `max()` — it can only
  raise caution, never lower it.

## Decisions worth not re-litigating

- **Forecast-only. No live wind sensor.** Station data lags ~10 min; it was
  deliberately dropped.
- Official forecast is primary; app is fallback (or primary if
  `prefer_app_forecast`). They are **never queried in parallel**, which is why
  one of the two per-source gust sensors always reads Unknown.
- **Fail-safe on gust:** retract if *every* gust source fails. A present-but-low
  gust is trusted.
- **Fail-open on radar by default** (`radar_fail_safe: false`), configurable.
- Missing sunshine → `None` (unknown), never silently "not sunny".
- Hysteresis is keyed on `prev_wind_high`, **not** `prev_retract` — otherwise a
  rain cycle shifts the wind threshold. Persisted across restarts.
- Radar HDF5 is read **in memory** (`io.BytesIO`) — no disk writes, no SD wear.
- Forecast cache: conditional GET, plus a forced full refresh past
  `forecast_max_cache_minutes` on a clock **we** own (not the server's), plus a
  sha256 mismatch check that logs an error if the ETag lied. `0` means **never
  force**, not "always download".
- Event sensors: state is the timestamp of when the current message **first**
  appeared; repeats don't update it. Attributes come from a dedicated small
  topic. Both exist so a persistent condition notifies once, not every cycle.
- Motion estimation is **gated on real echo** — see below.

## The 2026-07-28 incident (most instructive bug)

On a cloudless afternoon the app reported `rain: true` for exactly one cycle.
Replaying the archive (`tools/rain_forensics.py`) showed 0/16 wet cells and a
maximum of 0.010 mm/h anywhere within 5 km — ten times below the 0.1 threshold.

The cause: **phase correlation on an empty field returns noise.** Two overlapping
frame triplets gave `+5.50/−4.50` and `−7.00/−3.00` — ~90 km/h in opposite
directions, which no real system does. That fabricated vector projected the
sample point ~14 km away, where 4 marginal cells sat at exactly 0.100 mm/h.

Fix: `estimate_motion` now requires `MOTION_MIN_ECHO_CELLS` (3) cells above
threshold in the correlation window before it will trust a vector; otherwise it
returns zero motion (persistence), which is also physically correct — no echo
nearby means nothing can arrive within the lead time. Locked by `test_radar.py`.

**Do not "fix" this by raising `radar_threshold_mmh`.** The threshold was never
the problem and 0.1 is correct.

## Current state

- 20 MQTT entities. Operational: recommendation, the three shade decisions, and
  the weather inputs. Diagnostic: source, radar age, radar/forecast health,
  reason, last error/warning, per-source gusts.
- 17 config options, all documented in `README.md` **and** `translations/en.yaml`.
- Tests: 25 logic + 5 events + 5 radar, all passing, no external deps beyond
  numpy for the radar ones.
- `DOCS.md` is a symlink to `README.md` (install page and Documentation tab
  content, kept identical structurally rather than by manual duplication).
- `CHANGELOG.md` exists and is shown in the Supervisor's Changelog tab. Bump
  `version` (in `config.yaml` and the Dockerfile `LABEL`) and add an entry here
  together, in the same commit, every time — see "Version currently lives in
  three places" below.

## Hard-won operational lessons

- **The add-on folder name must equal the slug** (see Repository layout). A
  folder named `swiss-meteo-shade` against slug `swiss_meteo_shade` installs and
  runs fine but the Supervisor won't load `translations/en.yaml`, and the config
  screen silently shows raw keys. This cost a long debug session — don't rename
  the `swiss_meteo_shade/` directory without also updating `config.yaml`'s `slug`.
- **Removing an entity from the code does not remove it from Home Assistant.**
  MQTT discovery configs are retained. Clear them by publishing an empty retained
  payload to `homeassistant/<domain>/sms_<slug>/config`. Uninstalling the add-on
  does *not* do this.
- Heredoc `str.replace` edits have silently failed more than once — reporting
  success while the file was unchanged. **Always re-verify with a behavioural
  test, not just a success message.**
- Several review rounds flagged bugs that didn't exist, because the reviewer
  worked from a stale snapshot. Verify against the current file before fixing.

## Running things

```bash
# tests (from the repo root)
python3 -m pytest tests/ -q            # or run any test file directly
python3 tests/test_logic.py            # standalone runner, no pytest needed

# forensics: explain a past rain=true blip from the radar archive
python3 tools/rain_forensics.py --at 2026-07-28T15:44 --east <E> --north <N>

# deploy: copy the add-on folder to the HA box, then Rebuild in the UI
#   /addons/swiss_meteo_shade/     (name must match the slug)
```

Only `numpy` is needed for the radar tests; the logic and events tests have no
external dependencies.

## Known open items (none blocking)

- **B3 dwell timer** — deferred by the user. Rain has no hysteresis, so a genuine
  passing shower can still flap the awning. Spec: hold `retract` for N minutes
  after the last trigger, persisted alongside `wind_high.json`. Deferred
  deliberately: worth seeing real-world behaviour first.
- Cache eviction (`_file_meta`/`_file_series` grow unbounded, tiny per entry).
- No overall cycle budget; pathological cycles could exceed the interval.
- Radar re-downloads two already-seen frames each cycle (a 3-entry cache would
  cut radar traffic by two-thirds).
- Packaging: `build.yaml` with `ARG BUILD_FROM` and CI running the test suites
  are still open. `repository.yaml` and `CHANGELOG.md` are done. Version still
  lives in three places (`config.yaml`, the Dockerfile `LABEL`, and the "What
  it is" section above) with nothing automated keeping them in sync — bump all
  three by hand together.
- HA polish: `device_class` on numeric sensors (`wind_speed`, `temperature`,
  `duration`), `enum` on the recommendation sensor, per-entity icons.
- A `de.yaml` translation (Swiss German: `ss` not `ß`, dot as decimal separator).

## Verification still owed

Things only the user's box can confirm: container build under Supervisor, live
MQTT delivery, a `prefer_app_forecast: true` run exercising the app path, and
the real generated entity IDs (Developer Tools → States) matching what the
documented automations assume.
