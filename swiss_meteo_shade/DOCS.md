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

> **Names note.** Home Assistant 2026.2 renamed **Add-ons** to **Apps** in the
> interface. Only the labels changed; technical paths such as `/addons` are
> unchanged. This guide says *app*; on older Home Assistant you will see
> *add-on* in the same places.

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
- **Open-Meteo ICON** (`use_openmeteo`, on by default) — an independent gust
  forecast on a 1–2 km grid, from the same ICON model family MeteoSwiss uses.
  It only ever joins the gust decision on the cautious side: the gust used is
  the **maximum** across every source that answered, so an extra source can make
  the system more careful, never less.

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

## Step 2 — Get access to the `/addons` folder

Install either the **Samba share** app (`\\homeassistant\addons` from Windows,
`smb://homeassistant/addons` from macOS) or the **Advanced SSH & Web Terminal**
app. The folder is still called `addons` despite the Apps rename.

## Step 3 — Copy the add-on folder

Copy the whole `swiss_meteo_shade/` folder into `/addons` on your Home Assistant
machine, so you end up with `/addons/swiss_meteo_shade/`.

The folder name must match the `slug` in `config.yaml` character for character —
**underscores, not hyphens**. A mismatch (e.g. a folder called
`swiss-meteo-shade`) still installs and runs fine, but the Supervisor then can't
load `translations/en.yaml`, so the Configuration screen silently shows raw
option keys instead of the friendly names. If you see that, check the folder
name first.

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
readable name and description for each option instead of the raw keys — make
sure it comes along. Without it the add-on still works, but the options screen
is bare.

Nothing else from the repository belongs in `/addons`. The `tests/` and `tools/`
folders sit beside the add-on precisely so that copying the add-on folder can't
accidentally ship them.

## Step 4 — Make the app appear

1. Settings → **Apps** → **⋮** (top right) → **Check for updates**. This makes
   the Supervisor notice the new folder.
2. Immediately click **Install app** (bottom right) to open the store.

"Swiss Meteo Shade" appears under **Local apps**. It is only visible *inside the
store view*, not in the Apps list, until it has been installed.

> Not showing? Enable **Advanced Mode** in your user profile (bottom left) and
> repeat both steps.

## Step 5 — Install it

Click **Swiss Meteo Shade** → **Install**. The Supervisor builds a container on
your device (downloads numpy/h5py; a few minutes). The **Log** tab shows
progress.

## Step 6 — Find your coordinates

Open **https://map.geo.admin.ch/**, right-click your building, and read the
**CH1903+ / LV95** line — two numbers, e.g. `2 665 512, 1 211 882`. First is
easting, second northing. LV95 is used because the radar grid is native to it.

Also note your **postcode** for the `plz` option (used by the app fallback).

## Step 7 — Configure

Open the app's **Configuration** tab:

| Option | Meaning | Default |
| --- | --- | --- |
| `easting` | LV95 easting (step 6) | *Lucerne placeholder — replace* |
| `northing` | LV95 northing (step 6) | *Lucerne placeholder — replace* |
| `plz` | your postcode, for the app fallback | `"6006"` |
| `gust_limit_kmh` | retract the awning at/above this gust | `40` |
| `gust_release_kmh` | re-extend below this (hysteresis); `0` = off | `0` |
| `min_temp_c` | don't deploy shade below this °C; blank = off | *(blank)* |
| `use_openmeteo` | include the Open-Meteo ICON gust source | `true` |
| `prefer_app_forecast` | try the app feed before the official one | `false` |
| `lookahead_hours` | how many hours ahead to look | `2` |
| `sun_min_awning` | minutes of sun per hour before the **awning** extends | `20` |
| `sun_min_backup` | minutes of sun per hour before the **backup blind** closes | `20` |
| `sun_min_independent` | minutes of sun per hour before the **independent blinds** close | `20` |
| `radar_threshold_mmh` | rain rate that counts as rain | `0.1` |
| `radar_tolerance_km` | tolerance around your cell for forecast steps | `1` |
| `radar_fail_safe` | if the radar is unreachable, treat as rain (retract) instead of assuming dry | `false` |
| `interval_seconds` | how often to run | `300` |
| `forecast_max_cache_minutes` | force a full forecast re-download after this long, even if the server says unchanged; `0` = never force by timer (rely on the conditional request) | `60` |

Replace the coordinate defaults. `gust_limit_kmh` is the number you will most
likely tune — see *Tuning* below.

## Step 8 — Start it

**Info** tab → turn on **Start on boot** and **Watchdog**, then **Start**.

## Step 9 — Check it worked

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
means the official source is working. `Gust (Open-Meteo ICON)` is fetched
alongside the primary, so it's normally populated whenever `use_openmeteo` is on
— it's the one to compare the primary against.

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

Each gust source is exposed as its own diagnostic sensor so you can see which
one drove a retract: `sensor.swiss_meteo_shade_gust_official`,
`sensor.swiss_meteo_shade_gust_app`, and (if `use_openmeteo` is on)
`sensor.swiss_meteo_shade_gust_openmeteo`. The `forecast_gust` sensor is the
maximum across them — the value the decision actually uses.

---

## Step 10 — Automations

Extend the awning when safe, retract on the override:

```yaml
# Trigger on the single recommendation sensor, plus on HA start so the covers
# are reconciled to the current recommendation after a reboot.
alias: Shade follows the weather
triggers:
  - trigger: state
    entity_id: sensor.swiss_meteo_shade_shade_recommendation
    to: ["extend", "backup", "none"]   # real changes only, never attribute updates
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
> closed. The earlier version closed the backup blind in the `backup` branch but
> never reopened it, so once wind or rain triggered it, it stayed down forever.
> Now each branch drives the backup blind to its correct position, and the
> `homeassistant: start` trigger re-applies the right state after a reboot
> instead of leaving whatever survived the restart.

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

Notify when a new error or warning is recorded:

```yaml
alias: Notify on shade error
triggers:
  - trigger: state
    entity_id: sensor.swiss_meteo_shade_last_error   # state = event timestamp
conditions:
  # only when the timestamp changed to a real value -- not on startup, not on
  # attribute churn, not when it's unknown/empty (no error recorded yet)
  - condition: template
    value_template: >-
      {{ trigger.to_state.state not in ['unknown', 'unavailable', '', 'None']
         and trigger.to_state.state != trigger.from_state.state }}
actions:
  - action: notify.persistent_notification
    data:
      title: "Swiss Meteo Shade error"
      message: >-
        {{ state_attr('sensor.swiss_meteo_shade_last_error',
                      'last_error_message') }}
```

**Repeats don't re-notify.** The sensor's state is the timestamp of when the
current message *first* appeared, and it stays put while that same message keeps
recurring. A condition that persists for hours — a stale radar, a forecast on
its fallback — therefore notifies **once**, not every cycle. The attributes come
from a small dedicated topic carrying only the message and that timestamp, so
nothing churns between cycles either. Every individual occurrence is still
timestamped in the add-on **Log**, and a genuinely different message updates the
sensor and notifies again.

Swap `last_error` for `last_warning` for the softer channel. The condition is
still worth keeping: the trigger alone fires on every state publish (including attribute
updates each cycle), so without the guard you would get a notification every
few minutes reading "Unknown". The condition restricts it to an actual new
event whose timestamp differs from the previous one.

---

## Tuning `gust_limit_kmh`

40 km/h is a cautious default. To tune it, watch the `forecast_gust` sensor over
a week or two against how your awning actually behaves in wind. Raise the limit
if it retracts on breezes the awning tolerates; lower it if it stays out in wind
you would rather it didn't. Because the gust used is the max across sources, the
system errs toward retracting — the safe direction.

`lookahead_hours` widens or narrows how far ahead the gust/sun outlook reaches.
2 hours suits "should the awning be out right now". Increase it for earlier
warning at the cost of more cautious behaviour.

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
to track. Phase correlation on a clear sky returns a large, random vector (a
cloudless afternoon once produced ~90 km/h pointing two different ways between
overlapping frames), which would sample a cell 10-20 km away and could pick up
marginal echo that was never approaching. With nothing to track the app uses
zero motion, which is also physically right: no echo nearby means nothing can
arrive within the lead time.

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

`rain_forensics.py` is the one you're most likely to want: if the awning ever
moves and you can't see why, it reconstructs that exact cycle from the public
archive.

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
