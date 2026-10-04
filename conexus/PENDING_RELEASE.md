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



## Awaiting the next release or plugin cut (pinned: v7.69.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- outside-critique doc drift (nexus-aruua): `conexus/commands/devonthink-index.md` no longer says its data is pre-loaded with no tool calls needed; it says the sections are the `!` preamble's output and to gather them with the skill's tools when they are missing.
- outside-critique doc drift (nexus-aruua): `conexus/commands/rdr-list.md` same change; on a missing table it runs `nx rdr preamble rdr-list` or reads `docs/rdr/README.md` instead of reporting an empty index.
- outside-critique doc drift (nexus-aruua): `conexus/commands/rdr-gate.md` same change; text only.
- outside-critique doc drift (nexus-aruua): `conexus/commands/rdr-fix.md` same change; text only.
- outside-critique doc drift (nexus-aruua): `conexus/commands/rdr-accept.md` same change; text only.
- outside-critique doc drift (nexus-aruua): `conexus/skills/nexus/SKILL.md` hydration example no longer attributes the 300-record read cap to ChromaDB; text only.
- outside-critique doc drift (nexus-aruua): `conexus/agents/developer.md` drops the "do NOT commit `.beads/issues.jsonl`" clause; the export is untracked.
- outside-critique doc drift (nexus-aruua): `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` drops the "always commit `.beads/issues.jsonl`" bullet; the export is untracked.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/hooks.json` drops the six entries for the RDR-184 ledger and RDR-205 projector hooks and the behaviour census (SessionStart `behaviour_census.py`, PreToolUse `agent-dispatch-expect`, SubagentStart `subagent-start-stamp` and `subagent-start-tuple`, SubagentStop `subagent-stop` and `subagent-stop-tuple`); the SubagentStop event is no longer wired.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/behaviour_census.py` is deleted; its SessionStart entry is gone and the module it fed (`nx census`) no longer exists.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/mailbox_drain.py` has comment-only edits dropping references to the deleted `tuple_ledger_project.py` hook; no behaviour change.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/_endpoint_resolve.py` drops `resolve_endpoint_and_token`, the credential policy only the deleted ledger projector used; `resolve_base_url` and the readers are unchanged.
- cleanup step A1 (nexus-0r1uz): `conexus/hooks/scripts/routing/_lib.py` has comment-only edits dropping references to the deleted census producer and projector; no behaviour change.
- cleanup step A1 (nexus-0r1uz): `conexus/skills/orchestration/SKILL.md` deletes the Background-Teammate Ledger section, the ledger-reading wait-for-a-report section and the ledger-credit bullet of the resume section, because the hooks that wrote the ledger are deleted.
- cleanup step A1 (nexus-0r1uz): `conexus/commands/continuation.md` drops Step 0 audits 3 and 4 (declaration-completeness and the scenario-27 payload tripwire), which read the deleted ledger.
- cleanup step A1 (nexus-0r1uz): `conexus/README.md` drops the hook table rows and the command-tier explanation for the deleted ledger, projector and census hooks.
- cleanup step 15 (nexus-0r1uz): `conexus/skills/writing-nx-skills/SKILL.md` points at the renamed frontmatter test (`test_every_skill_frontmatter_is_valid`); text pointer only.
- cleanup step 12 (nexus-0r1uz): `conexus/skills/rdr-audit-checklist/SKILL.md` drops the `schedule` and `unschedule` subcommands and their plist and crontab templates; `list`, `status` and `history` stay. The template files under `scripts/` they pointed at are deleted.
- cleanup step 12 (nexus-0r1uz): `conexus/commands/rdr-audit.md` drops the `schedule` / `unschedule` text; the management subcommands are `list`, `status` and `history`, all read-only.
- cleanup step 7 (nexus-0r1uz): `conexus/skills/orchestration/SKILL.md` drops its text about the agent-verify-claims checker script (the script is deleted); the orchestrator still re-runs each reported COMMAND itself.
- cleanup step 7 (nexus-0r1uz): `conexus/skills/mailbox/SKILL.md` drops the unacked-request sweep bullet and its success criterion, since the inbound-relay-acks sweep script is deleted.
- cleanup steps A2 and A3 (nexus-0r1uz): `conexus/hooks/hooks.json` drops the PreToolUse Bash entries for the bd-close gate (`nx_hook_shim.py pre-close-verification`) and the phase-review close gate (`routing/phase_review_close_requires_gate.py`), and the `mcp_tool` entries for Stop (`hook_stop_verification`), StopFailure (`hook_stop_failure`), PostCompact (`hook_post_compact`) and PostToolUse Write|Edit (`hook_divergence_language_guard`).
- cleanup step A2 (nexus-0r1uz): `conexus/hooks/scripts/routing/phase_review_close_requires_gate.py` is deleted; a phase-review `bd close` is no longer gated on the PASSED sentinel.
- cleanup step A2 (nexus-0r1uz): `conexus/hooks/scripts/routing/registry.yaml` drops the `phase_review_close_requires_gate` rule; no rule is `fail_closed` now.
- cleanup step A2 (nexus-0r1uz): `conexus/hooks/scripts/routing/README.md` drops the phase-review rule and the bd-close gate from the cumulative-cap table (2 of 4).
- cleanup step A3 (nexus-0r1uz): `conexus/hooks/scripts/divergence-language-scan.py` is deleted along with the divergence-language guard that ran it.
- cleanup step A2 (nexus-0r1uz): `conexus/hooks/scripts/_hook_logging.py` has comment-only edits dropping references to the deleted phase-review close gate script; no behaviour change.
- cleanup step A2 (nexus-0r1uz): `conexus/hooks/scripts/_interpreter.py` has comment-only edits dropping references to the deleted phase-review close gate; no behaviour change.
- cleanup step A2 (nexus-0r1uz): `conexus/agents/code-review-expert.md` drops the "NEVER write a `review-completed` marker" section; the bd-close gate that read the marker is deleted.
- cleanup step A2 (nexus-0r1uz): `conexus/agents/substantive-critic.md` drops the "NEVER write a `review-completed` marker" section; the bd-close gate that read the marker is deleted.
- cleanup step A2 (nexus-0r1uz): `conexus/skills/code-review/SKILL.md` drops the "On Completion (Mandatory)" section about the `review-completed` marker.
- cleanup step A2 (nexus-0r1uz): `conexus/resources/agent-shared/CONTEXT_PROTOCOL.md` drops the reserved `review-completed` token section and keeps the note on handing findings to a sibling reviewer through T2.
- cleanup steps A2 and A3 (nexus-0r1uz): `conexus/README.md` drops the hook table rows for the bd-close gate, the phase-review close gate, Stop verification, StopFailure, PostCompact and the divergence-language guard, and the divergence scan script from the file tree.
- cleanup step 13 (nexus-0r1uz): `conexus/skills/phase-review-gate/SKILL.md` is rewritten as a short manual checklist: the Pass 1 / Pass 2 contract, the `--evidence` format, the sentinel and the T1 marker are gone, because the `nx rdr preamble phase-review-gate` verb that produced them is deleted. The cross-walk of §Approach against closing beads, the RDR-112 root cause and the limits stay.
- cleanup step 13 (nexus-0r1uz): `conexus/skills/rdr-close/SKILL.md` drops Step 1.5 (Problem Statement Replay, branches A to D), the `Force Implemented (audit)` short-circuit, the `rdr-close-active` T1 marker and every `--reason` / `--pointers` / `--force-implemented` / `--force` flag that only the deleted `nx rdr preamble rdr-close` verb parsed; the critic step takes an override reason from the user instead of a flag. The status flip via `nx rdr set-status`, the post-mortem and the T3 archival stay.
- cleanup step 13 (nexus-0r1uz): `conexus/skills/rdr-create/SKILL.md` stops saying `/conexus:rdr-close` enforces the Gap headings; only `/conexus:rdr-gate` Layer 1 does.
- cleanup step 13 (nexus-0r1uz): `conexus/resources/rdr/TEMPLATE.md` stops saying `/conexus:rdr-close` enforces the Gap headings; only `/conexus:rdr-gate` Layer 1 does.
- cleanup step 13 (nexus-0r1uz): `conexus/skills/using-nx-skills/SKILL.md` describes the phase-review-gate checklist without the Pass 1 / Pass 2 and BLOCKED wording of the deleted verb.
- cleanup step 13 (nexus-0r1uz): `conexus/registry.yaml` calls phase-review-gate a manual checklist that catches, not blocks, silent scope reduction.
- cleanup step 13 (nexus-0r1uz): `conexus/README.md` calls phase-review-gate a manual checklist that catches, not blocks, silent scope reduction.
- cleanup step A5 (nexus-0r1uz): `conexus/README.md` says the core server registers 2 internal `hook_*` tools, not 12; text only.

## Deferred to the next client release

_Empty. The entry deferred here (nexus-wbfpw.41, the `skills/upgrade/SKILL.md` text for the `rdr192-manifest-backfill` rung) shipped with the 7.68.0 client release._
