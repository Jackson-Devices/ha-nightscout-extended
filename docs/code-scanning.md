# Code scanning for maintainers

## Advanced setup on this fork

This fork uses advanced CodeQL setup from the workflows on `main`, with the repository Actions variable
`CODEQL_ADVANCED_ENABLED` set to `true`. [The CodeQL workflow](../.github/workflows/codeql.yml)
scans Python, JavaScript/TypeScript and GitHub Actions using the `security-extended` suite. That suite
runs the default queries plus additional security queries. Consult the published query list for each
language to see what the suite checks. [CodeQL query help](https://codeql.github.com/codeql-query-help/)

[Validate](../.github/workflows/validate.yml) is the automatic validation entry point. It runs the shared
[preflight](../.github/workflows/workflow-lint.yml) before starting the test jobs and CodeQL.
Draft pull requests run preflight only. Ready pull requests, including ones just marked ready for review,
qualifying pushes to `main`, manual Validate runs and the weekly run start full validation after preflight
passes. Superseded runs in the same concurrency group are cancelled.

To run checks manually, open **Actions**, select **Validate** for full validation or select the workflow
defined in [`codeql.yml`](../.github/workflows/codeql.yml) for preflight and CodeQL alone. Choose
**Run workflow** and select the branch. GitHub requires the workflow to exist on the default branch for
manual dispatch. [GitHub's manual workflow guide](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)

## GitHub-managed default setup

Use one CodeQL setup per repository. To use GitHub-managed default setup, enable Actions if necessary,
remove the `CODEQL_ADVANCED_ENABLED` repository variable, then select **Default** in the repository's
CodeQL settings and enable it. You can choose **Extended** when editing the default setup configuration.
To return to advanced setup with these workflows, disable default setup before setting the variable
to `true`. GitHub blocks advanced workflow SARIF uploads while default setup is enabled.
[Default setup guide](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configure-code-scanning) ·
[SARIF upload restriction](https://docs.github.com/en/code-security/reference/code-scanning/sarif-files/troubleshoot-sarif-uploads/default-setup-enabled)

## Runner usage and billing

Standard GitHub-hosted runner usage is free for public repositories like this fork. Preflight, job timeouts
and cancellation limit avoidable work. Workflow YAML provides no overall usage or spending cap. For metered
Actions usage, set a budget in **Billing and licensing** and enable **Stop usage when budget limit is reached**
where supported if you want GitHub to enforce the budget.
[Actions billing](https://docs.github.com/en/actions/concepts/billing-and-usage) ·
[GitHub budget settings](https://docs.github.com/en/billing/how-tos/set-up-budgets)
