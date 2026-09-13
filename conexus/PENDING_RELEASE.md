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



## Awaiting the next release or plugin cut (pinned: v7.43.0)

- nexus-cnzei.5: `conexus/hooks/scripts/auto-approve-nx-mcp.sh` — removed
  `mcp__plugin_conexus_nexus__daemon_uninstall` from the PermissionRequest/
  PreToolUse auto-approve allowlist (destructive, `confirm=true` is a
  trivial self-gate, not a human-in-the-loop check). Inert until the pin
  advances; a live session on the pinned tag still auto-approves it until
  then.
- `conexus/skills/peer-messaging/SKILL.md` (new, nexus-tacsg): standalone skill for messaging
  other Claude sessions and dispatched agents (channel choice, request acknowledgement,
  trust boundary, sharing one machine). Inert until the pin advances. Bead nexus-tacsg.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-tacsg): routes messaging and machine-sharing
  situations to /conexus:peer-messaging. Inert until the pin advances. Bead nexus-tacsg.
- `conexus/skills/orchestration/SKILL.md` (nexus-tacsg): points its mid-turn message section at
  /conexus:peer-messaging. Inert until the pin advances. Bead nexus-tacsg.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-x5kfm): rewrote the T2 ttl convention for
  the nexus-473mx reversal (omitting ttl is now permanent, not a 30-day default). Inert until
  the pin advances.
- `conexus/skills/knowledge-tidying/SKILL.md` (nexus-x5kfm): rewrote the memory_put write-back
  line for the same nexus-473mx ttl reversal. Inert until the pin advances.
- `conexus/skills/nexus/SKILL.md` (nexus-x5kfm): rewrote the memory_put example comment for the
  same nexus-473mx ttl reversal. Inert until the pin advances.
- `conexus/agents/_shared/CONTEXT_PROTOCOL.md` (nexus-cnzei.3): dropped a stale `ttl=30`
  from the memory_put write-back example. Inert until the pin advances.
- `conexus/agents/_shared/ERROR_HANDLING.md` (nexus-cnzei.3): rewrote the T2/T3 TTL sections —
  removed the nonexistent `expires_at=""` field and the retired "coerces ttl<=0 to NULL" claim.
  Inert until the pin advances.
- `conexus/agents/architect-planner.md` (nexus-cnzei.3): dropped `ttl=30` from the memory_put
  write-back bullet; fixed the `query(topic=...)` example (query has no topic parameter). Inert
  until the pin advances.
- `conexus/agents/code-review-expert.md` (nexus-cnzei.3): same two fixes as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/codebase-deep-analyzer.md` (nexus-cnzei.3): same two fixes as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/debugger.md` (nexus-cnzei.3): same two fixes as architect-planner.md above.
  Inert until the pin advances.
- `conexus/agents/deep-analyst.md` (nexus-cnzei.3): same two fixes as architect-planner.md
  above. Inert until the pin advances.
- `conexus/agents/deep-research-synthesizer.md` (nexus-cnzei.3): same two fixes as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/developer.md` (nexus-cnzei.3): same two fixes as architect-planner.md above.
  Inert until the pin advances.
- `conexus/agents/strategic-planner.md` (nexus-cnzei.3): same two fixes as architect-planner.md
  above. Inert until the pin advances.
- `conexus/agents/substantive-critic.md` (nexus-cnzei.3): same two fixes as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/test-validator.md` (nexus-cnzei.3): same two fixes as architect-planner.md
  above. Inert until the pin advances.
- `conexus/commands/knowledge-tidy.md` (nexus-cnzei.3): fixed the frontmatter description —
  nx_tidy is read-only; store_put is the T3 write. Inert until the pin advances.
- `conexus/commands/rdr-audit.md` (nexus-cnzei.3): dropped `ttl=0` (now rejected with a 400)
  from the memory_put step. Inert until the pin advances.
- `conexus/resources/rdr_process/INCIDENT-TEMPLATE.md` (nexus-cnzei.3): dropped `ttl=0` from
  the filing example; these entries are permanent by omission. Inert until the pin advances.
- `conexus/skills/analyze/SKILL.md` (nexus-cnzei.3): removed the `plan_save(...) for
  multi-agent pipeline outcomes` bullet — pipeline plans do not belong in the plan library.
  Inert until the pin advances.
- `conexus/skills/architecture/SKILL.md` (nexus-cnzei.3): dropped the string-typed
  `ttl="30d"` from the T2 execution-plan write-back (memory_put's ttl is int|None; execution
  plans are now permanent by omission). Inert until the pin advances.
- `conexus/skills/debug/SKILL.md` (nexus-cnzei.3): same plan_save fix as analyze/SKILL.md
  above. Inert until the pin advances.
- `conexus/skills/deep-analysis/SKILL.md` (nexus-cnzei.3): same plan_save fix as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/document/SKILL.md` (nexus-cnzei.3): same plan_save fix as analyze/SKILL.md
  above. Inert until the pin advances.
- `conexus/skills/knowledge-tidying/SKILL.md` (nexus-cnzei.3): same plan_save fix as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/nexus/SKILL.md` (nexus-cnzei.3): fixed the nx_tidy/nx_enrich_beads/
  nx_plan_audit example calls and descriptions (each requires real args; nx_tidy is
  read-only). Inert until the pin advances.
- `conexus/skills/nexus/reference.md` (nexus-cnzei.3): fixed the nx_tidy/nx_enrich_beads/
  nx_plan_audit section, the memory_put ttl table row and examples, and the store_put ttl
  example. Inert until the pin advances.
- `conexus/skills/query/SKILL.md` (nexus-cnzei.3): same plan_save fix as analyze/SKILL.md
  above. Inert until the pin advances.
- `conexus/skills/rdr-audit/SKILL.md` (nexus-cnzei.3): dropped every `ttl=0` (canonical-prompt
  load note, audit persistence call, checklist item, incident-filing note, PRODUCE line); also
  fixed a `memory_list` reference to the real `memory_get(title="")` listing call. Inert until
  the pin advances.
- `conexus/skills/research-synthesis/SKILL.md` (nexus-cnzei.3): same plan_save fix as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/research/SKILL.md` (nexus-cnzei.3): same plan_save fix as analyze/SKILL.md
  above. Inert until the pin advances.
- `conexus/skills/strategic-planning/SKILL.md` (nexus-cnzei.3): fixed the string-typed
  `ttl="30d"` on the continuation-state write-back to an integer `ttl=14` (memory_put's ttl is
  int|None; kept as a deliberate expiring row, not cargo-culted). Inert until the pin advances.
- `conexus/skills/substantive-critique/SKILL.md` (nexus-cnzei.3): dropped the string-typed
  `ttl="30d"` from the T2 critique-findings write-back (now permanent by omission). Inert
  until the pin advances.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-cnzei.3): fixed the plan_save bullet (same
  as analyze/SKILL.md) and the write-path line that attributed the T3 persist step to
  `/conexus:knowledge-tidy` itself rather than the `store_put` call after it. Inert until the
  pin advances.
- `conexus/commands/rdr-gate.md` (nexus-cnzei.3, fix round): dropped two `ttl="permanent"`
  sites (memory_put's ttl is int|None; the string form is store_put's contract, not this
  tool's). Inert until the pin advances.
- `conexus/commands/rdr-accept.md` (nexus-cnzei.3, fix round): same `ttl="permanent"` fix as
  rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-create/SKILL.md` (nexus-cnzei.3, fix round): dropped two
  `ttl="permanent"` sites, same fix as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-accept/SKILL.md` (nexus-cnzei.3, fix round): same `ttl="permanent"` fix
  as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-fix/SKILL.md` (nexus-cnzei.3, fix round): same `ttl="permanent"` fix as
  rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-close/SKILL.md` (nexus-cnzei.3, fix round): dropped three
  `ttl="permanent"` sites, same fix as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-research/SKILL.md` (nexus-cnzei.3, fix round): same `ttl="permanent"`
  fix as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-gate/SKILL.md` (nexus-cnzei.3, fix round): dropped four
  `ttl="permanent"` sites, same fix as rdr-gate.md above. Inert until the pin advances.
- nexus-r7xao — `conexus/hooks/scripts/_tuple_size_limits.py`, new: a
  stdlib-only, no-`nexus`-import mirror of the RDR-205 tuple-space size
  limits (body 4096 bytes global, key/dim/pattern values 256, subspace 256,
  nonce/claimant/claim_id 128) for the two hooks below, which cannot import
  the `nexus` package. Kept equal to the engine's own `TupleLimits.java` and
  the Python client's constants by `tests/db/test_tuple_size_limits_parity.py`.
  Until the pin advances, a running session's copies of the two hooks below
  carry no such module at all and send whatever size the caller gives them.
- nexus-r7xao — `conexus/hooks/scripts/tuple_ledger_project.py`: pre-checks
  the ledger tuple's subspace/keys/dims against the new size-limits mirror
  before posting, SKIPping (logged, exit 0) an oversized field instead of
  sending it. Until the pin advances, this projection still posts an
  oversized field and lets the engine be the only thing that refuses it.
- nexus-r7xao — `conexus/hooks/scripts/mailbox_drain.py`: pre-checks the
  address-derived subspace/pattern-value/claimant against the same mirror
  before any `rd`/`in`/`ack` POST for that address, logging a SKIP and
  returning instead of sending. Until the pin advances, this hook still
  posts an oversized address and relies solely on the engine's own refusal.
- nexus-r7xao — `conexus/skills/mailbox/SKILL.md`: a size-limit rule (body
  at most 4096 bytes, key/dim values at most 256, nonce at most 128 — over
  the limit is refused with `TooLarge`; longer content goes to T2/T3 with a
  reference in the tuple) plus a matching `## Success Criteria` row. Until
  the pin advances, a session reading this skill sees no size guidance and
  may still try to carry a document-length body in a tuple.
- `conexus/hooks/scripts/subagent-start.sh` (nexus-cnzei.2): Completion row scopes
  SendMessage-before-idling to background dispatches (foreground's final message
  is its own hand-back); T2-scan and Knowledge Map project resolution now use
  `git rev-parse --git-common-dir` so a worktree-isolated dispatch resolves the
  MAIN repo, not the worktree's own directory name, and the legacy
  `~/.config/nexus/context_l1.txt` global fallback is deleted; drops the
  machine-wide "Active Bead" line. Inert until the pin advances. Bead nexus-cnzei.2.
- (nexus-cnzei.2) `conexus/hooks/scripts/_hook_logging.py`: new shared helper
  (`configure_hook_logging()`) bridging structlog to stderr/logfile before a
  hook script's first `nexus.*` import; factored out of two hand-duplicated
  local copies (fix round 2, critic Significant). Inert until the pin
  advances. Bead nexus-cnzei.2.
- (nexus-cnzei.2) `conexus/hooks/scripts/rdr_hook.py`: calls the shared
  `_hook_logging.configure_hook_logging()` before importing
  `nexus.catalog`/`nexus.db`, so structlog's default stdout logger factory no
  longer leaks debug/warning lines into this SessionStart hook's own stdout.
  Inert until the pin advances. Bead nexus-cnzei.2.
- (nexus-cnzei.2) `conexus/hooks/scripts/routing/phase_review_close_requires_gate.py`:
  same shared-helper logging fix ahead of its `nexus.session` import; its
  deny message no longer hands the `# routing-allow:` escape to the gated
  subagent as its own move. Inert until the pin advances. Bead nexus-cnzei.2.
- (nexus-cnzei.2) `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py`:
  deny messages reword the hand-back instruction to scope SendMessage to
  background dispatches, and no longer hand the `# routing-allow:` escape
  to the gated subagent as its own move. Inert until the pin advances.
  Bead nexus-cnzei.2.
- (nexus-cnzei.2) `conexus/hooks/scripts/routing/README.md`: the authoring
  template's deny example now carries the same "only on the user's explicit
  instruction to use it" wording as the live hooks, so copying the template
  no longer reintroduces the pre-fix escape-ownership wording. Inert until
  the pin advances. Bead nexus-cnzei.2.
- (nexus-cnzei.2) `conexus/hooks/scripts/pre_close_verification_hook.sh`: the
  deny/warning messages no longer hand `NX_REVIEW_GATE_OVERRIDE=1` to the
  closing party as its own move, and the marker-write remedy now names the
  subagent-hands-back-to-orchestrator alternative. Inert until the pin
  advances. Bead nexus-cnzei.2.
