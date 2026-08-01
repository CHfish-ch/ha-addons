# Changelog

Entries are grouped as **Added** (new capability), **Changed** (existing
behaviour now differs), **Fixed** (a bug), and **Notes** (something worth
checking on your side — often not a code change at all). Anything that can
alter how your covers move is called out explicitly.

## 1.2.3 - 2026-08-01

### Fixed

- The device link added in 1.2.2 pointed at `/hassio/addon/<slug>/config`,
  which is the pre-2026.2 route and lands on a dead page there. Home Assistant
  2026.2 moved it to `/config/app/<slug>/config` as part of the Apps rename.
  The route is now chosen from the running Core version, so both old and new
  installs get a working link.

### Notes

- Documented that the `ha` CLI verb followed the 2026.2 rename — it is
  `ha apps …`, not `ha addons …` — and that your app's slug is **not**
  `swiss_meteo_shade`. The Supervisor prefixes it (`local_…` for a manual
  copy, `<repo-hash>_…` from this repository), so bare-name CLI commands fail
  with "doesn't exist". The README now says where to read the real one.
- Explained why the app page has no link *to* its MQTT device, while the
  device links back to the app. The app can read its own slug, but a device's
  address is a registry ID only Home Assistant assigns, and fetching it would
  need full Core API access for every user — not worth spending on a
  hyperlink. Use Settings → Devices & Services → MQTT.

## 1.2.2 - 2026-08-01

### Added

- The MQTT device page now links straight to this add-on's configuration page.
  The add-on and its device are one thing but appear in two unrelated places in
  Home Assistant; the link makes the relationship visible. Costs no extra
  add-on permission, and is skipped silently if the Supervisor can't be
  reached.

### Changed

- Changelog entries are now grouped by kind, so a bug fix is distinguishable
  from a new feature at a glance.

## 1.2.1 - 2026-08-01

### Fixed

- The decision to download the ~30 MB temperature file was held in module
  state rather than passed per call. Once a cycle had run with the
  `min_temp_c` gate on, later cycles could keep fetching that file even with
  the gate off. It now follows the call.

### Changed

- When the official forecast gives up on a parameter, the log and the `Last
  warning` sensor say why — no asset in the item, a network failure, or an
  HTTP status — instead of only the downstream "primary forecast
  unavailable". Riding the cache through a brief blip stays silent, as before:
  that is the cache working, not a fault.

## 1.2.0 - 2026-08-01

### Added

- Two `event` entities, `Error` and `Warning`. They fire once per new event
  and carry the text in a `message` attribute, so a notification automation is
  just a trigger and an action — no timestamp comparison:

      triggers:
        - trigger: state
          entity_id: event.swiss_meteo_shade_error
      actions:
        - action: notify.persistent_notification
          data:
            message: "{{ trigger.to_state.attributes.message }}"

  They stay silent for repeats of the same condition, and never re-announce an
  event from before the add-on started — the case the sensor-based automation
  needed a careful template condition to avoid.

### Notes

- The `Last error` / `Last warning` **sensors are unchanged** and existing
  automations keep working. If you use them, check yours still has the
  `conditions:` block from the README: without it you get re-notified about an
  old error on every Home Assistant restart, add-on restart, and availability
  blip.

## 1.1.1 - 2026-08-01

### Fixed

- **A crash on startup in 1.1.0.** `run.py` used the User-Agent constant
  without importing it, so the container died immediately with
  `NameError: name 'USER_AGENT' is not defined`. 1.1.0 could not start at all;
  update straight to 1.1.1.
- Upgrading from a config saved before 1.1.0 no longer inverts your setting.
  Such a config still carries the old `use_openmeteo` bool, which is now
  carried over (`false` → `never`, `true` → `always`) with a warning.
  Previously a deliberate `use_openmeteo: false` silently became fully on.

### Added

- `tests/test_imports.py`, which disassembles every function and checks the
  globals it references actually resolve. It reproduces the 1.1.0 failure and
  would have caught it. It also pins the Dockerfile `COPY` list to the modules
  on disk, and the hand-maintained version strings to each other.

## 1.1.0 - 2026-07-30

### Changed

- `use_openmeteo` (bool) replaced by `openmeteo_mode`: `always` (default,
  unchanged behaviour), `fallback_only` (used only when the MeteoSwiss gust is
  unavailable, so it can no longer override a valid MeteoSwiss reading), or
  `never`. Since 1.1.1 an old `use_openmeteo` is carried over automatically.
- **Affects cover movement:** `lookahead_hours` default changed from `2` to
  `1`, so the awning and blinds react closer to the affected hour instead of
  up to 2 hours ahead of it.
- App forecast failures distinguish "unreachable" from "this postcode has no
  data" in the log, rather than both reading as a transient blip.

### Added

- The configured postcode is checked once at startup. A few Swiss postcodes
  carry no app data at all, which silently disabled the forecast fallback; you
  now get a warning in the log and on the `Last warning` sensor instead of
  only finding out during an outage.

### Fixed

- The version shown in Home Assistant's device info was stuck at 1.0.0. The
  version and the User-Agent sent to MeteoSwiss/Open-Meteo now derive from one
  constant instead of being hardcoded in five places.

### Notes

- **Worth applying to your own automation — no add-on change is involved.**
  The example automation now also triggers on `Rain within 10 min` and `Wind
  high` turning on. Without them, rain arriving while already retracted for
  wind produces no change to `Shade recommendation`, so an awning you had put
  back out by hand would stay out through the rain.

## 1.0.1 - 2026-07-30

### Added

- A repo-root README indexing the add-ons in this repository.

### Changed

- Installation now leads with the Add-on Store; the manual `/addons` copy
  moved to an appendix.
- `DOCS.md` is a symlink to `README.md`, so the install page and the
  Documentation tab cannot drift apart.

## 1.0.0 - 2026-07-30

### Added

- Initial release: awning/blind automation from MeteoSwiss radar, MeteoSwiss
  forecast, and Open-Meteo gust data, published as 20 MQTT entities.
