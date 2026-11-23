# Contributing

These rules apply to every change, whoever or whatever writes it: people, Codex, Claude or any other agent.
A pull request that breaks any of them doesn't merge.

## The acceptance rule

**Behaviour must be 1:1 identical to stock Nightscout.** If the integration shows, computes, accepts or rejects
something differently from Nightscout at the pinned reference, that is a failure. The only exception is an entry
in the PR's exception register that the repository owner has approved in a PR comment.

Every pull request must keep that parity. "Close enough", "more correct than Nightscout" and "matches the
Swagger schema" are not the bar. What Nightscout's code *does* is the bar, cited by file and line at the pinned
commit.

Note on reviews: the repository owner, Codex and Claude all act through the same GitHub account, and GitHub
doesn't let a PR's author request changes. So reviews here are posted as **Comment** reviews. A review saying
"treat as changes requested" blocks the merge exactly as a formal request would.

## Checking stock parity

Validate runs preflight first. If preflight is red, nothing else runs, so no stock server starts and no CI time
is spent. If it's green, every job runs on every push to `main` and every pull request: there are no path filters
and no draft skips. `.github/scripts/check_workflow_gates.py` enforces both rules in preflight.

The Parity job compares three things on each of three stock site profiles (mg/dL default, mg/dL custom thresholds,
mmol thresholds entered in mmol):

| Name | What it is | Role |
|---|---|---|
| **Native** | Stock Nightscout 15.0.6 and MongoDB 5.0.32, pinned by image digest | The oracle. Expected values come from its responses, its stylesheet and its own source run from the container. |
| **Current** | The checked-out code of the commit being tested | **Gates the merge.** Any difference from native fails, apart from owner-approved exceptions. |
| **Legacy** | Upstream `aperetti/ha-nightscout-extended` at the fork point, `a8acabe` | **Report only.** The same suite runs against it on a clean site of the same profile. Its differences never fail the job. |

The job summary and the `parity-<profile>` artifact hold the three-way report. For each check it says whether
legacy and current match native, and classifies the result: corrected by current, still differing (blocks the
merge), new in current, or not applicable. It also lists where current differs from native by owner decision,
with the decision text and a link to its approval. That report is the record for the upstream developer: where
the original integration differs from Nightscout, what this fork corrected, and where this fork differs on purpose
and why.

The product under test is always the checked-out code. A check is never run against another branch's code in its
place. A check for a capability the product doesn't have, such as v3 reads on a v1-only branch, reports
"not applicable" with that reason.

Owner decisions on the card so far: **E-1d**, the card shows the number at 39 and above 400 mg/dL where
Nightscout's display prints its fixed `LOW`/`HIGH` text, with alarm state in a separate badge computed by
Nightscout's `simplealarms` rule from the site's alarm levels. **E-1b/E-1c/E-1e** are opt-in only (colour modes
other than `site`, and HA threshold overrides), so every default-configuration check stays strict.
[Stock display](https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/client/index.js#L518) ·
[Stock alarms](https://github.com/nightscout/cgm-remote-monitor/blob/9cd304f78a5c12401c9711cbd56d2a12eaca0632/lib/plugins/simplealarms.js#L39)

## Universal rules (U-1 to U-14)

| ID | Rule | Pass condition (all must hold) |
|---|---|---|
| **U-1** | **No silent fallbacks** | Every failure to read, parse or validate goes **visibly red**: the entity becomes `unavailable`, `UpdateFailed`, `HomeAssistantError`, a Repairs issue, or the card's error state. Each red carries a specific reason code, logged at least once at `WARNING`. Never substitute defaults, cached data or last-poll data and present them as current. **Only exception:** stock Nightscout does the same thing at the pinned SHA (cite file:line). Each failure path has a test asserting the red state **and** the reason code. |
| **U-2** | **Fail closed on unknown input** | An unknown unit, enum value, field type or response shape goes red (U-1). It's never guessed or coerced, except by Nightscout's own coercion rule where parity requires that rule (cite it). |
| **U-3** | **One pinned reference** | The PR description names one Nightscout commit SHA (plus image digest if a stock server is used). Every citation and fixture uses it. |
| **U-4** | **Parity proven against Nightscout itself** | Expected values come from Nightscout's code or a stock server at U-3, via a generated golden table or a differential test that runs **in CI**. Tests whose expected values were written by hand to mirror the implementation are not parity evidence. Mismatch count must be **0**. |
| **U-5** | **Mutation evidence** | Reverting the production change, or flipping its key operator or constant, makes at least one test fail. Link the run or commit that shows it. |
| **U-6** | **No weakened tests** | No deletion, skip, `xfail` or relaxed assertion. Each changed assertion is listed in the PR description with the AC that requires it. |
| **U-7** | **Fixed scope** | The PR description lists the files it may touch. The diff stays within them. No workflow, Dependabot, CodeQL, `SECURITY.md` or CONTRIBUTING changes in a feature PR. |
| **U-8** | **Exception register** | Every divergence from Nightscout is listed with evidence and needs the **repo owner's approval in a PR comment**, linked from `tests_parity/exceptions.json`. Files outside this repository (work orders, notes, chat logs) are not approval. An agent never adds an entry to the approved list on its own. An unapproved entry fails. |
| **U-9** | **Evidence at head** | Each AC tick links a CI run at the **current head SHA**. "Passes locally" or a run on an earlier SHA doesn't count. Validate must be green with 0 new CodeQL alerts. |
| **U-10** | **Stable identifiers** | `unique_id`, entity IDs, attribute names and service names are unchanged unless an AC names the change and gives a migration. Stored data, such as glucose history, doesn't change value after the fact without a migration AC. |
| **U-11** | **No secret leaks** | No credential (token, API secret or its SHA-1) in logs, exceptions, diagnostics or attributes. Every new error path has a test asserting this. |
| **U-12** | **Bounded work** | Each poll, calendar read and service call has a stated maximum for requests and rows, with a test at that maximum and one past it (the one past it goes red per U-1, not truncated). |
| **U-13** | **Deterministic tests** | Frozen clock, explicit time zones, no network except the pinned stock-server harness, no order dependence. |
| **U-14** | **User-visible change recorded** | `CHANGELOG.md` entry, `manifest.json` version (equal to `CARD_VERSION` if the card changed), README updated. |

### Agent rules

1. **Satisfy every AC and U-rule before anything else goes into the PR.** Commits that don't serve a numbered AC
   or U-rule will be reverted.
2. **Hardening means adding checks without changing observable behaviour.** When asked to harden, don't rewrite,
   reword, restructure or "improve" anything else. Every change to observable behaviour needs an AC that names it.
3. **Report status honestly.** An unticked AC stays unticked. Don't summarise a partial result as done.
4. **Don't treat the schema as the behaviour.** A Swagger `type` is evidence only where Nightscout enforces it.

## Definition of Ready (before code is written)

- **DoR-1** The pinned Nightscout reference (U-3) is in the PR description.
- **DoR-2** For each behaviour the PR touches, the description quotes the Nightscout code that defines it
  (file:line at the pinned SHA). This is written **before** the implementation. If no Nightscout code defines the
  behaviour, say so and get the owner to decide the rule in a PR comment.
- **DoR-3** The acceptance criteria are in the description as a checklist. Each one is testable (see below) and
  names at least one test.
- **DoR-4** The scope list (U-7) is in the description.
- **DoR-5** The branch is rebased on its current base and contains no unrelated commits. Any stacking or merge-order
  dependency on another PR is stated.

## Definition of Done (to merge)

- **DoD-1** Every AC and U-rule is ticked, each with a test name and a CI run link at the head SHA.
- **DoD-2** Validate is green at the head SHA, with 0 new CodeQL alerts.
- **DoD-3** The parity oracle (U-4) runs in CI and reports 0 mismatches.
- **DoD-4** No test is weakened (U-6). Every changed assertion is listed with its AC.
- **DoD-5** Mutation evidence (U-5) is linked.
- **DoD-6** The diff stays inside the scope list.
- **DoD-7** CHANGELOG, version and README are updated (U-14).
- **DoD-8** The description's verification table is regenerated from the head-SHA run. The exception register is
  empty unless the owner has approved an entry.

## Writing acceptance criteria

Each AC must be decidable by a test, with no judgement needed. Use whichever of these forms fits:

- **Boolean logic:** `stale ⇔ ageMin > S`, where `S = card.stale_minutes if set, else settings.alarmTimeagoWarnMins`.
- **Bands:** a table of input ranges and the required output for each, including the boundary values themselves
  (for example 38, 39 and 40 for the `> 39` rule).
- **Truth tables:** every combination of the inputs that matter (for example site unit × display unit).
- **Exhaustive domains:** when the domain is small, test all of it, such as every integer mg/dL from 39 to 400.
- **Differential oracles:** run Nightscout's own code or the stock server on the same input, and require 0
  mismatches.
- **Counts and limits:** "at most 10 requests", "0 mismatches over 21,091 pairs".

Every AC states its pass condition. "Handles X correctly" isn't an AC.

## Exception process

1. Add the divergence to the PR's exception register, with the Nightscout behaviour (file:line), the proposed
   behaviour, and why.
2. The repository owner approves or rejects it in a PR comment.
3. An approved exception gets its own AC and tests, and is documented in the README.
