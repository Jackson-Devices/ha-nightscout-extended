# Nightscout Extended for Home Assistant

Brings the values Nightscout already calculates into Home Assistant: **insulin on board, carbs on board, the AR2
glucose forecast**, current basal, and the latest glucose with its trend. It also makes Nightscout's measured
glucose history available to Home Assistant dashboards and actions. Home Assistant's built-in Nightscout
integration only provides glucose. This one can run alongside it.

> [!WARNING]
> **Not a medical device. Don't make treatment decisions from it.** Values are read from your Nightscout site and
> can be late, missing or wrong. IOB and COB are only as accurate as the treatments logged in Nightscout and the
> profile settings it calculates them from. The AR2 forecast is a short statistical extrapolation, not a prediction
> of what insulin or food will do. Always confirm with your meter, CGM app or pump.

## Sensors

| Sensor | Unit | Notes |
|---|---|---|
| Glucose | mg/dL or mmol/L | Follows your Nightscout's unit setting; attributes `direction`, `delta`, `thresholds_mgdl` (your site's alarm levels) |
| Glucose change | mg/dL or mmol/L | Nightscout's calculated change when it matches the displayed reading |
| Glucose trend | – | Rising quickly … Falling quickly |
| Last reading | timestamp | When the latest CGM value was taken |
| Insulin on board | U | Nightscout `iob` plugin; attribute `source` (e.g. Care Portal, Loop, OpenAPS) |
| Carbs on board | g | Nightscout `cob` plugin |
| Forecast in 15 minutes | mg/dL or mmol/L | Nightscout `ar2` plugin with `source_time` and `forecast_time` |
| Forecast in 30 minutes | mg/dL or mmol/L | Same timestamps, plus the predicted curve in `forecast` |
| Last carbs | g | Most recent nonzero carb intake at or before the current time. Attributes include `time`, `event_type` and `notes`. |
| Last insulin | U | Most recent nonzero insulin intake at or before the current time, with the same attributes. |
| Current basal | U/h | Disabled by default; enable it if you use a pump |
| Insulin age | h | Hours since the latest nonfuture `Insulin Change` in the site's retained treatments. Attributes `source_time` (UTC) and `event_type` identify the change. |
| Cannula age | h | Hours since the latest nonfuture `Site Change` in the site's retained treatments. Attributes `source_time` (UTC) and `event_type` identify the site change. |
| CGM sensor age | h | Hours since the latest nonfuture `Sensor Start` or `Sensor Change` in the site's retained treatments. Attributes `source_time` (UTC) and `event_type` identify the event. |
| Pump battery age | h | Hours since the latest nonfuture `Pump Battery Change` in retained treatments, with UTC `source_time` and `event_type`. Battery percentages and device uploads leave this age unchanged. |

Insulin age uses the Nightscout treatments read permission. It is unknown when the retained history contains no eligible
change, and unavailable when the lookup fails. Boluses leave the selected change timestamp intact. With the legacy API,
the retained range uses the UTC `created_at` field that Nightscout writes for treatments. Older imports without a
stored `created_at` field require v3 for age lookup.

CGM sensor age uses the same treatment history. `Sensor Change` wins if both events have the same timestamp.

### Insulin remaining

The **Insulin remaining** sensor shows the amount reported by AAPS for a Dana-i pump, in U.
It accepts zero and fractional values. Its `source_time` is the AAPS-observed pump-contact time,
shown in UTC. A reading is available for up to and including 60 minutes after contact.

Under **Configure**, you can enter the exact `device` label from your site's Nightscout device status
as **Reservoir uploader**. The label identifies the uploader and can include the phone model.
AAPS uploaders can share a label, so use a distinct source if you have multiple uploaders.
If you leave the option blank, Home Assistant saves the first unambiguous uploader that supplies a
valid reading. Reloading or restarting keeps that binding. An explicit choice takes precedence.
The live `uploader` attribute shows the label and is excluded from Recorder attributes and diagnostics.

This sensor uses `/api/v3/devicestatus` or the equivalent legacy `/api/v1/devicestatus/` read.
Nightscout needs to permit device-status reads. A denied read, missing or stale contact, ambiguous
uploader, or invalid or conflicting latest amount marks this sensor unavailable. Glucose and the
treatments calendar continue to use their own reads.

## Treatments calendar

`calendar.<site>_treatments` shows every Nightscout treatment as an event: meals, boluses, notes, temporary
targets, and anything logged for a **future** time. The summary reads like `Meal Bolus: 30 g carbs`,
`Correction Bolus: 0.5 U insulin`, or the first line of a note. The full note is in the event description.

Home Assistant uses a positive treatment duration from Nightscout when available. Momentary events and events with
no usable duration get a five-minute display interval. The calendar widens its read by the longest applicable
duration to include overlapping events that started before the requested range. If a range read fails or reaches
the legacy result cap, it uses the treatments from the last successful poll.

### Example: an alarm before a planned meal

Log a note at the meal's time in Nightscout (or let an app do it; Kitchen Carbs sends planned meals as
`Planned meal: …` notes). A calendar trigger fires at the event's start, or a set time before it:

```yaml
automation:
  - alias: Mealtime reminder
    triggers:
      - trigger: calendar
        event: start
        entity_id: calendar.nightscout_treatments
        offset: "-0:10:00"   # 10 minutes before
    conditions:
      - condition: template
        value_template: "{{ trigger.calendar_event.summary.startswith('Planned meal') }}"
    actions:
      - action: notify.notify
        data:
          title: Mealtime in 10 minutes
          message: "{{ trigger.calendar_event.summary }}"
```

Home Assistant checks calendars for upcoming events every 15 minutes, so a reminder for something logged less than
15 minutes ahead may be skipped.

> Log future meals as **notes**, not with carbs filled in. Nightscout counts carbs and insulin from the
> treatment's time onwards whether or not they happened, so a planned meal with carbs would show up in COB and the
> forecast if it's skipped.

## Glucose card

The integration comes with a glanceable glucose display for a kitchen tablet, a bedside screen or any dashboard.
It loads automatically: there's no resource to add. In a dashboard, **Add card → Nightscout Glucose**, or:

```yaml
type: custom:nightscout-glucose-card
name: Sam
```

| Day | Night (coloured text on black) |
|---|---|
| ![Glucose card](docs/glucose-card.png) | ![Glucose card at night](docs/glucose-card-night.png) |

- **Big reading, trend arrow and change**, with how long ago it was taken and a bar that empties as it gets older.
- **Coloured by your Nightscout alarm levels** (`BG_LOW`, `BG_TARGET_BOTTOM`, `BG_TARGET_TOP`, `BG_HIGH`): urgent
  low, low, in range, high, urgent high. Grey with a warning once there's been no reading for 15 minutes.
- **Chart behind the number**: measured Nightscout history with gaps, the target range, and the AR2 forecast as a dotted line.
- **Night mode** from 21:00 to 07:00 by default.
- **IOB, COB and the 30-minute forecast** along the bottom.
- mg/dL or mmol/L, following the sensor's unit.

It finds the integration's sensors itself; set `entity` if you have more than one Nightscout site.

| Option | Default | |
|---|---|---|
| `entity` | found automatically | The *Glucose* sensor of the site to show |
| `name` | none | Text at the top, e.g. whose readings these are |
| `fill_screen` | `false` | Fill the screen from the card's top edge down, for a panel view on a kiosk (works with the header hidden) |
| `hours_to_show` | `3` | Hours of history in the chart (1–48) |
| `text_size` | `100` | Size of the clock, reading age, name and IOB/COB line, in percent. They grow and shrink with the card and never go below a readable minimum; raise this on a small card |
| `show_chart`, `show_footer`, `show_clock` | `true` | Turn parts off |
| `night_mode` | `true` | Dim to coloured text on black at night |
| `night_start`, `night_end` | `21`, `7` | Night hours (0–23) |
| `stale_minutes` | `15` | Reading age at which the card turns grey. Also limits forecast source age and how far apart chart points can be connected. |

For a full-screen display, put the card alone in a **panel** view and set `fill_screen: true`.

> The card shows what Home Assistant last read from Nightscout. It is not an alarm: it makes no sound and can't
> tell you about readings Home Assistant hasn't received. Keep your CGM app's alerts on.

## Installation

**Marketplace (HACS):** search for *Nightscout Extended* and install. Until it's in the default list, add this
repository with **Add from link** (or *Custom repositories*, category *Integration*).

**Manual:** copy `custom_components/nightscout_extended` into your Home Assistant `config/custom_components/` folder.

Restart Home Assistant, then go to **Settings → Devices & services → Add integration → Nightscout Extended**.

## Setup

- **URL**: your Nightscout address, e.g. `https://mysite.example.com`.
- **Access token or API secret**: create a token in Nightscout under **Admin tools → Subjects** with the
  **readable** role and paste it here. The API secret works too, but it gives full write access, so a read-only
  token is safer. Leave it empty if your site is publicly readable.

Under the integration's **Configure** button, set **Update interval** (30–900 seconds, default 60) and
**Local glucose history** (1–30 whole days, default 1). Longer retention uses more local storage and takes
longer to sync. The update interval controls when Home Assistant checks for data.

### Nightscout API compatibility

Home Assistant reads Nightscout independently of the API used by your uploader. A site receiving v3 uploads can still serve this integration over v1 and v2. The integration selects its read path automatically and uses the same Home Assistant entities and display settings in either mode.

The stock reference is [Nightscout 15.0.6][stock-release], with its [v1 Swagger schema][stock-v1],
[v3 Swagger schema][stock-v3] and native server code. The table describes the intended Home Assistant behavior.
The [source-linked QA matrix at its fixed revision][stock-qa-matrix] lists the cases and evidence requirements
for both the stock wire and Home Assistant. See [QA pull request #2][stock-qa-pr] for the verification work.

- **Public or legacy site:** enter a URL such as `https://public.example`. Leave the credential empty for public reads, or enter the site's existing read token or API secret for authenticated legacy reads.
- **v3-capable site:** enter a URL such as `https://nightscout.example` and the site's readable subject token. After the legacy read check, the integration requests the public `/api/v3/version` and checks for a `3.x` API response, then exchanges the subject token for a bearer via `/api/v2/authorization/request/{subject-token}`. A missing capability route or a rejected subject exchange with HTTP 401 keeps already validated legacy reads. A malformed version or an unexpected API major raises an error. Protected collection requests use the bearer on the configured site. The client attempts one renewal after a protected HTTP 401 and reports persistent failures.

| Home Assistant behavior | Legacy reads | Reads with v3 enabled | Compatibility check |
|---|---|---|---|
| Setup, reauthentication, site title, units and alarm thresholds | `/api/v1/status.json` and `/api/v2/properties/bgnow` to validate access | Same site metadata and access validation, plus the v3 version check and token exchange | [Setup and identity][v3-check-setup], [renewal and denied reads][v3-check-auth] |
| Glucose and last reading | Newest eligible readable raw SGV from `/api/v1/entries.json`. A denied raw read can fall back to `bgnow` when the v2 measurement is readable. | Newest eligible readable raw SGV from `/api/v3/entries`, with its original measurement time | [Glucose selection and fallback][stock-qa-matrix] |
| Ordered glucose history | Bounded `/api/v1/entries.json` reads add or update readings in the retained window | Bounded `/api/v3/entries` reads plus changes from `/api/v3/entries/history/<cursor>` | [History action tests](tests_ha/test_sgv_ingestion.py) |
| Glucose change and trend | Use the selected raw SGV’s direction, including `TripleUp` and `TripleDown`. If direction is absent, trend is `unknown`. Keep the v2 delta only if the raw mg/dL value and measurement timestamp match exactly. If v1 denies the raw read and v2 provides the fallback, use its own direction and delta. | Same raw direction and exact delta matching rules | [Stock comparison cases][stock-qa-matrix], [legacy and v3 route tests](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/0817305cc69733cbffe4e23f9e995b7e84c4fc19/tests_contract/stock/three_way.py) |
| Insulin on board, carbs on board, and current basal | v2 `iob`, `cob`, and `basal` properties | Same computed values and available source attributes | [Calculated properties and settings][v3-check-derived] |
| 15- and 30-minute AR2 forecasts, and chart points | v2 `ar2.forecast.predicted`, anchored to `bgnow.mills` | Same v2 forecast and source time, even when the latest raw reading is newer | [Forecast selection and expiry][stock-qa-matrix] |
| Last carbs, last insulin and treatments calendar | `/api/v1/treatments.json` filtered by `created_at` | `/api/v3/treatments` combines bounded pages for `date` and `created_at` to include older records | [Treatment wire and calendar ranges][stock-qa-matrix] |
| Dashboard card and diagnostics | Home Assistant entities and `get_glucose_history` for the card, v1 site status and value presence for diagnostics | Same action, entities, and site settings | [Home Assistant integration tests][v3-check-setup] |

A v3 glucose reading ahead of Home Assistant's clock is shown only if it is less than 60 seconds ahead and has the same timestamp and glucose value as Nightscout's current v2 `bgnow` reading. Its original timestamp is preserved.

Both modes fetch site units and alarm settings via [v1 status (`/api/v1/status.json`)][stock-status] and
Nightscout's calculations via [v2 properties (`/api/v2/properties/bgnow,delta,direction,iob,cob,ar2,basal`)][stock-properties].

The v1 treatment schema declares numeric carbs and insulin. The v3 schema also declares numeric duration in
minutes. In the stock 15.0.6 check, v1 and its v2 treatment alias returned JSON `null` for quantities omitted
from the stored treatments, contrary to their declared number type. V3 omitted those fields. Explicit numeric
zero survived on all three routes. The v1 GET handler converts carbs and insulin to numbers before JSON
serialization, so the MongoDB representation can differ from the v1 and v2 wire values. [Stock v1 treatment serializer][stock-treatment-wire]

Home Assistant's treatment reader preserves finite JSON numbers, including explicit zero, for carbs, insulin and
duration on both read paths. Missing fields, `null` and malformed values such as numeric strings leave that
quantity unset. The event and its other valid fields remain available. The intake sensors select the latest
nonzero value at or before the current time using the treatment's event time.

[v3-check-setup]: https://github.com/aperetti/ha-nightscout-extended/blob/a8acabe41d6380f6647f9863c37bc9d776ffc9cc/tests_ha/test_integration.py#L32-L104
[v3-check-auth]: https://github.com/Jackson-Devices/ha-nightscout-extended/blob/a34f68f78a950330441c594869b4226971a37080/tests_contract/contract/cases.json#L244-L266
[v3-check-derived]: https://github.com/Jackson-Devices/ha-nightscout-extended/blob/a34f68f78a950330441c594869b4226971a37080/tests_contract/contract/cases.json#L286-L292
[stock-qa-pr]: https://github.com/Jackson-Devices/ha-nightscout-extended/pull/2
[stock-qa-matrix]: https://github.com/Jackson-Devices/ha-nightscout-extended/blob/0817305cc69733cbffe4e23f9e995b7e84c4fc19/docs/nightscout-stock-parity.md
[stock-release]: https://github.com/nightscout/cgm-remote-monitor/commit/9cd304f78a5c12401c9711cbd56d2a12eaca0632
[ha-glucose-conversion]: https://github.com/home-assistant/core/blob/357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b/homeassistant/util/unit_conversion.py#L230-L238
[stock-v1]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml
[stock-v3]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml
[stock-status]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api/status.js
[stock-properties]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api2/properties.js
[stock-treatment-wire]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api/treatments/index.js#L23-L80
[stock-ar2]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/ar2.js
[stock-timeago]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/timeago.js#L66-L112
[stock-settings]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/settings.js#L13-L45
[stock-history]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/generic/history/operation.js

## Glucose history

A poll may find zero, one, or many new or revised readings. The source measurement time (`date`) belongs to
the sample and is what `last_reading` and history use. The v3 server's modification clock (`srvModified`) tracks
changes, including late uploads with older measurement times. Home Assistant observes these changes on its own
polling schedule. A late reading can fill the chart at its original time without advancing current glucose.
Raw SGV explicitly labelled `mmol` or `mmol/L` is converted to mg/dL with the
[Home Assistant glucose conversion factor][ha-glucose-conversion] of 18.016. Raw mg/dL values retain their
amounts. In a synthetic Nightscout 15.0.6 check, v1 and v3 returned the raw 10 mmol/L sample with its original
measurement time. Stock v2 properties had no calculated `bgnow` measurement or delta for that sample.
The integration can show the raw sample as 180.16 mg/dL with its original time. It associates a calculated
delta only when the calculated amount and timestamp exactly match the selected reading.

Quiet polls preserve existing samples and timestamps. Repeated reads use stable record IDs to avoid duplicates,
and distinct IDs can share a timestamp. [Native v3 history handler][stock-history]

The bundled card uses the history action and shows gaps where coverage is missing or measurements are far
apart. Backfilled readings appear in this chart and in history action results. Home Assistant Recorder and
automations continue to receive live entity updates only, without retroactive state changes or trigger replay.

Use the site's primary **Glucose** sensor. The caller needs permission to read that entity. The range
includes the start and excludes the end. Supply ISO 8601 timestamps with an offset.

```yaml
- action: nightscout_extended.get_glucose_history
  data:
    entity_id: sensor.nightscout_glucose
    start: "2026-01-08T12:00:00+00:00"
    end: "2026-01-08T13:00:00+00:00"
  response_variable: glucose_history
```

- `readings` contains `id`, `date` (UTC epoch milliseconds), `sgv` (a number in mg/dL), and `direction`
  (a string or null), sorted by measurement time and ID. `unit` is always `mg/dL`.
- `status` is `complete` when the full range was checked, even if it contains no readings. `partial` means
  some of the range is known. `unavailable` means none of the requested range could be verified.
- `missing_ranges` lists uncovered intervals as `{start, end}` in UTC epoch milliseconds. `as_of` is
  the oldest coverage check used in the response (an ISO UTC timestamp or null).

Committed local history stays readable during an outage. Home Assistant asks Nightscout for uncovered
dates on demand. Recently fetched ranges can be reused briefly in memory and do not extend local retention.
V1 rereads the retained range on each poll. It adds returned readings and updates records with the same ID.
An omitted record stays in local history until it ages out because V1 cannot establish whether it was
hard-deleted. V3 applies reported soft deletes through the change feed and checks the retained range
periodically for hard deletes and older uploads. Existing local readings pick up the conversion when their
source rows are refreshed. Server limits or missing raw-read permission can leave
gaps, even when a legacy current glucose reading is available. Recovery depends on the records
Nightscout makes available.

## Forecasts and missing values

Home Assistant applies the earliest enabled Nightscout TimeAgo warning or urgent threshold to current glucose,
change and trend, using the selected reading's measurement time. Nightscout defaults to 15 minutes for
warning and 30 minutes for urgent. A reading remains fresh at the threshold and ages out once its age exceeds
it. With both settings disabled, there is no source-age limit. A local timer can mark a reading stale between
network polls. `last_reading` and stored glucose history keep their original measurement times.
[Nightscout settings defaults][stock-settings], [native TimeAgo][stock-timeago]

The two forecasts use the native AR2 curve from v2, anchored to `bgnow.mills` even when raw glucose is newer.
For each nominal horizon, Home Assistant selects the nearest usable point within three minutes of the anchor
plus 15 or 30 minutes. `source_time` is the v2 anchor and `forecast_time` is the selected point's actual time.
Both attributes use UTC, and a missing point has no target time. [Native AR2 plugin][stock-ar2]

Each forecast value becomes `unknown` when its own actual target time is reached or its v2 source exceeds
the enabled source-age limit. A disabled source-age limit leaves target expiry in effect. On expiry, Home
Assistant retains the two timestamps and the curve for charting. Repeated data keeps its original times,
so a poll cannot extend an expired forecast. The card omits expired points and uses its own `stale_minutes`
setting to hide stale curves. Forecast sensors use the site's display unit and the curve's `mgdl` values stay
in mg/dL.

A responding data source with no usable, current value leaves the affected sensor `unknown`. A failed request
can make its affected sensors `unavailable` while independent signals continue to update. A site-wide outage
makes the loaded sensors `unavailable` until polling succeeds again.

### Example: forecast chart

The `forecast` attribute is a list of `{time, mgdl}` points, which you can plot with e.g. ApexCharts card:

```yaml
type: custom:apexcharts-card
graph_span: 3h
span: { end: hour, offset: +1h }
series:
  - entity: sensor.nightscout_glucose
  - entity: sensor.nightscout_forecast_in_30_minutes
    name: Forecast
    data_generator: |
      return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.mgdl]);
```

## Development

```bash
uv run --with pytest --with aiohttp pytest tests -q                       # parsing and auth
uv run --python 3.14 --with pytest-homeassistant-custom-component \
  pytest tests_ha -q -c tests_ha/pytest.ini -p no:cacheprovider          # inside Home Assistant
node --test tests_js/*.test.mjs                                           # glucose card logic
```

The card is a single dependency-free file, `custom_components/nightscout_extended/www/nightscout-glucose-card.js`.
Its `CARD_VERSION` must match `manifest.json` (a test checks this); the version is part of the URL Home Assistant
loads it from, so browsers fetch the new card after an update.

### Releasing

1. Add a section for the new version at the top of `CHANGELOG.md` and bump `version` in `manifest.json` and
   `CARD_VERSION` in the card.
2. Commit, then tag and push: `git tag v0.3.0 && git push origin main v0.3.0`.

The *Release* workflow checks that the tag matches `manifest.json`, then publishes the release with that
version's changelog section as its notes (which Home Assistant shows in the update dialog).
