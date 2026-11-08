# Unreleased changelog fragments

One file per change, named after its bead: `<bead-id>.md`, or `<bead-id>-<slug>.md` when a bead
has more than one entry (lower case, e.g. `nexus-3ceda.2.md`). Write the entry here, never under
`## [Unreleased]` in `CHANGELOG.md`: `scripts/changelog_fold.py check` (lint bucket) refuses a
hand-written bullet there.

    ### Fixed

    - **What changed, in one bold lead sentence.** The detail, the bead id (nexus-xyz).

The first line is one section: Added, Changed, Deprecated, Removed, Fixed, Security, Performance or
Docs. Then one or more `- ` bullets; a bullet may wrap onto lines indented by two spaces. At release
`scripts/changelog_fold.py fold` moves every fragment under `## [Unreleased]`, grouped by section and
ordered by the commit that added it, and deletes the fragments.

Two entries for one bead need distinct names (`nexus-abc-cli.md`, `nexus-abc-docs.md`); two sessions
writing the same name for one bead still collide, as they would on any shared file.
