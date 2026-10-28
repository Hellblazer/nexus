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
## Awaiting the next release (pinned: v7.87.1)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/commands/setup.md` (nexus-tr1rj.5): the install-size sentence names the PDF support as MinerU and torch; the PDF stack no longer includes the removed layout extractor.
- `conexus/launch/nx_standin.py` (nexus-tr1rj.5): the same name list in the stand-in's sizes note and slim description, kept equal to the real server's (`src/nexus/mcp/setup_tools.py`).
- `prose-edit/.claude-plugin/plugin.json` (nexus-scmcs): the new opt-in conexus-prose-edit plugin's manifest; /prose-edit moves out of the nexus repo's .claude/skills so it can run in any repository.
- `prose-edit/agents/line-editor.md` (nexus-scmcs): the line-editor agent, moved from .claude/agents and dispatched as conexus-prose-edit:line-editor; a brief's Repository line roots every relative path it gives.
- `prose-edit/skills/prose-edit/SKILL.md` (nexus-scmcs): the skill, moved from .claude/skills: script prefixes under ${CLAUDE_PLUGIN_ROOT} pre-approved by allowed-tools, an absolute path edits the checkout that holds it, and a `setup` run (Nexus and T2, the two temp-dir allow rules, genre map, exemplars).
- `prose-edit/skills/prose-edit/scripts/brief.py` (nexus-scmcs): `setup` report; the brief names the document by absolute path with a Repository line; the site-page layer comes from the repository's own .claude/skills/site-page and is skipped when absent.
- `prose-edit/skills/prose-edit/scripts/memory.py` (nexus-scmcs): an absolute path argument picks the repository (a worktree document was refused as outside the repository); `status` for setup.
- `prose-edit/skills/prose-edit/scripts/review.py` (nexus-scmcs): moved from .claude/skills/prose-edit; unchanged.
- `conexus/hooks/scripts/version_lockstep_hook.py` (nexus-scmcs): the no-marketplace fallback plugin list names conexus-prose-edit.

## Deferred to the next client release

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/skills/upgrade/SKILL.md` (nexus-6pfgb.14): the list of what an upgrade does not carry no longer says a note's TTL is lost; it names `nx store export` (each chunk's `ttl_days` and `indexed_at` as stored) and the `nx catalog export` recovery bundle (each `store_put` note's own `ttl_days`, clock restarted at import; a note stored by 7.87.0 or earlier or brought in by an `.nxexp` import keeps it only when its chunk row is provably its own write, nexus-6pfgb.34; tags and category also on sole ownership, nexus-6pfgb.35). Deferred: the bead also changes `src/nexus/catalog/recovery_bundle.py`.
