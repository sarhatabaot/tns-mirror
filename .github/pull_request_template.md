<!--
Keep it short. The commit messages carry the detail; this is the summary a
reviewer reads first. Delete any section that does not apply.
-->

## What and why

<!-- One or two sentences. What changes, and what problem it solves. -->

## Contract

<!-- Delete all but one. -->

- [ ] Schema unchanged
- [ ] Additive (new nullable column or index) — minor
- [ ] Breaking (rename, drop, type or unit change) — major, in lockstep with the
      client, announced in `CHANGELOG.md`

## Checks

- [ ] `uvx pre-commit run --all-files --hook-stage manual`
- [ ] New behaviour is covered by a test
- [ ] Version touched? `VERSION` only, then `python3 scripts/check_versions.py --write`
- [ ] Credentials still redacted, and any suppression is in `security/waivers.yml`
      with an owner and an expiry

## Notes for the reviewer

<!-- Anything surprising: a defect found on the way, a decision worth arguing
     with, something deliberately left out. Omit if there is none. -->
