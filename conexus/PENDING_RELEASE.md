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


## Awaiting the next release or plugin cut (pinned: v7.36.0)

- `conexus/hooks/scripts/pre_close_verification_hook.sh` — nexus-fv65m: the command is tokenized quote-aware before it is split on operators, so a ';' or 'do' inside a quoted --reason no longer harvests prose ids as close targets; with an unbalanced quote the flag value is blanked before the raw-scan fallback.
- `conexus/hooks/scripts/rdr_hook.py` — nexus-owna8: collection existence is asked of the T3 client, not a substring of `nx collection list`; a resolution failure is logged instead of swallowed; the T3 call has a 4s deadline inside the hook's 10s cap; the summary counts every document the indexer walks (recursive) beside the RDR count.
- `conexus/agents/_shared/CONTEXT_PROTOCOL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/architect-planner.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/code-review-expert.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/codebase-deep-analyzer.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/debugger.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/deep-analyst.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/deep-research-synthesizer.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/developer.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/strategic-planner.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/substantive-critic.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/agents/test-validator.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/commands/knowledge-tidy.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/architecture/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/catalog/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/code-review/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/codebase-analysis/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/debugging/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/deep-analysis/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/nexus/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/nexus/reference.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/rdr-gate/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/rdr-research/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/research-synthesis/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/strategic-planning/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/substantive-critique/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/writing-nx-skills/SKILL.md` — nexus-fjc8v: examples name `collection="<subject>"` instead of the placeholder `knowledge` (a subject area per docs/collections.md; the bare name minted knowledge__knowledge, 1464 chunks live).
- `conexus/skills/using-nx-skills/SKILL.md` — nexus-fjc8v: Common Mistakes row on placeholder collections.
