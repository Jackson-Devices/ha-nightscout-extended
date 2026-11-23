"""Compare native (stock Nightscout), legacy (the upstream integration) and current (this branch).

Usage:
    python tests_parity/three_way_report.py CURRENT.json LEGACY.json EXCEPTIONS.json > report.md

Both JSON files come from the same parity suite run against the same stock site profile, with PARITY_REPORT set
(see conftest.py). Only the current product gates a merge. The legacy product's results are a record for the
upstream developer: where the original integration differs from Nightscout, which of those this branch corrected,
and where this branch differs from Nightscout on purpose, with the owner's recorded reason.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

NATIVE = "stock Nightscout 15.0.6 (`nightscout/cgm-remote-monitor@sha256:868f024f…`)"
EXAMPLES = 3


def _load(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {"missing": True, "tests": {}}
    return json.loads(file.read_text())


def _short(nodeid: str) -> str:
    return nodeid.split("::", 1)[-1]


def _kinds(entry: dict) -> str:
    groups: dict[str, int] = {}
    for item in entry.get("mismatches", []):
        kind = str(item.get("what", item.get("field", "mismatch")))
        groups[kind] = groups.get(kind, 0) + 1
    if groups:
        return "; ".join(f"{kind} ×{count}" for kind, count in groups.items())
    return entry.get("message") or "failed"


def _status(entry: dict | None) -> str:
    if entry is None:
        return "not run"
    return {"passed": "matches native", "failed": "differs", "skipped": "not applicable"}[entry["outcome"]]


def _meaning(legacy: dict | None, current: dict | None) -> str:
    legacy_outcome = legacy["outcome"] if legacy else None
    current_outcome = current["outcome"] if current else None
    if current_outcome == "failed":
        return "**current differs: blocks merge**" + (" (regression: legacy matched)" if legacy_outcome == "passed" else "")
    if current_outcome == "skipped":
        return "not applicable to current"
    if legacy_outcome == "failed":
        return "corrected by current"
    if legacy_outcome == "skipped" or legacy is None:
        return "new in current; matches native"
    return "both match native"


def _examples(items: list) -> list[str]:
    return [f"  - `{json.dumps(item, default=str, ensure_ascii=False)}`" for item in items[:EXAMPLES]]


def render(current: dict, legacy: dict, exceptions: dict) -> str:
    profile = current.get("profile") or legacy.get("profile") or "unknown"
    lines = [
        f"# Parity: native vs legacy vs current ({profile})",
        "",
        f"- **Native:** {NATIVE}",
        f"- **Legacy:** upstream `aperetti/ha-nightscout-extended` at `{legacy.get('product_sha', 'not run')[:12]}`"
        + (" (legacy run produced no report)" if legacy.get("missing") else ""),
        f"- **Current:** `{current.get('product_sha', 'not run')[:12]}`"
        + (" (current run produced no report)" if current.get("missing") else ""),
        "",
        "Only **current** gates the merge. Legacy is a record for the upstream developer.",
        "",
        "| Check | Legacy | Current | Meaning |",
        "|---|---|---|---|",
    ]
    names = sorted(set(current["tests"]) | set(legacy["tests"]), key=_short)
    for name in names:
        cur, leg = current["tests"].get(name), legacy["tests"].get(name)
        lines.append(f"| `{_short(name)}` | {_status(leg)} | {_status(cur)} | {_meaning(leg, cur)} |")

    corrected = [n for n in names if (legacy["tests"].get(n) or {}).get("outcome") == "failed"
                 and (current["tests"].get(n) or {}).get("outcome") == "passed"]
    still = [n for n in names if (legacy["tests"].get(n) or {}).get("outcome") == "failed"
             and (current["tests"].get(n) or {}).get("outcome") == "failed"]
    lines += ["", "## Where legacy differs from native, and current corrected it", ""]
    if not corrected:
        lines.append("None.")
    for name in corrected:
        entry = legacy["tests"][name]
        lines.append(f"- **`{_short(name)}`**: legacy: {_kinds(entry)}")
        lines += _examples(entry.get("mismatches", []))

    lines += ["", "## Where both legacy and current differ from native (blocks merge)", ""]
    if not still:
        lines.append("None.")
    for name in still:
        lines.append(f"- **`{_short(name)}`**: legacy: {_kinds(legacy['tests'][name])}; "
                     f"current: {_kinds(current['tests'][name])}")
        lines += _examples(current["tests"][name].get("mismatches", []))

    approved = exceptions.get("approved", {})
    used: dict[str, list] = {}
    for entry in current["tests"].values():
        for item in entry.get("exceptions", []):
            used.setdefault(item["id"], []).append(item)
    lines += ["", "## Where current differs from native by owner decision", ""]
    if not used:
        lines.append("None in this run.")
    for exception_id, items in sorted(used.items()):
        record = approved.get(exception_id, {})
        decision = record.get("decision", "NOT IN THE APPROVED REGISTER")
        approval = record.get("approval")
        lines.append(f"- **{exception_id}** ({len(items)} case(s)): {decision}"
                     + (f" [approval]({approval})" if approval else ""))
        lines += _examples(items)

    opt_in = exceptions.get("approved_opt_in", {})
    if opt_in:
        lines += ["", "## Approved opt-in differences (off in the default configuration tested here)", ""]
        for exception_id, record in sorted(opt_in.items()):
            lines.append(f"- **{exception_id}**: {record['decision']} [approval]({record['approval']})")

    failing = [n for n in names if (current["tests"].get(n) or {}).get("outcome") == "failed"]
    lines += ["", "## Current failures", ""]
    if not failing:
        lines.append("None. Current matches native everywhere checked, apart from the owner decisions above.")
    for name in failing:
        entry = current["tests"][name]
        lines.append(f"- **`{_short(name)}`**: {_kinds(entry)}")
        lines += _examples(entry.get("mismatches", []))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    current, legacy = _load(argv[1]), _load(argv[2])
    exceptions = json.loads(Path(argv[3]).read_text())
    sys.stdout.write(render(current, legacy, exceptions))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
