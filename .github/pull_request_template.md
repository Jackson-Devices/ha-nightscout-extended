<!--
Fill in every section. A section left empty or deleted means the PR isn't ready.
Rules: CONTRIBUTING.md. The acceptance rule is 1:1 parity with stock Nightscout.
-->

## Summary

<!-- What changes, in one or two sentences. -->

## Parity reference (U-3)

- Nightscout commit: `<sha>`
- Stock image digest (if a stock server is used): `<digest>`

## Nightscout behaviour this PR reproduces (DoR-2)

<!-- For each behaviour touched: quote Nightscout's code with a file:line link at the pinned commit.
     If no Nightscout code defines it, say so and link the owner's decision. -->

| Behaviour | Nightscout source (file:line @ pinned SHA) | Rule in one line |
|---|---|---|
| | | |

## Scope (U-7)

Files this PR may touch:

-

## Acceptance criteria

<!-- Each AC: testable pass condition (boolean, bands, truth table, exhaustive domain or differential oracle).
     Tick only with a test name and a CI run link at the current head SHA. -->

- [ ] **AC-1** … — test: `…` — run: <link>

## Universal rules

- [ ] U-1 No silent fallbacks: every failure path goes red with a reason code (tests: …)
- [ ] U-2 Unknown input fails closed
- [ ] U-3 One pinned reference, used by every citation and fixture
- [ ] U-4 Parity oracle runs in CI with 0 mismatches (job/run: …)
- [ ] U-5 Mutation evidence: <link>
- [ ] U-6 No weakened tests (changed assertions listed below)
- [ ] U-7 Diff stays within the scope list
- [ ] U-8 Exception register below is empty, or every entry is owner-approved (link the comment)
- [ ] U-9 All evidence is from CI at the head SHA; Validate green; 0 new CodeQL alerts
- [ ] U-10 Identifiers and stored data unchanged, or migration AC included
- [ ] U-11 No credential in logs, errors, diagnostics or attributes
- [ ] U-12 Request and row limits stated and tested at and past the limit
- [ ] U-13 Tests are deterministic
- [ ] U-14 CHANGELOG, version and README updated (or not user-visible: say why)

## Changed test assertions (U-6)

| Test | Old assertion | New assertion | AC requiring the change |
|---|---|---|---|
| | | | |

## Exception register (U-8)

| ID | Nightscout behaviour | Proposed behaviour | Owner approval (comment link) |
|---|---|---|---|
| | | | |

## Verification at head `<sha>`

| Check | Result | Run |
|---|---|---|
| Validate (all jobs) | | |
| Parity oracle mismatches | | |
| CodeQL new alerts | | |
