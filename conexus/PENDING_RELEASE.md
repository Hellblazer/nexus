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

**Deferring a straddling entry (nexus-2x3qy).** A plugin cut (`scripts/
cut_plugin_release.py`) refuses when a ledger entry's bead also touches wheel
content (`src/`, `conexus/plans/`, `conexus/daemon/`, `mcpb/`, `dt/`) the
wholesale import cannot hold back on a per-entry basis. The only fix is moving
that entry under `## Deferred to the next client release` below: the cut then
holds the entry's channel path(s) back from itself too (restored to the base
branch's own content) so the whole bead ships together, in one piece, at the
next client release. A deferred entry is exempt from the release-window
"ledger must be empty" rule above -- still declared there is correct, not
stale -- and stays exactly where it is until moved back deliberately.

---



## Awaiting the next release or plugin cut (pinned: v7.81.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- Agent bodies carry skill procedure (nexus-dg7i6.3): `conexus/agents/code-review-expert.md` carries the round rules the code-review skill held (report everything on a first review, `ship_blockers` against the relay's acceptance bar, the confirmation-pass reply), since the model dispatches the agent and never loads the skill.

- Agent bodies carry skill procedure (nexus-dg7i6.3): `conexus/agents/substantive-critic.md` carries the confirmation-pass reply the substantive-critique skill pointed to, since the model dispatches the agent and never loads the skill.

- Agent bodies carry skill procedure (nexus-dg7i6.3): `conexus/agents/developer.md` asks the developer to hand back the first live run of any check, gate, census or lint it adds, a rule the development skill held for the caller.

- Agent bodies carry skill procedure (nexus-dg7i6.3): `conexus/agents/strategic-planner.md` gives the continuation-state `memory_put` example `ttl=14`, as the strategic-planning skill did, so resume snapshots expire.

- `conexus/hooks/hooks.json` (nexus-3ya9d): every SessionStart entry's timeout is its own work budget plus a 30 s cold-start allowance for `uv tool run` (a Python fetch on the first session, the one-time tool environment, nine processes waiting on the same uv locks): 5 to 35 s for the setup notice and the lockstep hook, 10 to 40 s for preflight, session-start, session-context and rdr, 30 to 60 s for upgrade-auto and self-gc, 20 to 50 s for mcp-connect-wait. Warm hooks return when done and pay nothing.



## Deferred to the next client release

- Desktop restart step (nexus-93xx0): `conexus/launch/nx_standin.py` tells the user, wherever it says to reconnect the `nexus` server, that Claude Desktop needs the app quit and opened again because a new chat keeps the running stand-in. Deferred because the same bead changes `src/nexus/mcp/setup_tools.py`, whose `setup_status` description is kept equal to the stand-in's.
- Desktop restart step (nexus-93xx0): `conexus/launch/nx_setup.py` gives a finished run the next step `RECONNECT_NEXT`, which names Claude Desktop's quit-and-reopen. Deferred because the same bead changes `src/nexus/mcp/setup_tools.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/architect-planner.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/code-review-expert.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/codebase-deep-analyzer.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/debugger.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/deep-analyst.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/deep-research-synthesizer.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/developer.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/strategic-planner.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/substantive-critic.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/agents/test-validator.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/resources/agent-shared/RELAY_TEMPLATE.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/architecture/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/finishing-branch/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/rdr-close/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/rdr-show/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/writing-nx-skills/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/analyze-code.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/create-plan.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/debug.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/implement.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/nx-preflight.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/plan-audit.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/rdr-list.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/research.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/review-code.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Hidden from the model's listing (nexus-dg7i6.2): `conexus/commands/test-validate.md` carries `disable-model-invocation: true`, so the description leaves the model's skill listing and the `/slash` form stays for the user. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Stale skill name fixed (nexus-dg7i6.2): `conexus/commands/rdr-audit.md` says to follow the `rdr-audit-checklist` skill body, where it named the retired `rdr-audit` skill. Deferred with the rest of the bead.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/why-was-this-written/SKILL.md` names `/conexus:debugging` where it named the hidden `/conexus:debug`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/decision-drift-review/SKILL.md` names `/conexus:code-review` and `/conexus:debugging` where it named the hidden commands. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/design-to-code-trace/SKILL.md` names `/conexus:research-synthesis` where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/analyze/SKILL.md` names `/conexus:research-synthesis` where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/plan-first/SKILL.md` names `/conexus:design-to-code-trace` as its verb-skill example where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/rdr-show/SKILL.md` falls back to `nx rdr preamble rdr-list` where it delegated to the hidden `/conexus:rdr-list`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/agents/deep-research-synthesizer.md` names `/conexus:research-synthesis` where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Retired wrapper skill (nexus-dg7i6.6): `conexus/skills/code-review/SKILL.md` is deleted. The model loaded it zero times in 30 days and the eval loaded it 0 of 3 with true premises; its caller-side rules moved to the orchestration skill and the agents, and the SessionStart text and the routing no longer name it. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skill (nexus-dg7i6.6): `conexus/skills/debugging/SKILL.md` is deleted. The model loaded it zero times in 30 days and the eval loaded it 0 of 3 with true premises; its caller-side rules moved to the orchestration skill and the agents, and the SessionStart text and the routing no longer name it. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skill (nexus-dg7i6.6): `conexus/skills/development/SKILL.md` is deleted. The model loaded it zero times in 30 days and the eval loaded it 0 of 3 with true premises; its caller-side rules moved to the orchestration skill and the agents, and the SessionStart text and the routing no longer name it. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skill (nexus-dg7i6.6): `conexus/skills/test-validation/SKILL.md` is deleted. The model loaded it zero times in 30 days and the eval loaded it 0 of 3 with true premises; its caller-side rules moved to the orchestration skill and the agents, and the SessionStart text and the routing no longer name it. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skill (nexus-dg7i6.6): `conexus/skills/query/SKILL.md` is deleted. The model loaded it zero times in 30 days and the eval loaded it 0 of 3 with true premises; its caller-side rules moved to the orchestration skill and the agents, and the SessionStart text and the routing no longer name it. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skill (nexus-dg7i6.6): `conexus/skills/using-nx-skills/SKILL.md` is deleted. The model loaded it zero times in 30 days and the eval loaded it 0 of 3 with true premises; its caller-side rules moved to the orchestration skill and the agents, and the SessionStart text and the routing no longer name it. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/commands/debug.md` dispatches the debugger agent directly where it invoked the retired skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/commands/review-code.md` dispatches the code-review-expert agent directly where it invoked the retired skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/commands/test-validate.md` dispatches the test-validator agent directly where it invoked the retired skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/commands/implement.md` dispatches the developer agent directly where it invoked the retired skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/orchestration/SKILL.md` gains the review gate, the implementer brief checklist and the debugger escalation handling the retired skills held for the caller. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/orchestration/reference.md` gains the skill routing menu, the nx_answer cost rule, the working rules, Common Mistakes and Red Flags the retired using-nx-skills and query skills held. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/nexus/SKILL.md` names the nx_answer tool where it named the retired query skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/phase-review-gate/SKILL.md` points at the orchestration skill's review gate where it pointed at the retired code-review skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/substantive-critique/SKILL.md` points at the orchestration skill's review gate where it pointed at the retired code-review skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/why-was-this-written/SKILL.md` names the debugger agent where it named the retired debugging skill. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/decision-drift-review/SKILL.md` names the code-review-expert and debugger agents where it named the retired skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/skills/writing-nx-skills/SKILL.md` routes new skills to the orchestration reference where it routed them to the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/architect-planner.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/code-review-expert.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/codebase-deep-analyzer.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/debugger.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/deep-analyst.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/deep-research-synthesizer.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/developer.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/strategic-planner.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/substantive-critic.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
- Retired wrapper skills (nexus-dg7i6.6): `conexus/agents/test-validator.md` cites the orchestration skill's Red Flags table where its pre-flight cited the retired using-nx-skills. Deferred because the same bead changes `src/nexus/session_start_guidance.py`, whose text no longer names the retired skills.
