---
description: Audit a project's RDR lifecycle for silent-scope-reduction base rate, or inspect/manage scheduled periodic audits
---

# RDR Audit

!{
NEXUS_RDR_ARGS="${ARGUMENTS:-}" NEXUS_PROJECT_ROOTS="${NEXUS_PROJECT_ROOTS:-}" python3 "$CLAUDE_PLUGIN_ROOT/resources/rdr_commands/rdr_audit.py"
}

## Arguments

$ARGUMENTS

## Action

Follow the `rdr-audit` skill body.

The preamble above has already derived the target project and pre-scoped the evidence layer. The skill body should:

1. **Seed T1 link context** to RDR-067 tumbler (1.1.771) so audit findings auto-link
2. **Run the main-session transcript pre-step** — NOT delegatable (see skill body §Main-Session PRE-STEP). Honor the `--no-transcripts` flag if passed.
3. **Load the canonical prompt** from T2 `nexus_rdr/067-canonical-prompt-v1` and substitute `{project}` + `{transcript_excerpts}`
4. **Dispatch `deep-research-synthesizer`** via Agent tool with the substituted prompt as the task body
5. **Parse the subagent output** for verdict, incident count, confidence, drift distribution
6. **The skill body owns `memory_put`** — persist the full output to T2 `rdr_process/audit-<project>-<YYYY-MM-DD>` with `ttl=0`. Do NOT rely on the subagent to self-persist (Phase 1b finding: 0/3 runs self-persisted).
7. **Surface a compact summary** to the user: verdict, rate, confidence, drift distribution, T2 record id for the full record
8. **Discrepancy check**: `memory_search(project="rdr_process", query="audit-<project>")` — if this audit contradicts a prior one (different verdict category or dominant drift category), flag it for user review before returning

For management subcommands (`list` / `status` / `history` / `schedule` / `unschedule`), follow the `## Management Subcommands` section in the skill body. Honor the safety split: read-only subcommands (`list`/`status`/`history`) must not mutate OS or T2 state; print-only subcommands (`schedule`/`unschedule`) must not execute `launchctl load`, `launchctl unload`, crontab edits, or plist file writes — print the install/uninstall templates for the user to run manually.
