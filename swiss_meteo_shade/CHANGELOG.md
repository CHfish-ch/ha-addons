# Changelog

Entries are grouped as **Added** (new capability), **Changed** (existing
behaviour now differs), **Fixed** (a bug), and **Notes** (something worth
checking on your side — often not a code change at all). Anything that can
alter how your covers move is called out explicitly.

## 1.4.0 - 2026-08-09

### Added

- **`sun_model`: a second way to judge "sunny enough".** The existing
  `sunshine` model (minutes of sun per hour) stays the default and is
  unchanged. The new `irradiance` model computes the solar power actually
  arriving on the surface in W/m², from the MeteoSwiss global and diffuse
  radiation forecast, and compares it against `irradiance_min_awning` /
  `_backup` / `_independent` (default 250 W/m² — expect to tune this, as with
  `gust_limit_kmh`).

  It captures three things minutes-of-sunshine cannot: intensity (overcast
  ~100–200 W/m², clear sun 700–1000), diffuse light on an overcast day, and
  surface geometry — a low winter sun hits a *vertical* blind almost head-on
  while grazing a 45° awning, and vice versa in summer. Each output is judged
  on the plane it physically is.
- Options for the irradiance model: `albedo` (ground reflectance — over snow
  this alone can add 200 W/m² to a blind), `min_solar_elevation` (raise it to
  match a real horizon of buildings or trees), and `irradiance_substeps`.
- Sensors `Irradiance (awning, 45°)` and `Irradiance (blind, vertical)`, plus
  `Global radiation`, `Diffuse fraction` and `Sun model` as diagnostics. All
  read Unknown under the `sunshine` model, since nothing is being computed.

### Notes

- Switching model changes **which thresholds are read**; the other set is
  ignored rather than combined.
- Radiation is only fetched when the irradiance model is selected, so the
  default costs nothing extra.
- If the radiation forecast is unavailable the sun signal becomes **unknown**
  (shade kept in, `Forecast data` → *Problem*) rather than quietly reverting
  to the sunshine model — a silent model switch would make the entities
  impossible to interpret. Only the official source carries radiation, so
  there is no app fallback for this model.
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
