# Nightscout 15.0.6 compatibility results

The published OpenAPI gate failed on the stock server. Both candidate Home Assistant modes passed the real-stock comparison for every field exercised in this fixture. The same server also reproduced a missing calendar event in historical Extended and observed the repaired calendar returning it.

## The server and its published contracts

The four HA readers used one digest-pinned disposable server and one synthetic dataset at the 2026-11-22 14:05 UTC observation. The 35 effective permission checks agreed. The native data snapshot before and after the readers was identical. The initial seed included records imported into Mongo, representing data a legacy uploader could have left behind. A separate late insert used the authenticated v3 API. The read-side observations below apply to this fixture and release. [Nightscout release source](https://github.com/nightscout/cgm-remote-monitor/tree/9cd304f78a5c12401c9711cbd56d2a12eaca0632) · [Published v1 JSON](https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/server/swagger.json) · [Published v3 JSON](https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/api3/swagger.json)

The full response validator found 59 distinct route and field diagnostics, in eight patterns, across 11 routes. Repeated reads produced 186 occurrences. Seven documented routes passed. The nine reads of the v2 calculated-properties route have a stock implementation and no separate published response schema. The v1 profile schema also leaves parts of its response shape unconstrained. The native fixture separately checks the returned array. The machine witness records each route verdict and content hash.

| Observed response | Declared schema | Distinct diagnostics | Occurrences |
|---|---|---:|---:|
| v1 and mounted v2 treatment `carbs` or `insulin` present as `null` | Number if present | 26 | 104 |
| v1 and mounted v2 device status `loop` object | String | 2 | 8 |
| v1 and mounted v2 status `alarmTypes` | Returned value outside enum | 2 | 6 |
| v1 and mounted v2 status `timeFormat` integer | String | 2 | 6 |
| v3 generic document list is an array | `DocumentArray` declares an object | 5 | 14 |
| v3 generic document list matches zero alternatives | `oneOf` requires exactly one | 4 | 9 |
| v3 generic document list matches multiple alternatives | `oneOf` requires exactly one | 2 | 5 |
| v3 records read from the initial seed omit `app` | Required by the separately checked collection model | 16 | 34 |

## What the readers saw

All four readers reported a glucose of 180 mg/dL measured at 14:04 UTC, a delta of +12 mg/dL, and AR2 values of 199 and 204 mg/dL. The candidate readers exposed the original forecast source at 14:04 and selected target timestamps at 14:19:02 and 14:34:02. They exposed 0 U of insulin remaining with pump contact at 14:03, sensor/insulin/cannula/battery ages of 20/40/60/96 hours, and the historical sequence 157, 168 and 180 mg/dL. Shared stock values were 1.375 U IOB, 7.5 g COB and 0.85 U/h basal. All values are synthetic. [Four-reader stock comparison](../tests_contract/stock/evidence/phase-b.json)

| Reader | Stock comparison |
|---|---|
| Untouched Extended at `a8acabe4` | Omitted the 12:35–13:05 UTC Temp Basal that overlapped the queried calendar window. |
| Candidate using legacy v1/v2 | All compared public HA fields matched, including the overlapping Temp Basal. |
| Candidate using v3 | All compared public HA fields matched, including the overlapping Temp Basal. |
| Focused legacy calendar repair | Recovered the event on the same snapshot with the same effective permissions. |

The eight-case controlled replay varies endpoint timing and access independently. Both successful raw reads now display 162 mg/dL at 12:07 with unknown delta because v2 still describes the 12:00 sample. The deliberate legacy v1 403 fallback displays 153 mg/dL at 12:00 with native delta +7. Both keep the forecast tied to its 12:00 v2 source. The pre-fix legacy path displayed 153 after the successful v1 raw read. [Measured pre-fix modes](../tests_contract/stock/evidence/phase-a-recovery.json) · [Measured post-fix modes](../tests_contract/stock/evidence/phase-b.json)

## Decisions made by Home Assistant

| Decision | Implemented rule |
|---|---|
| Current SGV and trend | For an eligible positively observed raw SGV, keep its original measurement time and direction. Preserve an absent direction. Attach v2 delta only when both its `bgnow` source time and normalized glucose agree exactly. On a genuine legacy raw denial, use the available v2 reading and its metadata. |
| Freshness and availability | Apply the site's enabled Nightscout TimeAgo limits to live glucose, delta and trend. Crossing a source-age limit makes the applicable live value `unknown` on the next representable stock millisecond. Both limits disabled means no source-age expiry. A source failure uses the applicable HA `unavailable` state. Last-reading time and retained history stay accessible. |
| Forecasts | Nightscout computes AR2. HA selects each horizon from the native curve within the documented tolerance, using the original v2 source time. It makes each scalar `unknown` at its selected target or when its own source goes stale. The source time, actual selected target and historical curve remain inspectable. |
| Treatment quantities and calendar | Keep explicit numeric zero in the parsed record. Missing and invalid quantities cannot create a dose. Last intake uses the latest nonzero quantity. HA gives momentary treatments a display duration and returns events overlapping a half-open requested window. |
| History, units and presentation | Store and order readings by measurement time, use server modification time to discover changes, retain local history for the configured window and fetch older dates on request. HA chooses display units and enum labels. |

The [signal matrix](nightscout-stock-parity.md) links every supported family to the relevant Swagger path, stock implementation and regression. The unmodified five-file frozen comparison remains a separate diagnostic.

## Final verification

The tested and final candidate share component tree `c5402deaed17b3425b3f767202c3761b6f90d48b`. After the live stock run, the product changed one documentation row and pinned the HA threshold test to its fixture's time. All 56 files from the final product checkout matched the tested QA checkout at closure. The seven Swagger controls and eight controlled API-mode comparisons passed. The application suites passed 197 API tests, 87 HA tests and 16 card tests. The API and HA runs reported zero skips, expected failures, unexpected passes and deselections.

The six forecast observer tests passed. All 16 integrated supplemental and negative-control cases passed. The five frozen files remained byte-identical. Their raw historical comparison returned one passing case, twelve mismatching cases and 69 field differences, with zero harness errors. The original comparator controls returned `0/1/1/2`. This historical result and the strict stock Swagger failure are both included in the public record.

Coverage reached 1,752 of 1,897 statements (92.36%) and 537 of 650 branches (82.62%). The combined tree of the two focused main-branch fixes, `c94a370f16039208cd4dda7dd8b038d5e7b930b0`, passed 53 API tests and 16 HA tests. Those suites include the 41 treatment wire cases and the calendar overlap regression. [Coverage by module and missing line](../tests_contract/stock/evidence/term-missing.txt) · [Test counts and hashes](../tests_contract/stock/evidence/phase-b-closure.json)

The independent verifier exported 4,683 bytes of compact JSON directly from the stock and matrix receipts. Its SHA-256 is `5c083e145f7621275956397bb83ca4ae5454fcc86394654b3df90c9a0b4a55a4`. It includes the fixture hash, matched before/after snapshot hashes, per-route verdicts, reader outcomes, original measurement and forecast times, and the tested commits. [Exact machine-readable witness](../tests_contract/stock/evidence/public-witness.json) · [First failing baseline](../tests_contract/stock/evidence/phase-a.json)
