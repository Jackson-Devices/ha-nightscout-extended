# Nightscout Extended release handoff

**Publication stays on hold.** The Signals receiver qualified the product and the separate QA checkout below. The release-candidate receiver still owns the isolated preflight, the three refusal controls and the persisted hold receipt.

| Input | Exact pin |
| --- | --- |
| Combined product | `01ebf02d5bcc0275412e457f0f4c2821d14ea67b` |
| QA test checkout | `0628a3989d0a89eaefa806d3db22643758cce6d3` |
| Operator-accepted five-file H1 freeze | `f1a4e7dddf071f2c6d3eb2b27499ee4536d76ab3` |
| Published five-file freeze copy | `f2e2443df6f4961e3bd54d8daddb3d4e15871bc0` |
| Isolated rehearsal tag | `v0.3.2` |
| Observed upstream `v0.3.2` target | `4bf8594a6baa82189aac53b3e76cded687b87e6d` |

The upstream tag targets an earlier release. The maintainer controls the eventual upstream tag and release version. Use the inherited manifest and card version only to exercise the checkout, tag, changelog and package checks in a throwaway copy. [Product tree](https://github.com/Jackson-Devices/ha-nightscout-extended/tree/01ebf02d5bcc0275412e457f0f4c2821d14ea67b) · [QA tree](https://github.com/Jackson-Devices/ha-nightscout-extended/tree/0628a3989d0a89eaefa806d3db22643758cce6d3) · [Published suite copy](https://github.com/Jackson-Devices/ha-nightscout-extended/tree/f2e2443df6f4961e3bd54d8daddb3d4e15871bc0/tests_contract/contract) · [Existing upstream release](https://github.com/aperetti/ha-nightscout-extended/releases/tag/v0.3.2)

## Contribution heads

The six published fork refs are recorded in [candidate.json](candidate.json) with the exact tested compare base, first parent, head, and evidence. PR1 targets the pinned upstream main basis. PR2 and PR6 depend on adoption of PR1. PR3 through PR5 each depend on PR2 independently. Recheck upstream ancestry and scope before any future opening of a PR. There are no public upstream PRs from this release workflow. Claude verification is operator-owned and was not run.

| Slice | Qualified head | Focused evidence |
| --- | --- | --- |
| PR1 · Nightscout compatibility and ordered SGV | [4da9035](https://github.com/Jackson-Devices/ha-nightscout-extended/commit/4da903510209ecd84a37dc12d5c1149dd82b576b) | 161 cumulative local checks and both stock server smokes |
| PR2 · Insulin age | [cacb087](https://github.com/Jackson-Devices/ha-nightscout-extended/commit/cacb087398d8d73bd3931a6b9f1904b1c3248380) | 7 focused checks |
| PR3 · Cannula age | [019d9b6](https://github.com/Jackson-Devices/ha-nightscout-extended/commit/019d9b65fd3979b3abc3f539c422c8035545ce06) | 2 focused checks |
| PR4 · CGM sensor age | [db323d6](https://github.com/Jackson-Devices/ha-nightscout-extended/commit/db323d6da8bbe4fd14cfc3ddb0cd38bf9ad0c13c) | 8 focused checks |
| PR5 · Pump battery age | [7c44fc8](https://github.com/Jackson-Devices/ha-nightscout-extended/commit/7c44fc8184b4c8b5828dff299b28780926a61b24) | 4 focused checks |
| PR6 · AAPS/Dana-i insulin remaining | [86b9220](https://github.com/Jackson-Devices/ha-nightscout-extended/commit/86b92203118e496ac1afc3ee2452650faf39b638) | 18 direct checks and 3 Home Assistant checks on the corrected head |

## Evidence and limits

- The corrected composition has **203/203 comparable checks**, comprising 39 fresh and 164 carried-forward results. The separate two-entry reservoir isolation probe passed. Both stock Nightscout **15.0.6** disposable smokes exited 0. They exercised older and empty ranges, maintenance markers, zero and fractional reservoir readings, ambiguous uploaders, and the 121-minute-old uploader control. Protected v3 also exercised late insert, edit, tombstone and subject revocation. Cleanup removed the disposable resources. [Pinned native runner](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/0628a3989d0a89eaefa806d3db22643758cce6d3/tests_contract/harness/run.py) · [Native signal probe](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/0628a3989d0a89eaefa806d3db22643758cce6d3/tests_contract/harness/server_signals.py)
- The frozen strict comparison remains a separate historical diagnostic. The H2 baseline had 32 first-step mismatches because the strict fixture rejected `/api/v1/status.json` with HTTP 400, leaving HA in setup retry and masking later behavior. A later raw supplemental comparison recorded 1 pass and 12 comparison failures. Its integrated product contract ran separately. Retain those classifications and the five frozen blobs without rewriting historical expectations to obtain a green gate. [Frozen replay inputs](https://github.com/Jackson-Devices/ha-nightscout-extended/tree/f2e2443df6f4961e3bd54d8daddb3d4e15871bc0/tests_contract/contract) · [Integrated replay](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/0628a3989d0a89eaefa806d3db22643758cce6d3/tests_contract/adapter/test_supplemental_replay.py)
- All observations were synthetic. Stock-server smokes used disposable instances, with public legacy and protected read-only v3 clients. One earlier verifier stdout excerpt displayed a public synthetic authentication fixture value. Shared receipts must carry only the sanitized observation. A live-site comparison, production tag, release ingestion and Daybreak review remain open. The historical version heading supplies a consistency check. The proposed contribution text below is separate from the historical changelog. [Pinned historical changelog](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/CHANGELOG.md)

## Independent rehearsal

Run in the authorized Linux receiver with Git, Python 3 and Docker Compose. Pass the exact assembled release-tooling commit as the first positional argument. The QA and product clones are separately detached and checked. The only tags created below are local to the scratch product clone after its remote is removed. Use normal pinned dependency channels and keep evidence outside the three checkouts.

```bash
set -euo pipefail

tooling_sha=${1:?Pass the immutable NSX-RC assembly commit}
[[ $tooling_sha =~ ^[0-9a-f]{40}$ ]]
repo=https://github.com/Jackson-Devices/ha-nightscout-extended.git
qa_sha=0628a3989d0a89eaefa806d3db22643758cce6d3
product_sha=01ebf02d5bcc0275412e457f0f4c2821d14ea67b
work=$(mktemp -d)

pin_checkout() {
  local sha=$1 destination=$2
  git clone --quiet --no-tags --no-checkout "$repo" "$destination"
  git -C "$destination" fetch --quiet --no-tags origin "$sha"
  git -C "$destination" checkout --quiet --detach "$sha"
  test "$(git -C "$destination" rev-parse HEAD)" = "$sha"
  test -z "$(git -C "$destination" status --porcelain)"
}

pin_checkout "$tooling_sha" "$work/tooling"
pin_checkout "$qa_sha" "$work/qa"
pin_checkout "$product_sha" "$work/product"
git -C "$work/product" remote remove origin
test ! -e "$work/product/.git/refs/tags/v0.3.2"
git -C "$work/product" tag v0.3.2 "$product_sha"

candidate="$work/tooling/release/candidate.json"
notes="$work/tooling/release/daybreak.md"
preflight="$work/tooling/harness/release_preflight.py"
python3 "$work/qa/tests_contract/harness/bootstrap.py" --state "$work/state"
python3 "$preflight" collect --candidate "$candidate" --qa "$work/qa" \
  --state "$work/state" --output "$work/gates" --runner-id nsx-rcv --pull
python3 "$preflight" preview --candidate "$candidate" --qa "$work/qa" \
  --product "$work/product" --evidence "$work/gates/gates.json" \
  --notes "$notes" --output "$work/preview"
```

Keep the collector's real source identities, dependency and disposable-server receipts, cleanup, gate IDs and actual exit codes. The collector runs API, HA, card, HA positive and integrated supplemental checks, the four H1 controls, and the public legacy and protected v3 server smokes. Preserve the strict comparison separately. Preview consumes `gates.json`, checks the product and QA custody and the isolated tag, checks manifest/card/changelog agreement and these proposal notes, and produces `receipt.json` and `nightscout_extended-preview.zip`. Inspect the independently unpacked archive and its inventory against the candidate integration directory, including `__init__.py`, `manifest.json`, `translations/en.json`, `services.yaml` and the card asset `www/nightscout-glucose-card.js`. There must be one installable integration payload without unrelated project files or unsafe archive paths.

Run the three negative controls on scratch copies after the successful preview. Give each invocation a fresh output directory. Confirm its `receipt.json` identifies the intended refusal and that it produced no package.

```bash
python3 - "$candidate" "$work/gates/gates.json" "$work" <<'PY'
import json
from pathlib import Path
import sys

candidate_path, gates_path, target = map(Path, sys.argv[1:])
candidate = json.loads(candidate_path.read_text())
gates = json.loads(gates_path.read_text())
candidate["proposed_tag"] = "v0.0.0"
(target / "wrong-version.json").write_text(json.dumps(candidate))
missing = json.loads(json.dumps(gates))
del missing["gates"]["ha"]
(target / "missing-gate.json").write_text(json.dumps(missing))
failed = json.loads(json.dumps(gates))
failed["gates"]["ha"].update(status="failed", exit_code=1)
(target / "failed-gate.json").write_text(json.dumps(failed))
PY

git -C "$work/product" tag v0.0.0 "$product_sha"
expect_refusal() {
  local input=$1 evidence=$2 output=$3 rc=0
  python3 "$preflight" preview --candidate "$input" --qa "$work/qa" \
    --product "$work/product" --evidence "$evidence" --notes "$notes" \
    --output "$output" || rc=$?
  test "$rc" -eq 1
  test -s "$output/receipt.json"
  test ! -e "$output/nightscout_extended-preview.zip"
}
expect_refusal "$work/wrong-version.json" "$work/gates/gates.json" "$work/refused-version"
expect_refusal "$candidate" "$work/missing-gate.json" "$work/refused-missing"
expect_refusal "$candidate" "$work/failed-gate.json" "$work/refused-failed"
```

A failed or incomplete rehearsal stays on hold with its artifacts and observed failure. Preserve the upstream tag mismatch as a separate publication hold even when the isolated rehearsal passes.

## Defensive Daybreak review

Use only product commit `01ebf02d5bcc0275412e457f0f4c2821d14ea67b`, the QA pin and the independent preflight receipts for the eventual review. Use synthetic fixtures and disposable services. Require evidence for these behavior and security checks before any release recommendation.

- Trace normal HA polling as a scheduling clock. Empty and quiet polls preserve previously observed SGVs. Mixed batches and backfills preserve source IDs and original measurement times. Verify current-value selection, recorded gaps, partial and stale availability, equal-time distinct IDs, updates and deletions, and checkpoint recovery after paging failure or restart. Confirm the authorized per-entry history service exposes retained or on-demand source readings without inventing backdated Recorder states. [SGV wire adapter](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/sgv_source.py) · [SGV history store](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/sgv_history.py)
- Check AR2 against the v2 `bgnow` anchor independently of the current v3 glucose. Check each missing horizon and stale or expired card points, then verify recovery with a shifted anchor. Confirm anonymous legacy and retained v1/v2 reads, protected v3 scope and revocation, continued calculated values on legacy raw-read denial, and independent state on product-family outages. [API and source clocks](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/api.py) · [HA coordinator](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/coordinator.py)
- Check that each age follows its latest eligible treatment occurrence, including older/backfilled, duplicate, missing and future records. Legacy age lookup uses Nightscout's stored UTC `created_at`; older imports missing it need v3. Check that the reservoir uses UTC pump-contact time and an inclusive 60-minute freshness rule while uploader discovery uses the outer upload window. Review zero, invalid, stale, missing, conflicting and ambiguous measurements. A denied device-status read must leave the other available data families and a second site usable. [Age mapping](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/age.py) · [Sensor projections](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/sensor.py)
- Check credential redaction and read-only endpoint/role scope, token refresh, redirect and auth-error handling, and bounded query/paging behavior. Check per-entry file permissions, lifecycle, cache and uploader identity containment, diagnostics, Recorder attribute exclusion, and entity authorization across two configured entries. Review pinned dependency and image custody, integration packaging, CI permissions, event inputs and absence of unintended write authority. Keep all logs and regression evidence free of auth material. [Entry lifecycle and service authorization](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/__init__.py) · [Diagnostics](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/custom_components/nightscout_extended/diagnostics.py) · [CI permissions and jobs](https://github.com/Jackson-Devices/ha-nightscout-extended/blob/01ebf02d5bcc0275412e457f0f4c2821d14ea67b/.github/workflows/validate.yml)

The receiver records the pass or refusal and persists the source/evidence hashes and hold state outside this session. Close builder and receiver capabilities and cancel recovery before returning the handoff. The hold has no outgoing execution edges. Keep work, recovery, mode changes, publication, release ingestion and automation disabled across restart. Daybreak can begin only in a separate operator-initiated run with an explicit grant tied to the immutable candidate and recorded findings. That grant does not itself approve a production tag or publication.

## Proposed contribution release notes

- Add protected v3 reads while retaining existing public legacy and v1/v2 calculations and credentials. Keep each Home Assistant data family available when its own source remains healthy.
- Retain an ordered, configurable local window of Nightscout glucose observations and fetch older intervals on demand. Render real timestamps and gaps. Account for quiet polls, late uploads, edits, deletions and recovery when checking raw history.
- Keep AR2 forecasts tied to their original v2 calculation clock and let either horizon become unknown independently.
- Add treatment-based insulin, cannula, CGM and pump-battery ages with source timestamps. Add optional AAPS/Dana-i insulin remaining in units, with per-site uploader selection and a UTC pump-contact timestamp. Preserve usable zero readings and mark stale or ambiguous data unavailable.
- Cover legacy and protected v3 with synthetic tests and disposable stock Nightscout. Operators can review the exact tested heads and compatibility scope in the candidate manifest.
