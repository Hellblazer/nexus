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


## Awaiting the next release or plugin cut (pinned: v7.26.0)

- `conexus/hooks/scripts/rdr_hook.py` — bead: nexus-e19sa — Sam's ruling 2026-09-02. The SessionStart
  hook's file filter (`re.match(r"\d+", p.stem)`) matched zero of this
  repo's `rdr-NNN-*.md` files, so the hook exited before any logic on every
  session since it was written. The file<->T2 status RECONCILE half
  (`_reconcile`, `_update_file_status`, `_update_t2_status`, the
  `_STATUS_ORDER`/`_TERMINAL` derivation) is deleted, not switched on: a
  never-watched two-way writer would have resolved nine known file/T2
  disagreements by a ranking rule nobody had seen run. (`nx rdr set-status`
  does NOT write T2 -- the lifecycle skills do -- so the drift class still
  exists; `nx rdr preamble rdr-audit` now prints a `DRIFT:` line per
  file-vs-T2 disagreement instead of anything reconciling it.) The read-only
  summary stays and the filter is fixed (`rdr-NNN-*`, `rdrNNN-*`, `NNN-*`
  stems; `docs/rdr/*.md` non-recursive; `_EXCLUDE_FILES` kept), so the
  `RDR: N documents (...)` line prints for the first time. The
  plugin-shipped table copy and the `[lifecycle]` section it read (added
  under nexus-j9z30.5 and deleted again before any tag carried them, so net
  drift for that path is zero and it is deliberately not listed separately
  -- that breadcrumb entry was folded into this one at release prep, on its
  author's instruction, because at a cut boundary it describes machinery
  the release does not contain) had no reader left and is removed; the
  `[lifecycle] terminal_preserving_events` section that fed the deleted
  derivation is gone from the packaged table too. Inert for installed
  users until this ships.
- `conexus/skills/query/SKILL.md` — bead: nexus-4e75w.4 — describes
  `nx_answer`'s two possible return shapes now that continuation mode
  exists (composed answer, or a reduction instruction the calling
  session executes in-context). No routing change; the nexus-h33x8.6
  narrowing stands. RDR-200 Phase 1b.
- `conexus/skills/using-nx-skills/SKILL.md` — bead: nexus-4e75w.4 —
  same one-paragraph description in the routing table's `nx_answer`
  entry. RDR-200 Phase 1b.
- `conexus/hooks/scripts/auto-approve-nx-mcp.sh` — bead: nexus-4e75w.5 —
  adds `nx_answer_report` to the auto-approve list so the new completion-
  report tool does not prompt on every call while every sibling nx_* tool
  auto-approves. RDR-200 Phase 1c.
- `conexus/plans/builtin/document-discovery.yml` — bead: nexus-rl59s —
  `default_bindings.corpus` widened from `knowledge` to
  `knowledge,code,docs,rdr` so the single-step reroute reaches rdr__
  (RDR-200 Phase 1b degenerate class A, critique [24066]).
- `conexus/plans/builtin/corpus-coverage-check.yml` — bead: nexus-rl59s —
  same widening.
- `conexus/skills/rdr-close/SKILL.md` — bead: nexus-j9z30.4 — the
  Reverted-or-Abandoned flow now runs `nx rdr set-status NNN abandoned`
  (not `reverted`, retired from the rdr-lifecycle table's status domain
  — RDR-201 Phase 1); the "reverted" reason is recorded in T2/post-mortem
  only. The Superseded flow now writes `superseded_by` into the old RDR's
  frontmatter BEFORE calling `set-status ... superseded`, matching the
  table's `successor` guard, which reads that key from the file at call
  time and refuses `successor-not-named` otherwise.
- `conexus/commands/rdr-audit.md` — bead: nexus-j9z30.8 — documents the
  `nx rdr preamble rdr-audit` closed-vocabulary scan (RDR-201 Phase 1):
  the preamble now reports every `docs/rdr/*.md` frontmatter `status:`
  value outside the packaged `rdr-lifecycle` table's domain as a
  `FINDING:` line (file + value), skips (and separately counts)
  `kind: companion` files, and prints a separately-labelled `T2 <repo>_rdr
  status census:` line that is never merged into the file findings. No
  behavior change to the skill body's own silent-scope-reduction audit
  dispatch — this only documents the preamble's new output for the agent
  following the command.

All five are INERT until the next release or plugin cut: sessions load the
plugin from the pinned tag, so continuation mode is live in the CLIENT
(shipped 85c79761e) while these descriptions, the auto-approve entry, the
rdr_hook.py/rdr-lifecycle.toml changes, and the rdr-audit.md scan
documentation are not yet loaded by any session.
