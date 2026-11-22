# Security policy

## Reporting a vulnerability

Open the affected repository on GitHub. Under **Security and quality** (or
**Security**), open **Advisories** and select **Report a vulnerability**. If the
button is unavailable, check the affected repository and its upstream for a
documented private reporting channel. If neither publishes one, ask a
maintainer only for their preferred private security contact. Keep
vulnerability details, code, site URLs and clinical data out of public
issues, discussions and pull requests.
[GitHub's private reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)

Include the affected release or commit, the relevant Home Assistant and
Nightscout versions, and a short description of the expected behavior,
observed behavior and impact. Use synthetic examples or redacted summaries.
Omit credentials, site URLs containing authentication information, Home
Assistant diagnostics and real patient data.

Use the private route for suspected security issues involving confidentiality,
authentication or data integrity. That includes issues affecting the accuracy
or timing of displayed glucose values, trends or timestamps, such as a stale
reading shown as current.

## CodeQL setup

Use one CodeQL setup per repository. GitHub rejects SARIF uploads from the
advanced workflow while GitHub-managed default setup is active.
[GitHub's CodeQL upload restriction](https://docs.github.com/en/code-security/reference/code-scanning/sarif-files/troubleshoot-sarif-uploads/default-setup-enabled)

With default setup enabled, leave `CODEQL_ADVANCED_ENABLED` unset and use the
**Extended** query suite in the repository's CodeQL default setup settings if
you want broader coverage.
[Configure default setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configure-code-scanning)

To opt into the advanced workflow, first switch the repository's CodeQL
setting to advanced setup. Then set the repository Actions variable
`CODEQL_ADVANCED_ENABLED` to `true`. The workflow uses the
`security-extended` query suite for Python, JavaScript/TypeScript and
GitHub Actions.
[Configure advanced setup](https://docs.github.com/en/code-security/how-tos/find-and-fix-code-vulnerabilities/configure-code-scanning/configuring-advanced-setup-for-code-scanning)
