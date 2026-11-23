"""Negative controls for the exception register and pull request policy checks run in CI."""

from pathlib import Path
import unittest

from check_exception_register import check
from check_pr_policy import REQUIRED_HEADINGS, changelog_errors, template_errors

REPO = "Jackson-Devices/ha-nightscout-extended"
LINK = f"https://github.com/{REPO}/pull/1#issuecomment-7097425189"
DECISION = "The card shows the number at 39 and above 400 mg/dL."


def register(**sections):
    base = {"approved": {}, "approved_opt_in": {}, "rejected": {}}
    base.update(sections)
    return base


class ExceptionRegisterTests(unittest.TestCase):
    def test_repository_register_passes(self):
        import json
        path = Path("tests_parity/exceptions.json")
        if path.exists():
            self.assertEqual(check(json.loads(path.read_text()), REPO), [])

    def test_valid_entry_passes(self):
        self.assertEqual(check(register(approved={"E-1d": {"decision": DECISION, "approval": LINK}}), REPO), [])

    def test_out_of_repository_approval_is_rejected(self):
        entry = {"decision": DECISION, "approval": "Owner direction in the work order oracle-dispatch.md"}
        self.assertTrue(any("approval must link" in e for e in check(register(approved={"E-1d": entry}), REPO)))

    def test_other_repository_comment_is_rejected(self):
        entry = {"decision": DECISION, "approval": "https://github.com/someone/else/pull/1#issuecomment-1"}
        self.assertTrue(any("approval must link" in e for e in check(register(approved={"E-1d": entry}), REPO)))

    def test_missing_decision_text_is_rejected(self):
        entry = {"decision": "", "approval": LINK}
        self.assertTrue(any("decision text" in e for e in check(register(approved={"E-1d": entry}), REPO)))

    def test_bare_string_entry_is_rejected(self):
        self.assertTrue(any("exactly" in e for e in check(register(approved={"E-1d": LINK}), REPO)))

    def test_duplicate_across_sections_is_rejected(self):
        entry = {"decision": DECISION, "approval": LINK}
        found = check(register(approved={"E-1d": entry}, rejected={"E-1d": entry}), REPO)
        self.assertTrue(any("also listed" in e for e in found))

    def test_bad_id_and_unknown_section_are_rejected(self):
        found = check({**register(approved={"exception": {"decision": DECISION, "approval": LINK}}), "extra": {}}, REPO)
        self.assertTrue(any("ID must" in e for e in found) and any("unknown sections" in e for e in found))


class PullRequestPolicyTests(unittest.TestCase):
    def filled(self):
        return "\n".join(f"{heading}\nfilled in\n" for heading in REQUIRED_HEADINGS)

    def test_filled_template_passes(self):
        self.assertEqual(template_errors(self.filled()), [])

    def test_missing_section_is_rejected(self):
        body = self.filled().replace("## Exception register (U-8)", "")
        self.assertIn("missing section: ## Exception register (U-8)", template_errors(body))

    def test_placeholder_is_rejected(self):
        self.assertTrue(template_errors(self.filled() + "\n- Nightscout commit: `<sha>`"))

    def test_the_template_itself_is_not_accepted_unfilled(self):
        self.assertTrue(template_errors(Path(".github/pull_request_template.md").read_text()))

    def test_product_change_needs_a_changelog_entry(self):
        self.assertTrue(changelog_errors(["custom_components/nightscout_extended/api.py"]))
        self.assertEqual(changelog_errors(["custom_components/nightscout_extended/api.py", "CHANGELOG.md"]), [])
        self.assertEqual(changelog_errors(["tests_parity/conftest.py", ".github/workflows/parity.yml"]), [])


if __name__ == "__main__":
    unittest.main()
