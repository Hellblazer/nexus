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

---


## Awaiting the next release (pinned: v7.9.0)

- `conexus/skills/orchestration/SKILL.md` — Quick Routing table's "Review
  code" row routed to substantive-critic only "(if critical)", contradicting
  the mandatory both-reviewers-always gate stated elsewhere in this file and
  in `~/.claude/CLAUDE.md` § Review Discipline. Now routes unconditionally.
  (nexus-0yeer)
- `conexus/skills/orchestration/reference.md` — the same conditional-critic
  shape in its routing digraph (`[label="if critical"]`) and Quick Reference
  table row, found by BOTH round-1 reviewers of the SKILL.md fix; edited to
  the same always-both wording. (nexus-0yeer round-1 ship-blocker)
- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py`
  — guard-evidence-cluster fixes (T2 nexus/guard-evidence-cluster-root-
  cause-2026-08-18): shell-redirection tokens (`2>&1`, `> /dev/null`) no
  longer read as phantom refspecs/destination branches (F1, closes LEG A
  and B1); an inline `NX_REVIEW_GATE_OVERRIDE=1` command-text prefix is now
  parsed and honored, with every override logging an audited `escape`
  routing event (F2, closes B2); every deny's Remedy block now opens with a
  warning that the marker write must be a SEPARATE tool call (F4); a T2
  marker whose bead id lives only in the printed `-t review-<bead-id>`
  title now satisfies coverage (B3). (nexus-cr4lp)
- `conexus/hooks/scripts/pre_close_verification_hook.sh` — same cluster:
  the `bd` verb matcher now skips a leading inline env-assignment prefix so
  `NX_REVIEW_GATE_OVERRIDE=1 bd close ...` is recognized and the override
  is honored + audited (F2); bead-id harvesting now skips
  `--reason`/`--description`/`--notes`/`-m` OPTION VALUES so prose ids are
  never demanded as close targets while positional/loop-variable ids still
  are (F3, closes LEG D1); the deny Remedy opens with the same
  separate-tool-call warning (F4); the T2 title-only marker fix (B3).
  (nexus-cr4lp, nexus-iwlq4)
