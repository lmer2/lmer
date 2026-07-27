# changelog.d — unreleased changelog fragments

Unreleased changelog entries live here as small per-branch YAML files
instead of being appended to `CHANGELOG.yaml`'s `unreleased:` lists.
Two branches never touch the same file, which eliminates the CHANGELOG
merge-conflict class entirely.

## Writing a fragment

One fragment per branch/MR, named `YYYYMMDD-<topic>.yaml` where `<topic>`
is the branch slug (e.g. `feature/issue-113-slim-image-addons` →
`20260715-issue-113-slim-image-addons.yaml`). Content is a mapping of
section name → list of entry strings:

```yaml
# changelog.d/20260718-ctl-changelogd.yaml
added:
- "changelog.d fragment support to eliminate CHANGELOG merge conflicts"
```

Valid sections (lowercase, case-sensitive): `added`, `fixed`, `changed`,
`deprecated`, `removed`, `security` — the same vocabulary as
`CHANGELOG.yaml`. Entry-quality rules are unchanged: write for users of the
project, skip internal refactors/test-only/CI changes.

Non-YAML files in this directory (like this README) are ignored.

## Releasing

`ctl changelog release <version>` collects all fragments into the new
version section of `CHANGELOG.yaml` (residual `unreleased:` entries first,
then fragments in filename order) and deletes them. Fragment support
requires a ctl build containing the `feature/changelog-d` branch of
[20c/ctl](https://github.com/20c/ctl), not yet in a released ctl version —
until it lands in a release, roll fragments with ctl from that branch.
