"""Check tests_parity/exceptions.json, the owner's register of divergences from Nightscout (CONTRIBUTING.md, U-8).

Every entry must carry its decision text and link to a comment on one of this repository's own pull requests or
issues. A file outside the repository, a work order or a bare statement is not approval. This can't check who
wrote the linked comment: the owner, Codex and Claude share one GitHub account.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

SECTIONS = ("approved", "approved_opt_in", "rejected")
EXCEPTION_ID = re.compile(r"E-\d+[a-z]")


def check(register: object, repository: str) -> list[str]:
    approval = re.compile(
        rf"https://github\.com/{re.escape(repository)}/(?:pull|issues)/\d+#issuecomment-\d+", re.IGNORECASE
    )
    if not isinstance(register, dict):
        return ["the register must be a JSON object"]
    errors = []
    unknown = set(register) - set(SECTIONS) - {"_comment"}
    if unknown:
        errors.append(f"unknown sections: {', '.join(sorted(unknown))}")
    seen: dict[str, str] = {}
    for section in SECTIONS:
        entries = register.get(section)
        if not isinstance(entries, dict):
            errors.append(f"{section}: missing or not an object")
            continue
        for exception_id, entry in entries.items():
            where = f"{section}.{exception_id}"
            if not EXCEPTION_ID.fullmatch(exception_id):
                errors.append(f"{where}: ID must look like E-1d")
            if exception_id in seen:
                errors.append(f"{where}: also listed under {seen[exception_id]}")
            seen[exception_id] = section
            if not isinstance(entry, dict) or set(entry) != {"decision", "approval"}:
                errors.append(f"{where}: must have exactly 'decision' and 'approval'")
                continue
            if not isinstance(entry["decision"], str) or len(entry["decision"].strip()) < 20:
                errors.append(f"{where}: decision text is missing or too short to say what was decided")
            if not isinstance(entry["approval"], str) or not approval.fullmatch(entry["approval"]):
                errors.append(f"{where}: approval must link a comment on a {repository} pull request or issue")
    return errors


def main() -> int:
    path = Path("tests_parity/exceptions.json")
    if not path.exists():
        print("No exception register in this checkout.")
        return 0
    repository = os.environ.get("GITHUB_REPOSITORY") or "Jackson-Devices/ha-nightscout-extended"
    try:
        register = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{path}: unreadable ({exc})", file=sys.stderr)
        return 1
    errors = check(register, repository)
    for error in errors:
        print(f"{path}: {error}", file=sys.stderr)
    if errors:
        return 1
    print("Exception register passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
