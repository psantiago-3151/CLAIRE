# Security

CLAIRE is meant to run **on your machine**, bound to `127.0.0.1`. Do not expose it as a public website.

- Do not commit `.env`, `claire.conf`, `memory/`, `models/`, or `bin/`.
- Do not set `CLAIRE_UI_HOST=0.0.0.0` unless you accept that anyone on the LAN can start the mic and the assistant.
- There are no cloud API keys in this repo. Keep it that way.

**Report a vulnerability:** open a GitHub [issue](https://github.com/psantiago-3151/CLAIRE/issues). This is a hobby project; there is no private bounty program or SLA.

See also the Security section in [README.md](README.md).
