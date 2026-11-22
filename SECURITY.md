# Security policy

## Affected versions

You can report a problem in a tagged release or a branch build. Include the affected release tag or the exact
commit SHA. If you installed from a fork, also name that repository. Reports do not require upgrading first.

## Reporting a vulnerability

Open the GitHub page for the repository whose code is affected. Select **Security** (or **Security and quality**),
then **Advisories** and **Report a vulnerability**. For fork-specific changes, use the fork's form. For a problem
in upstream code, use the upstream project's own reporting channel.
[GitHub's private reporting guide](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)

If the button is unavailable, look for a private security contact in the affected project's policy or documentation.
If none is listed, ask one of its maintainers for a private contact without sharing the details. Keep vulnerability
details, code, site URLs and clinical data out of public issues, discussions and pull requests.

In your private report, include the affected release or commit, your Home Assistant and Nightscout versions,
what you expected, what happened, the impact and enough context to reproduce the problem with synthetic data.
Use synthetic examples or redacted summaries. Leave out access tokens and API secrets, URLs that carry
credentials, Home Assistant diagnostics files and real patient data.

Maintainers can discuss disclosure and credit with you in the private report. Let them know if you prefer to stay
anonymous.

## What's in scope

- A Nightscout access token or API secret appearing in logs, diagnostics or errors, or being sent to a host
  other than the one you configured, including through a redirect.
- The bundled glucose card running script or rendering markup supplied by Nightscout or Home Assistant state.
- Data from a Nightscout site causing the integration to do anything beyond reading values.
- Anything that weakens authentication between Home Assistant and Nightscout.
- Incorrect glucose values, trends, changes, forecasts, colours or timestamps. Examples include stale data
  displayed as current or a value shown on the wrong side of an alarm threshold.

Use the private reporting route for incorrect readings even if they appear accidental. The integration is for
display only. Please do not use it to make treatment decisions.

For problems in Home Assistant or Nightscout itself, use the affected project's reporting channel.
[Home Assistant](https://github.com/home-assistant/core/security/policy) ·
[Nightscout](https://github.com/nightscout/cgm-remote-monitor/security)
