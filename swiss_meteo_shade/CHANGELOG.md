# Changelog

Entries are grouped as **Added** (new capability), **Changed** (existing
behaviour now differs), **Fixed** (a bug), and **Notes** (something worth
checking on your side — often not a code change at all). Anything that can
alter how your covers move is called out explicitly.

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
