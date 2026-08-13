# Changelog

Entries are grouped as **Added** (new capability), **Changed** (existing
behaviour now differs), **Fixed** (a bug), and **Notes** (something worth
checking on your side — often not a code change at all). Anything that can
alter how your covers move is called out explicitly.

## 1.7.0 - 2026-08-14

### Fixed

- **⚠ The automation this README told you to build can leave the awning out in
  wind. You have to change it yourself — updating the add-on does not fix it.**
  *This affects how your covers move.*

  Every README example since 1.3.0 closes the awning on two triggers, `Rain
  within 10 min` and `Wind high`. Those two do **not** cover every hazard. The
  add-on retracts on three things:

  ```
  retract = wind_high OR rain OR (all gust sources failed)
  ```

  and the third has no component sensor. When every gust source fails, the
  add-on cannot vouch for wind safety and keeps the awning in — but `Wind high`
  stays **off** while it does so, deliberately: an *unknown* gust must not be
  reported as a *high* gust. So both of your triggers stay quiet, your
  automation never runs, and the awning sits out in precisely the situation the
  fail-safe exists for.

  **What to do:** replace those two triggers with one, and check any other
  automation or dashboard that keys off that pair.

  ```yaml
  - trigger: state
    entity_id: binary_sensor.swiss_meteo_shade_awning_unsafe
    to: "on"
  ```

  `Awning unsafe` is new in this release (below) and is true for all three
  hazards. The README examples have been updated. If you would rather not
  change anything, adding a third trigger on `Gust data` → *Problem* closes the
  same hole.

- **⚠ The documented sun gate could stop the awning closing at all.**
  *This affects how your covers move.* If you followed *"Only shade when the
  sun is actually on that window"*, you were told to add the facade gate to the
  automation's `conditions:`:

  ```yaml
  conditions:
    - {sun.sun elevation above 5}
  ```

  paired with a trigger on `elevation below 5`. Those fight each other. The
  trigger fires *because* the sun just dropped below 5 — and the condition,
  evaluated on that same run, now fails, so the automation stops before its
  actions and the awning is never closed. It stays out overnight. The warning
  box telling you to add the trigger made this **more** likely, not less.

  **What to do:** move that gate out of `conditions:` and into the `extend`
  branch of the `choose`, where failing it falls through to `default:` instead
  of aborting the automation. The README section now shows the branch in full.
  Worth checking tonight whether your awning is actually coming in.

- **Documentation that had gone stale.** The README still described the
  `Last error` / `Last warning` sensors renamed in 1.5.2, still claimed events
  are persisted to `/data` (removed in the same release), and still described
  `Solar elevation` as gating the awning after that gate was removed in 1.5.1.

### Added

- **`Awning unsafe`** — one entity that is true for **every** hazard, so a
  close automation cannot miss one. See the fix above for why this matters.

  It is a binary sensor rather than a fourth `Shade recommendation` value
  because it **crosses** the enum — true in `backup`, and in the hazardous half
  of `none`. As an enum value every automation would have to match a *set*, and
  adding a value later would silently break each of those conditions. Making it
  an entity also means **nothing existing changes meaning**: `none` still means
  what it always did.

  It does resolve the long-standing ambiguity in `none`, which conflated two
  opposite situations:

  | `Shade recommendation` | `Awning unsafe` | Meaning |
  | --- | --- | --- |
  | `none` | off | Nothing to shade, nothing dangerous — the awning **may stay out** |
  | `none` | on | Not sunny **and** a hazard — the awning **must** come in |

- **`Gust data`** (diagnostic, OK / Problem) — the hazardous half of `Forecast
  data` on its own. A missing sunshine forecast costs you shade; a missing gust
  forecast means wind safety cannot be vouched for, and until now no entity
  told the two apart.

### Notes

- **Why your awning comes in before sunset.** MeteoSwiss reports **0 sunshine
  minutes for the hour containing sunset — on every day, including cloudless
  ones**. Measured at Lucerne across a week of forecasts: the 19:00–20:00 hour
  gives 45 minutes and 103 W/m² at 10.6° of elevation, the 20:00–21:00 hour
  gives 0 and 8 W/m² at 1.1° — while that hour still holds 39 minutes of sun,
  due west, straight into the room.

  The data is right. Sunshine duration counts only minutes with a direct beam
  above 120 W/m² (WMO), and below ~5° of elevation the air path is long enough
  that a cloudless sun falls under that line. **No setting fixes it**: the
  figure is exactly `0`, so no `sun_min_awning` above zero helps, and the
  `irradiance` model reconstructs the beam from horizontal radiation, which is
  precisely where a 5° sun deposits nothing. Both models measure power; what
  you notice at that hour is geometry.

  The README now carries the automation pattern for it — let hazards close the
  awning and let `sun.sun` end the day, rather than closing on the add-on's sun
  verdict.

## 1.6.0 - 2026-08-12

### Changed

- **`radar_fail_safe` now defaults to `true`** — *this can change how your
  covers move.* It was `false` when it fired on every radar hiccup, where
  "a brief outage during clear weather shouldn't pull the awning in" was a
  fair call. It is now the last resort, reached only when the radar **and**
  all three precipitation series are unavailable at once, so that argument no
  longer applies while the asymmetry does: assuming dry wrongly leaves the
  awning out in rain, assuming rain wrongly costs an hour of shade.

  Set it back to `false` in the Configuration tab if you prefer the old
  behaviour. Worth knowing how narrow this really is: if *everything* is
  unreachable the entities go unavailable in Home Assistant instead, so your
  covers simply stay put and this setting never reaches them. It governs only
  the case where the radar and precipitation fail while gust and sunshine keep
  working — typically a transient blip lasting a single cycle.

## 1.5.2 - 2026-08-12

### Fixed

- **A sustained fault could notify you every few minutes instead of once.**
  Warnings are deduplicated by their text so an ongoing problem reports once —
  but several messages embedded a value that changes every cycle: the radar
  frame's age ticking up, or an exception whose Python repr contains a memory
  address. Each cycle therefore looked like a brand-new event, moving the
  `Last warning` timestamp and re-firing the `Warning` event entity. During a
  four-hour radar outage that is roughly fifty notifications for one fault.

  Varying values now go in a detail field written to the Log and never used
  for deduplication, so the same outage fires **once** while every occurrence
  stays in the Log with its own timestamp and a running count.

### Added

- **The forecast now stands in for the radar during an outage.** Previously a
  radar outage left `radar_fail_safe` — a constant — as the only rain signal,
  so a shower during those four hours would have gone unnoticed on the default
  setting. Rain is now taken from forecast precipitation instead, as the
  **maximum** across the official `rre150h0` and the MeteoSwiss app's two
  precipitation arrays: any one of them seeing rain is enough. That is the
  cautious direction, matching the existing gust rule — for an awning a false
  alarm costs an hour of shade while a miss costs a soaked awning, and the two
  app series are known to miss *different* events.

  `radar_fail_safe` still applies, but only as the last resort when the
  forecast is unavailable too.

  It is a real downgrade and is reported as one: `Rain source` reads `radar` /
  `forecast` / `assumed`, `Rain (forecast fallback)` shows the millimetres,
  and a warning is recorded. Hourly and point-resolution, it answers "is rain
  expected this hour" rather than "is rain arriving in ten minutes", so a
  convective shower the model missed can still slip through.

- **`Last error` / `Last warning` are now `Active error` / `Active warning`.**
  They read `"<what is wrong> since <when>"` and clear to `none` as soon as a
  cycle completes without the fault, so a resolved problem stops looking live.
  Previously they never cleared. Nothing is lost: the `event` entities have
  carried the permanent history since 1.2.0, so the sensors are free to be
  state. A fault returning after a clean spell counts as a new occurrence with
  a new *since* time.

  **Entity IDs change** — `sensor.…_last_error` becomes
  `sensor.…_active_error`. Update any dashboard card or automation that
  referenced them. Events are no longer persisted across restarts either,
  since reloading a fault from before a restart would claim one that may
  already be over.

### Notes

- If you saw repeated `radar frame … min old` warnings on 2026-08-12: the
  add-on was right and the outage was real. MeteoSwiss stopped publishing
  radar frames at 09:30 UTC, and every later frame returned 403. Rain was
  treated as unavailable and your `radar_fail_safe` setting applied, which is
  the intended behaviour — only the notification volume was wrong.
- `tools/precip_probe.py` scores the three series against the radar archive
  and accumulates across runs, so the false-alarm rates can be measured rather
  than assumed. It refuses a verdict below 100 hours including 10 wet ones.

## 1.5.1 - 2026-08-10

### Changed

- **Removed `awning_min_elevation`.** It gated the awning on the sun being
  high enough to shade the window, but the geometry says otherwise: a pitched
  awning's fabric hangs *below* the window head, so it always covers something
  and the gate computed to 0° — dead config for every retractable awning. Only
  a flat awning or fixed canopy sits high enough for it to matter, and that
  belongs in an automation condition on `sun.sun`, alongside the azimuth gate.
  The decision reverts to `awning_extend = sun AND NOT retract`.
- `min_solar_elevation` is documented accurately. It was described as a
  numerical floor below which refraction makes the angle unreliable; measuring
  against real forecast data shows that never bites — below ~2° of elevation
  MeteoSwiss already reports the radiation as 90–100% diffuse, so the direct
  beam is essentially zero and the setting cannot change a decision there. It
  is a **site horizon** control: raise it if a treeline, buildings or a slope
  block your low sun, and leave it at 0 if your horizon is clear.

### Added

- `docs/awning-geometry.html` — a self-contained interactive cross-section
  showing how much of a window an awning covers at a given sun elevation,
  including an awning set back under a balcony. Useful for writing that
  automation condition, and for seeing whether the sun at 47°N ever gets high
  enough in winter.

## 1.5.0 - 2026-08-09

### Added

- **`sun_model`: a second way to judge "sunny enough".** The existing
  `sunshine` model (minutes of sun per hour) stays the default and is
  unchanged. The new `irradiance` model computes the solar power actually
  arriving on the surface in W/m², from the MeteoSwiss global and diffuse
  radiation forecast, and compares it against `irradiance_min_shade` and
  `irradiance_min_independent` (default 250 W/m² — expect to tune this, as
  with `gust_limit_kmh`).

  It captures three things minutes-of-sunshine cannot: intensity (overcast
  ~100–200 W/m², clear sun 700–1000), diffuse light on an overcast day, and
  geometry — sun entering a room **peaks when the sun is low**, roughly
  885 W/m² on the window at 10° of elevation against 529 at 65°. All three
  outputs are judged on the window plane, because what matters is sun
  entering the room, not sun landing on a shading device.
- The backup blind now also takes over when the awning is **ineffective**, not
  only when it is unsafe. Previously it was tied to wind and rain alone, so a
  bright low evening sun left the awning correctly in and the blind pointlessly
  up, with the opening unshaded. Unchanged under the sunshine model.
- `awning_min_elevation` — an awning only shades while the sun is high enough;
  below that the beam passes underneath it. Compute yours as `atan(H / P)`
  where H is the height above the sill and P the projection (2.5 m high and
  3 m out = 40°). Default 35°. Blinds are not gated: they block sun at any
  height, which is why they are the tool for a low evening sun.
- Further irradiance options: `albedo` (ground reflectance — over snow this
  alone can add 200 W/m² to a blind), `min_solar_elevation` (raise it to match
  a real horizon of buildings or trees), and `irradiance_substeps`.
- Sensor `Irradiance (window)`, plus `Global radiation`, `Diffuse fraction`,
  `Solar elevation` and `Sun model` as diagnostics. All read Unknown under the
  `sunshine` model, since nothing is being computed.

### Fixed

- **A spurious "Rain within 10 min" on a completely dry cell.** Field motion is
  estimated by correlating consecutive radar frames, and the per-pair results
  were averaged without checking each one. A pair that saturated at exactly the
  120 km/h speed limit slipped past a `>` comparison on the boundary and
  dragged the average into a fabricated northward vector, which sampled a storm
  ~20 km away that was never approaching. Saturated pairs are now discarded
  before averaging. Observed 2026-08-09; the radar archive was replayed with
  `tools/rain_forensics.py` and the regression test reproduces the exact value.

### Notes

- Switching model changes **which thresholds are read**; the other set is
  ignored rather than combined.
- Radiation is only fetched when the irradiance model is selected, so the
  default costs nothing extra.
- If the radiation forecast is unavailable, the add-on **falls back to the
  sunshine model** for that cycle using your `sun_min_*` thresholds — an
  outage should not stop the shade working when a good sunshine forecast is in
  hand, and only the official source carries radiation, so this is a realistic
  failure to plan for. The switch is announced, never silent: `Sun model`
  reads `sunshine_fallback`, a warning lands on `Last warning`, and the
  `Warning` event entity fires. The `Irradiance` sensors go Unknown rather
  than holding a stale figure. If sunshine is missing too, the sun is
  genuinely unknown and the shade is kept in.
- **Coming from a pre-1.5.0 build?** Several irradiance options were renamed
  or merged during development and are not carried over. Check the
  Configuration tab after updating rather than assuming your old values
  survived.
- The irradiance figure assumes the surface faces the sun, making it an upper
  bound that holds for every window on the building. *Which* window the sun is
  actually on remains a question for your automation — see the sun-position
  guide in the README.

## 1.3.0 - 2026-08-01

First public release.

### Added

- Weather-driven awning and blind recommendations for Switzerland, from
  MeteoSwiss radar and forecast open data plus Open-Meteo ICON, published to
  Home Assistant over MQTT discovery as 22 entities.
- **`Shade recommendation`** (`extend` / `backup` / `none`) is the one entity
  to drive automations from, alongside the three individual decisions — awning
  extend, backup blinds close, independent blinds close.
- Rain from the radar composite at 1 km / 5 min, projected to +10 minutes.
  Gust and sunshine from the MeteoSwiss forecast, with the app feed as an
  automatic fallback and Open-Meteo ICON as an independent gust source
  (`openmeteo_mode`: `always`, `fallback_only`, or `never`).
- Safety behaviour: hysteresis around the gust limit, a fail-safe that keeps
  the awning in when every gust source fails, an optional minimum-temperature
  gate, and `radar_fail_safe` for treating a radar outage as rain.
- Diagnostics for when something looks wrong: which forecast source answered,
  radar age and health, a plain-language reason for the current decision,
  per-source gust values, and `Error` / `Warning` **`event` entities** that
  fire once per new event so a notification automation needs no timestamp
  logic.
- The MQTT device links back to this app's own configuration page.

### Notes

Two things live in *your* automation rather than in the app, and both are
documented with worked examples in the README:

- **Re-asserting on a new hazard.** Rain arriving while already retracted for
  wind does not change `Shade recommendation`, so an automation watching only
  that sensor will not re-fire — and an awning you had put back out by hand
  would stay out through the rain. Trigger on `Rain within 10 min` and
  `Wind high` turning on as well.
- **Shading only when the sun is on that window.** The app knows whether it is
  sunny and safe, not which way your window faces, so on a clear day it says
  `extend` from sunrise to sunset. Gate on Home Assistant's `sun.sun`
  attributes — `elevation` for an east-facing window, `azimuth` for a
  west-facing one — and add triggers on the same thresholds, or nothing
  re-evaluates when the sun moves off.
