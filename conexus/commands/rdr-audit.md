---
allowed-tools: Bash
description: Audit a project's RDR lifecycle for silent-scope-reduction base rate, or inspect/manage scheduled periodic audits
---

# RDR Audit

!`nx rdr preamble rdr-audit`

## Arguments

$ARGUMENTS

**Targeted dispatch**: if `$ARGUMENTS` carries flags/targets, re-run, via the
Bash tool, `nx rdr preamble rdr-audit -- <parsed args>` passing each parsed
token as a real argv token. Never splice raw `$ARGUMENTS` into a shell-quoted
line — free text with apostrophes/quotes breaks the quoting (nexus-ybvyo).

## Action

Follow the `rdr-audit` skill body.

The preamble above has already derived the target project and pre-scoped the evidence layer, AND (RDR-201 P1.8, nexus-j9z30.8) run a closed-vocabulary scan of the target's `docs/rdr/*.md` frontmatter against the packaged `rdr-lifecycle` table: any on-disk `status:` value outside the table's domain is printed as a `FINDING:` line naming the file and the value; a file carrying `kind: companion` is skipped and counted separately (companions carry no lifecycle status at all). A separate `T2 <repo>_rdr status census:` line reports the same repo's T2-side status counts — this is informational only, never merged into the file findings. Directly under it, one `DRIFT:` line per RDR whose file frontmatter status and T2 status disagree, plus a count: nothing reconciles the two surfaces automatically (the SessionStart reconcile that claimed to had never run and was deleted, nexus-e19sa; `set-status` writes the file, the lifecycle skills write T2), so a disagreement is a finding for a human to settle by hand (bead nexus-nxn5g carries the known nine). If those `FINDING:` lines are non-empty, surface them to the user alongside the skill body's own drift-audit summary below; they are a distinct, mechanically-checked signal from the LLM-judged silent-scope-reduction audit the rest of this command runs.

A `Needs re-examination (T2 <repo>_rdr markers):` section follows the census (RDR-201 P3.3, nexus-j9z30.22): every `needs-reexamination: RDR-<from> <old>-><new>` line that `nx rdr set-status` appended to a dependent's T2 entry when a record joined to it by a `supersedes` edge changed status. Each marker names the flipped record, its transition, and the edge (`RDR-a supersedes RDR-b`) so the direction is on the line; a repeated flip does not stack a duplicate. Report-only: surface the list verbatim, never treat a marker as blocking. A marker is CLEARED by hand, by deleting its line from the dependent's T2 entry once that record has actually been re-examined (and its own status flipped if the re-examination changes it) -- the audit listing is what is left to do, and an entry that keeps accumulating lines is one nobody re-examined.

The skill body should:

1. **Seed T1 link context** to RDR-067 tumbler (1.1.771) so audit findings auto-link
2. **Run the main-session transcript pre-step** — NOT delegatable (see skill body §Main-Session PRE-STEP). Honor the `--no-transcripts` flag if passed.
3. **Load the canonical prompt** from T2 `nexus_rdr/067-canonical-prompt-v1` and substitute `{project}` + `{transcript_excerpts}`
4. **Dispatch `deep-research-synthesizer`** via Agent tool with the substituted prompt as the task body
5. **Parse the subagent output** for verdict, incident count, confidence, drift distribution
6. **The skill body owns `memory_put`** — persist the full output to T2 `rdr_process/audit-<project>-<YYYY-MM-DD>` with `ttl=0`. Do NOT rely on the subagent to self-persist (Phase 1b finding: 0/3 runs self-persisted).
7. **Surface a compact summary** to the user: verdict, rate, confidence, drift distribution, T2 record id for the full record
8. **Discrepancy check**: `memory_search(project="rdr_process", query="audit-<project>")` — if this audit contradicts a prior one (different verdict category or dominant drift category), flag it for user review before returning

For management subcommands (`list` / `status` / `history` / `schedule` / `unschedule`), follow the `## Management Subcommands` section in the skill body. Honor the safety split: read-only subcommands (`list`/`status`/`history`) must not mutate OS or T2 state; print-only subcommands (`schedule`/`unschedule`) must not execute `launchctl load`, `launchctl unload`, crontab edits, or plist file writes — print the install/uninstall templates for the user to run manually.
