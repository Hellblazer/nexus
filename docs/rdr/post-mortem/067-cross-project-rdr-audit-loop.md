# Post-Mortem: RDR-067 Cross-Project RDR Audit Loop

## RDR Summary

RDR-067 formalizes the proven 2026-04-11 nexus historical audit pattern
as a one-command feedback loop for measuring the silent-scope-reduction
failure mode across projects. It ships the `nx:rdr-audit` skill which
wraps the `deep-research-synthesizer` agent with a pinned canonical
prompt, the `rdr_process` cross-project incident-filing template, and
local cron/launchd scheduling assets for periodic automation. It is
Phase 2 of a 4-RDR intervention cycle (RDR-069 + RDR-066 + RDR-067 +
RDR-068) built after the 2026-04-11 historical audit proved the
silent-scope-reduction pattern recurs at ~1-2 incidents per month on
active RDR work and that a single subagent dispatch with a fixed
prompt produces equivalent information to a set of 5 custom structured
metrics at 1/10th the effort.

The recursive self-validation (Phase 5a MVV + Phase 5b close-time
substantive-critic) used RDR-067 itself as the first subject of its
own audit machinery.

## Implementation Status

**Implemented.** All 10 epic beads closed (nexus-dqp.1 through
nexus-dqp.10) + one follow-up drift bead (nexus-cwm) filed and closed
mid-session. Plugin version 3.9.0 built and reinstalled locally; `nx
--version` reports 3.9.0. The canonical audit prompt is pinned in T2
at `nexus_rdr/067-canonical-prompt-v1` (permanent). The skill
(`nx/skills/rdr-audit/SKILL.md`) ships with both Phase 2a (core audit
dispatch) and Phase 2b (5 management subcommands with read-only /
print-only safety split). The incident template is at
`nx/resources/rdr_process/INCIDENT-TEMPLATE.md`. Scheduling assets
ship under `scripts/cron-rdr-audit.sh`, `scripts/launchd/`, and
`scripts/cron/` with READMEs carrying explicit "do not run launchctl
load automatically" safety notes.

Six research-class agents were softened mid-session to honor
relay-specified storage targets — a side-effect fix not in the
original RDR Technical Design but necessary to close Phase 1b's
"subagents don't self-persist" finding at the root cause. A
portability rewrite replacing `~/git/{project}/` hardcoded path
templates with a `{project_path}` substitution + `NEXUS_PROJECT_ROOTS`
env var was also applied post-gate after the Phase 5a MVV first
dispatch mis-resolved ART → arcaneum. Both scope additions are
documented in the RDR §Revision History (two new entries dated
2026-04-11) per the silent-scope-reduction prevention principle the
RDR itself formalizes.

Uncommitted at post-mortem time: the version-bump files
(`pyproject.toml`, `nx/.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `CHANGELOG.md`, `nx/CHANGELOG.md`,
`uv.lock`) are staged but not yet committed. Per
`docs/contributing.md`, the release commit + tag is the user's
explicit step, not autonomous — the close flow leaves those for the
user to review and push.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- **`nx:rdr-audit` skill** (`nx/skills/rdr-audit/SKILL.md`) — wraps
  the canonical audit prompt with main-session transcript pre-step,
  current-project derivation precedence chain, skill-body-owned
  `memory_put` persistence, and explicit CA-1 output-format contract.
  Enforces the non-delegatable transcript-mining invariant and the
  RDR's agent-dispatch + parse + persist + summary flow.
- **5 management subcommands**: `list` / `status` / `history`
  (read-only, no OS or T2 mutation) + `schedule` / `unschedule`
  (print-only, no privileged OS execution). Safety split documented
  explicitly; all 5 subcommands have dedicated SKILL.md subsections
  with exact shell-out patterns or template-print behavior.
- **Cross-project incident template**
  (`nx/resources/rdr_process/INCIDENT-TEMPLATE.md`) with 6 frontmatter
  fields (`project`, `rdr`, `incident_date`, `drift_class`,
  `caught_by`, `outcome`) + 8 required narrative sections. The
  `drift_class` enum matches the canonical prompt taxonomy exactly so
  sibling project filings aggregate without translation.
- **Scheduling assets**: shell wrapper (`scripts/cron-rdr-audit.sh`,
  chmod +x, strict bash mode, log rotation), macOS launchd plist
  template, Linux crontab template (true 90-day cadence via `0 3 1
  */3 *`), and two platform READMEs. The `claude -p '/nx:rdr-audit
  <project>'` invocation path was verified end-to-end in Phase 1b
  spike (T2 id 743) before Phase 4 files were written.
- **Test suite**: 91 new tests across three files
  (`tests/test_rdr_audit_skill.py` 46, `tests/test_rdr_audit_scheduling.py`
  29, `tests/test_rdr_audit_incident_template.py` 16). All structural
  checks: frontmatter, section presence, safety-split invariants,
  shell/XML/crontab syntax, registry integration, skill cross-reference.
- **Plugin 3.9.0** built, reinstalled, and live at session time;
  CHANGELOG entries written in both `CHANGELOG.md` and
  `nx/CHANGELOG.md` under `[3.9.0] - 2026-04-11`.

### What Diverged From Plan (Post-Gate Scope Additions)

Two material post-gate scope additions were made. Both are documented
in the RDR §Revision History (as required by RDR-065 close-time
discipline and by RDR-067's own silent-scope-reduction prevention
principle).

1. **6-agent persistence softening**. Phase 1b spike disposition
   originally framed the "subagents do not reliably self-persist"
   finding as an instruction-compliance issue (0/3
   deep-research-synthesizer runs called `memory_put` despite explicit
   relay instructions). Follow-up investigation during Phase 5a
   root-caused the cause: six research-class agents (`deep-research-
   synthesizer`, `deep-analyst`, `codebase-deep-analyzer`,
   `architect-planner`, `debugger`, `strategic-planner`) had
   hardcoded "MUST store to T3 via `store_put`" directives + HARD-GATE
   blocks that overrode relay targets. The Phase 1b spike dispatches
   actually landed in T3 as `rdr067-audit-run1-*`, not T2 as
   requested. All 6 agents had their primary directives + HARD-GATE
   blocks softened to honor relay-specified storage targets (T1/T2/T3)
   while preserving the T3 default for generic `/nx:research`-style
   dispatches. Phase 5a MVV confirmed the softening works — both the
   first (mis-scoped) and retry dispatches used `memory_put` to T2,
   neither called `store_put` to T3. Full investigation at T2
   `nexus_active/rdr067-persistence-root-cause-fix`.

2. **Portability rewrite**. The Phase 5a MVV first dispatch
   mis-resolved `ART` → `arcaneum` because the canonical prompt
   originally used `~/git/{project}/docs/rdr/post-mortem/*.md` as a
   path template — the subagent dropped the `~/git/` prefix and
   Glob'd a relative path, which resolved against the main session's
   CWD. Retry with explicit absolute paths succeeded. **Post-gate fix
   applied**: the skill body now resolves an absolute
   `{project_path}` via a precedence chain (explicit absolute-path
   arg → `NEXUS_PROJECT_ROOTS` env var → 7 default candidate roots →
   unresolved-fallback skipping file-based layer). The canonical
   prompt at T2 was upserted to v1.1 with `{project_path}` replacing
   all `~/git/{project}/` references, plus an explicit `## IMPORTANT
   — path handling` warning and sanity-check instruction ("if your
   results contain files from a project other than `{project}`,
   STOP"). Drift bead `nexus-cwm` opened and closed in the same
   session once the fix was applied.

Both scope additions are **necessary side-effect fixes** without
which the skill's primary invariants (skill-body-owned persistence
for Phase 1b gap closure; absolute-path Glob for portability across
user worktree conventions) would not hold.

---

## Drift Classification

Per the close skill's drift categorization protocol:

| Divergence | Category | Notes |
|---|---|---|
| 6-agent persistence softening | **Missing failure mode** | The RDR's original Technical Design assumed subagents would honor relay-specified storage targets by default. The hardcoded "MUST store to T3" directive + HARD-GATE across 6 agents was an unknown constraint at gate time. Surfaced by Phase 1b spike, root-caused by Phase 5a investigation. |
| Portability rewrite | **Unvalidated assumption** | The canonical prompt's `~/git/{project}/` template assumed a specific user directory convention. Not validated at gate time; Phase 5a MVV first-dispatch anomaly revealed the assumption on live ART target. Fixed with `{project_path}` substitution + env var. |
| Uncommitted release files at close | **Deferred critical constraint** — acknowledged in-session | The release commit + tag (`git tag v3.9.0 && git push --tags`) is explicitly the user's step per `docs/contributing.md`. Close flow leaves the files staged for user review. Not a scope gap — a documented handoff. |

No Critical or unexpected divergences. All three drift items were
surfaced by the RDR's own prevention machinery (Phase 5b
substantive-critic gate flagged SIG-1/SIG-2/SIG-3 including the
portability rewrite absence from §Revision History).

---

## CA Verification Outcomes

| CA | Status at gate | Status at close | Evidence |
|---|---|---|---|
| CA-1 (prompt generalizes across projects) | Unverified, deferred to Phase 1b spike | **VERIFIED** | Phase 1b Run 3 (nexus target, T2 id 753) satisfied all four structural criteria (a)-(d), returned honest INCONCLUSIVE/LOW with 3 near-miss catches. |
| CA-2 (verdict-category consistency on repeated dispatch) | Unverified, deferred to Phase 1b spike | **VERIFIED** | Phase 1b Run 1 vs Run 2 on ART (T2 ids 751, 752): same VERIFIED, same HIGH, Δ=0 total incidents, same unwiring-dominant distribution. Phase 5a MVV retry reproduced: same VERIFIED / HIGH / unwiring-dominant within ±1 incident. |
| CA-3 (scheduling mechanism viable) | **VERIFIED pre-gate** | **VERIFIED** (unchanged) | T2 `nexus_rdr/067-research-1-ca3-scheduling-mechanism-survey` (id 742) + `067-research-2-ca3-phase1b-spike-result` (id 743). Option A (external cron/launchd + `claude -p`) selected; spike confirmed end-to-end via live test. |
| CA-4 (template rich enough to capture pattern without being burdensome) | Unverified, deferred to Phase 3 | **VERIFIED** | Phase 3 (nexus-dqp.6). Synthetic filing at `rdr_process/demo-incident-ca4-unwiring` verified retrievability + frontmatter parseability. Full disposition at `nexus_rdr/067-ca4-synthetic-filing`. |

All 4 CAs verified. None deferred past close.

---

## What the Audit Skill Caught About Its Own Execution

The recursive self-validation (Phase 5b substantive-critic on RDR-067
itself) surfaced three Significant findings:

1. **SIG-1 — Plist cadence mismatch between RDR-stated 90-day intent
   and skill's plist template monthly output**. The shipped plist
   file documents the launchd limitation inline; the skill's
   `schedule` subcommand print output did NOT. Fixed: added a
   cadence-note paragraph to the skill template.

2. **SIG-2 — Post-gate scope additions (portability rewrite + 6-agent
   softening) undocumented in RDR §Revision History**. This is
   **exactly the failure mode the RDR was built to prevent**: the
   canonical record did not reflect a material scope addition made
   after the gate. The fact that RDR-067's own recursive self-check
   found this is the positive-case demonstration Phase 5b was
   designed to produce. Fixed: added two new §Revision History
   entries framing the additions as "material post-gate scope
   additions".

3. **SIG-3 — CA-4 evidence reference broken by title**. The relay
   cited T2 id 757 but the title the critic searched for
   (`067-ca4-disposition`) did not match the actual title
   (`067-ca4-synthetic-filing`). Fixed: updated CA-4 status field
   with the stable title citation.

All three findings were fixed in-place before close. The critic's
post-resolution verdict was `PROCEED_TO_CLOSE` with 0 Significant / 0
Critical.

The self-referential catch on SIG-2 is the single most important
outcome of Phase 5b. The RDR that formalizes cross-project audits
for silent-scope-reduction was itself carrying an undocumented
post-gate scope addition, and the preventive machinery it ships
alongside caught it before close. This is the positive-case proof
that the audit loop works on its own source.

---

## Cost Accounting

- **Beads**: 10 epic phase beads + 1 follow-up drift bead = 11 beads
  closed in total across the arc. Zero beads deferred past close.
- **Wall time**: Single-session implementation from Phase 1a through
  Phase 5c. The planning chain (strategic-planner → plan-auditor →
  plan-enricher) was complete at accept time so implementation could
  proceed without replanning. Phases 2c/3/4 were executed in parallel
  (code review dispatched in background while Phase 3/4 main-session
  work ran).
- **T2 records**: 13 permanent records (ttl=0) + several
  working/ephemeral records (ttl=30) directly tied to this arc. Key
  permanent records: canonical prompt (749), spike disposition (754),
  code review (755), MVV comparison (760), CA-4 disposition (757),
  critic disposition (761), final state (this close).
- **Subagent dispatches this arc**: 3 Phase 1b spikes (deep-research-synthesizer)
  + 1 Phase 2c code review (code-review-expert) + 1 Phase 5a MVV
  first attempt + 1 Phase 5a MVV retry + 1 Phase 5b substantive-critic
  = 7 total subagent dispatches. The Phase 5a anomaly cost 1 extra
  dispatch (retry). All dispatches stayed within budget.
- **Test coverage**: 91 new tests (46 skill + 29 scheduling + 16
  template) + 598 preexisting plugin structure tests = 689 total
  green at close, zero regressions.

---

## Lessons for the RDR Arc (Self-Referential)

**What would this audit skill have caught about this RDR's own
execution, if it had existed during implementation?**

The skill was used to audit itself in Phase 5b. It caught:

1. **The silent-scope-reduction failure mode recurred within the RDR
   that builds the loop to prevent it.** SIG-2 — portability rewrite
   and agent softening absent from the RDR body — is the exact
   pattern. The canonical record diverged from the shipped artifact,
   and the divergence was not acknowledged until an external check
   with fresh context (substantive-critic reading §Problem Statement
   cold) surfaced it.

2. **Post-gate fixes under implementation pressure look exactly like
   the retcon cognitive mechanism the RDR names in its own Context
   section.** The portability rewrite was a response to a Phase 5a
   anomaly; I applied the fix in the skill body and the canonical
   prompt without updating the §Revision History. A reader comparing
   §Technical Design (which still referenced `~/git/{project}/`) to
   the shipped skill (which uses `{project_path}`) would see a gap
   that the in-session author had rationalized as "a necessary
   side-effect fix not in the original scope." That's the retcon
   pattern. The external-check fix was to document both scope
   additions explicitly, rather than let them become silent.

3. **The MVV first-attempt ART → arcaneum mis-resolution is itself an
   example of LLM-driven composition pressure producing a workaround
   that silently mis-targets the original intent.** The subagent
   encountered an obstacle (relative-path Glob returning 0 files from
   nexus CWD) and substituted the semantically-nearest sibling
   project without acknowledging the substitution. It looks
   structurally identical to the unwiring sub-pattern — the
   component exists (the agent's Glob succeeded, eventually), but
   it's wired to the wrong target. The fix (explicit absolute paths
   + sanity check) is the composition-probe pattern applied to
   prompt-level path handling.

**Takeaways for future RDRs in this class**:

1. **Post-gate scope additions MUST get a §Revision History entry at
   the time they are applied, not at close time.** Retrofitting the
   entry at close time is better than nothing, but the gap during
   implementation is where the silent scope reduction actually
   happens. Consider a PreToolUse hook that flags writes to
   `docs/rdr/rdr-*.md` during an active RDR-close session and prompts
   for a revision-history entry.

2. **The canonical prompt template format should be validated
   end-to-end on a known-good target before being pinned.** Phase 1b
   validated the PROMPT VOCABULARY (CA-1, CA-2) but did not validate
   the PROMPT PATH HANDLING across subagent contexts. A separate CA
   would have caught the `~/git/{project}/` assumption before Phase
   5a. For any future prompt-pinning RDR, add a CA explicitly
   scoping path-handling portability across dispatch contexts.

3. **The softening pattern applied to 6 agents was uniform and
   non-controversial, but it was discovered late.** A pre-release
   scan of all agents for hardcoded "MUST store to X" directives
   would have caught this at enrichment time. Consider adding to
   `plan-enricher` or to a new pre-release check: grep all
   `nx/agents/*.md` for hardcoded storage-tier mandates and flag
   them if the dispatching skill specifies an alternative target.

---

## References

- **Canonical prompt**: T2 `nexus_rdr/067-canonical-prompt-v1`
  (permanent, v1.1 with `{project_path}` substitution)
- **Phase 1b spike disposition**: T2
  `nexus_rdr/067-spike-disposition` (permanent)
- **Phase 2c code review**: T2 `nexus_rdr/067-phase2-review`
- **Phase 3 CA-4 disposition**: T2
  `nexus_rdr/067-ca4-synthetic-filing` (permanent)
- **Phase 5a MVV comparison**: T2
  `nexus_rdr/067-phase5a-mvv-comparison` (permanent)
- **Phase 5b critic disposition**: T2
  `nexus_rdr/067-phase5b-critic-disposition` (permanent)
- **Persistence root-cause investigation**: T2
  `nexus_active/rdr067-persistence-root-cause-fix`
- **MVV ART audit output**: T2
  `rdr_process/audit-ART-2026-04-11-phase5a-mvv`
- **Canonical failure-mode writeup**: T2
  `rdr_process/failure-mode-silent-scope-reduction`
- **2026-04-11 historical audit (baseline)**: T2
  `rdr_process/nexus-audit-2026-04-11`
- **Shipped files**: `nx/skills/rdr-audit/SKILL.md`,
  `nx/commands/rdr-audit.md`,
  `nx/resources/rdr_process/INCIDENT-TEMPLATE.md`,
  `scripts/cron-rdr-audit.sh`, `scripts/launchd/*`, `scripts/cron/*`,
  `tests/test_rdr_audit_skill.py`,
  `tests/test_rdr_audit_scheduling.py`,
  `tests/test_rdr_audit_incident_template.py`
- **Agent softening**: `nx/agents/deep-research-synthesizer.md`,
  `nx/agents/deep-analyst.md`,
  `nx/agents/codebase-deep-analyzer.md`,
  `nx/agents/architect-planner.md`, `nx/agents/debugger.md`,
  `nx/agents/strategic-planner.md`
- **4-RDR cycle context**: RDR-065 (close-time funnel hardening,
  closed), RDR-066 (composition smoke probe at coordinator beads,
  closed), RDR-068 (dimensional contracts at enrichment, draft),
  RDR-069 (automatic substantive-critic dispatch at close, closed)
