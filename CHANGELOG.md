# Changelog

Each version's section becomes its GitHub release notes, which Home Assistant shows in the update dialog.
Add a section here before tagging a release.

## [0.3.3] - 2026-11-22

### Added
- **Glucose card colour modes.** By default (`colour_mode: site`) the card now paints readings exactly like
  Nightscout's own display, following your site's theme. `three`, `five`, `custom_three` and `custom_five` are
  available in the card editor. The glucose sensor has a new `site_theme` attribute for this.
- **Glucose card** shows active low and high threshold alarms in a separate, accessible badge. Home Assistant
  uses the site's settings by default and provides individually clearable mg/dL threshold overrides in
  **Configure**. The integration sends no notifications and makes no changes to Nightscout.

### Fixed
- **Glucose card**: exact Nightscout thresholds show the correct colour in mg/dL and mmol/L, including for
  older glucose entities without canonical values. Change and forecast now follow the card's glucose unit
  even when their Home Assistant entities use a different unit.
- **Glucose card** keeps readings such as 39 and 401 mg/dL numeric in either display unit and uses Nightscout's
  mmol/L rounding for native values. The chart expands vertically when needed so extreme readings remain distinct.
- Glucose, change and forecast sensors expose `native_mgdl` for the unrounded mg/dL value. The glucose
  sensor also includes `source_time`, pairing the reading with its original timestamp.

## [0.3.2] - 2026-11-21

### Changed
- **Glucose card**: the clock, reading age, name and IOB/COB line no longer shrink to an unreadable size on a small
  card. A new `text_size` option (in percent, also in the card editor) makes them bigger or smaller.

## [0.3.1] - 2026-11-13

### Fixed
- **Glucose card** no longer gets cut off on the right when it sits next to a taller card in a horizontal stack,
  e.g. on a tablet in portrait. It now fills its slot instead of growing wider than it.

## [0.3.0] - 2026-11-12

### New
- **Glucose card** (`custom:nightscout-glucose-card`): a large, glanceable glucose display for a tablet or bedside
  screen, with the trend arrow, change, reading age, a chart of recent readings and the AR2 forecast, and IOB/COB.
  It's coloured by your Nightscout alarm levels, dims to coloured text on black at night, and can fill the screen
  on a kiosk. It's served by the integration and loads on every dashboard automatically; no resource or extra
  card to install. See the README for options.
- The **Glucose** sensor has a new `thresholds_mgdl` attribute with your site's alarm levels (`BG_LOW`,
  `BG_TARGET_BOTTOM`, `BG_TARGET_TOP`, `BG_HIGH`), in mg/dL. Nightscout's defaults fill in any that aren't set.

## [0.2.0] - 2026-11-09

### New
- **Treatments calendar**: every Nightscout treatment (meals, boluses, notes, temporary targets) as a calendar
  event, including ones logged for a future time. Use a calendar trigger to set an alarm before a planned meal; see
  the README for an example automation.
- **Last carbs** and **Last insulin** sensors, with the time, event type and notes as attributes.

### Note
- Log future meals as notes rather than with carbs filled in, so Nightscout doesn't count them in carbs on board
  before they're eaten.

## [0.1.0] - 2026-11-09

### New
- Glucose, glucose change, trend and last-reading sensors.
- Insulin on board and carbs on board (Nightscout `iob` and `cob` plugins).
- AR2 forecast at 15 and 30 minutes, with the full predicted curve as an attribute.
- Current basal (disabled by default).
- Setup from the UI with a read-only access token or the API secret, re-authentication, and an adjustable update
  interval.
- Follows the site's mg/dL or mmol/L setting.
