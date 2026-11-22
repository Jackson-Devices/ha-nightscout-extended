# Code scanning (maintainers)

Use one CodeQL setup per repository. GitHub rejects SARIF uploads from the advanced workflow while
GitHub-managed default setup is active.
[GitHub's CodeQL upload restriction](https://docs.github.com/en/code-security/reference/code-scanning/sarif-files/troubleshoot-sarif-uploads/default-setup-enabled)

## Default setup (current)

Leave the `CODEQL_ADVANCED_ENABLED` repository variable unset; the jobs in `.github/workflows/codeql.yml` are
then skipped. For broader coverage, choose the **Extended** query suite in the repository's CodeQL default setup
settings.
[Configure default setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configure-code-scanning)

## Advanced setup (opt-in)

1. Switch the repository's CodeQL setting to advanced setup.
2. Set the repository Actions variable `CODEQL_ADVANCED_ENABLED` to `true`.

The workflow then scans Python, JavaScript/TypeScript and GitHub Actions with the `security-extended` query suite.
[Configure advanced setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configuring-advanced-setup-for-code-scanning)
