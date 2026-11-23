"""Negative controls for the workflow gate checked by each preflight."""

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from check_workflow_gates import WorkflowLoader, validate


class WorkflowGateTests(unittest.TestCase):
    def setUp(self):
        self.source = Path.cwd()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.workflows = self.root / ".github/workflows"
        shutil.copytree(self.source / ".github/workflows", self.workflows)

    def change(self, filename, mutation):
        path = self.workflows / filename
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=WorkflowLoader)
        mutation(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def assert_rejected(self, error):
        found = validate(self.root)
        self.assertTrue(any(error in issue for issue in found), found)

    def test_current_workflows_are_accepted(self):
        self.assertEqual(validate(self.root), [])

    def test_new_automatic_entrance_is_rejected(self):
        (self.workflows / "extra.yaml").write_text("on: push\njobs: {}\n", encoding="utf-8")
        self.assert_rejected("extra.yaml: unexpected workflow")

    def test_new_ungated_job_is_rejected(self):
        self.change("validate.yml", lambda document: document["jobs"].update(extra={"runs-on": "ubuntu-latest"}))
        self.assert_rejected("extra must directly need preflight")

    def test_job_cannot_override_failed_preflight(self):
        self.change("release.yml", lambda document: document["jobs"]["release"].update({"if": "always()"}))
        self.assert_rejected("release has an unapproved gate condition")

    def test_preflight_cannot_skip(self):
        self.change("validate.yml", lambda document: document["jobs"]["preflight"].update({"if": False}))
        self.assert_rejected("preflight must be an unconditional same-commit call")

    def test_preflight_failure_cannot_be_ignored(self):
        self.change(
            "workflow-lint.yml",
            lambda document: document["jobs"]["workflow-lint"]["steps"][2].update({"continue-on-error": True}),
        )
        self.assert_rejected("workflow-lint cannot use continue-on-error")

    def test_codeql_cannot_assert_preflight_via_input(self):
        self.change(
            "codeql.yml",
            lambda document: document["jobs"]["scan"].update({"with": {"preflight_passed": True}}),
        )
        self.assert_rejected("scan must not take caller inputs")

    def test_reusable_job_cannot_drop_the_caller_requirement(self):
        self.change("codeql-scan.yml", lambda document: document["jobs"]["analyze"].update({"if": True}))
        self.assert_rejected("analyze has an unapproved caller condition")

    def test_reusable_job_cannot_gain_a_schedule(self):
        filename = "parity.yml" if (self.workflows / "parity.yml").is_file() else "codeql-scan.yml"
        self.change(filename, lambda document: document["on"].update(schedule=[{"cron": "0 4 * * *"}]))
        self.assert_rejected(f"{filename}: unapproved event configuration")

    def test_path_filters_are_rejected(self):
        self.change("validate.yml", lambda document: document["on"]["pull_request"].update(
            {"paths-ignore": ["docs/**"]}))
        self.assert_rejected("validate.yml: unapproved event configuration")

    def test_draft_skips_are_rejected(self):
        self.change("validate.yml", lambda document: document["jobs"]["parity"].update(
            {"if": document["jobs"]["parity"]["if"].replace(" }}", " && !github.event.pull_request.draft }}")}))
        self.assert_rejected("parity has an unapproved gate condition")

    def test_duplicate_yaml_keys_are_rejected(self):
        path = self.workflows / "release.yml"
        with path.open("a", encoding="utf-8") as target:
            target.write("\njobs: {}\n")
        self.assert_rejected("release.yml: invalid YAML or duplicate key")


if __name__ == "__main__":
    unittest.main()
