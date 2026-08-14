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
- `rain` = radar shows rain within 10 minutes (or, if the radar is out,
  the forecast expects rain this hour — see *When the radar is unavailable*)

And the outputs:

```
sun       = sun signal passes AND temperature gate passes
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

Whether *exactly* one of them fires depends on your thresholds. Under the
**irradiance** model they share one threshold (`irradiance_min_shade`), so
they partition cleanly: exactly one fires whenever it is sunny enough.

Under the **sunshine** model they have separate thresholds, and setting them
**differently** leaves conditions where **neither** fires — with
`sun_min_awning: 20` and `sun_min_backup: 30`, a 25 min/h forecast in high wind
is sunny enough that the awning stays in but not enough to close the backup,
so nothing deploys. That is intended for independent thresholds, not a bug; set
`sun_min_backup` at or below `sun_min_awning` if you always want the backup to
take over.

### `Awning unsafe` — the safety override

`retract` (= `wind_high OR rain OR all-gust-sources-failed`) is the safety
override, published as **`Awning unsafe`**. `Shade recommendation` answers
*what to deploy*; `Awning unsafe` answers *may the awning be out at all*. It is
the one entity to close on, because it is true for **every** hazard.

> Use it rather than assembling your own from the component sensors. `Rain
> within 10 min` and `Wind high` look like they cover everything and do not:
> when every gust source fails, `wind_high` stays `false` by design — an
> *unknown* gust must not be reported as a *high* one — while `retract` goes
> `true`. There is no component sensor for that third hazard, so a pair of
> triggers on those two stays quiet in exactly the case the fail-safe exists
> for.

It is a binary sensor rather than a fourth `Shade recommendation` value because
it **crosses** the enum: it holds in `backup` (a hazard while sunny) and in the
hazardous half of `none` (a hazard while not sunny). As an enum value every
automation would have to match a *set*, and adding a value later would silently
break each of those conditions. `Shade recommendation` answers *what to deploy*;
`Awning unsafe` answers *may the awning be out at all*.

That distinction is what makes `none` usable. On its own, `none` conflates "no
reason to be out" with "must come in now" — both read `none`. Paired with
`Awning unsafe` it is unambiguous:

| `Shade recommendation` | `Awning unsafe` | Meaning |
| --- | --- | --- |
| `extend` | off | Sunny and safe — awning out |
| `backup` | on | Sunny but hazardous — awning in, hard blind down |
| `none` | **off** | Nothing to shade, but nothing dangerous either — the awning **may stay out** if you want it there |
| `none` | **on** | Not sunny **and** a hazard — the awning **must** come in |

### Two ways to judge "sunny enough" — `sun_model`

**`sunshine` (default).** Uses the forecast **minutes of sunshine per hour**,
compared against `sun_min_awning` / `_backup` / `_independent`. Simple and
robust, but blind to intensity: a hazy hour and a blazing one both count as
60 min/h, and it says nothing about how much of that sun actually lands on a
vertical blind versus a sloped awning.

**`irradiance`.** Computes the solar power arriving on the surface in **W/m²**,
from the MeteoSwiss global and diffuse radiation forecast, and compares it
against `irradiance_min_shade` and `irradiance_min_independent`. It captures
three things the minutes model cannot:

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

The awning and the backup blind cover the same opening, so under this model
they share one threshold (`irradiance_min_shade`); the independent blinds keep
their own, since they are usually different windows and often wanted in weaker
sun.

**Where the awning stops helping — and why this is not an option.** As the sun
drops, an awning's shadow line climbs the wall and it covers less of the
window, from the bottom up. It is tempting to gate on that, but the geometry
says otherwise: a pitched awning's fabric hangs *below* the window head, so it
still shades something at any sun height, and the gate would never fire. Only a
**flat** awning or a fixed canopy sits high enough for it to bite. If that is
your case, put the condition in your automation next to the azimuth gate, using
`sun.sun`'s `elevation`.

> To work out the angles for your own installation — including an awning set
> back under a balcony — open
> [`docs/awning-geometry.html`](../docs/awning-geometry.html) in a browser. It
> is self-contained and needs no network, and it also shows whether the sun at
> 47°N ever reaches that height in winter.

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
`sunshine_fallback`, a warning is recorded on `Active warning`, and the
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
  warning — visible in the **Log** tab and on the `Active warning` sensor — if
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
| `irradiance_min_shade` | *(irradiance model)* W/m² on the **window** before the opening gets shaded (awning or backup) | `250` |
| `irradiance_min_independent` | *(irradiance model)* W/m² on the window before the **independent blinds** close | `250` |
| `albedo` | *(irradiance model)* ground reflectance, 0–0.9 | `0.20` |
| `min_solar_elevation` | *(irradiance model)* ignore the direct beam below this sun height (°) | `3.0` |
| `irradiance_substeps` | *(irradiance model)* sun-path samples per hour | `12` |
| `radar_threshold_mmh` | rain rate that counts as rain | `0.1` |
| `radar_tolerance_km` | tolerance around your cell for forecast steps | `1` |
| `radar_fail_safe` | last resort if radar **and** forecast precipitation are unavailable: treat as rain instead of dry | `true` |
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

> **Check your own entity IDs before writing automations.** The IDs below are
> what a fresh install usually generates, but they are **not** guaranteed.
> Home Assistant assigns an entity_id **once**, when it first sees the entity,
> and then keeps it forever — while the *friendly name* is re-read from this
> add-on on every reconnect. So a renamed entity keeps its original ID, and the
> two can disagree permanently. One observed instance carries
> `binary_sensor.swiss_meteo_shade_awning_retract` with the friendly name
> *Swiss Meteo Shade Awning unsafe*.
>
> Open **Developer Tools → States**, filter on `swiss_meteo_shade`, and use
> what you actually see.
>
> To realign all of them at once: **Settings → Devices & Services → MQTT →
> Swiss Meteo Shade → ⋮ → Recreate entity IDs**. It lists every rename before
> you confirm — note them down, because it does **not** update your
> automations, scripts or dashboards, and a reference to a missing entity
> fails silently rather than raising.
>
> *(Entities left behind by older versions are cleaned up automatically from
> 1.7.1 onward — that is a separate mechanism, and it cannot fix an ID that no
> longer matches its name. See the changelog.)*

**Controls (binary):**

| Entity | On when |
| --- | --- |
| `binary_sensor.swiss_meteo_shade_awning_extend` | sunny, calm, dry — safe to extend the awning |
| `binary_sensor.swiss_meteo_shade_backup_blinds_close` | sunny but windy or rain coming — use the hard blind |
| `binary_sensor.swiss_meteo_shade_independent_blinds_close` | sunny (and warm enough, if a temp gate is set) — blinds with no wind/rain vulnerability |
| `binary_sensor.swiss_meteo_shade_awning_unsafe` | **any** hazard — wind, rain, or no gust source answering. The awning must be in, whatever the recommendation says |

**Component signals (binary):** `rain_within_10_min`, `sun_expected`,
`wind_high`, plus two *problem*-class health signals:

- `forecast_unavailable` — on when a safety-relevant forecast is missing:
  either **no gust source answered** (all failed), in which case the awning is
  kept in as a precaution regardless of sun, or the sunshine forecast is
  missing (treated as not sunny). A present-but-low gust is trusted normally
  and does **not** trigger this.
- `gust_unknown` — the **hazardous half** of the above, on its own. A missing
  sunshine forecast costs you shade; a missing gust forecast means wind safety
  cannot be vouched for, and only this entity tells the two apart. It is what
  makes `Awning unsafe` turn on when the sky is otherwise quiet.

**Irradiance model sensors** (all read Unknown under the `sunshine` model,
since nothing is being computed): `irradiance_window` (W/m² arriving on the
window plane) is the weather input all three decisions use. `ghi` (global
radiation as forecast), `diffuse_fraction` (what share is diffuse — high means
overcast), `solar_elevation` (the sun's height, below which
`min_solar_elevation` suppresses the direct beam) and `sun_model` are
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
| `Gust data` | OK / Problem | *Problem* = **no** gust source answered. Wind safety can't be vouched for, so `Awning unsafe` turns on |
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
the resulting awning/blind decisions, `Awning unsafe`, and the weather inputs
that drive them (`Forecast gust`, `Forecast sunshine`, `Forecast temperature`,
`Rain`, `Sun expected`, `Wind high`). *Diagnostic* entities describe how the
add-on is working rather than the weather: provenance and health (`Forecast
source`, `Radar age`, `Radar data`, `Forecast data`, `Gust data`, `Shade
reason`, `Active error`/`Active warning`, and the per-source gust breakdown).
They appear in a
separate "Diagnostic" section on the device page and are the ones to check when
something looks off, not for daily automations.

**Diagnostics:** `forecast_source` (`official` / `app`), `radar_age` (min),
`rain_source` (`radar` / `forecast` / `assumed`), `shade_reason` — a
plain-language explanation of the current decision, e.g. *"sun, but gust
46>=40 km/h and rain approaching"* — and two health sensors:

- **`Active error` / `Active warning`** read `"<what is wrong> since <when>"`,
  and go to `none` as soon as a cycle completes without it. They describe the
  situation **right now**, so a fault that has resolved stops showing. A
  condition that persists keeps its original *since* time rather than ticking,
  so the value stays put while it lasts.
- If the same fault returns after a clean spell it counts as a **new**
  occurrence, with a new *since* time — it is not the old one continuing.

Nothing is lost by clearing: the **`event` entities** fire once per new event,
so Home Assistant's logbook and any notification you have set up keep the
permanent record. The sensors are the current state; the events are the
history.

**Events (the ones to automate on):** `event.swiss_meteo_shade_error` and
`event.swiss_meteo_shade_warning`. Unlike the sensors above, these *fire* once
per new event rather than holding a value, and carry the text in a `message`
attribute — so an automation is a plain trigger with no timestamp arithmetic
(see Step 7). They stay silent for repeats of the same condition and across
restarts. The `Active error` / `Active warning` sensors cover the other
question — seeing at a glance what is wrong *right now*, and since when.

Each gust source is exposed as its own diagnostic sensor so you can see which
one drove a retract: `sensor.swiss_meteo_shade_gust_official`,
`sensor.swiss_meteo_shade_gust_app`, and (if `openmeteo_mode` fetched it this
cycle) `sensor.swiss_meteo_shade_gust_openmeteo`. The `forecast_gust` sensor is
the maximum across them — the value the decision actually uses.

---

## Step 7 — Automations

> **First: find out which action extends your awning.** There is no universal
> convention and getting it backwards means a safety automation that pushes the
> awning **out** into the wind it was supposed to bring it in from.
>
> Every example below assumes `cover.open_cover` = **extended** and
> `cover.close_cover` = **retracted**. Many awnings are the other way round —
> a roller-type motor unrolls the fabric to extend, which the actuator reports
> as *closing*, so `close_cover` extends and `open_cover` retracts. Both are
> common and neither is wrong.
>
> Check yours before running anything: **Developer Tools → Actions**, call
> `cover.open_cover` on your awning, and watch what it does. If it retracts,
> swap **every** `open_cover` / `close_cover` on the awning in the examples
> below. The backup blind is a separate device and may follow the opposite
> convention again — test it too.

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
  # itself does not change -- see "Manual overrides" below. Without this, an
  # awning you put back out by hand stays out when rain arrives.
  # One trigger covers all three hazards: wind, rain, and every gust source
  # failing. Do NOT substitute `rain_within_10_min` + `wind_high` -- that pair
  # cannot see the gust fail-safe.
  - trigger: state
    entity_id: binary_sensor.swiss_meteo_shade_awning_unsafe
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

**This is the starting point, not the finished automation.** It shades from
sunrise to sunset on a clear day, because the add-on has no idea which way your
window faces, and its `default:` branch closes on `none` — including the
`none` + `Awning unsafe` off case that the table above says may stay out. Two
later sections refine exactly those two branches, on the *same* automation:
*Only shade when the sun is actually on that window* adds the facade gate to
the `extend` branch, and *Why the awning comes in before sunset* replaces
`default:`. Work through both before leaving this running unattended.

### Manual overrides, and why the hazard triggers matter

This add-on publishes *recommendations*; your automation moves the covers. It
has no idea what position they are actually in, so if you extend the awning by
hand against a `backup` recommendation, nothing here fights you. That is
deliberate — but it needs one safeguard.

`retract` is true when **wind is high OR rain is coming OR no gust source
answered**, and `Shade recommendation` collapses all of them into the same
`backup` value. So if it is already `backup` because of wind and rain then
arrives, the recommendation does **not** change — there is no state transition,
and an automation watching only that sensor never re-fires. An awning you had
manually put back out would sit there through the rain.

The `Awning unsafe` trigger closes that hole: each new hazard re-runs the
automation, which re-applies the current recommendation and brings the awning
back in. Because it fires only on the transition to **on**, it doesn't nag you
while conditions are merely unchanged — a manual override survives until
something genuinely new shows up, and then safety wins.

> **Use `Awning unsafe`, not the component sensors.** Triggering on `Rain
> within 10 min` and `Wind high` looks equivalent and is not: when every gust
> source fails, `wind_high` stays **off** by design — an unknown gust must not
> by itself force a retract — while `retract` goes **on**. Both component
> triggers therefore stay quiet in exactly the situation the fail-safe exists
> for, and the awning stays out. `Awning unsafe` is true for all three.

If you would rather your manual override always stick until you undo it, drop
that trigger. Then nothing re-asserts until the recommendation itself changes —
at the cost of the rain case above.

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

**Put the gate on the `extend` branch — not on the automation's `conditions:`.**
This matters more than it looks; see the warning below. For a **west-facing**
awning, the first branch of the `choose` in Step 7 becomes:

```yaml
      # sunny and safe -- AND the sun is actually on this window
      - conditions:
          - condition: template
            value_template: >-
              {{ states('sensor.swiss_meteo_shade_shade_recommendation') == 'extend' }}
          - condition: numeric_state
            entity_id: sun.sun
            attribute: azimuth
            above: 180          # sun has passed due south
          - condition: numeric_state
            entity_id: sun.sun
            attribute: elevation
            above: 5            # and is high enough to clear the neighbour's roof
        sequence:
          - action: cover.open_cover
            target: {entity_id: cover.terrace_awning}
          - action: cover.open_cover
            target: {entity_id: cover.backup_blind}
```

Leave the `backup` branch and `default:` as they are. Now when the sun moves
off the window the `extend` branch simply stops matching, `default:` runs, and
the awning comes in.

> **Why not the automation's `conditions:`.** It is the obvious place and it
> quietly breaks the retract. A blanket `elevation above 5` condition is
> evaluated on *every* run, including the run that fires **because** the sun
> just dropped below 5 — so the condition fails, the automation stops before
> its actions, and the awning is never closed. It stays out all night. On the
> `extend` branch the same test does the right thing: failing it falls through
> to `default:` instead of aborting the automation.

> **Add matching triggers too.** Conditions are only examined when something
> already fired the automation, and the recommendation does not change as the
> sun moves. Without these, nothing re-evaluates when a threshold is crossed:
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
>     above: 5          # sun clears the obstruction
>   - trigger: numeric_state
>     entity_id: sun.sun
>     attribute: elevation
>     below: 0          # the day is over
> ```
>
> Whatever threshold you put in a branch needs a trigger on it, or nothing
> re-evaluates when it is crossed. If the sun leaves your facade well before it
> sets — a south or south-east window — add a trigger on the azimuth at that
> end too.

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

### Why the awning comes in before sunset — and what to do about it

If your awning retracts while the sun is visibly still in the room, this is
why. **MeteoSwiss reports 0 sunshine minutes for the hour containing sunset —
on every day, including cloudless ones.** Sampled at Lucerne over a week of
forecasts, with sunset around 20:37 local:

| Hour (local) | Sunshine min | GHI W/m² | Sun elevation |
| --- | --- | --- | --- |
| 19:00–20:00 | 45 | 103 | 10.6° |
| **20:00–21:00** | **0** | **8** | **1.1°** |

That hour holds 39 minutes of sun, due west at azimuth 285–290°, straight into
a west-facing room. The data is not wrong: sunshine duration counts only
minutes with a direct beam above 120 W/m² (WMO), and below about 5° of
elevation the air path is so long that even a cloudless sun falls under that
line. It is still perfectly visible, and at that angle it reaches deepest into
the room.

**No setting fixes this.** The figure is exactly `0`, so no `sun_min_awning`
above zero helps. The `irradiance` model doesn't rescue it either: GHI in those
hours is 4–9 W/m², and since the direct beam is reconstructed from GHI minus
diffuse *on a horizontal plane* — precisely where a 5° sun deposits nothing —
the window figure computes to single digits. Both models measure radiative
power, and the power genuinely is small. What you are reacting to is a low sun
in your line of sight, which is geometry, not energy. On cloudier days the
effect starts earlier: an hour reading 7–9 minutes falls under the default
threshold of 20 and pulls the awning in around 19:00, an hour and a half before
sunset.

**The fix is one more refinement of the same automation: stop treating "not
sunny" as a reason to close.** The add-on's sun verdict is a good *opening*
signal — it knows about cloud, and `sun.sun` does not — but it is a poor
*closing* signal at the edges of the day, because it reaches zero before the
sun does. Let a hazard or the geometry end the day instead.

Replace the `default:` branch from Step 7 with this:

```yaml
    # Not sunny -- but that alone is not a reason to close. Below ~5 deg the
    # sunshine forecast reads 0 whatever the sky is doing, and the `extend`
    # branch has already stopped matching, so without this the awning would
    # come in with the sun still in the room. Close only for a real reason.
    default:
      - choose:
          - conditions:
              - condition: or
                conditions:
                  - condition: state          # any hazard
                    entity_id: binary_sensor.swiss_meteo_shade_awning_unsafe
                    state: "on"
                  - condition: numeric_state  # the day is over
                    entity_id: sun.sun
                    attribute: elevation
                    below: 0
            sequence:
              # RETRACT -- swap for open_cover if yours is the other way round
              - action: cover.close_cover
                target: {entity_id: cover.terrace_awning}
      # the backup blind goes back up either way -- there is nothing to shade
      - action: cover.open_cover
        target: {entity_id: cover.backup_blind}
```

Read together with the `extend` branch, the three cases compose the way you
want without any extra triggers or timers:

| Sun elevation | `extend` branch | `default:` branch | Result |
| --- | --- | --- | --- |
| above `5` | matches — awning out | — | Shaded as usual |
| `0`–`5` | fails (too low) | closes only on a hazard | **An awning already out stays out**; a closed one is not opened |
| below `0` | fails | closes | Comes in at sunset |

That middle row is the whole point. It defers the *closing* until the sun is
genuinely down, without opening the awning fresh at 20:15 under a cloudy sky.
If the sun leaves your facade before it sets, add the azimuth test to the same
`or:` alongside the elevation one.

**The cost, honestly:** on a genuinely overcast evening an awning that is
already out now stays out for that last half hour instead of coming in. You
lose a little fabric wear and nothing else — `Awning unsafe` is in the same
`or:`, so wind, rain and a gust blackout still bring it in immediately, at any
elevation.

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

The `Active error` / `Active warning` **sensors** read
`"<what is wrong> since <when>"` while a fault is live and `none` once a cycle
completes without it, which is what you want on a dashboard — but don't build
notifications on them. They are *state*, not a feed: a fault that clears and
returns looks like one value changing, and an automation would have to work out
for itself whether it is seeing something genuinely new. The `event` entities
exist precisely so you never have to write that logic.

**Repeats don't re-notify.** An event fires only when the message is genuinely
new. A condition that persists for hours — a stale radar, a forecast stuck on
its fallback — therefore notifies **once**, not every cycle. Every individual
occurrence is still timestamped in the add-on **Log**.

**A restart doesn't re-notify either.** Events are not carried across restarts:
reloading a fault recorded before a restart would claim one that may already be
over. The first cycle after starting only takes note of what it finds, and
never announces it as new. Home Assistant also discards replayed retained
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

**When the radar is unavailable.** MeteoSwiss radar outages are real — it
stopped publishing for four hours on 2026-08-12 — so rain falls back through
three stages:

1. **Radar** (1 km, 5 min, observed, projected to +10 min) — normal operation.
2. **Forecast precipitation**, if the radar is out. Taken as the **maximum**
   across the official `rre150h0` and the MeteoSwiss app's two precipitation
   arrays, so any one of them seeing rain is enough. That is the cautious
   direction, the same rule the gust sources use: for an awning a false alarm
   costs an hour of shade, while a miss costs a soaked awning.
3. **`radar_fail_safe`**, only if the forecast is unavailable too. `true`
   (default) assumes rain and retracts; `false` assumes dry. On by default
   because the outcomes are not equal — assuming dry wrongly leaves the awning
   out in rain, assuming rain wrongly costs an hour of shade.

The forecast is a genuine downgrade and the add-on says so rather than hiding
it: `Rain source` reads `radar` / `forecast` / `assumed`, `Rain (forecast
fallback)` shows the millimetres, and a warning is recorded. It is hourly and
point-resolution, answering *"is rain expected this hour"* rather than *"is
rain arriving in ten minutes"*, so a convective shower the model missed can
still slip through. Better than a constant; not a substitute for radar.

> The three series are unproven against each other — they are known to miss
> *different* events, which is why all three are combined, but their
> false-alarm rates have never been measured. `tools/precip_probe.py` scores
> them against the radar archive and accumulates across runs if you want to
> check yours.

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
32 MB forecast costs only the request round-trip.

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
