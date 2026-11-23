"""Pull request policy: a filled-in template (Definition of Ready) and a CHANGELOG entry for product changes (U-14).

Run by Validate's pr-policy job with these environment variables:
    EVENT_NAME   github.event_name
    PR_BODY      the pull request description
    PR_AUTHOR    the pull request author's login
    BASE_SHA     the pull request base commit (fetched before this runs)
Outside pull requests there is nothing to check, and the script says so. Dependabot's PRs are exempt: they change
only pinned dependencies and use GitHub's own description format.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# The headings of .github/pull_request_template.md. Every pull request description must keep all of them.
REQUIRED_HEADINGS = (
    "## Summary",
    "## Parity reference (U-3)",
    "## Nightscout behaviour this PR reproduces (DoR-2)",
    "## Scope (U-7)",
    "## Acceptance criteria",
    "## Universal rules",
    "## Changed test assertions (U-6)",
    "## Exception register (U-8)",
    "## Verification at head",
)
# Template placeholders that mean a section was left unfilled.
PLACEHOLDERS = ("`<sha>`", "`<digest>`", "<link>", "**AC-1** …", "test: `…`")
PRODUCT_PATHS = ("custom_components/",)
EXEMPT_AUTHORS = {"dependabot[bot]"}


def template_errors(body: str) -> list[str]:
    errors = [f"missing section: {heading}" for heading in REQUIRED_HEADINGS if heading not in body]
    errors += [f"unfilled template placeholder: {text}" for text in PLACEHOLDERS if text in body]
    return errors


def changelog_errors(changed: list[str]) -> list[str]:
    product = [path for path in changed if path.startswith(PRODUCT_PATHS)]
    if product and "CHANGELOG.md" not in changed:
        return [f"product files changed without a CHANGELOG.md entry (U-14): {', '.join(product[:5])}"]
    return []


def changed_files(base: str) -> list[str]:
    result = subprocess.run(["git", "diff", "--name-only", base, "HEAD"], capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise RuntimeError(f"could not diff against the base commit: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    if os.environ.get("EVENT_NAME") != "pull_request":
        print("Not a pull request: no description or changelog to check.")
        return 0
    if os.environ.get("PR_AUTHOR") in EXEMPT_AUTHORS:
        print("Dependabot pull request: exempt from the template and changelog checks.")
        return 0
    body = re.sub(r"\r\n?", "\n", os.environ.get("PR_BODY") or "")
    errors = template_errors(body) + changelog_errors(changed_files(os.environ["BASE_SHA"]))
    for error in errors:
        print(f"::error::{error}")
    if errors:
        print("Fill in the pull request description from .github/pull_request_template.md (CONTRIBUTING.md, DoR).")
        return 1
    print("Pull request policy passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
