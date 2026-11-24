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
## Awaiting the next release (pinned: v8.5.5)

ONE PATH PER BULLET, on the bullet's FIRST line.

- `conexus/skills/catalog/SKILL.md`: text only: `implements-heuristic` is a legacy type whose automated pass no longer runs (off since engine v0.1.57), and graph traversal excludes it by default, nexus-ybj1b.
- `conexus/skills/nexus/SKILL.md`: text only: the link-type line marks `implements-heuristic` legacy (no longer generated), nexus-ybj1b.
- `conexus/skills/rdr-close/SKILL.md`: text only: step 1 of catalog links no longer says the indexer auto-generates `implements-heuristic` links; it says to link a new RDR's files with `implements` directly, nexus-ybj1b.
- `conexus/agents/architect-planner.md`: text only: the T2 context line says only the SubagentStart hook injects it (SessionStart prints an index), 8.5.6 docs audit.
- `conexus/agents/code-review-expert.md`: text only: same T2 context line.
- `conexus/agents/codebase-deep-analyzer.md`: text only: same T2 context line; the bead is closed by the caller, not the agent; the search example names the bare `code` corpus.
- `conexus/agents/debugger.md`: text only: same T2 context line; the persistence default is T1 scratch (T3 only for validated findings); the caller closes the bead.
- `conexus/agents/deep-analyst.md`: text only: same T2 context line; T1-first persistence default; the caller closes the bead.
- `conexus/agents/deep-research-synthesizer.md`: text only: same T2 context line; a relay-specified T2 `ttl` omitted means permanent (there is no default 30); the collection example is a placeholder; the link-type list marks `implements-heuristic` legacy.
- `conexus/agents/developer.md`: text only: same T2 context line; no Agent tool and no commit stated in When to Delegate and PRODUCE; the review gate is the caller's policy, not a push guard; T2 default TTL is permanent.
- `conexus/agents/substantive-critic.md`: text only: same T2 context line.
- `conexus/agents/test-validator.md`: text only: same T2 context line.
- `conexus/commands/pdf-process.md`: text only: the collection argument is the bare subject, not a `knowledge__` name.
- `conexus/commands/rdr-audit.md`: text only: the RDR-067 link-context tumbler is looked up in the catalog, not hard-coded.
- `conexus/commands/rdr-gate.md`: text only: the preamble flag is `--skip-gaps`, the only override it honours; there is no `--skip-research`.
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md`: text only: T1 lifetime and sharing, the restore advice for the shared tree, the PRODUCE and Beads rules for an implementer (no commit, no close), and the project-namespace count.
- `conexus/resources/agent-shared/ERROR_HANDLING.md`: text only: T1 scope and lifetime errors, and the collection-name rules for new knowledge subjects.
- `conexus/resources/agent-shared/MAINTENANCE.md`: text only: the search parameter is `limit`, not `n`.
- `conexus/resources/rdr/TEMPLATE.md`: text only: Revision History holds one dated line per gate round, not the findings.
- `conexus/skills/composition-probe/SKILL.md`: text only: the subagent's tool budget names the probe file write and the pinned runner it needs.
- `conexus/skills/decision-drift-review/SKILL.md`: text only: the plan's required binding is `change_set`, filled from the question; nothing raises when it is missing.
- `conexus/skills/document/SKILL.md`: text only: the document plan ends in a `compare`; no extract chain.
- `conexus/skills/mailbox/SKILL.md`: text only: `mailbox_send` resolves a name to a session or an identity mailbox; `address_kind` values are agent, session and identity.
- `conexus/skills/nexus/reference.md`: text only: tool count and list, the parameters the tables lacked, the nx_answer budget fields, store_get ids, no `--chunk-size`, and the collection-naming rule.
- `conexus/skills/orchestration/SKILL.md`: text only: the mid-turn message to an agent goes through `mailbox_send`.
- `conexus/skills/rdr-show/SKILL.md`: text only: the example catalog links show `implements` and mark `implements-heuristic` as an older edge.
- `conexus/skills/rdr-audit-checklist/SKILL.md`: text only: the RDR-067 tumbler is resolved through the catalog; the schedule subcommands read triggers the user installed by hand; the prompt loads from the shipped file first.
- `conexus/skills/rdr-gate-checklist/SKILL.md`: text only: the prior-art step lists collections with `collection_list`, not `store_list`.
- `conexus/skills/upgrade/SKILL.md`: text only: a section on the RDR-229 stamp moves and the one engine restart; the managed-cloud hop text points at the migration runbook for the carry list.
- `conexus/skills/why-was-this-written/SKILL.md`: text only: the plan's required binding is `symptom` only, filled from the question; the plan does one query, no link walk.
- `conexus/skills/writing-nx-skills/SKILL.md`: text only: bead text is appended with `--append-notes`; `--body-file` and `--description` replace the description.

## Deferred to the next client release
