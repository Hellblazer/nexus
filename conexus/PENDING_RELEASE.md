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



## Awaiting the next release or plugin cut (pinned: v7.44.0)

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
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` (nexus-cnzei.3): dropped a stale
  `ttl=30` from the memory_put write-back example (path updated nexus-cnzei.4: this file
  moved from `agents/_shared/` — see below). Inert until the pin advances.
- `conexus/resources/agent-shared/ERROR_HANDLING.md` (nexus-cnzei.3): rewrote the T2/T3 TTL
  sections — removed the nonexistent `expires_at=""` field and the retired "coerces ttl<=0
  to NULL" claim (path updated nexus-cnzei.4: this file moved from `agents/_shared/` — see
  below). Inert until the pin advances.
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
- `conexus/skills/why-was-this-written/SKILL.md` (nexus-cnzei.3): same plan_save fix as
  analyze/SKILL.md above (path updated nexus-cnzei.4: this skill was `skills/debug/SKILL.md`
  — see below). Inert until the pin advances.
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
- `conexus/skills/rdr-audit-checklist/SKILL.md` (nexus-cnzei.3): dropped every `ttl=0` (canonical-prompt
  load note, audit persistence call, checklist item, incident-filing note, PRODUCE line); also
  fixed a `memory_list` reference to the real `memory_get(title="")` listing call. Inert until
  the pin advances.
- `conexus/skills/research-synthesis/SKILL.md` (nexus-cnzei.3): same plan_save fix as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/design-to-code-trace/SKILL.md` (nexus-cnzei.3): same plan_save fix as
  analyze/SKILL.md above (path updated nexus-cnzei.4: this skill was
  `skills/research/SKILL.md` — see below). Inert until the pin advances.
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
- `conexus/skills/rdr-accept-checklist/SKILL.md` (nexus-cnzei.3, fix round): same `ttl="permanent"` fix
  as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-fix-checklist/SKILL.md` (nexus-cnzei.3, fix round): same `ttl="permanent"` fix as
  rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-close/SKILL.md` (nexus-cnzei.3, fix round): dropped three
  `ttl="permanent"` sites, same fix as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-research/SKILL.md` (nexus-cnzei.3, fix round): same `ttl="permanent"`
  fix as rdr-gate.md above. Inert until the pin advances.
- `conexus/skills/rdr-gate-checklist/SKILL.md` (nexus-cnzei.3, fix round): dropped four
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

nexus-cnzei.4 (one entry point per name): the collision/collapse work touched a
large number of files under one bead. Each is its own bullet, one path per
bullet, because the ledger parser only reads backtick spans on a bullet's
OPENING line — a wrapped multi-path bullet silently drops every path after
the first line.

- `conexus/commands/architecture.md` (nexus-cnzei.4): DELETED — collided under the same
  name as its own skill (a Skill-tool listing collision that shadowed the skill). The skill
  is now the sole entry point. Inert until the pin advances.
- `conexus/commands/deep-analysis.md` (nexus-cnzei.4): DELETED, same reason as
  architecture.md above. Inert until the pin advances.
- `conexus/commands/substantive-critique.md` (nexus-cnzei.4): DELETED, same reason as
  architecture.md above. Inert until the pin advances.
- `conexus/commands/enrich-plan.md` (nexus-cnzei.4): DELETED, same reason as
  architecture.md above. Inert until the pin advances.
- `conexus/commands/upgrade.md` (nexus-cnzei.4): DELETED, same reason as architecture.md
  above. Inert until the pin advances.
- `conexus/commands/phase-review-gate.md` (nexus-cnzei.4): DELETED, same reason as
  architecture.md above. Inert until the pin advances.
- `conexus/commands/knowledge-tidy.md` (nexus-cnzei.4): DELETED — duplicated
  `knowledge-tidying` under a different name. Its fuller nx_tidy → store_put → verify
  workflow was merged into `conexus/skills/knowledge-tidying/SKILL.md`. Inert until the pin
  advances.
- `conexus/agents/knowledge-tidier.md` (nexus-cnzei.4): DELETED outright — a 40-line
  RDR-080 stub agent that only redirected to `nx_tidy`. Callers call the MCP tool directly,
  or use the `knowledge-tidying` pointer skill. Inert until the pin advances.
- `conexus/agents/plan-auditor.md` (nexus-cnzei.4): DELETED, same reason as
  knowledge-tidier.md above (redirected to `nx_plan_audit`). Inert until the pin advances.
- `conexus/agents/plan-enricher.md` (nexus-cnzei.4): DELETED, same reason as
  knowledge-tidier.md above (redirected to `nx_enrich_beads`). Inert until the pin advances.
- `conexus/skills/why-was-this-written/SKILL.md` (nexus-cnzei.4): RENAMED from
  `skills/debug/SKILL.md` — the bare-verb nx_answer retrieval skill collided under
  `/conexus:debug` with the debugger-dispatch command of the same name. The
  `dimensions={"verb": "debug"}` value passed to `nx_answer` is unchanged. Inert until the
  pin advances.
- `conexus/skills/design-to-code-trace/SKILL.md` (nexus-cnzei.4): RENAMED from
  `skills/research/SKILL.md`, same reason as why-was-this-written above (collided under
  `/conexus:research` with the deep-research-synthesizer dispatch command). Inert until the
  pin advances.
- `conexus/skills/decision-drift-review/SKILL.md` (nexus-cnzei.4): RENAMED from
  `skills/review/SKILL.md` — `/conexus:review` invited confusion with
  `/conexus:review-code`. Inert until the pin advances.
- `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` (nexus-cnzei.4): MOVED from
  `agents/_shared/CONTEXT_PROTOCOL.md` — as a subdirectory of `agents/`, the five shared
  reference docs were listed as dispatchable `conexus:_shared:*` agent entries with nothing
  meaningful to invoke. Inert until the pin advances.
- `conexus/resources/agent-shared/ERROR_HANDLING.md` (nexus-cnzei.4): MOVED, same reason as
  CONTEXT_PROTOCOL.md above. Inert until the pin advances.
- `conexus/resources/agent-shared/MAINTENANCE.md` (nexus-cnzei.4): MOVED, same reason as
  CONTEXT_PROTOCOL.md above. Inert until the pin advances.
- `conexus/resources/agent-shared/README.md` (nexus-cnzei.4): MOVED, same reason as
  CONTEXT_PROTOCOL.md above. Inert until the pin advances.
- `conexus/resources/agent-shared/RELAY_TEMPLATE.md` (nexus-cnzei.4): MOVED, same reason as
  CONTEXT_PROTOCOL.md above. `src/nexus/commands/agents_cmd.py`'s `compose_worktree_developer`
  (outside this ledger's tracked surface) was updated to rewrite links to the new location.
  Inert until the pin advances.
- `conexus/agents/architect-planner.md` (nexus-cnzei.4): relative-link rewrite only
  (`./_shared/` -> `../resources/agent-shared/`), per the move above. Inert until the pin
  advances.
- `conexus/agents/code-review-expert.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/codebase-deep-analyzer.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/debugger.md` (nexus-cnzei.4): same link rewrite as architect-planner.md
  above. Inert until the pin advances.
- `conexus/agents/deep-analyst.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above; also drops its "Completion Protocol" section's contradiction
  of its own `<HARD-GATE>` (the former said persist to T2 AND T3; the HARD-GATE says exactly
  one). Inert until the pin advances.
- `conexus/agents/deep-research-synthesizer.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/developer.md` (nexus-cnzei.4): same link rewrite as architect-planner.md
  above. Inert until the pin advances.
- `conexus/agents/strategic-planner.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/substantive-critic.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above. Inert until the pin advances.
- `conexus/agents/test-validator.md` (nexus-cnzei.4): same link rewrite as
  architect-planner.md above. Inert until the pin advances.
- `conexus/commands/analyze-code.md` (nexus-cnzei.4): relative-link rewrite only
  (`../agents/_shared/` -> `../resources/agent-shared/`), per the move above. Inert until
  the pin advances.
- `conexus/commands/create-plan.md` (nexus-cnzei.4): same link rewrite as
  analyze-code.md above. Inert until the pin advances.
- `conexus/commands/debug.md` (nexus-cnzei.4): same link rewrite as analyze-code.md above.
  Inert until the pin advances.
- `conexus/commands/implement.md` (nexus-cnzei.4): same link rewrite as analyze-code.md
  above. Inert until the pin advances.
- `conexus/commands/research.md` (nexus-cnzei.4): same link rewrite as analyze-code.md
  above. Inert until the pin advances.
- `conexus/commands/review-code.md` (nexus-cnzei.4): same link rewrite as analyze-code.md
  above. Inert until the pin advances.
- `conexus/commands/test-validate.md` (nexus-cnzei.4): same link rewrite as analyze-code.md
  above. Inert until the pin advances.
- `conexus/skills/analyze/SKILL.md` (nexus-cnzei.4): relative-link rewrite
  (`../../agents/_shared/` -> `../../resources/agent-shared/`); also drops the stale "falls
  through to /conexus:query" phrasing (nx_answer inline-plans on a miss, it does not hand
  off to a sibling skill) for "inline-plans on a miss". Inert until the pin advances.
- `conexus/skills/architecture/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same
  as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/code-review/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/codebase-analysis/SKILL.md` (nexus-cnzei.4): relative-link rewrite only,
  same as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/composition-probe/SKILL.md` (nexus-cnzei.4): relative-link rewrite only,
  same as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/debugging/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/deep-analysis/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same
  as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/development/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/document/SKILL.md` (nexus-cnzei.4): relative-link rewrite, plus the same
  stale-phrase fix as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/orchestration/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same
  as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/rdr-audit-checklist/SKILL.md` (nexus-cnzei.4): relative-link rewrite, plus fixes a
  `memory_list` reference (no such MCP tool exists) to `memory_get(project=..., title="")`.
  Inert until the pin advances.
- `conexus/skills/rdr-gate-checklist/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same as
  analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/rdr-research/SKILL.md` (nexus-cnzei.4): relative-link rewrite only, same
  as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/research-synthesis/SKILL.md` (nexus-cnzei.4): relative-link rewrite only,
  same as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/strategic-planning/SKILL.md` (nexus-cnzei.4): relative-link rewrite only,
  same as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/substantive-critique/SKILL.md` (nexus-cnzei.4): relative-link rewrite
  only, same as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/test-validation/SKILL.md` (nexus-cnzei.4): relative-link rewrite only,
  same as analyze/SKILL.md above. Inert until the pin advances.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-cnzei.4): routing table updated to the
  renamed skill names above (`why-was-this-written`, `design-to-code-trace`,
  `decision-drift-review`, `knowledge-tidying`); description shortened to under 260 chars.
  Inert until the pin advances.
- `conexus/skills/writing-nx-skills/SKILL.md` (nexus-cnzei.4): relative-link rewrite; the
  quality checklist now names the actual CI-checked keyword list (`Triggers:`, `user says`,
  `workflow`, `process:`) instead of a wider list CI never enforced; the placeholder
  cross-reference examples switched to `/conexus:<skill-name>` so they read as a
  placeholder, not a broken reference. Inert until the pin advances.
- `conexus/skills/plan-first/SKILL.md` (nexus-cnzei.4): description shortened to under 260
  chars (was 422). Inert until the pin advances.
- `conexus/skills/test-authoring/SKILL.md` (nexus-cnzei.4): description shortened to under
  260 chars (was 306). Inert until the pin advances.
- `conexus/skills/knowledge-tidying/SKILL.md` (nexus-cnzei.4): body enriched with the
  nx_tidy → store_put → verify workflow merged in from the deleted `knowledge-tidy.md`
  command above. Inert until the pin advances.
- `conexus/skills/query/SKILL.md` (nexus-cnzei.4): "Verb-scoped shortcuts" section updated
  to the renamed verb-skill names above. Inert until the pin advances.

nexus-cnzei.4 fix round (review pass): finished the four rdr-* pairs that had no pinned-test
block, per pair below. rdr-gate/rdr-fix/rdr-accept/rdr-audit are unchanged — genuinely
pinned by TestRdrGateLoopRemedies and test_rdr_audit_skill.py; both files stay.

- `conexus/commands/rdr-close.md` (nexus-cnzei.4 fix round): DELETED — the skill
  (skills/rdr-close/SKILL.md) is independently executable via Bash/MCP tool calls and does
  not depend on the command's bash-injected data; no test or E2E fixture referenced this
  file. Inert until the pin advances.
- `conexus/commands/rdr-create.md` (nexus-cnzei.4 fix round): DELETED, same reason as
  rdr-close.md above. Inert until the pin advances.
- `conexus/commands/rdr-research.md` (nexus-cnzei.4 fix round): DELETED, same reason as
  rdr-close.md above. Inert until the pin advances.
- `conexus/commands/rdr-show.md` (nexus-cnzei.4 fix round): DELETED, same reason as
  rdr-close.md above. Inert until the pin advances.
- `conexus/skills/rdr-list/SKILL.md` (nexus-cnzei.4 fix round): DELETED — the reverse of the
  usual pattern in this bead. Its content explicitly said "the /conexus:rdr-list command
  already gathered" the data it formats, so it has no independent capability; the command
  (`!`nx rdr preamble rdr-list``) is the operative surface and is a live, tested fixture in
  two E2E scenarios (tests/cc-validation/scenarios/19_command_bash_injection_renders.sh and
  23_rdr130_flipped_command_renders.sh) that assert on its actual injected RDR-table content.
  Deleting the command instead (the default pattern) would have broken both. Inert until the
  pin advances.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-cnzei.4 fix round): RDR lifecycle line
  gains a note on which of the five rdr-* names now resolve via command-only, skill-only, or
  both. Inert until the pin advances.

- nexus-cnzei.6 (checkable agent reports, item 1): `conexus/skills/orchestration/SKILL.md`'s
  "VERIFY Line Convention" section and design-of-record brief template's "## VERIFY" step now
  require a closing VERIFY block (`commit=`/`<command> => rc=... passed`/`t2=` lines) instead
  of the old single free-form line, and point the orchestrator at
  `scripts/check_agent_verify_claims.py` before accepting a round. Inert until the pin
  advances.
- nexus-cnzei.6 (checkable agent reports, item 1): `conexus/resources/agent-shared/RELAY_TEMPLATE.md`
  gains an "Every Report Ends With a VERIFY Block" section carrying the same convention. Inert
  until the pin advances.
- nexus-cnzei.6 (checkable agent reports, item 2): `conexus/hooks/scripts/tuple_ledger_project.py`'s
  report path now parses the stopping agent's transcript for VERIFY lines and fills the
  ledger tuple's `commit`/`t2_ref`/`verify` dims (engine support: nexus-d9k5h,
  engine-service-v0.1.118); an engine that refuses the new dims (HTTP 400) is retried once
  with the legacy dims-only body, so the row is never dropped. Inert until the pin advances.

nexus-cnzei.6 fix round 1 (review pass): both reviewers found no ship-blockers but nothing
lands until fixed. Coverage below, per file.

- `conexus/hooks/scripts/tuple_ledger_project.py` (nexus-cnzei.6 fix round 1, CRE findings
  2/3): the legacy-dims retry now reads the HTTP 400 response BODY and only treats it as the
  below-floor undeclared-dimension case when the body matches the engine's exact
  `SchemaViolationException` shape for an unknown dimension — a generic 400 (a different
  schema violation, or no recognisable body) is a plain skip, never retried. `commit=`/`t2=`
  VERIFY keys are now case-insensitive. Inert until the pin advances.
- `conexus/hooks/scripts/expectations.sh` (nexus-cnzei.6 fix round 1, item 3; copied
  byte-identical to `tests/e2e/lib/expectations.sh`): `expectations_census` gained
  `_expectations_census_verify_absent`, printed after the existing SPACE_* lines — a
  `VERIFY_ABSENT_COUNT`/`VERIFY_UNVERIFIABLE`/`VERIFY_FALLBACK` line, gated on the connected
  engine's ledger template actually declaring the `verify` dimension (the DESIGN's own
  promise, undelivered in the original commit — critic Significant a). Never affects
  `expectations_census`'s own exit code. Inert until the pin advances.
- `conexus/skills/orchestration/SKILL.md` (nexus-cnzei.6 fix round 1, item 2 second half):
  the "VERIFY Line Convention" section now states plainly what the checker confirms (commit
  exists and touches named paths; `t2_ref` exists in T2; a VERIFY block is present) and what
  it does not (whether a claimed command ran, passed, or was the right one) — the coordinator
  still reads the command lines. Also documents the checker's UNVERIFIABLE branch. Inert
  until the pin advances.
- `conexus/resources/agent-shared/RELAY_TEMPLATE.md` (nexus-cnzei.6 fix round 1, critic
  observation): the "Every Report Ends With a VERIFY Block" section no longer repeats the
  three-line shape and explanatory prose verbatim — it points at
  `conexus/skills/orchestration/SKILL.md`'s "VERIFY Line Convention" section as the one copy.
  Inert until the pin advances.
nexus-cnzei.6: resolved the four rdr-* command/skill name collisions nexus-cnzei.4 deferred
(rdr-gate, rdr-fix, rdr-accept, rdr-audit). Neither file could be deleted without breaking
something real: the command carries the load-bearing `!`nx rdr preamble <name>`` bash
injection and $ARGUMENTS parsing that TestRdrGateLoopRemedies and test_rdr_audit_skill.py
pin, and the skill carries content those same tests pin elsewhere. Renamed the skill side
instead — same strategy nexus-cnzei.4 used for the "debug"/"research"/"review" verb skills.

- `conexus/skills/rdr-gate-checklist/SKILL.md` (nexus-cnzei.6): RENAMED from
  `skills/rdr-gate/SKILL.md` (directory + frontmatter `name:`) to resolve the collision with
  `commands/rdr-gate.md`. Content unchanged. Inert until the pin advances.
- `conexus/skills/rdr-fix-checklist/SKILL.md` (nexus-cnzei.6): RENAMED from
  `skills/rdr-fix/SKILL.md`, same reason, against `commands/rdr-fix.md`. Content unchanged.
  Inert until the pin advances.
- `conexus/skills/rdr-accept-checklist/SKILL.md` (nexus-cnzei.6): RENAMED from
  `skills/rdr-accept/SKILL.md`, same reason, against `commands/rdr-accept.md`. Content
  unchanged. Inert until the pin advances.
- `conexus/skills/rdr-accept/SKILL.md` -> `conexus/skills/rdr-accept-checklist/SKILL.md`
  (nexus-cnzei.6): same rename, same reason, against `commands/rdr-accept.md`. Content
- `conexus/skills/rdr-audit/SKILL.md` -> `conexus/skills/rdr-audit-checklist/SKILL.md`
  (nexus-cnzei.6): same rename, same reason, against `commands/rdr-audit.md`. Content
- `conexus/skills/rdr-audit-checklist/SKILL.md` (nexus-cnzei.6): RENAMED from
  `skills/rdr-audit/SKILL.md`, same reason, against `commands/rdr-audit.md`. Content
  unchanged. Inert until the pin advances.
- `conexus/registry.yaml` (nexus-cnzei.6): `rdr_skills:` keys `rdr-gate`/`rdr-fix`/
  `rdr-accept`/`rdr-audit` renamed to `rdr-gate-checklist`/`rdr-fix-checklist`/
  `rdr-accept-checklist`/`rdr-audit-checklist` to match the renamed skill directories;
  `slash_command:`/`command_file:` values are unchanged (still the unrenamed `/rdr-gate`
  etc. command). Inert until the pin advances.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-cnzei.6): RDR lifecycle line updated to
  name the skill-side rename instead of "both files real" under the same name. Inert until
  the pin advances.
- `conexus/hooks/scripts/subagent-start.sh` (nexus-cnzei.6): the code-nav/code-review
  agent-purpose classification now also checks the dispatch's own `agent_type` field
  (`code-review`, `Explore`/`codebase-deep-analyzer`), not only the always-empty `TASK_TEXT`
  the harness never populates in a real dispatch (injection audit S1). `TASK_TEXT` stays as
  a fallback OR-condition. Inert until the pin advances.
- `conexus/registry.yaml` (nexus-cnzei.6): `pipelines.feature`/`pipelines.bug` sequences
  gain `substantive-critic` after `code-review-expert` — they omitted it entirely, contradicting
  development/SKILL.md's mandatory-both-reviewers gate (injection audit C5). This `pipelines:`
  section is now the one place the implement-code pipeline order lives; orchestration/SKILL.md's
  Quick Routing table points here instead of carrying its own copy. Inert until the pin advances.
- `conexus/skills/orchestration/SKILL.md` (nexus-cnzei.6): Quick Routing's "Implement code" row
  gains `-> substantive-critic`, matching the fixed registry.yaml pipeline and
  development/SKILL.md's existing mandatory-both-reviewers rule. Replaced two stale
  `~/.claude/CLAUDE.md § Review Discipline` / `§ Testing "serial-vs-parallel"` citations (neither
  section exists in that file — injection audit S6) with pointers at the real in-repo locations
  (development/SKILL.md § Post-Implementation Review + Commit; this same file's own
  "service/ builds: one builder at a time" paragraph). Inert until the pin advances.
- `conexus/skills/using-nx-skills/SKILL.md` (nexus-cnzei.6): "Needs design across modules" line
  reordered from `/conexus:architecture` then `/conexus:create-plan` to the reverse — matching
  architecture/SKILL.md's own stated Pipeline Position (strategic-planner before
  architect-planner) and registry.yaml's `architect-planner` predecessor, which the old wording
  contradicted (injection audit C5). Inert until the pin advances.
- `conexus/skills/test-authoring/SKILL.md` (nexus-cnzei.6): DELETED — moved to
  `.claude/skills/test-authoring/SKILL.md` (repo-local, not shipped in the plugin). It was
  entirely nexus-repo-specific (this repo's own test suite, dev-loop layers, tests/AGENTS.md)
  shipping to every conexus plugin user for no benefit outside this repo (injection audit S6).
  `conexus/registry.yaml`'s `standalone_skills.test-authoring` entry removed to match.
  `conexus/README.md`'s skill count and table row, and `conexus/evals/`'s p03 case (which
  positively tested this skill triggering — now impossible from the plugin's own surface,
  same reason nexus-dkotg dropped n03/p07/p08 for release/engine-release) also updated. Inert
  until the pin advances (the DELETION half; the .claude/skills/ addition is repo-local and
  active immediately, not gated by the plugin pin at all).
