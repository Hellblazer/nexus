# Pending release: plugin changes that are NOT live yet

`.claude-plugin/marketplace.json` pins `plugins[].source.ref` to an immutable
release tag. Claude Code loads this plugin's hooks, commands, skills, and agents
from **that tag**, not from your working tree. So every change below is merged
on `develop` and **inert in every running session** until the next release ships
and users install it.

This file is the acknowledgement ledger for that gap. It exists because the gap
is otherwise invisible: on 2026-07-25 a subagent ran `git stash -u` in a shared
tree and the guard that covers exactly that verb did not fire, because the
coverage had landed hours earlier and the installed plugin was still `v6.18.1`.
Three guards had been merged, closed as "mechanized", and were protecting
nothing.

**Rules, enforced by `tests/test_plugin_release_drift_ledger.py`:**

- Every file under the behavioural surface that differs from the pinned tag MUST
  be listed here. Adding a guard without declaring it fails the suite.
- When a release ships and the pin advances, drift goes to zero and this list
  MUST be emptied. A stale entry also fails the suite, so the ledger cannot
  quietly become fiction.
- Do NOT "fix" a failure by deleting entries. The entry is the honest statement
  that the thing is not yet live.

**Do not use this to justify skipping a release.** If a guard matters enough to
mechanize, it matters enough to ship.

**Deferring a straddling entry (nexus-2x3qy, nexus-rbmja.10).** A plugin-only
release cherry-picks commits from `develop` onto a branch off the last client
tag (`release` skill, "Plugin-only fast lane"). The eligibility gate reads the
bead ids named in those commits and refuses the release when any other commit
on `origin/develop` since the tag that names the same bead touches a path the
lane does not ship (`src/`, `service/`, `scripts/`, `.github/`, `mcpb/`,
`conexus/plans/`, `conexus/daemon/`, release machinery under `tests/`, and so
on). Attribution is by commit message, matched on the whole bead id, so a
commit that does not name its bead is not seen by that check. To keep a
straddling entry out of the release, move it under
`## Deferred to the next client release` below and do not cherry-pick its
commits, so the whole bead ships together at the next normal release. A
deferred entry is exempt from the release-window "ledger must be empty" rule
above -- still declared there is correct, not stale -- and stays exactly where
it is until moved back deliberately.

---
## Awaiting the next release (pinned: v7.88.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `prose-edit/skills/prose-edit/SKILL.md` (nexus-scmcs): the setup run is a guided wizard, and the run-time genre question, the accept and reject step and the step 13 offers are `AskUserQuestion` choice prompts instead of free-text questions.
- `prose-edit/skills/prose-edit/scripts/brief.py` (nexus-scmcs): `setup` reports at most eight `genre_candidates` with `ready`, `try_path` and `unmapped_other` in place of `no_genre`; new `suggest-genre`; the genre description table the skill and the README share.
- `prose-edit/skills/prose-edit/scripts/memory.py` (nexus-scmcs): `add-entry` refuses a `genre_map` entry whose genre is not one of the six and stores nothing from that call, so a mistyped genre cannot break later runs in a folder.
- `conexus/commands/setup.md` (nexus-tr1rj.5): the install-size sentence names the PDF support as MinerU and torch; the PDF stack no longer includes the removed layout extractor.
- `conexus/launch/nx_standin.py` (nexus-tr1rj.5): the same name list in the stand-in's sizes note and slim description, kept equal to the real server's (`src/nexus/mcp/setup_tools.py`).
- `conexus/hooks/scripts/routing/_lib.py` (nexus-njn1a.1): the routing hooks send the `routing_events` write from a detached child, so a guard's deny or `routing-allow` escape is emitted without waiting on the engine; the child has a 5 s total deadline in place of the former 0.25 s per-socket timeout, and a failed write still lands one drop-meter record.
