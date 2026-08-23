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


## Awaiting the next release or plugin cut (pinned: v7.15.0)

- `conexus/evals/` (NEW): nexus-7zup9 — the first `claude plugin eval`
  corpus for the plugin's skill-triggering surface (15 cases: positive
  triggering for the highest-traffic skills, four release/engine-release
  and debugging/test-validation disambiguation pairs, five
  absorption-class negative cases guarding against the `nexus-77cct`
  "word 'plan' absorbs every question" failure mode recurring elsewhere).
  Authored, never run: `claude plugin eval` is early access and disabled
  on the authoring box, so every grader regex and every `llm` criteria
  field is a best-effort guess against the documented format, not a
  verified one — see `conexus/evals/README.md` § Open questions for the
  five concrete unknowns (skill-name form in `input_match`, no documented
  negation primitive, `case.yaml` scaffolds, repo-local skill scope for
  `release`/`engine-release`, `experimental.evals` declaration). An
  installed plugin at the pinned v7.15.0 tag has no eval suite at all
  until the next release ships.
- `conexus/hooks/scripts/expectations.sh`: nexus-2v0v7 (epic nexus-qkbo7) —
  new `expectations_reconcile` function: cross-checks the ledger's
  outstanding background STARTs against the harness's own
  `background_tasks` ground truth (CC 2.1.145, on Stop/SubagentStop hook
  input), reporting `STRANDED` (ledger outstanding, harness no longer
  tracks it — the silent-death class no prior consult surface could see)
  and `UNDECLARED_TASK` (harness-tracked, no ledger START row; corroborates
  `expectations_undeclared`'s existing rc=2 class). New rc=4. Fields
  absent/malformed => no-op, zero behavior change on a pre-2.1.145
  harness. **SCHEMA NOT YET INDEPENDENTLY VERIFIED** — see the function's
  own docstring in `expectations.sh` for the candidate-field-list and
  mixed-population caveats; a companion research pass on the exact
  `background_tasks` per-task schema was in flight when this landed.
- `conexus/hooks/scripts/stop_verification_hook.sh`: wires
  `expectations_reconcile` into the top-level Stop hook as a WARN-ONLY
  addition — folds a rc=4 finding into the hook's existing advisory
  `WARNINGS` text, never blocks, never changes the exit code, gated on
  `NX_ORCH_STOP_GUARD` (same gate as the rest of the expectations-ledger
  machinery). Independent of the pre-existing `on_stop` verification
  toggle, since this is a distinct RDR-184 concern.
  **INERT UNTIL RELEASE.** Hooks load from the pinned v7.15.0 tag, so
  neither change fires in any running session until the next release
  ships.
- `conexus/agents/strategic-planner.md`: nexus-ll7zm — the plan-audit
  loop terminates. Adds the round-counting and effort-budget call
  contract for `nx_plan_audit`, how to read the three verdicts
  (`READY`, `NOT READY`, `RESIDUALS-ONLY`), the rule that
  `DISCOVER-AT-IMPLEMENTATION` findings are recorded rather than
  re-planned, and the instruction to evaluate the LOOP rather than the
  finding after round 2. Origin: a five-round audit loop against the
  RDR-197 plan on 2026-08-23 where every finding was real and the loop
  was still the defect.
- `conexus/skills/plan-validation/SKILL.md`: nexus-ll7zm — same
  contract at the skill surface, since the skill is what most callers
  reach for.
- `conexus/commands/plan-audit.md`: nexus-ll7zm — same contract at the
  slash-command surface.
- `conexus/agents/architect-planner.md`: nexus-ll7zm — one-paragraph
  pointer to the same contract, because this agent also instructs
  "always call nx_plan_audit" and would otherwise reintroduce the
  unterminated loop through the other planner.
  **SPLIT DELIVERY, READ THIS BEFORE ASSUMING THE FIX IS LIVE.** The
  enforcement half of nexus-ll7zm ships in the WHEEL
  (`src/nexus/plans/audit_rounds.py` and the `nx_plan_audit` tool in
  `src/nexus/mcp/core.py`) and goes live on `uv tool install` /
  `nx upgrade`. These three plugin files ship on the PINNED TAG and go
  live only at the next release or plugin cut. Between those two
  moments the tool enforces the cap while the guidance still reads as
  though it does not, which is safe in that direction: callers who
  never pass `round_number` keep blocking-capable round-1 semantics.
  One thing DOES change for every caller at wheel upgrade, with or
  without `round_number`: the verdict is now computed from classified
  findings rather than taken from the model, so a round whose findings
  are all `DISCOVER-AT-IMPLEMENTATION` reads READY where the model
  might have said NOT READY. That direction is the fix itself, not a
  regression — but it is a behavior change at upgrade, not a no-op.
