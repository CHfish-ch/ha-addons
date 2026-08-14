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
│   ├── config.yaml          manifest, 23 options
│   ├── Dockerfile           COPY paths are relative to this dir; no change needed
│   ├── run.py               entrypoint
│   ├── shade.py             orchestrator + MQTT discovery
│   ├── forecast.py          gust/sun/temp, three sources, conditional-GET cache
│   ├── logic.py             pure decision function
│   ├── radar.py             RZC composite → rain nowcast
│   ├── events.py            error/warning recorder
│   ├── version.py           VERSION + USER_AGENT; dependency-free on purpose
│   ├── irradiance.py        plane-of-array physics; pure, no astronomy/clock
│   ├── solar.py             the ONLY module importing astral
│   ├── translations/en.yaml option names + descriptions for the config screen
│   ├── README.md            shown on the install page
│   └── DOCS.md              symlink → README.md; shown in the Documentation tab
├── tests/                   conftest + test_logic, test_events, test_events_entity,
│                            test_radar, test_imports, test_options, test_forecast,
│                            test_device_link, test_irradiance, test_solar,
│                            test_sun_model, test_rain_fallback, test_entities,
│                            test_discovery_cleanup
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
so no module stubbing is required anywhere. `test_radar.py` needs real numpy,
and `test_solar.py` needs `astral` (`sudo apt install python3-astral`, or pip).
`irradiance.py` deliberately takes elevations as arguments and imports neither,
so the 18 acceptance cases run with a bare interpreter.

## What it is

A Home Assistant **add-on** (not an integration, not HACS-installable) that turns
MeteoSwiss open data into awning/blind recommendations published over MQTT
discovery. Slug `swiss_meteo_shade`, version 1.7.1. Runs on the user's own HA OS
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
  the state dict, publishes MQTT discovery for 31 entities (19 sensors,
  10 binary, 2 `event`).
- `forecast.py` — gust/sun/temp from official OGD + app fallback + Open-Meteo,
  plus the conditional-GET cache.
- `logic.py` — pure decision function, no I/O, fully unit-testable.
- `radar.py` — RZC composite → rain now/+5/+10 via Lagrangian persistence.
- `events.py` — records the ACTIVE error/warning for two diagnostic sensors;
  clears after a cycle completes without the fault, and is not persisted.

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

**`sre000h0` collapses to 0 before sunset — every day, clear or not**
(probed 2026-08-14, point 604500, seven forecast days). The hour *containing*
sunset reports **0 sunshine minutes** and GHI 4–9 W/m², while that hour still
holds ~39 minutes of sun above the horizon. Sampled figures, sunset ~20:37
local: 19:00–20:00 → 45 min / 103 W/m² at 10.6° elevation; 20:00–21:00 → 0 min
/ 8 W/m² at 1.1°.

The data is correct — sunshine duration counts only minutes with DNI ≥ 120 W/m²
(WMO), and below ~5° elevation the air mass puts a cloudless sun under that
line. Consequences, all confirmed:
- **No threshold fixes it.** The value is exactly 0, so no `sun_min_awning`
  above zero helps.
- **The irradiance model does not rescue it.** DNI is reconstructed from
  GHI − DHI on a *horizontal* plane, which is exactly where a 5° sun deposits
  nothing; the window POA computes to single digits. Both models measure
  radiative power, and the power really is small — the complaint is geometry
  (a low sun deep in the room), which neither model represents.
- On cloudier days it bites earlier: an hour reading 7–9 minutes is under the
  default threshold of 20 and pulls the awning in around 19:00.

This is **not fixable in the add-on** — it cannot know the facade. The README
carries the automation pattern (hazards close the awning, `sun.sun` ends the
day). Do not respond to a repeat report by adding a low-elevation option.
`tools/` has no probe for this; the script lived in the scratchpad.

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
- **`retract` is an entity (`Awning unsafe`), never a fourth `recommendation`
  value** (1.7.0). It was asked for as an enum state — "unconditional vs
  optional retract" — and a binary sensor is the right shape for three reasons,
  in descending order of importance:
  1. It **crosses** the enum. `retract` is true in `backup` *and* in the
     hazardous half of `none`, so as an enum member every consumer would write
     `state in ('backup', 'retract')`, and adding any future value would break
     each of those conditions silently.
  2. A new enum value is a **silent safety regression** for anyone installed:
     `recommendation == none` → close is safe today; carve `retract` out of
     `none` and that condition stops matching the hazard case. Adding an entity
     breaks nothing.
  3. `retract` was already in the published state JSON — only the discovery
     entry was missing, so it was one line in `BINARY`.

  The gap it closes is real and was in our own README: an automation triggering
  on `Rain within 10 min` + `Wind high` **cannot see the gust fail-safe**.
  `_wind_high` returns False when `gust_kmh is None` (deliberate — an unknown
  gust must not by itself force retract), so with every gust source down both
  component sensors stay off while `retract` is on. `test_entities.py` pins
  this; if that test ever passes vacuously the gap has moved.
- **`none` was ambiguous and is no longer.** It meant both "nothing to shade,
  nothing dangerous" and "not sunny AND a hazard". `Awning unsafe` is the
  discriminator; the README carries the 2×2 table. Do not "fix" this again by
  splitting the enum.
- **Sun gates belong on the `extend` BRANCH, never in the automation's
  `conditions:`** (1.7.0). The README shipped the wrong version for four
  releases. A blanket `elevation above 5` condition is evaluated on the very
  run that fires *because* elevation crossed below 5, so it fails, the
  automation aborts before its actions, and the awning is never closed — it
  stays out overnight. On the branch, failing the same test falls through to
  `default:` instead. The README's own "add matching triggers" callout made it
  worse by guaranteeing the doomed run happened.
- **README automations are ONE automation, refined section by section.** Step 7
  gives the base; later sections replace named branches of it. Do not add a
  second standalone `alias:` — a reader who pastes both gets two automations
  firing on the same events with opposite outcomes. This was nearly shipped in
  1.7.0 and caught on review.
- **Two different stores, and they are constantly confused** (1.7.1):
  - a **retained discovery config** lives on the *broker*. Publishing the
    current set never removes an old one — only an empty retained payload
    retracts it. `retire_orphan_discovery()` does this at startup by comparing
    what is retained against `discovery_topics()`. Never hardcode a list of
    retired slugs; the whole point is that it reads reality.
  - an **entity_id** lives in Home Assistant's *entity registry*, is assigned
    once at first discovery, and never follows a later name change. Only the
    user can fix it: MQTT device → ⋮ → **Recreate entity IDs**. The add-on
    cannot, and must not try (it would silently re-ID everyone's entities).

  `discovery_topics()` MUST stay in step with what `announce_discovery()`
  publishes or the sweep deletes live entities — `test_discovery_cleanup.py`
  pins that with a drift test, and the mutation of dropping one domain from it
  is caught.
- **The repository does not go back to the beginning.** First commit is
  2026-07-30; the add-on was built in Claude Chat before that and had been
  running on the user's box for weeks. So "grep the git history" does NOT
  prove an entity or option never existed — five entities were found in the
  field (`on_backup`, `irradiance_awning`, `irradiance_wall`, `last_error`,
  `last_warning`, under pre-repo slugs) with no trace in git. I asserted twice
  that something had "never existed" on the strength of a history search; the
  user was right both times.
- **Never assert an entity ID or a cover direction as fact.** Both are
  instance-specific and neither is observable from here; asserting them cost
  two wrong calls in one review (2026-08-14). Home Assistant assigns an
  entity_id **once** and keeps it while re-reading the friendly name from
  discovery on every reconnect, so the two disagree permanently: one instance
  carries `binary_sensor.swiss_meteo_shade_awning_retract` named *Awning
  unsafe*, although that name has never been published (checked across the
  whole git history of `shade.py`). And `cover.open_cover` extends some awnings
  and retracts others — a roller motor unrolls to extend, which the actuator
  reports as *closing*. The README now tells the reader to verify both in
  Developer Tools rather than trusting a printed ID or action.
- **"Not sunny" is not a reason to close the awning.** Only a hazard or the sun
  actually being down is. See the `sre000h0` verified fact above for why: the
  sun signal reaches zero up to 90 minutes before the sun does.
- `lookahead_hours`' window is always "now − 1h" to "now + lookahead_hours" —
  it **always** includes the current hour no matter how small the setting is.
  Shrinking it doesn't filter a bad *current*-hour forecast; it only reduces
  how many hours *before* a forecast hour the system starts reacting to it.
- **`radar_fail_safe` defaults to TRUE since 1.6.0**, and is now the LAST
  resort rather than the first. It was false when it fired on every radar
  hiccup; with the radar -> forecast -> fail-safe chain it is reached only
  when the radar and all three precipitation series fail together, so the old
  "a brief outage shouldn't pull the awning in" reasoning no longer applies
  while the asymmetry does.
  Measure before re-litigating: if EVERYTHING is unreachable, `healthy` goes
  False, the operational entities expire in HA, and the setting never reaches
  the covers at all -- they stay put. It governs only the narrow case where
  radar + precipitation fail while gust/sun still answer, usually one cycle.
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
- **An event message IS the dedup key -- never interpolate a moving value.**
  `events.warn/error(message, detail=...)`: `message` is stored, shown in HA
  and compared for repeats; `detail` goes to the Log only. A changing value in
  `message` silently breaks deduplication -- every cycle reads as a new event,
  the sensor timestamp moves and the `event` entity re-fires. Bit us
  2026-08-12: "radar frame 250.7 min old" ticked up each cycle, so a 4 h radar
  outage produced ~50 notifications for one fault. The same trap hides in
  exception reprs, which embed object addresses (`<...HTTPSConnection object
  at 0x7f...>`) -- three identical failures give three distinct strings. Put
  `type(exc).__name__` in the message and `str(exc)` in the detail. Locked by
  `test_events.py`.
- **Rain falls back radar -> forecast -> `radar_fail_safe`** (1.5.2). The
  2026-07-30 rejection of a forecast fallback used the WRONG criterion: it
  rejected the app feed for MISSING events, but a miss in a fallback degrades
  to "dry" -- exactly what `radar_fail_safe: false` already gives, so never
  worse than the baseline. The cost that matters is a FALSE ALARM.
  `forecast.precipitation_now` takes the **max** across official `rre150h0`
  plus the app's `precipitation10m` and `precipitation1h`. Max, not one
  source: the two app series miss DIFFERENT events (2026-07-30 and the morning
  after), so either alone is one-for-two while together they caught both. Same
  cautious-direction rule as the gust maximum, and justified by the asymmetry
  -- a false alarm costs an hour of shade, a miss costs a soaked awning. Only
  reached when the radar is down, so the exposure is bounded.
  **False-alarm rates are still unmeasured.** `tools/precip_probe.py` scores
  all three against the radar archive and accumulates into a ledger; it
  refuses a verdict under 100 hours / 10 wet. If it later shows a series
  crying wolf, drop that series -- do not drop the max.
  Note `radar._item_assets` returns `{name: href}` while `forecast`'s returns
  `{name: {"href":...}}` -- confusing the two silently yields "no radar".
- **One condition must emit exactly ONE warning.** Only the latest warning is
  stored, so TWO warnings alternating each cycle defeat deduplication just as
  surely as one message with a moving value in it -- each alternation reads as
  a new event and re-fires. Caught by `test_rain_fallback.py` before shipping:
  warning "radar is stale" and then "using the forecast" in the same cycle
  produced a fresh timestamp every cycle. Collapse to one warning and carry
  the reason in `detail`.
- **`Active error` / `Active warning` are STATE, not a log** (1.5.2). They read
  "<message> since <when>" and clear to "none" on the first cycle that does
  not re-report the fault. `events.start_cycle()` at the top of
  `shade.evaluate` is what makes "resolved" knowable -- the code is told when
  things go wrong, never when they come right, so "nothing reported this
  cycle" is the only available signal.
  Two separate jobs inside `start_cycle`, and conflating them costs a cycle of
  lag: `_this_cycle` gates `snapshot`/`state_text` so a clean cycle clears
  IMMEDIATELY, while `_last` is dropped only after a whole clean cycle so a
  recurrence gets a NEW `since` (and re-fires the event entity).
  This is only safe because the `event` entities carry the permanent history.
  Do not "restore" persistence to /data: reloading a fault from before a
  restart would claim one that may already be over.
  The state deliberately carries a fixed `since` rather than a duration -- a
  ticking value would rewrite the state every cycle and flood the recorder.
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
- **1.3.0 is the first public release; nothing before it is supported.** The
  add-on was never shared during 1.0.x–1.2.x, so no installation of those
  versions exists anywhere. Consequences, both deliberate:
  (a) `CHANGELOG.md` was collapsed from nine releases into one 1.3.0 entry —
  the Changelog tab was leading new users with fixes to bugs in versions they
  could never have run, which reads badly and is pure noise. The real history
  is in git and in this file, where it is useful to a maintainer rather than
  an installer.
  (b) The `use_openmeteo` → `openmeteo_mode` carry-over in `apply_options` was
  removed with its tests. It was also of uncertain reachability: the
  Supervisor validates stored options against the current schema *before*
  `run.py` runs, so if it strips unknown keys the migration could never fire.
  **Don't add migration code for a pre-1.3.0 config** — there is no such
  config. From 1.3.0 on, normal deprecation applies again.
  User-facing docs (README/DOCS) should describe the add-on **as it is now**,
  never as a diff against an earlier version. Past defects belong here.
- **`sun_model` picks the sun signal; the two models never blend.** `sunshine`
  (minutes/hour) is the default and unchanged; `irradiance` computes W/m2 on
  the surface from `gre000h0`/`ods000h0`. Only the selected model's thresholds
  are read. Radiation is fetched ONLY under the irradiance model -- it is
  another ~60 MB of files. A failed radiation fetch **falls back to the
  sunshine model** -- availability beats strictness, and only the official
  source carries radiation so the outage is realistic. Never silently, though:
  `Sun model` reports `sunshine_fallback`, a warning fires, and the Irradiance
  sensors go Unknown rather than holding a stale value. If sunshine is missing
  too the sun stays unknown -- the fallback must not invent a signal.
  **All three outputs read the VERTICAL (window) plane.** An early version
  judged the awning on a 45 deg plane -- the irradiance on its own fabric --
  which is not merely imprecise but ANTI-CORRELATED with what matters: sun
  entering a room peaks at LOW elevation (885 W/m2 vertical at 10 deg vs 529
  at 65), while a 45 deg surface peaks near 50. An awning is a shading device,
  not a collector; what lands on the fabric is irrelevant.
  **A geometric "can the awning even shade?" gate was built and then removed
  (1.5.1) -- do not rebuild it.** The idea was sound (an awning only shades
  while the sun clears its outer edge) but it never fires in practice: a
  pitched awning's fabric hangs BELOW the window head, so it always covers
  something and `atan((edge_height - head) / edge_distance)` comes out <= 0.
  Measured across real installations, only a FLAT awning or fixed canopy sits
  high enough (12-31 deg). It was dead config plus an `awning_effective`
  parameter, an `awning_usable` term and an extra reason branch. Anyone with a
  flat canopy should put the condition in their automation on `sun.sun`
  elevation, next to the azimuth gate. `docs/awning-geometry.html` computes
  the angles interactively, including a set-back awning under a balcony.
  Awning and backup remain a strict partition on `retract`, so they still
  share `IRRADIANCE_MIN_SHADE`; only the independent blinds keep a separate
  threshold. Do not re-split it -- unequal values reopen the "neither fires"
  gap for no gain.
  `min_solar_elevation` is a SITE HORIZON control, not a numerical floor. The
  "below 3 deg refraction makes it unreliable" claim was wrong: real
  MeteoSwiss data below ~2 deg elevation is 90-100% diffuse, so the beam is
  already zero and the setting cannot change a decision there (Lucerne 05:00,
  GHI 13 / DHI 12 -> 34 W/m2 on the window). 0 is safe with a clear horizon.
  **The irradiance figure is an UPPER BOUND** -- the surface is assumed to face
  the sun, so azimuth cancels out of the geometry. That is what keeps one
  number valid for every window; which window the sun is on stays an
  automation question (see the sun-position guide). Do not add a configurable
  surface azimuth without redoing the spec's acceptance cases, which all
  assume tracking.
- **Window orientation is deliberately NOT an add-on option.** The decision is
  "is it sunny and safe", which is identical for every window on the building;
  whether the sun is on a *particular* facade is per-cover, and users routinely
  have several facing different ways. Encoding one orientation in the options
  would serve one cover and mislead the rest, and `sun.sun` already carries
  `azimuth`/`elevation` for free. Documented as automation conditions in the
  README instead (1.3.0). If this is ever revisited, note the same trap that
  applies to the hazard triggers: sun *conditions* without matching *triggers*
  leave the awning out after the sun moves off, because the recommendation
  itself does not change as the sun crosses a threshold.
  Solar figures in that section were checked against computed positions for
  47°N (summer azimuth ~55→305, peak elevation ~66°; winter ~127→234, ~20°) —
  a first draft taken from 3-hourly samples understated both spans, so
  re-derive rather than trusting round numbers.
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

### The 2026-08-09 recurrence (same symptom, different cause)

Rain reported for one cycle on a cell with **zero echo within 5 km across every
frame**. The 07-28 guard was working and was not at fault -- the correlation
window held a real storm (hundreds of wet cells, peaks 43-118 mm/h), just none
of it near home, so `_has_echo` passed correctly.

The cause was **one saturated pair being averaged in**. Per-pair estimates from
`rain_forensics.py`:

    16:25->16:30   drow = -10.00     <- exactly MAX_MOTION_KMH (120 km/h)
    16:30->16:35   drow =  +1.00
                   mean = -4.50      <- the vector that did the damage

`MAX_MOTION_KMH / 12 == 10.0` and the check was `> max_cells`, so a value
landing precisely ON the cap survived the boundary, then dragged the mean to
-4.50. That projected the sample ~20 km south onto the storm the wide scan
found at +16 rows. Fix: reject a saturated pair BEFORE averaging, not just cap
the mean. Locked by `test_saturated_pair_is_rejected_before_averaging`, which
reproduces the -4.5 exactly.

**Investigated and deliberately NOT changed** (evidence in the same session):
- *Shrinking `MOTION_WINDOW_KM`.* Note it is `size // 2`, so 128 means +/-64 km,
  NOT +/-128. That is not arbitrary: max lead is 10 min + 4 anchor steps = 30
  min, and at the 120 km/h cap a system covers 60 km -- the window already
  matches the furthest cell the code samples. Shrinking it alone would leave us
  sampling cells we never tracked; it only makes sense together with
  MAX_MOTION_KMH / MAX_ANCHOR_STEPS.
- *Requiring per-pair agreement.* Measured on the same event, 3 of 4 triplets
  would have been rejected -> persistence, during active convection. Cost is
  losing lead time, not detection. `motion_spread_cells` is now recorded in the
  radar result so this can be decided from data later.
- *Border effects.* A non-issue for the window: the grid is 710x640 km and
  every Swiss extreme tested (Geneva, Chiasso, Scuol, Schaffhausen) is >=147 km
  from the nearest grid edge, so the +/-64 km window is always full, and
  `_window` clips with max/min anyway. 0 NaN cells observed. The real border
  caveat is radar QUALITY -- higher, more blocked beams far from the Swiss
  sites -- which no code change here can address.

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

- 31 MQTT entities. Operational: recommendation, the three shade decisions,
  `Awning unsafe`, and the weather inputs. Diagnostic: source, radar age,
  radar/forecast/gust health, reason, active error/warning, per-source gusts.
- 23 config options, all documented in `README.md` **and** `translations/en.yaml`.
- Tests: 140 across 14 files, all passing; no external deps beyond numpy for
  the radar ones and astral for the solar ones. `test_entities.py` also carries
  the **manifest guard**: every state-key declared in `BINARY`/`SENSORS` must
  exist in the dict `evaluate()` returns, because a typo there publishes an
  entity that reads Unknown forever and nothing else notices.
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
- **The import guard has two halves, and the second was added late.**
  `test_no_unresolved_global_names` catches a missing IMPORT, but not a call
  into a sibling module whose function was deleted: `events` resolves fine as
  a global and the missing name is an ATTRIBUTE on it. A leftover
  `events.set_persist_path(...)` in `run.py` survived a refactor exactly that
  way and would have crashed the container at boot.
  `test_no_calls_into_missing_module_attributes` pairs LOAD_GLOBAL with the
  following LOAD_ATTR to cover it. Note `_functions()` filters on
  `__module__`: without that it descends into astral's code via `solar.py`'s
  imports and reports its internals as our unresolved names.
  Keep `MODULES` in that file in step with the Dockerfile COPY list, or newer
  modules are simply not checked.
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
