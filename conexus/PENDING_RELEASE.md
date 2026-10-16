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
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/code-review/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/finishing-branch/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/rdr-close/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/rdr-show/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
- bd commands replace /beads: pointers (nexus-dg7i6.4): `conexus/skills/test-validation/SKILL.md` names `bd <verb>` where it named the beads plugin's `/beads:<verb>` commands, which nexus no longer installs; agents already run `bd` through Bash. Deferred because the same bead re-hosts `bd prime` in `src/nexus/hooks/session_context.py`.
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
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/using-nx-skills/SKILL.md` routes to the skill each hidden command wrapped (debugging, strategic-planning, codebase-analysis, development, code-review, plan-validation, test-validation, research-synthesis) and to `nx rdr preamble rdr-list`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/why-was-this-written/SKILL.md` names `/conexus:debugging` where it named the hidden `/conexus:debug`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/decision-drift-review/SKILL.md` names `/conexus:code-review` and `/conexus:debugging` where it named the hidden commands. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/development/SKILL.md` names `/conexus:code-review` where it named the hidden `/conexus:review-code`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/design-to-code-trace/SKILL.md` names `/conexus:research-synthesis` where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/analyze/SKILL.md` names `/conexus:research-synthesis` where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/query/SKILL.md` names the retired `research`, `debug` and `review-code` commands without a slash form, as history. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/plan-first/SKILL.md` names `/conexus:design-to-code-trace` as its verb-skill example where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/skills/rdr-show/SKILL.md` falls back to `nx rdr preamble rdr-list` where it delegated to the hidden `/conexus:rdr-list`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
- Model-facing routing repointed (nexus-dg7i6.2): `conexus/agents/deep-research-synthesizer.md` names `/conexus:research-synthesis` where it named the hidden `/conexus:research`. Deferred because the same bead repoints `src/nexus/hooks/preflight_verb.py`.
