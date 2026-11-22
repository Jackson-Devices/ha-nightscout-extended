# Nightscout 15.0.6 stock parity and Home Assistant policy

The stock reference is the [Nightscout 15.0.6 release commit][release] (`9cd304f78a5c12401c9711cbd56d2a12eaca0632`) and disposable server image digest `sha256:868f024f0aaf746aa9c6f639b4add7a78b07bcf4382f9955c5af61d413cad989`. Use the [release's API v1 schema][v1] and [API v3 schema][v3] (spec version 3.0.4) together with the linked implementation. The [original freeze's separate reference][prior] (`c6a0d8ed65637bc90d3090e6e7a0f752041c57fd`) is Nightscout 15.0.9 with v3 spec 3.0.5. The frozen H1 suite is a separate diagnostic at `f1a4e7dddf071f2c6d3eb2b27499ee4536d76ab3`. The [untouched Extended legacy revision][upstream] and the candidate are clients under test.

The stock runner uses direct GETs against one seeded disposable server with equivalent effective read rights and a shared observation fence. Record the input, HTTP status, route, response shape, exact allowlisted values and their clocks before comparing the HA clients. Prove each seeded positive appears. Check deliberate absences explicitly. Compare response types and explain shape and clock differences before comparing values. A schema/server disagreement remains a failure of the affected stock claim. Equal client outputs, fixture expectations, plugin source and combined coverage each supply different evidence. This matrix specifies evidence to collect. Per-row observations and pass/fail outcomes require runtime receipts from the exact tested revisions.

| Signal or route | Stock 15.0.6 contract and raw comparison | HA projection and policy | Executable evidence |
| --- | --- | --- | --- |
| Current glucose and `last_reading` | [v1 entries][v1-entry] and [v3 entries][v3-entry] document SGV payloads. Compare the typed `sgv`, measurement epoch-millisecond `date`, and direction for the same raw records, including earlier readings. Separately record v2 `bgnow.last` and `bgnow.mills`. [bgnow][bgnow] calculates from analysed buckets. The latest raw row and the calculated source can advance at different times. | Match mg/dL values and exact measurement time across successful candidate v1 raw and v3 raw paths. HA unit presentation is separate. Legacy raw denial can retain the measured v2 fallback, with that route outcome explicit. | [Stock runner][stock], [three-way aligned and raw-advance fixture][threeway], [SGV ingestion][sgv-ingestion], [data recovery][recovery] |
| Delta and direction | For matched data capture v2 `delta.mgdl`, `direction.value` and the associated SGV clock. [bgnow][bgnow] derives the rounded delta from recent/previous bucket means and interpolates to five minutes for some gaps. [direction][direction] uses the last SGV entry. v1/v3 entry directions are source fields. Neither entries schema defines the v2 delta calculation. | Pair the direction with the selected reading. Expose calculated delta only when it applies to that current reading. The offline raw-advance case expects the same newer reading in both successful raw paths and unknown delta while v2 is unchanged. Unknown direction maps to unknown trend. | [Stock runner][stock], [three-way fixture][threeway], [SGV source][sgv-source], [SGV wiring][sgv-wiring], [data recovery][recovery] |
| SGV history, corrections and no-change | Compare v1 `find`/count results and v3 ordered search for distinct measurements and tied or older rows. [v3 history][v3-history] provides an edit/deletion feed with a server-modification clock and fallback for older rows. Its [implementation][history-impl] uses `srvModified`/watermarks. The measurement clock stays `date`. v1 range reads and v3 update/history polling have different contracts. | Apply late, duplicate, revised and replayed records by source identity/version, preserving exact measurement timestamp and value in the ordered `get_glucose_history` result. Keep committed rows through outage or a partial fetch. Quiet polls and equal-valued newer samples require separate checks. | [Stock runner][stock] for real v3 advance/no-change, [SGV source][sgv-source], [scheduled HA timeline][sgv-ingestion], [store][sgv-store], [SGV wiring][sgv-wiring] |
| Glucose freshness | [v1 status settings][v1-settings] expose `alarmTimeagoWarn`, `alarmTimeagoWarnMins`, `alarmTimeagoUrgent`, `alarmTimeagoUrgentMins`. [Stock defaults][stock-settings] enable warning/urgent at 15/30 minutes. [timeago][timeago] tests age strictly greater than the enabled threshold and checks urgent first. v3 status has different content. | HA applies the earliest enabled threshold to current glucose, delta and trend, preserving `last_reading` and history. Equality is still fresh. A local timer can change the state between network polls. A failed transport group and a responding group with stale or missing values have distinct unavailable/unknown outcomes. | [Stock runner][stock] for settings, [HA timeago boundaries][ha-freshness], [data recovery][recovery] and [scheduled HA timeline][sgv-ingestion] |
| AR2 +15, +30 and curve | Capture actual `ar2.forecast.predicted` `(mills, mgdl)` pairs and v2 `bgnow.mills`, or explicit omission/empty curve. [AR2][ar2] depends on usable bgnow/delta and emits five-minute steps anchored to bgnow with the point timestamp offset by 2,000 ms. Collection entries and uploader predictions do not convey this property. | The two horizon states use the v2 source even when raw glucose has advanced. Keep native target times and the AR2 attribution on the projection. Check each target just before, at and after its actual instant. Equality expires that HA horizon, and the other horizon can remain current. Keep the selected target, source time and curve after scalar expiry. A missing target has no timestamp. This local expiry and automation visibility are HA policy. | [Stock runner][stock], [HA subprocess probe][stock-probe], [three-way fixture][threeway], [forecast pairing and updates][ha-v3], [HA horizon boundaries][ha-freshness] |
| IOB and COB | Read `iob.iob`, `cob.cob` and available plugin provenance from v2. [IOB][iob] and [COB][cob] contain uploader-specific and treatment-based selection/math. Link comparisons to property presence at the fence. Treat numeric zero separately from missing plugin or result. Neither collection schema promises the calculated property. | Expose IOB in U and COB in g and preserve source attributes when provided. A missing property affects its own entity. | [Stock runner and probe][stock], [optional properties API tests][api], [SGV wiring][sgv-wiring] |
| Current basal | `basal.current.totalbasal` is a v2 property from [basalprofile][basal], through the profile and temporary basal effective at the server time. Read the actual number or establish absence. A profile document alone is insufficient. | The optional current basal sensor exposes U/h where calculated. | [Stock runner and probe][stock], [optional properties API tests][api] |
| Treatment timestamps, identity and quantities | The [v1 Treatment schema][v1-treatment] declares `carbs` (g) and `insulin` (U) as numbers alongside `created_at` and `eventType`. It omits `duration`. The [v3 Treatment schema][v3-treatment] declares both quantities and `duration` in minutes as numbers and inherits `date` and native document fields. Compare source IDs, event times, types, notes and actual typed values, including fractions and zero. [v1 GET serialization][treatment-impl] calls `Number` on quantities, so a string in storage can appear as a valid JSON number and a missing stored quantity can serialize as null. Verify actual null/omission outcomes separately. Never coalesce them to zero while comparing stock. | Validate finite JSON numbers received by the client and preserve valid sibling fields when an individual field is malformed. Retaining numeric zero in a parsed record is separate from selecting a recent positive intake for a last-carbs or last-insulin sensor. Use source event time, not upload or modification time. Adverse fixtures do not assert server rejection of malformed input. | [Stock runner][stock], [treatment wire tests][treatment-wire], [treatment parity][treatment-parity], [HA sensors and calendar][ha-integration] |
| Calendar overlapping ranges | [v1 treatments][v1-treatment] support nested `find` and `count`. [v3 search][v3-search] documents range, ordering and pagination. Compare raw rows for a long event starting before the range and events on both boundaries. Test dates from each upload family and preserve an observed legacy `duration` extension separately from schema requirements. [v3 collection setup][v3-collections] defines a treatment date fallback. | The calendar uses authoritative widened reads for positive durations and five-minute display intervals for momentary/default-duration events. Strict overlap requires `event.start < range.end` and `event.end > range.start`. Check older starts, cached fallback and range transitions. Event extent, event title and cache behavior are HA semantics. | [Stock runner][stock] for raw treatments, [native calendar probe][native-calendar] for positive overlap and duration-query wire routes, [HA overlap and fallback][ha-v3], [calendar trigger][ha-integration] |
| Cannula age | Assert the exact `Site Change` treatment occurrence and timestamp in v1 and v3. [Cannula Age][cage] uses source treatments to calculate/display age in Nightscout. | HA selects the newest matching site marker at or before now. Reservoir or insulin change does not substitute for it. | [Stock runner and probe][stock], [HA cannula][ha-cage] |
| CGM sensor age | Compare the actual `Sensor Start` and `Sensor Change` records as available. [Sensor Age][sage] applies site sensor marker semantics, including a change superseding a start at the same time. A CGM reading does not supply the event. | Choose the newest applicable marker, giving `Sensor Change` precedence on a tie, and preserve occurrence time across new glucose or later upload of an older marker. | [Stock runner and probe][stock], [sensor selection][api-sage], [HA sensor age][ha-sage] |
| Insulin age | Compare the exact `Insulin Change` event and occurrence time. [Insulin Age][iage] derives native display from insulin-change treatments. | Select this independent marker, including valid old events and revisions. A dose or a reservoir level is a different signal. | [Stock runner and probe][stock], [marker queries/revisions][api-iage], [HA insulin age][ha-iage] |
| Pump battery age | Compare the exact `Pump Battery Change` event and occurrence time. [Battery Age][bage] reads pump battery-change treatments. | Preserve this marker when newer telemetry arrives. Succeed with no age on a genuinely empty lookup and check recovery. | [Stock runner and probe][stock], [HA battery age][ha-bage] |
| Reservoir, contact and zero | Read [v1 devicestatus][v1-device] and [v3 DeviceStatus][v3-device] for raw uploader identity and the AAPS payload. The [v1 pump schema][v1-pump] declares numeric `pump.reservoir` and string timestamp `pump.status.timestamp`. The v3 schema leaves the nested status structure open. Compare the actual fields and numeric zero. v1 uses the [device status handler][device-impl]. v3 has the generic [collection date fallback][v3-collections]. [Native Pump][pump] uses its own recency and pump clock rules. | HA selects contact time with an exact uploader binding and checks the latest qualifying contact in its one-hour policy. Upload/serialization time bounds queries and cannot substitute for pump contact. Zero U is a valid result. Ambiguity, old contact, missing fields or denied device-status access stay scoped to the reservoir entity. | [Stock runner and probe][stock], [native signal probe][native-signals], [reservoir API][api-reservoir], [HA reservoir][ha-reservoir] |
| Site metadata, alarm thresholds and settings | [v1 status][v1-settings] supplies settings, enabled features, units and thresholds via its [handler][status-impl]. [v3 status][v3-status] supplies server/API metadata and collection grants via [its handler][v3-status-impl]. It does not mirror the v1 site settings object. The v3 `settings` collection is a separate application store. | Both readers use v1 for site title, units and alarm thresholds. Keep thresholds in mg/dL and apply HA fallback for missing keys. A successful v3 status request alone does not prove data read access. | [Stock runner][stock], [site units and thresholds][ha-integration], [HA v3][ha-v3] |
| Auth, capability and denials | Record successful reads by domain using equivalent rights. Legacy [authorization][auth-impl] accepts default roles and explicit credential mechanisms, while [v2 authorization request][auth-exchange] can issue the JWT for [v3 security][v3-auth]. The stock [readable role][roles] grants read, and [v2 properties][props] require both entries and treatments read. Show actual allowed route reads and lack of create rights with permission evidence. [v1 verifyauth][verifyauth] reports broad rights, so check relevant permissions individually. | Public or stored legacy-secret entries can use legacy. Eligible subject-token entries can use v3 collection reads plus v1 metadata and v2 computed properties. A protected collection denial is distinct from lack of v3 capability, a whole-site outage and a legacy raw SGV denial. Test each fallback or failure under its own route outcome. | [Stock runner][stock], [three-way positive and denied raw cases][threeway], [API and auth][api], [HA reload and legacy credentials][ha-v3], [HA scoped denial][sgv-wiring] |

The supported scope is the reads and projections above. It excludes patient sites, client writes, server-version generalization, direct profile or other application collections as substitutes for plugin output, and extrapolating AAPS reservoir fields to other uploaders. A plugin can be disabled or omit values given insufficient input. The presence or absence of IOB, COB, basal or AR2 in one dataset does not qualify other profiles, uploaders or configurations. Separate assertions for maintenance timestamp and HA age avoid implying equivalence with stock's age-pill display and notifications.

## Runners and evidence

Provide separate checkouts of untouched Extended and the composed QA candidate, the [locked HA environment][stage], and fresh scratch paths outside the checkout. The stock command needs Docker and starts a disposable pinned Nightscout/Mongo pair. The offline three-way command uses controlled fixtures. The HA freshness tests use the normal integration and scheduled time advancement.

```bash
python3 tests_contract/stock/run_stock.py \
  --upstream "$UPSTREAM" --candidate "$CANDIDATE" \
  --ha-python "$HA_PYTHON" --output "$SCRATCH/stock"

python3 tests_contract/stock/three_way.py \
  --upstream "$UPSTREAM" --candidate "$CANDIDATE" \
  --ha-python "$HA_PYTHON" --output "$SCRATCH/three-way"

python3 tests_contract/stock/coverage.py \
  --candidate "$CANDIDATE" --ha-python "$HA_PYTHON" --output "$SCRATCH/coverage"
```

For a focused run of the timeline tests during development, use this command from the composed checkout. The full coverage command already includes these cases.

```bash
"$HA_PYTHON" -m pytest -q -c tests_ha/pytest.ini -p no:cacheprovider \
  tests_ha/test_signal_freshness.py \
  tests_ha/test_sgv_ingestion.py::test_scheduled_timeline_preserves_source_times_and_public_history \
  tests_ha/test_data_recovery.py::test_same_entry_recovers_from_missing_data_outage_and_late_uploads
```

The [coverage command][coverage] is also the API and HA Python test gate. It invokes `tests/` and `tests_ha/` once each using the staged HA Python and locked coverage/pytest-cov, with separate test processes and data files. It preserves their logs and combines branch data into a `term-missing.txt` report with uncovered lines and branches. A test or coverage-tool failure exits nonzero. The report has no minimum percentage and covers the integration's Python files. Stock, offline differential, frozen H1 and card checks have their own evidence.

[release]: https://github.com/nightscout/cgm-remote-monitor/commit/9cd304f78a5c12401c9711cbd56d2a12eaca0632
[prior]: https://github.com/nightscout/cgm-remote-monitor/commit/c6a0d8ed65637bc90d3090e6e7a0f752041c57fd
[upstream]: https://github.com/Jackson-Devices/ha-nightscout-extended/commit/a8acabe41d6380f6647f9863c37bc9d776ffc9cc
[v1]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml
[v1-entry]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml#L675-L715
[v1-treatment]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml#L862-L909
[v1-device]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml#L716-L742
[v1-pump]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml#L743-L776
[v1-settings]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.yaml#L927-L1024
[v3]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml
[v3-entry]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml#L1263-L1303
[v3-treatment]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml#L1411-L1458
[v3-device]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml#L1252-L1260
[v3-history]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml#L392-L447
[v3-search]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml#L605-L699
[v3-status]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.yaml#L520-L548
[v3-status-impl]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/specific/status.js
[v3-collections]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/generic/setup.js
[history-impl]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/generic/history/operation.js
[v3-auth]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/security.js
[props]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api2/properties.js
[status-impl]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api/status.js
[stock-settings]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/settings.js#L13-L45
[timeago]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/timeago.js#L66-L112
[bgnow]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/bgnow.js
[direction]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/direction.js
[ar2]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/ar2.js
[iob]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/iob.js
[cob]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/cob.js
[basal]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/basalprofile.js
[treatment-impl]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api/treatments/index.js#L23-L80
[cage]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/cannulaage.js
[sage]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/sensorage.js
[iage]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/insulinage.js
[bage]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/batteryage.js
[pump]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/pump.js
[device-impl]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api/devicestatus/index.js
[auth-impl]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/authorization/index.js
[auth-exchange]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/authorization/endpoints.js#L21-L29
[roles]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/authorization/storage.js#L115-L124
[verifyauth]: https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api/verifyauth.js
[stock]: ../tests_contract/stock/run_stock.py
[stock-probe]: ../tests_contract/stock/ha_probe.py
[threeway]: ../tests_contract/stock/three_way.py
[coverage]: ../tests_contract/stock/coverage.py
[stage]: ../tests_contract/math_review/README.md
[api]: ../tests/test_api.py
[sgv-source]: ../tests/test_sgv_source.py
[treatment-wire]: ../tests/test_treatment_wire_contract.py
[treatment-parity]: ../tests/test_treatment_parity.py
[api-sage]: ../tests/test_cgm_sensor_age.py
[api-iage]: ../tests/test_insulin_age.py
[api-reservoir]: ../tests/test_reservoir.py
[native-calendar]: ../tests_contract/harness/server_calendar.py
[native-signals]: ../tests_contract/harness/server_signals.py
[ha-v3]: ../tests_ha/test_v3_integration.py
[ha-integration]: ../tests_ha/test_integration.py
[sgv-ingestion]: ../tests_ha/test_sgv_ingestion.py
[ha-freshness]: ../tests_ha/test_signal_freshness.py
[sgv-store]: ../tests_ha/test_sgv_store.py
[sgv-wiring]: ../tests_ha/test_sgv_wiring.py
[recovery]: ../tests_ha/test_data_recovery.py
[ha-cage]: ../tests_ha/test_cannula_age.py
[ha-sage]: ../tests_ha/test_cgm_sensor_age_ha.py
[ha-iage]: ../tests_ha/test_insulin_age.py
[ha-bage]: ../tests_ha/test_pump_battery_age.py
[ha-reservoir]: ../tests_ha/test_reservoir_integration.py
