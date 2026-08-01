# Changelog

Entries are grouped as **Added** (new capability), **Changed** (existing
behaviour now differs), **Fixed** (a bug), and **Notes** (something worth
checking on your side — often not a code change at all). Anything that can
alter how your covers move is called out explicitly.

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
