# Changelog

## 1.1.1 - 2026-08-01

- **Fixes a crash on startup in 1.1.0.** `run.py` used the User-Agent constant
  without importing it, so the container died immediately with
  `NameError: name 'USER_AGENT' is not defined`. 1.1.0 could not start at all;
  update straight to 1.1.1.
- Added `tests/test_imports.py`, which disassembles every function and checks
  the globals it references actually resolve. It reproduces the 1.1.0 failure
  and would have caught it. It also pins the Dockerfile `COPY` list to the
  modules on disk, and the three hand-maintained version strings to each other.

## 1.1.0 - 2026-07-30

- Replaced `use_openmeteo` (bool) with `openmeteo_mode`: `always` (default,
  unchanged behaviour), `fallback_only` (only used when the MeteoSwiss gust is
  unavailable, so it can no longer override a valid MeteoSwiss reading), or
  `never`. If you had `use_openmeteo: false`, set `openmeteo_mode: never` to
  keep that behaviour -- the old option is no longer read.
- `lookahead_hours` default changed from `2` to `1`, so the awning/blinds
  react closer to the affected hour instead of up to 2 hours ahead of it.
- The configured postcode is now checked once at startup: a few Swiss
  postcodes carry no app data at all, which silently disabled the forecast
  fallback. You now get a warning in the log and on the `Last warning` sensor
  instead of only finding out during an outage.
- App forecast failures now distinguish "unreachable" from "this postcode has
  no data" in the log, rather than both reading as a transient blip.
- Fixed the version shown in Home Assistant's device info, which was stuck at
  1.0.0. The version and the User-Agent sent to MeteoSwiss/Open-Meteo now
  derive from one constant instead of being hardcoded in five places.
- Documentation: the example automation now also triggers on `Rain within
  10 min` and `Wind high` turning on. Without them, rain arriving while
  already retracted for wind produces no change to `Shade recommendation`, so
  an awning manually put back out by hand would stay out through the rain.
  **Worth applying to your own automation** -- no add-on change is involved.

## 1.0.1 - 2026-07-30

- Lead with Add-on Store installation; manual `/addons` copy moved to an
  appendix.
- `DOCS.md` is now a symlink to `README.md`.
- Added a repo-root README indexing the add-ons in this repository.

## 1.0.0 - 2026-07-30

- Initial release: awning/blind automation from MeteoSwiss radar, MeteoSwiss
  forecast, and Open-Meteo gust data, published as 20 MQTT entities.
