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
│   ├── version.py           VERSION + USER_AGENT; dependency-free on purpose
│   ├── translations/en.yaml option names + descriptions for the config screen
│   ├── README.md            shown on the install page
│   └── DOCS.md              symlink → README.md; shown in the Documentation tab
├── tests/                   conftest + test_logic, test_events, test_events_entity,
│                            test_radar, test_imports, test_options, test_forecast,
│                            test_device_link
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
discovery. Slug `swiss_meteo_shade`, version 1.2.3. Runs on the user's own HA OS
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
  the state dict, publishes MQTT discovery for 22 entities (20 sensors +
  2 `event` entities).
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
- **Some postcodes carry no data at all**: HTTP 200, but the `graph` object is
  absent entirely (verified 2026-07-31 against 1195 Dully; 1946 and 6006 are
  fine). `from_app` already degraded correctly, but silently — so
  `validate_plz` now runs once at startup and warns, and `from_app`
  distinguishes "unreachable" from "this postcode has no data" in the log.
  Don't auto-substitute a neighbouring postcode: the replacement can be data-
  less too, and it turns a clear config error into a silent wrong-location one.
- Array **lengths vary between requests** (`precipitation10m` was 138/139 one
  afternoon, 99 the next morning), so never assume a fixed length; always
  index by time from `graph['start']`.

**Open-Meteo**
- Model `meteoswiss_icon_ch1`, field `wind_gusts_10m`, `forecast_days=2`,
  timezone UTC. Gust only.
- `openmeteo_mode` (`always` / `fallback_only` / `never`) controls when it's
  fetched at all. Under `always` and `fallback_only` (when it does fetch), it
  joins the gust set via `max()` — it can only raise caution, never lower it.
  See the 2026-07-30 incident below for why `fallback_only` exists.

## Decisions worth not re-litigating

- **Forecast-only. No live wind sensor.** Station data lags ~10 min; it was
  deliberately dropped.
- Official forecast is primary; app is fallback (or primary if
  `prefer_app_forecast`). They are **never queried in parallel**, which is why
  one of the two per-source gust sensors always reads Unknown.
- **Fail-safe on gust:** retract if *every* gust source fails. A present-but-low
  gust is trusted.
- `lookahead_hours`' window is always "now − 1h" to "now + lookahead_hours" —
  it **always** includes the current hour no matter how small the setting is.
  Shrinking it doesn't filter a bad *current*-hour forecast; it only reduces
  how many hours *before* a forecast hour the system starts reacting to it.
- **Fail-open on radar by default** (`radar_fail_safe: false`), configurable.
- **No app-forecast fallback for the radar.** Investigated 2026-07-30/31 and
  rejected against live data; `tools/precip_probe.py` re-runs the test. The
  app does expose `precipitation10m` / `precipitation1h`, anchored to
  `graph['start']` like the other arrays, in **mm accumulated per bucket**
  (x6 for a 10-min bucket to compare with the radar's mm/h -- calibration was
  good: app 0.7 mm/10min vs radar 3.61 mm/h). But it is not trustworthy as a
  safety input: at Wolfenschiessen `precipitation10m` read **0.0 while the
  radar saw 9.35 mm/h** (a fast convective cell, 9.35 -> 0.11 mm/h in one
  frame -- exactly the case the awning cares about), and the next morning
  `precipitation10m` tracked widespread rain correctly while
  `precipitation1h` read 0.0 for that same hour. Each series missed a
  radar-confirmed event in one of two tests, so there is no single series to
  code against. Postcode resolution is also structurally wrong for convective
  showers. `radar_fail_safe` stays the answer: it is honest about not knowing,
  where this would have been confidently wrong.
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
- **`event` entities (1.2.0) are the automation surface; the `last_error` /
  `last_warning` sensors are kept for display and for existing automations.**
  The sensors put the burden of "is this timestamp actually new?" on a
  hand-written template condition, which a real user simply left out — the
  symptom (re-notified about an old error on every restart) is not obvious.
  `event` entities fire once per new event and carry `message` as an
  attribute, so the automation is a bare trigger.
  Firing rules live in `shade._publish_new_events` and are locked by
  `tests/test_events_entity.py`: published **non-retained** (HA discards
  replayed retained messages for this platform anyway), skipped when the
  timestamp is unchanged, and the **first publish after a start only seeds the
  tracker** — events.py restores the last error/warning from `/data`, and
  firing those would re-announce them on every restart.
  The sensors were NOT removed: MQTT discovery configs are retained, so
  removal needs an empty retained payload per entity, and it would break
  automations already in the wild.
- Motion estimation is **gated on real echo** — see below.
- **`recommendation` deliberately collapses wind and rain into one `backup`
  value**, so it does NOT change when rain is added to an existing wind
  retract. That is not an oversight: splitting it (e.g. `backup_wind` vs
  `backup_rain`) would break every existing automation's `to:` list for no
  gain. The consequence — an awning manually re-extended against a `backup`
  recommendation would sit through arriving rain, because no state transition
  re-fires the automation — is handled in the **automation**, by also
  triggering on `Rain within 10 min` / `Wind high` turning on. Documented under
  "Manual overrides" in README. A `retract_reasons` sensor was considered as
  the alternative and deferred: it adds a 21st entity to save two trigger
  lines, and entities are far easier to add than to remove (see below).
- **MQTT stays; getting rid of it means writing an integration.** Only code
  running inside Home Assistant can register real entities. An add-on is a
  separate container, so its only channels are MQTT discovery, the REST API,
  or WebSocket — and the REST route (`POST /api/states/...`) creates states
  that are NOT in the entity registry: they vanish on restart, can't be
  renamed, assigned to an area, or grouped into a device. Strictly worse.
  Evaluated 2026-08-01. The alternatives are a full `custom_components`
  rewrite (HACS; numpy and h5py both ship musllinux wheels now, so deps are no
  longer the blocker — but the radar's 30 MB HDF5 parse and FFT would need an
  executor, container isolation is lost, and the test suite is rebuilt), or an
  add-on plus a thin integration polling it over HTTP. Both change entity
  `unique_id`s, so without first clearing the retained MQTT discovery configs
  Home Assistant appends `_2` and every existing automation silently points at
  a dead entity. Chosen instead: `configuration_url` on the device, deep-
  linking to the add-on page (`homeassistant://hassio/addon/<slug>/config`).
  The slug must come from `/addons/self/info` at runtime — a repository
  install is hash-prefixed and a local one is `local_`, so config.yaml's slug
  is the wrong value. That endpoint needs no `hassio_api` permission.
  **The UI route moved in 2026.2** with the Apps rename: `/hassio/addon/<slug>
  /config` became `/config/app/<slug>/config`. 1.2.2 shipped the old one and
  landed on a dead page; 1.2.3 picks the route from the Core version read via
  `/info` (also permission-free). The trap is that the *filesystem* path
  `/addons` did NOT change, so "paths are unchanged" reads as true when the
  route it actually needs has moved. Same applies to the CLI: `ha addons` is
  now `ha apps`.
  **The reverse link (app page → device) is not achievable** and shouldn't be
  attempted: a device's URL is `/config/devices/device/<registry-uuid>`, the
  UUID is assigned by Home Assistant and only readable over the WebSocket
  device registry, which needs `homeassistant_api: true` — full Core API
  access for every user, to save one click. Documented in the README instead.
  **This prefix bites users too, not just code**: `ha apps info
  swiss_meteo_shade` fails with "doesn't exist", which reads like a broken
  install. The fastest way to read the real slug is the browser address bar on
  the app page (`…/hassio/addon/<slug>/config`); it doubles as the
  local-vs-repository test, since `local_` never updates from GitHub.
  Documented in the README so it is answered before it is asked.
- The add-on **never knows the actual cover position** and never commands the
  covers. Any "did the user override us?" logic would be guesswork; keep the
  decision one-way (publish a recommendation) and let the automation own it.

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

## The 2026-07-30 incident (Open-Meteo gust spike)

User reported `Gust (Open-Meteo ICON)` reading 72.7 km/h, stuck for 80 minutes,
while `Gust (MeteoSwiss official)` read 37.1 km/h and trees were visibly calm.

Live query of the Open-Meteo API for the user's exact coordinates confirmed the
app reported the number faithfully — no unit or coordinate-conversion bug
(both were re-verified against this incident: `_lv95_to_wgs84` in `shade.py`
and `wind_speed_unit=kmh` in `forecast.py` are correct). The raw hourly series
showed an isolated single-hour spike sandwiched between ~12-15 km/h neighbors,
and the same afternoon slot the following day spiked even harder (52 → 77 →
131 km/h) — both patterns consistent with ICON-CH1's convective/thunderstorm
gust diagnostic firing at this grid cell rather than a real, verifying wind
event. MeteoSwiss's more calibrated official forecast didn't show it.

This is not a bug to "fix" in the sense of the 07-28 incident — the model
output itself is (apparently) noisy at this grid cell, not the code. Two
config changes came out of the investigation:

- `use_openmeteo` (bool) → **`openmeteo_mode`** (`always` / `fallback_only` /
  `never`). `fallback_only` means Open-Meteo only fills in when the
  MeteoSwiss official/app gust is unavailable that cycle, so it can no longer
  override a valid MeteoSwiss reading with a single-source spike — at the
  cost of losing the "catches a local gust MeteoSwiss smooths away" benefit
  that was the reason Open-Meteo was added in the first place. `always`
  (unchanged default) keeps the original max-across-sources behaviour.
- `lookahead_hours` default **2 → 1**. This does *not* address the reading
  itself (the spike was for the current hour, always in-window regardless of
  setting) — it addresses how early the blinds started reacting to that hour
  *before* it arrived. See the "Decisions worth not re-litigating" note above.

## Current state

- 22 MQTT entities. Operational: recommendation, the three shade decisions, and
  the weather inputs. Diagnostic: source, radar age, radar/forecast health,
  reason, last error/warning, per-source gusts.
- 17 config options, all documented in `README.md` **and** `translations/en.yaml`.
- Tests: 25 logic + 5 events + 7 event-entity + 5 radar + 6 imports/manifest + 7 options + 5 forecast + 9 device-link, all passing; no external deps beyond
  numpy for the radar ones.
- `DOCS.md` is a symlink to `README.md` (install page and Documentation tab
  content, kept identical structurally rather than by manual duplication).
- `CHANGELOG.md` exists and is shown in the Supervisor's Changelog tab. Add an
  entry in the same commit as every version bump, grouped under **Added /
  Changed / Fixed / Notes** — a reader needs to tell a bug fix from a feature
  at a glance, and "Notes" covers things they must act on that are not code
  changes (the automation triggers in 1.1.0, for instance). Anything that can
  alter how the covers move gets said outright. Accumulate entries under
  `## Unreleased` between releases and rename that heading on the bump.
- **Bumping the version touches exactly three files**: `version.py` `VERSION`
  (the single source for all Python — the MQTT `sw_version` in HA's device
  info and the outbound `USER_AGENT` both derive from it), `config.yaml`
  `version:`, and the Dockerfile `LABEL io.hass.version`. The latter two are
  build manifests the Supervisor reads before Python runs, so they can't
  import `version.py`. This drifted twice before `version.py` existed (1.0.1
  shipped a 1.0.0 `sw_version`; 1.1.0 was caught pre-commit with a stale
  Dockerfile label) — grep for the old number after bumping.
- `version.py` is **dependency-free on purpose** so every module can import it
  without a circular import. It must stay in the Dockerfile `COPY` line;
  leaving it out breaks the container at import time, not at build time.

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
- **The Add-on Store does not see a new version immediately, and the way it
  fails looks like a bug.** The Supervisor refreshes add-on repositories on its
  own timer, not on push. Meanwhile the **Changelog tab reads the file live**
  while **"Latest version" comes from the Supervisor's cached parse of
  `config.yaml`** — so the dialog shows the new release's changelog next to
  "Installed 1.1.0 / Latest 1.1.0". Verified 2026-08-01: 1.1.1 was pushed at
  01:45 and the store showed exactly that contradiction for hours, then began
  offering the update on its own with **no repo change and no `ha store
  reload`** (`git diff 0bd58cf..HEAD -- config.yaml Dockerfile version.py` is
  empty across that window).
  **There are three independent staleness layers**, and they have each sent us
  chasing a non-existent bug: (1) the Supervisor's repository pull, on a timer;
  (2) its parsed copy of `config.yaml`; (3) the **frontend cache** — seen
  2026-08-01 with `ha apps info` reporting 1.2.2 while the web UI still said
  1.2.1. The Changelog tab reads the file live through all of this, which is
  why it keeps contradicting the version numbers next to it. **`ha apps info`
  is the authority** — it hits the Supervisor API directly, with no frontend
  in the way; a hard browser reload (Ctrl/Cmd+Shift+R) clears layer 3.
  **Do not "fix" this by bumping the version again** — that publishes a release
  nobody needed and does not make the store refresh any sooner. Either wait, or
  force it with `ha store reload` from the SSH add-on (the CLI verb for add-ons
  themselves is `ha apps …` on 2026.2+, renamed with the UI; `ha store` is
  unchanged). Only start suspecting the
  manifest if it never refreshes: `tests/test_imports.py` covers the config
  faults that genuinely make the Supervisor keep a cached copy.
- **Renaming an option needs a migration, not just a schema edit.** Options
  arrive from a file written by an *earlier* version, so a rename that looks
  clean on a fresh install can invert a returning user's setting —
  `use_openmeteo: false` became `openmeteo_mode: always`, the exact opposite.
  `run.apply_options` now carries the old key over; `tests/test_options.py`
  locks every upgrade path. Do the same for any future rename, and don't add
  the legacy key back to `config.yaml`'s schema (it would reappear in the
  Configuration UI).
- **`py_compile` is not a verification.** 1.1.0 shipped `run.py` referencing
  `USER_AGENT` without importing it: a missing import is a runtime NameError,
  not a syntax error, so compiling passed and every unit test passed (none
  exercise `run.py:main`). It crashed on the first real container start.
  `tests/test_imports.py` now disassembles every function and checks its
  global lookups resolve — run it after any import or refactor change. When
  you edit several files by `sed`/bulk replace, assume one was missed until a
  test proves otherwise.
- Several review rounds flagged bugs that didn't exist, because the reviewer
  worked from a stale snapshot. Verify against the current file before fixing.
  A 2026-08-01 review reported two bugs in `radar.py`, both false, and both
  from **misreading a dict literal as an assignment**: it quoted
  `result["speed_kmh"] = round(...),` and concluded the trailing comma built a
  1-tuple, when the real line is `"speed_kmh": round(...),` inside
  `result = { ... }` — an ordinary dict entry. It then wanted an `np is None`
  guard there, but `read_rzc` raises `RuntimeError("numpy and h5py are
  required...")` for every frame before that dict is built, so the guard would
  be unreachable. Runtime check settles both in seconds:
  `radar.evaluate()["speed_kmh"]` is a float. **Reproduce a claim before
  acting on it** — a plausible-sounding diff can be flatly wrong about what
  the file says.

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
  are still open. `repository.yaml` and `CHANGELOG.md` are done.
- HA polish: `device_class` on numeric sensors (`wind_speed`, `temperature`,
  `duration`), `enum` on the recommendation sensor, per-entity icons.
- A `de.yaml` translation (Swiss German: `ss` not `ß`, dot as decimal separator).

## Verification still owed

Things only the user's box can confirm: container build under Supervisor, live
MQTT delivery, a `prefer_app_forecast: true` run exercising the app path, and
the real generated entity IDs (Developer Tools → States) matching what the
documented automations assume.
