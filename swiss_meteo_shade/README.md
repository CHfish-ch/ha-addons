# Swiss Meteo Shade

Weather-driven awning & blind automation for Home Assistant, built on MeteoSwiss
open data. **Switzerland only** — it relies on the Swiss radar grid, Swiss
coordinates (LV95), and MeteoSwiss forecasts.

It answers three questions every few minutes and publishes them as binary
sensors you can drive automations from:

- Should the **awning** be out? (sun expected, wind calm, no rain coming)
- Should the **backup blinds** close instead? (sunny, but wind or rain makes the
  awning unsafe)
- Should the **independent blinds** close? (sunny — they have no weather
  vulnerability, so wind and rain are ignored)

> **Names note.** Home Assistant 2026.2 renamed **Add-ons** to **Apps**. This
> guide says *app*; on older Home Assistant you will see *add-on* in the same
> places. The rename went deeper than the labels:
>
> | | before 2026.2 | 2026.2 and later |
> | --- | --- | --- |
> | CLI | `ha addons …` | `ha apps …` |
> | UI route | `/hassio/addon/<slug>/config` | `/config/app/<slug>/config` |
> | filesystem | `/addons` | `/addons` *(unchanged)* |
>
> The unchanged filesystem path is what makes the moved UI route easy to miss.
> Run `ha apps --help` if a subcommand is not recognised.

> **Finding your app's slug.** Several CLI commands want it, and it is **not**
> `swiss_meteo_shade` — the Supervisor prefixes it: `local_swiss_meteo_shade`
> for a manual `/addons` copy, or `<repo-hash>_swiss_meteo_shade` when
> installed from this repository. The quickest way to read it is the browser
> address bar on the app's own page:
> `…/hassio/addon/<this-is-your-slug>/config`. A `local_` prefix also tells you
> the copy will never update from GitHub — reinstall it from the repository if
> you want updates.

---

## How it decides

Three independent signals feed the logic:

| Signal | Source | Resolution |
| --- | --- | --- |
| **Rain** (now → +10 min) | MeteoSwiss radar RZC composite | 1 km grid, 5 min |
| **Gust** (outlook) | MeteoSwiss local forecast | point (postcode/station), hourly |
| **Gust** (outlook) | Open-Meteo ICON-CH1 | 1–2 km grid, hourly |
| **Sun** (outlook) | MeteoSwiss local forecast sunshine | point (postcode/station), hourly |

The two gust sources are combined by taking the **maximum** — the cautious
direction — so the finer-grained Open-Meteo grid can catch a local gust the
postcode-level MeteoSwiss forecast smooths away, while never lowering the
safety threshold.

From those:

- `sun` = meaningful sunshine expected in the look-ahead window
- `wind_high` = highest forecast gust in the window ≥ your `gust_limit_kmh`
- `rain` = radar shows rain within 10 minutes

And the outputs:

```
sun       = sunshine expected AND temperature gate passes
retract   = wind_high OR rain OR (all gust sources failed)

awning_extend            = sun AND NOT retract
backup_blinds_close      = sun AND retract
independent_blinds_close = sun
```

Where `wind_high` uses hysteresis around `gust_limit_kmh` / `gust_release_kmh`,
and the temperature gate (`min_temp_c`) — if set — blocks *all* shade below the
threshold, including the independent blinds (so "no shade when cold" applies
uniformly).

When it is not sunny, none of the three fire. `awning_extend` and
`backup_blinds_close` can **never both** be true — one requires `retract`, the
other requires `not retract`, so they are mutually exclusive by construction and
can never contradict each other.

Whether *exactly* one of them fires depends on your thresholds. With
`sun_min_awning` and `sun_min_backup` set to the **same** value, exactly one
fires whenever it is sunny: calm → extend, windy/wet → backup. If you set them
**differently**, there are conditions where **neither** fires — for example with
`sun_min_awning: 20` and `sun_min_backup: 30`, a 25 min/h forecast in high wind
is sunny enough that the awning would want to be out (so it retracts) but not
sunny enough to close the backup blind, so nothing deploys. That is intended
behaviour for independent thresholds, not a bug; set `sun_min_backup` at or
below `sun_min_awning` if you always want the backup to take over.
`retract` (= `wind_high OR rain OR all-gust-sources-failed`) is the safety
override. It is part of the published state JSON but has no entity of its own —
`Shade recommendation` already distinguishes all three outcomes, and `Wind high`
and `Rain within 10 min` show why.

### Two ways to judge "sunny enough" — `sun_model`

**`sunshine` (default).** Uses the forecast **minutes of sunshine per hour**,
compared against `sun_min_awning` / `_backup` / `_independent`. Simple and
robust, but blind to intensity: a hazy hour and a blazing one both count as
60 min/h, and it says nothing about how much of that sun actually lands on a
vertical blind versus a sloped awning.

**`irradiance`.** Computes the solar power arriving on the surface in **W/m²**,
from the MeteoSwiss global and diffuse radiation forecast, and compares it
against `irradiance_min_awning` / `_backup` / `_independent`. It captures three
things the minutes model cannot:

- **Intensity.** Overcast is roughly 100–200 W/m², hazy sun 300–500, full clear
  sun 700–1000.
- **Diffuse light.** On a heavily overcast day a vertical blind still receives
  real heat, and half of what it sees is sky.
- **Geometry.** Sun entering a room **peaks when the sun is low**, not high —
  a low beam shines straight through the glass and reaches deep inside, while
  a high summer sun mostly strikes the roof and the ground. On a clear day the
  window plane sees roughly 885 W/m² at 10° of elevation but only 529 at 65°.
  The minutes model cannot express this at all.

All three outputs are judged on the **window** plane (vertical), because what
matters is sun entering the room — not sun landing on a shading device.

**`awning_min_elevation` — why the awning alone needs a second condition.**
An awning is a horizontal projection above the window, so it only shades while
the sun is high enough; below that the beam simply passes underneath and the
awning shades nothing. That limit is pure geometry:

```
minimum elevation = atan( H / P )      H = awning height above the sill
                                       P = how far it projects
```

| Awning | needs |
| --- | --- |
| 2.5 m high, 2.0 m out | 51° |
| 2.5 m high, 3.0 m out | 40° |
| 2.2 m high, 3.5 m out | 32° |
| 2.0 m high, 4.0 m out | 27° |

Measure yours and set the option; the default is 35°. Below it the awning
stays in even in blazing sun, which is correct — it could not have helped.
Blinds have no such gate: they block sun at any height, which is precisely why
they are the tool for a low evening sun.

> **What the number means.** The surface is assumed to always face the sun, so
> the figure is an **upper bound** — a real facade with a fixed orientation
> receives this much only while the sun is roughly square-on to it, and less
> otherwise. That is deliberate: it keeps one number valid for every window on
> the building, and *which* window the sun is actually on stays a question for
> your automation (see [Step 7](#step-7--automations)). The practical
> consequence is that the reading overstates most near the edges of a facade's
> azimuth range, which is a reason to narrow that range rather than widen it.

Switching model changes **which thresholds are read** — the other set is
ignored, not combined. The `Sun model` diagnostic sensor reports which one is
live. Radiation is only fetched when the irradiance model is selected, so the
default costs nothing extra.

**If the radiation forecast is unavailable** the add-on falls back to the
sunshine model for that cycle, using your `sun_min_*` thresholds — an outage
should not stop the shade working when a perfectly good sunshine forecast is
in hand. Only the official source carries radiation (the app feed has no
equivalent), so this is a realistic failure to plan for.

The switch is never silent. Three things show it: `Sun model` reads
`sunshine_fallback`, a warning is recorded on `Last warning`, and the
`Warning` event entity fires — so you can alert on it exactly as on the
forecast-source fallback. The two `Irradiance` sensors go Unknown rather than
holding a stale figure. If sunshine is missing *too*, the sun signal is
genuinely unknown and the shade is kept in (`Forecast data` → *Problem*).

**Design choice — forecast-only, no live wind sensor.** An earlier design used a
measured gust from the nearest weather station, but that data arrives with a
~10-minute lag from a station kilometres away — too late and too displaced to
protect an awning. So the retract decision is driven by *forecast* gust (which
warns hours ahead) and by the *radar* (which sees an approaching rain/gust front
at 1 km / 5 min). Nothing safety-critical depends on a laggy measurement.

**This is decision support, not a safety guarantee.** A forecast can be wrong and
a gust can be local. If awning damage matters to you, keep the threshold
conservative and bias toward retracting. The physical safety call remains yours.

---

## Data sources and fallback

The forecast has a primary source and a fallback, reported live by the
the `Forecast source` entity (a string: `official` or `app`):

- **Primary: `ch.meteoschweiz.ogd-local-forecasting`** — documented, licensed
  federal open data. Chosen for durability.
- **Fallback: the MeteoSwiss app's `plzDetail` endpoint** — undocumented but
  updates more frequently. Used automatically if the official source fails, or
  made primary by setting `prefer_app_forecast: true`.

  A few Swiss postcodes carry **no app data at all** (the endpoint answers
  normally but returns nothing). That would silently leave you without a
  fallback, so the add-on checks your postcode once at startup and logs a
  warning — visible in the **Log** tab and on the `Last warning` sensor — if
  yours is one of them. Pick a neighbouring postcode if you see it. The
  warning is about the *fallback* only; the official source is unaffected.
- **Open-Meteo ICON** (`openmeteo_mode`) — an independent gust forecast on a
  1–2 km grid, from the same ICON model family MeteoSwiss uses. Three modes:
  - `always` (default) — fetched every cycle; the gust used is the
    **maximum** across every source that answered, so it can only raise
    caution, never lower it.
  - `fallback_only` — only fetched when the MeteoSwiss official/app gust is
    unavailable that cycle, so it never overrides a MeteoSwiss reading, only
    stands in for a missing one.
  - `never` — MeteoSwiss gust only.

  `always` is the more cautious choice — a genuinely finer grid can catch a
  local gust MeteoSwiss's postcode-level forecast misses. But ICON-CH1's raw
  gust diagnostic can occasionally spike hard for a single hour at a specific
  grid cell (a convective/thunderstorm signal that may not verify) while
  MeteoSwiss's more calibrated forecast stays level for the same hour; under
  `always` that spike wins the max and drives a retract. If that happens
  often at your location, `fallback_only` trades away the extra-cautious
  local-gust catch in exchange for not reacting to Open-Meteo's noise.

If the primary forecast is unavailable, `Forecast source` reads `app` —
alert on it if you want to know when you are running on the unofficial feed.

---

## Requirements

- **Home Assistant OS** (the appliance install) on `aarch64` or `amd64`. A 32-bit
  Raspberry Pi OS (`armv7`) has no prebuilt numpy/h5py wheels and will not build.
- The **Mosquitto broker** app and the **MQTT** integration (steps below).
- A location in Switzerland.

---

## Step 1 — Install the Mosquitto broker

1. Settings → **Apps**.
2. Click **Install** (top right) to open the store, search **Mosquitto broker**,
   install it, and **Start** it. Defaults are fine.
3. Connect Home Assistant to it: Settings → **Devices & Services**. **MQTT**
   appears as *discovered* — click **Add** / **Configure** and confirm. It is
   discovered but **not added automatically**, so this click is required.

> No MQTT username or password is needed. The app gets credentials from the
> Supervisor at startup.

## Step 2 — Add this repository and install the add-on

1. Settings → **Apps** (Add-on Store on older Home Assistant) → **⋮** (top
   right) → **Repositories** → paste
   `https://github.com/CHfish-ch/ha-addons` → **Add** → **Close**.
2. The Supervisor rescans automatically; if "Swiss Meteo Shade" doesn't appear
   within a few seconds, **⋮** → **Check for updates** and reopen the store.
3. **Swiss Meteo Shade** now appears in the store. Click it → **Install**. The
   Supervisor builds a container on your device (downloads numpy/h5py; a few
   minutes). The **Log** tab shows progress.

> Not showing? Enable **Advanced Mode** in your user profile (bottom left) and
> repeat step 2.

*(Don't want to add a repository? See [Alternative: manual
install](#alternative-manual-install-without-adding-the-repository) near the
end of this page.)*

## Step 3 — Find your coordinates

Open **https://map.geo.admin.ch/**, right-click your building, and read the
**CH1903+ / LV95** line — two numbers, e.g. `2 665 512, 1 211 882`. First is
easting, second northing. LV95 is used because the radar grid is native to it.

Also note your **postcode** for the `plz` option (used by the app fallback).

## Step 4 — Configure

Open the app's **Configuration** tab:

| Option | Meaning | Default |
| --- | --- | --- |
| `easting` | LV95 easting (step 3) | *Lucerne placeholder — replace* |
| `northing` | LV95 northing (step 3) | *Lucerne placeholder — replace* |
| `plz` | your postcode, for the app fallback | `"6006"` |
| `gust_limit_kmh` | retract the awning at/above this gust | `40` |
| `gust_release_kmh` | re-extend below this (hysteresis); `0` = off | `0` |
| `min_temp_c` | don't deploy shade below this °C; blank = off | *(blank)* |
| `openmeteo_mode` | when to use the Open-Meteo ICON gust source: `always` / `fallback_only` / `never` | `always` |
| `prefer_app_forecast` | try the app feed before the official one | `false` |
| `lookahead_hours` | how many hours ahead to look | `1` |
| `sun_model` | how "sunny enough" is judged: `sunshine` or `irradiance` | `sunshine` |
| `sun_min_awning` | *(sunshine model)* minutes of sun per hour before the **awning** extends | `20` |
| `sun_min_backup` | *(sunshine model)* minutes of sun per hour before the **backup blind** closes | `20` |
| `sun_min_independent` | *(sunshine model)* minutes of sun per hour before the **independent blinds** close | `20` |
| `irradiance_min_awning` | *(irradiance model)* W/m² on the **window** before the **awning** extends | `250` |
| `irradiance_min_backup` | *(irradiance model)* W/m² on the window before the **backup blind** closes | `250` |
| `irradiance_min_independent` | *(irradiance model)* W/m² on the window before the **independent blinds** close | `250` |
| `awning_min_elevation` | *(irradiance model)* don't extend the awning below this sun height (°) | `35` |
| `albedo` | *(irradiance model)* ground reflectance, 0–0.9 | `0.20` |
| `min_solar_elevation` | *(irradiance model)* ignore the direct beam below this sun height (°) | `3.0` |
| `irradiance_substeps` | *(irradiance model)* sun-path samples per hour | `12` |
| `radar_threshold_mmh` | rain rate that counts as rain | `0.1` |
| `radar_tolerance_km` | tolerance around your cell for forecast steps | `1` |
| `radar_fail_safe` | if the radar is unreachable, treat as rain (retract) instead of assuming dry | `false` |
| `interval_seconds` | how often to run | `300` |
| `forecast_max_cache_minutes` | force a full forecast re-download after this long, even if the server says unchanged; `0` = never force by timer (rely on the conditional request) | `60` |

Replace the coordinate defaults. `gust_limit_kmh` is the number you will most
likely tune — see *Tuning* below.

## Step 5 — Start it

**Info** tab → turn on **Start on boot** and **Watchdog**, then **Start**.

## Step 6 — Check it worked

The **Log** tab prints one timestamped line per cycle (each line begins with a
UTC ISO timestamp). If you prefer local-time stamps in the viewer, Home
Assistant's Supervisor log adds its own timestamps when the add-on log option
`log_level` is used; the app's own lines carry a UTC timestamp regardless:

```json
{"updated": "...", "awning_extend": false, "backup_blinds_close": false,
 "independent_blinds_close": false, "retract": true, "gust_kmh": 42.1,
 "sun": false, "rain": true, "forecast_source": "official", "on_backup": false}
```

Then Settings → Devices & Services → **MQTT** → **1 device** shows *Swiss Meteo
Shade* with all entities (below).

> **Moving between the app and its device.** They are one thing shown in two
> places, so the device page carries a **Visit** / configuration link straight
> back to this app's Configuration tab.
>
> The reverse direction — app page → device — is **not** available as a link,
> and that asymmetry is structural rather than an oversight. The app can look
> up its own slug from the Supervisor, so it can build a link *to* itself; but
> a device's address is a registry ID that only Home Assistant assigns
> (`/config/devices/device/<uuid>`), and reading it would need full Core API
> access for every user of this app — a permission not worth spending on a
> hyperlink. Go via Settings → Devices & Services → **MQTT** instead.

---

## Entities

**Controls (binary):**

| Entity | On when |
| --- | --- |
| `binary_sensor.swiss_meteo_shade_awning_extend` | sunny, calm, dry — safe to extend the awning |
| `binary_sensor.swiss_meteo_shade_backup_blinds_close` | sunny but windy or rain coming — use the hard blind |
| `binary_sensor.swiss_meteo_shade_independent_blinds_close` | sunny (and warm enough, if a temp gate is set) — blinds with no wind/rain vulnerability |

**Component signals (binary):** `rain_within_10_min`, `sun_expected`,
`wind_high`, and `forecast_unavailable`
(*problem* class — on when a safety-relevant forecast is missing: either **no
gust source answered** (all failed), in which case the awning is kept in as a
precaution regardless of sun, or the sunshine forecast is missing (treated as
not sunny). A present-but-low gust is trusted normally and does **not** trigger
this.)

**Irradiance model sensors** (all read Unknown under the `sunshine` model,
since nothing is being computed): `irradiance_window` (W/m² arriving on the
window plane) is the weather input all three decisions use. `ghi` (global
radiation as forecast), `diffuse_fraction` (what share is diffuse — high means
overcast), `solar_elevation` (which gates the awning) and `sun_model` are
diagnostics.

Solar *azimuth* is deliberately not published: `sun.sun` already provides it
and nothing here uses it, since the irradiance figure is orientation-agnostic
by design.

**Values (sensors):** `shade_recommendation` (**the enum: `extend` / `backup` /
`none` — trigger automations on this**), `forecast_gust` (km/h),
`forecast_sunshine` (min/hour), `forecast_temperature` (°C).

**Reading the state words.** Two entities use Home Assistant `device_class`es
whose state words are fixed by HA, not by this add-on:

| Entity | States | Meaning |
| --- | --- | --- |
| `Forecast data` | OK / Problem | *Problem* = a safety-relevant forecast is missing (no gust source answered, or sunshine unknown) |
| `Radar data` | Connected / Disconnected | *Disconnected* = the radar was unreachable or the newest frame was too old to trust |

They're named as subjects ("Forecast data") rather than conditions ("Forecast
missing") precisely so they read correctly with those fixed words.

**Why one gust source shows Unknown.** `Gust (MeteoSwiss official)` and `Gust
(MeteoSwiss app, fallback)` are a **primary/fallback pair** — the app feed is
only fetched when the official source fails, or when `prefer_app_forecast` is
on. They are never queried in parallel, so whichever one isn't in use reads
`Unknown`. That's normal: seeing `Gust (MeteoSwiss app, fallback)` as Unknown
means the official source is working. `Gust (Open-Meteo ICON)` is populated
whenever `openmeteo_mode: always` (fetched alongside the primary every cycle —
the one to compare the primary against), or only during a MeteoSwiss outage
under `fallback_only`; under `never` it's always Unknown.

**Sensor vs Diagnostic.** Home Assistant splits entities into two groups.
*Primary* entities (no category) are the ones you act on: the recommendation,
the resulting awning/blind decisions, and the weather inputs that drive them
(`Forecast gust`, `Forecast sunshine`, `Forecast temperature`, `Rain`, `Sun
expected`, `Wind high`). *Diagnostic* entities describe how the add-on is
working rather than the weather: provenance and health (`Forecast source`,
`Radar age`, `Radar data`, `Forecast data`, `Shade reason`, `Last
error`/`Last warning`, and the per-source gust breakdown). They appear in a
separate "Diagnostic" section on the device page and are the ones to check when
something looks off, not for daily automations.

**Diagnostics:** `forecast_source` (`official` / `app`), `radar_age` (min),
`shade_reason` — a plain-language explanation of the current decision, e.g.
*"sun, but gust 46>=40 km/h and rain approaching"* — and two event sensors:
`last_error` and `last_warning`. Each holds the **timestamp** of the most
recent event of that level as its state (so it renders as a time and you can
trigger on it), with the message in the `last_error_message` /
`last_warning_message` attributes. "Last error" may be from long ago — the name
reflects that it does not clear; a clean run simply doesn't update it.

The two event sensors (`last_error`, `last_warning`) carry the full state as
attributes; the message lives in `last_error_message` / `last_warning_message`.
Other sensors don't carry attributes (to keep the recorder small).

**Events (the ones to automate on):** `event.swiss_meteo_shade_error` and
`event.swiss_meteo_shade_warning`. Unlike the sensors above, these *fire* once
per new event rather than holding a value, and carry the text in a `message`
attribute — so an automation is a plain trigger with no timestamp arithmetic
(see Step 7). They stay silent for repeats of the same condition and across
restarts. The `Last error` / `Last warning` sensors cover the other question —
seeing at a glance *when* something last went wrong.

Each gust source is exposed as its own diagnostic sensor so you can see which
one drove a retract: `sensor.swiss_meteo_shade_gust_official`,
`sensor.swiss_meteo_shade_gust_app`, and (if `openmeteo_mode` fetched it this
cycle) `sensor.swiss_meteo_shade_gust_openmeteo`. The `forecast_gust` sensor is
the maximum across them — the value the decision actually uses.

---

## Step 7 — Automations

Extend the awning when safe, retract on the override:

```yaml
# Trigger on the recommendation sensor, on each hazard appearing, and on HA
# start so the covers are reconciled to the current recommendation after a
# reboot.
alias: Shade follows the weather
triggers:
  - trigger: state
    entity_id: sensor.swiss_meteo_shade_shade_recommendation
    to: ["extend", "backup", "none"]   # real changes only, never attribute updates
  # A NEW hazard re-asserts the recommendation even when the recommendation
  # itself does not change -- see "Manual overrides" below. Without these two,
  # an awning you put back out by hand stays out when rain arrives.
  - trigger: state
    entity_id: binary_sensor.swiss_meteo_shade_rain_within_10_min
    to: "on"
  - trigger: state
    entity_id: binary_sensor.swiss_meteo_shade_wind_high
    to: "on"
  - trigger: homeassistant
    event: start                        # re-apply current state after a reboot
conditions:
  - condition: template
    value_template: >-
      {{ states('sensor.swiss_meteo_shade_shade_recommendation')
         in ['extend', 'backup', 'none'] }}
actions:
  - choose:
      # sunny and safe: awning out, backup blind back up
      - conditions: >-
          {{ states('sensor.swiss_meteo_shade_shade_recommendation') == 'extend' }}
        sequence:
          - action: cover.open_cover
            target: {entity_id: cover.terrace_awning}
          - action: cover.open_cover
            target: {entity_id: cover.backup_blind}
      # sunny but unsafe: awning in, backup blind closed
      - conditions: >-
          {{ states('sensor.swiss_meteo_shade_shade_recommendation') == 'backup' }}
        sequence:
          - action: cover.close_cover
            target: {entity_id: cover.terrace_awning}
          - action: cover.close_cover
            target: {entity_id: cover.backup_blind}
    # not sunny: awning in, backup blind back up (nothing to shade)
    default:
      - action: cover.close_cover
        target: {entity_id: cover.terrace_awning}
      - action: cover.open_cover
        target: {entity_id: cover.backup_blind}
```

> **Why every branch sets the backup blind explicitly:** a closed cover stays
> closed. If only the `backup` branch touched it, wind or rain would put it
> down once and nothing would ever raise it again. Each branch therefore
> drives the backup blind to its correct position, and the
> `homeassistant: start` trigger re-applies the right state after a reboot
> instead of leaving whatever survived the restart.

### Manual overrides, and why the hazard triggers matter

This add-on publishes *recommendations*; your automation moves the covers. It
has no idea what position they are actually in, so if you extend the awning by
hand against a `backup` recommendation, nothing here fights you. That is
deliberate — but it needs one safeguard.

`retract` is true when **wind is high OR rain is coming**, and
`Shade recommendation` collapses both into the same `backup` value. So if it is
already `backup` because of wind and rain then arrives, the recommendation does
**not** change — there is no state transition, and an automation watching only
that sensor never re-fires. An awning you had manually put back out would sit
there through the rain.

The `Rain within 10 min` and `Wind high` triggers close that hole: each new
hazard re-runs the automation, which re-applies the current recommendation and
brings the awning back in. Because they fire only on a hazard turning **on**,
they don't nag you while conditions are merely unchanged — a manual override
survives until something genuinely new shows up, and then safety wins.

If you would rather your manual override always stick until you undo it, drop
those two triggers. Then nothing re-asserts until the recommendation itself
changes — at the cost of the rain case above.

### Only shade when the sun is actually on that window

This add-on answers *"is it sunny, and is it safe"* — it has no idea which way
your window faces. On a clear day it says `extend` from sunrise to sunset, so
a west-facing awning would go out at breakfast, hours before any sun reaches
it. Home Assistant's built-in `sun.sun` entity closes that gap; no extra
integration needed.

It carries two attributes:

| Attribute | Meaning |
| --- | --- |
| `elevation` | Height of the sun above the horizon, in degrees. **Negative when it is below** — so `elevation > 0` simply means "the sun is up". |
| `azimuth` | Compass direction of the sun, in degrees **clockwise from north**: `0` = N, `90` = E, `180` = S (solar noon), `270` = W. |

Through the day the azimuth climbs steadily: sunrise in the east, `180` due
south around midday, sunset in the west.

**The rule:** a wall gets direct sun while the sun's azimuth is within **±90°
of the direction that wall faces**. East faces `90`, so `0`–`180`; south faces
`180`, so `90`–`270`; west faces `270`, so `180`–`360`. Anything in between —
a south-west facade at `225` — follows the same arithmetic.

Which attribute actually does the work is different at each end of the day:

| Window faces | Sun arrives when… | Sun leaves when… |
| --- | --- | --- |
| **East** (`90`) | it rises — `elevation` goes above `0` | it swings south — `azimuth` passes `180` |
| **South** (`180`) | `azimuth` passes `90` | `azimuth` passes `270` |
| **West** (`270`) | it swings past south — `azimuth` passes `180` | it sets — `elevation` drops below `0` |

So an **east** window is gated mainly on `elevation` (the sun is on that wall
the moment it appears, so the only question is whether it is up), while a
**west** window is gated mainly on `azimuth` (the sun is long since up, the
question is whether it has come round yet). Each facade needs `elevation`
*and* `azimuth` in practice — the table just shows which one moves first.

Worth narrowing those edges, though: at exactly ±90° the sun grazes the glass
edge-on and delivers almost nothing, so starting a west facade at `200` rather
than `180` often matches what you actually feel.

For scale, at Swiss latitudes (~47°N) the sun's azimuth sweeps roughly
`55`→`305` across a summer day and climbs to about 66° elevation — but in
midwinter it only manages `127`→`234`, peaking near 20°. An east or west gate
that works in July can therefore leave a winter awning idle all day, because
the sun never reaches that facade at all. That is usually the right outcome,
and it is what `min_temp_c` is for anyway.

Add the gate as conditions — for a **west-facing** awning:

```yaml
conditions:
  - condition: numeric_state
    entity_id: sun.sun
    attribute: azimuth
    above: 180          # sun has passed due south
  - condition: numeric_state
    entity_id: sun.sun
    attribute: elevation
    above: 5            # and is high enough to clear the neighbour's roof
```

> **Add matching triggers too, or it will not retract.** Conditions are only
> examined when something already fired the automation. The recommendation
> does not change as the sun moves, so without triggers on the same
> thresholds the awning stays out long after the sun has left. For the
> west-facing example, add both ends:
>
> ```yaml
> triggers:
>   - trigger: numeric_state
>     entity_id: sun.sun
>     attribute: azimuth
>     above: 180        # sun arrives on this facade
>   - trigger: numeric_state
>     entity_id: sun.sun
>     attribute: elevation
>     below: 5          # sun drops away again
> ```
>
> Same principle as the hazard triggers above: whatever threshold you put in a
> condition needs a trigger on it, or nothing re-evaluates when it is crossed.

**The numbers above are starting points, not your numbers.** Real facades are
never exactly east or west, and a neighbouring roof, a tree, a balcony above,
or a deep reveal all cut into the window. Your own values are easy to measure:
open **Developer Tools → States**, filter for `sun.sun`, and read `azimuth` and
`elevation` at the two moments that matter — when direct sun first touches the
window, and when it leaves. Do it on a clear day, note both pairs, and use
those. A minimum `elevation` is usually worth keeping even when the azimuth
range is right: very low sun is often blocked by whatever is on the horizon,
and when it does get through, it comes in under an awning rather than being
stopped by it.

Alert when running on the unofficial fallback:

```yaml
alias: Notify on forecast fallback
triggers:
  - trigger: state
    entity_id: sensor.swiss_meteo_shade_forecast_source
    to: "app"        # states are 'official' / 'app', not on/off
    for: "00:30:00"
actions:
  - action: notify.persistent_notification
    data: {message: "Shade running on the app forecast (official source down)."}
```

Add `for: "00:05:00"` to control triggers if you want to ignore brief flips near
a threshold.

Notify when a new error or warning is recorded. Use the **`event` entities** —
they fire once per new event and carry the message as an attribute, so no
timestamp comparison is needed:

```yaml
alias: Notify on shade error
triggers:
  - trigger: state
    entity_id: event.swiss_meteo_shade_error
conditions:
  # skip the entity's initial 'unknown' when it is first created
  - condition: template
    value_template: "{{ trigger.to_state.state not in ['unknown', 'unavailable'] }}"
actions:
  - action: notify.persistent_notification
    data:
      title: "Swiss Meteo Shade error"
      message: "{{ trigger.to_state.attributes.message }}"
```

Swap `event.swiss_meteo_shade_error` for `event.swiss_meteo_shade_warning` for
the softer channel.

The `Last error` / `Last warning` **sensors** carry the same information as a
timestamp you can look at, which is handy on a dashboard — but don't build
notifications on them. Their state is *when* the message first appeared, so an
automation would have to work out for itself whether that timestamp is
genuinely new; get it wrong and you are re-notified about an old error on
every Home Assistant restart and every availability blip. The `event` entities
exist precisely so you never have to write that logic.

**Repeats don't re-notify.** An event fires only when the message is genuinely
new. A condition that persists for hours — a stale radar, a forecast stuck on
its fallback — therefore notifies **once**, not every cycle. Every individual
occurrence is still timestamped in the add-on **Log**.

**A restart doesn't re-notify either.** The add-on remembers the last
error/warning across restarts (they're persisted to `/data`), and on the first
cycle after starting it only takes note of them — it never announces an event
recorded before that run. Home Assistant also discards replayed retained
messages for `event` entities, and these are published non-retained anyway, so
a reconnecting broker can't resurrect an old one.

---

## Tuning `gust_limit_kmh`

40 km/h is a cautious default. To tune it, watch the `forecast_gust` sensor over
a week or two against how your awning actually behaves in wind. Raise the limit
if it retracts on breezes the awning tolerates; lower it if it stays out in wind
you would rather it didn't. Because the gust used is the max across sources, the
system errs toward retracting — the safe direction.

`lookahead_hours` widens or narrows how far ahead the gust/sun outlook reaches.
The window is always "now − 1h" to "now + lookahead_hours", so it always
includes the current hour regardless of this setting — what it controls is how
many hours *before* a forecast hour the system starts reacting to it. 1 (the
default) reacts only once a windy/sunny hour is at most 1h away; raising it
trades earlier warning of a genuine event for retracting further ahead of an
hour that may turn out to be a forecast miss.

---

## Optional behaviour

**Hysteresis (`gust_release_kmh`).** With a plain threshold, a gust hovering at
39.8 / 40.2 km/h flaps the awning in and out. Set `gust_release_kmh` below
`gust_limit_kmh` (e.g. limit 40, release 32): once retracted for wind, the
awning stays retracted until the gust drops below 32, then re-extends. Setting
`0` means **no hysteresis** — the awning re-extends as soon as the gust falls
back below `gust_limit_kmh` (the same threshold both directions). "Off" does
**not** mean "never re-extend"; it means the retract and re-extend thresholds
are identical. The value must be at or below `gust_limit_kmh`, or it is ignored
with a warning.

**When is "sun expected" true?** Each shade has its **own** sunshine threshold,
because they shade different things:

- `sun_min_awning` (default 20) — minutes of sun per hour before the awning
  extends. Drives `Awning extend` and the headline `Sun expected` sensor.
- `sun_min_backup` (default 20) — threshold for the backup blind.
- `sun_min_independent` (default 20) — threshold for the independent blinds.
  Interior glare/heat blinds often want a **lower** value (e.g. 5–10) so they
  come down even in hazy sun, while the awning waits for stronger sun.

Each is the peak forecast sunshine in the look-ahead window. So at
`sun_min_awning: 20`, a forecast of 11 min/h reads as *not* awning-sun — too
cloudy to bother extending — while `sun_min_independent: 5` would still bring
the interior blinds down. The raw figure is on the `Forecast sunshine` sensor;
these three thresholds turn it into per-output yes/no decisions.

**Minimum temperature (`min_temp_c`).** Below this forecast temperature, no shade
is deployed at all — useful in winter when you want solar gain rather than
shade. To block shade below exactly 0 °C, enter `0`; negative thresholds work
too. Leave the field **empty** to disable the gate — empty means off, `0` does
not. Temperature is the coldest hour from the current hour through the look-ahead window, from the same
forecast source as gust and sunshine.

**Availability.** Operational entities carry `expire_after` (interval + backoff headroom, 1200 s at defaults); diagnostics use a longer 6× interval: if the app
stops publishing — crash, network loss, or repeated cycle failures — Home
Assistant marks them unavailable on its own, rather than leaving stale values
that look fresh. After a long run of consecutive failures the app exits so the
Supervisor Watchdog restarts the container, clearing any stuck network state. A radar outage alone does not hide the forecast-driven
entities; the cycle stays healthy as long as either radar or forecast answers.

**Fail-safe on radar.** If the radar service is unreachable, rain is unknown.
By default (`radar_fail_safe: false`) the app assumes dry and leaves the awning
out if wind is calm — a brief radar outage during clear weather shouldn't pull
it in. In high-exposure spots where a missed shower is costly, set
`radar_fail_safe: true` to treat a radar outage as rain and retract. Either way
the outage is logged to `last_warning`.

**Fail-safe on wind.** Gust is the safety input for the awning. If *every* gust
source fails in a cycle (official, app, and Open-Meteo all unreachable), the
awning is kept in (`retract` on, `forecast_unavailable` on) even if it is sunny
— missing the safety input fails safe. A single source failing is fine: the
gust used is the max of whatever answered. A temperature-gate configured but
without a temperature that cycle simply doesn't apply, and logs a line.

## Resource use

Radar files are read **in memory** (never written to disk), so the app causes no
SD-card wear on Raspberry-Pi installs despite polling every few minutes. The
30 MB forecast files use conditional requests rather than re-downloading: the CDN
(data.geo.admin.ch, S3/CloudFront) returns *304 Not Modified* with zero bytes
when a file is unchanged — verified against the live service — so an unchanged
32 MB forecast costs only the request round-trip. The `last_error` /
`last_warning` diagnostics are persisted to `/data` so they survive a restart.

**Nowcast honesty.** The rain projection only advects when there is real echo
to track. Phase correlation on a clear sky returns a large, random vector —
readings equivalent to ~90 km/h pointing in two different directions between
overlapping frames are typical — which would sample a cell 10–20 km away and
could pick up marginal echo that was never approaching. With nothing to track
the app uses zero motion, which is also physically right: no echo nearby means
nothing can arrive within the lead time.

## Correctness notes

Verified against live data during development:

- **Radar mm/h** decoding confirmed against a real file (values are already
  mm/h; grid origin LV95 2255000/1480000, 710×640 km cells).
- **Forecast timezone** — the forecast files timestamp in **UTC**, verified by
  matching the sunshine series against sunrise. The whole pipeline runs in UTC,
  so it is unaffected by the summer/winter clock change and by date/year
  rollover (tested at both DST boundaries and New Year).
- **Partial data** — just after midnight UTC the day's forecast files publish
  progressively. The app requires the specific files it needs and falls back to
  the newest complete forecast until they appear, rather than reading a
  half-published set.
- **Forecast freshness** — the official forecast files are ~30 MB each. Rather
  than re-downloading them every cycle, the app uses a conditional request
  (`If-None-Match`): the server answers *304 Not Modified* in a few bytes when
  the file is unchanged, and the full file is pulled only when it actually
  changes. As an independent safety net — not trusting the server's freshness
  signal blindly — a full re-download is forced once the cache exceeds
  `forecast_max_cache_minutes` (default 60). On that forced refresh the body is
  hashed; if the server claimed the file was unchanged (same ETag) but the hash
  differs, an ERROR is logged and the fresh data is used. Set
  `forecast_max_cache_minutes: 0` to never force a timed refresh, relying on the
  conditional request alone.

---

## Diagnostics (optional, standalone)

The repository's `tools/` folder holds standalone scripts for investigating the
data sources. They are **not** part of the add-on and are never copied to
`/addons`. Run them on any machine with `pip install requests` (plus `numpy` and
`h5py` for the radar ones).

Most exist because a specific assumption turned out to be wrong and needed
settling against live data — the answers they produced are recorded in
`HANDOFF.md`, so you shouldn't need to re-run them unless something changes
upstream.

| Script | Answers |
| --- | --- |
| `rain_forensics.py --at <UTC> --east E --north N` | Why did the app report rain at a given moment? Replays the radar archive and shows what the decision logic saw. |
| `forecast_probe.py --lv95 E,N` | Official forecast structure and your nearest forecast point. |
| `headers_probe.py` | Do the forecast files support conditional GETs? |
| `head_get_probe.py` | Does the CDN honour HEAD, and does `If-None-Match` return 304? |
| `file_structure_probe.py` | Is each forecast file one hour, or the full 9-day forecast? |
| `encoding_id_probe.py` | Which text encoding, and is `point_id` globally unique? |
| `dup_probe.py` | Are same-key duplicate points identical rows or different places? |
| `timing_probe.py` | What is the app forecast's array anchored to? |
| `tz_probe.py --point <id>` | Reconfirm the forecast timezone against sunrise. |
| `precip_probe.py --lv95 E,N` | Could the app's precipitation stand in for the radar? Puts both side by side for the same place and moment. (Answer so far: no — see below.) |

`rain_forensics.py` is the one you're most likely to want: if the awning ever
moves and you can't see why, it reconstructs that exact cycle from the public
archive.

**Why there is no rain fallback when the radar is down.** The app feed does
carry precipitation, so it looks like an obvious stand-in — it isn't, and
`precip_probe.py` is what settled it. Measured against the radar during real
rain, the app's 10-minute series read **0.0 while the radar saw 9.35 mm/h**
during a fast-moving shower — precisely the situation the awning cares about —
and on another occasion its 10-minute and hourly series contradicted each
other for the same hour. A source that confident and that wrong is worse than
admitting ignorance, which is what `radar_fail_safe` does. Re-run the probe
during rain if you want to check whether MeteoSwiss has improved it.

---

## Alternative: manual install without adding the repository

If you'd rather not add a repository to your Add-on Store — or you're testing
a local change before pushing it — you can copy the add-on folder onto the box
directly instead of doing [Step 2](#step-2--add-this-repository-and-install-the-add-on):

1. Install either the **Samba share** app (`\\homeassistant\addons` from
   Windows, `smb://homeassistant/addons` from macOS) or the **Advanced SSH &
   Web Terminal** app. The folder is still called `addons` despite the Apps
   rename.
2. Copy the whole `swiss_meteo_shade/` folder from this repository into
   `/addons` on your Home Assistant machine, so you end up with
   `/addons/swiss_meteo_shade/`.

   The folder name must match the `slug` in `config.yaml` character for
   character — **underscores, not hyphens**. A mismatch still installs and
   runs fine, but the Supervisor then can't load `translations/en.yaml`, so the
   Configuration screen silently shows raw option keys instead of the friendly
   names. If you see that, check the folder name first.

   ```
   swiss_meteo_shade/            → copy this whole folder to /addons/
   ├── config.yaml
   ├── Dockerfile
   ├── run.py
   ├── shade.py          orchestrator
   ├── forecast.py       gust + sunshine, official/app/Open-Meteo
   ├── logic.py          the awning/blind decision
   ├── radar.py          rain from the radar composite
   ├── events.py         records the latest error/warning for the two event sensors
   ├── translations/
   │   └── en.yaml       friendly names + descriptions for the Configuration screen
   ├── README.md         shown on the app's page during install
   └── DOCS.md           shown in the app's Documentation tab afterwards
   ```

   The `translations/` subfolder is what makes the **Configuration** tab show a
   readable name and description for each option instead of the raw keys —
   make sure it comes along. Without it the add-on still works, but the
   options screen is bare.

   Nothing else from the repository belongs in `/addons`. The `tests/` and
   `tools/` folders sit beside the add-on precisely so that copying just the
   add-on folder can't accidentally ship them.
3. Settings → **Apps** → **⋮** (top right) → **Check for updates**, then
   immediately click **Install app** (bottom right) to open the store.
   "Swiss Meteo Shade" appears under **Local apps** — but only *inside the
   store view*, not in the Apps list, until it has been installed.
4. Click **Swiss Meteo Shade** → **Install**.

This copy will not auto-update — pull new changes and re-copy the folder
yourself when you want to update.

---

## Troubleshooting

**Build fails on install** — likely a 32-bit architecture; run on `amd64` /
`aarch64`.

**No entities appear** — check Mosquitto is running and the MQTT integration was
*added* (step 1.3), not just discovered.

**Entities unavailable** — radar data older than ~20 min (MeteoSwiss outage or no
internet), or the app stopped. A last-will marks entities unavailable on crash
rather than leaving stale values.

**`Forecast source` reads `app`** — the official forecast was unreachable and
the app feed is in use. Usually transient.

**"postcode … has no app forecast data" in the log** — that postcode isn't
covered by the app feed, so the fallback can't work. Everything still runs on
the official source; set `plz` to a neighbouring postcode to restore the
fallback.

**`awning_extend` never turns on** — check `sun_expected`; if the sunshine
forecast reads low, it may simply be a cloudy stretch. Confirm `forecast_gust`
and `shade_reason` to see which condition is blocking it.

**It says rain and the sky is clear** — radar sees precipitation aloft that can
evaporate before landing (virga), common in dry or föhn conditions. Raise
`radar_threshold_mmh`.

---

## Attribution

Radar and forecast data © MeteoSwiss, open government data — *Source:
MeteoSwiss*. Open-Meteo gust data © Open-Meteo (CC-BY). This is a community
project and is **not** affiliated with or endorsed by MeteoSwiss.
