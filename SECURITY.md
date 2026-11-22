# Security policy

## Supported versions

Only the latest release gets security fixes. Update to it before reporting.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No |

## Reporting a vulnerability

Please **don't open a public issue or pull request** for a security problem.

Report it privately through GitHub instead:
[Report a vulnerability](https://github.com/Jackson-Devices/ha-nightscout-extended/security/advisories/new)
(the **Security** tab → **Advisories** → **Report a vulnerability**).
[How GitHub's private reporting works](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)

Include:

- the affected release or commit, and your Home Assistant and Nightscout versions;
- what you expected, what happened instead, and what the impact is;
- steps to reproduce.

Use synthetic examples or redacted summaries. **Leave out** access tokens and API secrets, site URLs that carry
credentials, Home Assistant diagnostics files, and real patient data.

You should get a first reply within 7 days. Once a fix is released, the advisory is published with credit to you
unless you'd rather stay anonymous.

## What's in scope

Problems in this repository's code, for example:

- the Nightscout access token or API secret leaking: into logs, diagnostics, error messages, or to a host other
  than the one you configured (including through a redirect);
- the bundled glucose card running script or markup that came from Nightscout or Home Assistant state;
- data from a Nightscout site being able to make the integration do something other than read values;
- anything that weakens authentication between Home Assistant and Nightscout;
- **any wrong reading**: a glucose value, trend, change, forecast, colour or timestamp that is shown incorrectly,
  for example a stale reading shown as current, or a value on the wrong side of an alarm threshold.

This integration is not a medical device and must not be used to make treatment decisions. Because people still
glance at it to decide what to do next, every wrong-reading bug goes through the private route above, whether or
not someone could trigger it on purpose.

Bugs in Home Assistant or Nightscout themselves belong with those projects:
[Home Assistant](https://github.com/home-assistant/core/security/policy),
[Nightscout](https://github.com/nightscout/cgm-remote-monitor).
