# Security policy

## Supported versions

Only the latest release gets security fixes. Update to it before reporting.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No |

## Reporting a vulnerability

Please **don't open a public issue** for a security problem.

Report it privately through GitHub instead:
[Report a vulnerability](https://github.com/Jackson-Devices/ha-nightscout-extended/security/advisories/new)
(the **Security** tab → **Report a vulnerability**).

Include what you found, how to reproduce it, the version you tested, and what an attacker could do with it.
You should get a first reply within 7 days. Once a fix is released, the advisory is published with credit to you
unless you'd rather stay anonymous.

## What's in scope

Problems in this repository's code, for example:

- the Nightscout access token or API secret leaking: into logs, diagnostics, error messages, or to a host other
  than the one you configured;
- the bundled glucose card running script or markup that came from Nightscout or Home Assistant state;
- data from a Nightscout site being able to make the integration do something other than read values;
- anything that weakens authentication between Home Assistant and Nightscout.

Bugs in Home Assistant or Nightscout themselves belong with those projects
([Home Assistant](https://github.com/home-assistant/core/security/policy),
[Nightscout](https://github.com/nightscout/cgm-remote-monitor)).

## A note on safety

This integration is not a medical device and must not be used to make treatment decisions. A bug that shows a
wrong glucose value, colour or trend is still serious: if someone could trigger it deliberately, report it
privately as above; otherwise open a normal issue so it gets fixed quickly.
