# RDR-067 Canonical Audit Prompt v1 (patch v1.2 — code-verification gate for PARTIAL/SCOPE-REDUCED)

**Purpose**: Pinned prompt text for the `rdr-audit-checklist` skill. The skill dispatches `deep-research-synthesizer` with the prompt below, substituting `{project}`, `{project_path}`, and `{transcript_excerpts}` before dispatch. This file ships with the plugin so the audit runs on an install whose T2 holds no `nexus_rdr` entries; a T2 entry `nexus_rdr/067-canonical-prompt-v1`, when present, overrides it (same structure: everything below the `PROMPT TEXT` line is the prompt).

**Template parameters**:
- `{project}` — target project name, bare (e.g., `ART`, `nexus`). Used for display and for RDR citation text.
- `{project_path}` — **absolute filesystem path** to the target project's worktree. The skill resolves this from `NEXUS_PROJECT_ROOTS` env var + candidate-root probing; there is no universal convention for where users keep project worktrees, so the prompt never assumes one. Must be an absolute path at substitution time — no `~`, no `$HOME`, no relative components. If the skill cannot resolve an absolute path, `{project_path}` is the literal string `(unresolved)` and the subagent skips the file-based evidence layer.
- `{transcript_excerpts}` — the main-session transcript pre-step block (empty on the fast path).

**Design principle**: The problem is universal; the particulars are project-private. This prompt embeds the universal failure-mode vocabulary inline so it stands alone without referencing any project's internal paths or narratives. Each dispatch produces project-specific particulars in its output; those land in project-private local T2 records, not in the prompt or the skill.

**v1.2**: mandatory code-verification gate for PARTIAL and SCOPE-REDUCED classifications. Two independent audit runs produced false-positive SCOPE-REDUCED/PARTIAL verdicts because unchecked success-criteria checkboxes were treated as evidence of non-implementation when the code had actually shipped. The gate requires a spot-check against the codebase (Grep for function/class names from the success criteria) before any non-CLEAN verdict is emitted.

**Output-format contract**: The `## Output format (REQUIRED)` section below enforces the four CA-1 structural elements:
- (a) confirmed incidents OR explicit `INCONCLUSIVE` verdict
- (b) sampling caveats
- (c) frequency estimate with HIGH / MEDIUM / LOW confidence
- (d) drift-category per incident

Structural absence of any of (a)-(d) fails CA-1 for that audit run.

---

## PROMPT TEXT (everything below this line is the prompt the skill embeds)

---

# RDR Silent-Scope-Reduction Audit — {project}

You are conducting a post-hoc audit of `{project}`'s RDR lifecycle to measure the base rate of the silent-scope-reduction composition-failure mode. This audit is the feedback loop for a multi-RDR intervention program; your verdict drives decisions about whether the preventive interventions are working on real work.

## Question

Does the silent-scope-reduction composition-failure mode occur in real work on `{project}`, at a frequency that justifies (or continues to justify) process intervention?

**Silent-scope-reduction definition**: an RDR is closed reporting the original scope delivered, but the actual artifact ships with a material subset of the planned surface area missing. Distinguishes from a truthful mid-course re-scope (where the plan is explicitly updated and the new scope is stated clearly). The hallmark is a gap between RDR §Problem Statement and the shipped artifact that is neither acknowledged in the close nor reflected in the §Lessons.

**Cognitive mechanism (the retcon)**: under single-session pressure, an agent that encounters an obstacle to the planned scope reclassifies the current failure as a pre-existing plan error — preserving prior work's validity at the cost of silently dropping scope. The workaround isn't framed as "I can't afford to reopen"; it's framed as "the plan was wrong and I'm fixing it." This reframing is cognitive, not economic. The most reliable detection is an external check with fresh context (e.g., a substantive-critic reading §Problem Statement cold and comparing to what shipped) — the original agent's context is polluted by the rationalization.

**Sub-patterns to watch for**:
- **unwiring** — components exist but aren't wired into the composition/production path (dead islands)
- **dim-mismatch** — a structural/type/shape incompatibility is papered over with a workaround that silently disables the original intent
- **deferred-integration** — "building blocks only" declared done; integration step marked follow-on and then forgotten
- **other** — structurally dead feature, test-tests-nothing substitution, resonance/verification guard quietly removed, etc. Name the sub-pattern explicitly.

## Scope to audit

Consult the sources below in order. Budget guidance is a ceiling, not a floor — stop early when evidence is obvious.

**IMPORTANT — path handling**: The skill resolved `{project_path}` to an absolute filesystem path before this prompt was substituted. Every file-based lookup below uses that absolute path. Do NOT drop the `{project_path}/` prefix when you call `Glob` or `Read` — if you Glob a relative path like `docs/rdr/post-mortem/*.md`, your Glob will resolve against the dispatcher's CWD, not the target project, and you may silently audit the wrong files. If `{project_path}` is the literal string `(unresolved)`, skip the file-based layer entirely and rely on T2 + PRE-GATHERED excerpts only.

1. **`{project}` post-mortems** (primary evidence source)
   - Preferred path: `{project_path}/docs/rdr/post-mortem/*.md`
   - Fallbacks (try in order if the preferred path yields nothing): `{project_path}/docs/post-mortem/*.md`, `{project_path}/post-mortems/*.md`, or Glob for `post-mortem` under `{project_path}/docs/`
   - Budget: Glob first (absolute path, no relative fallback), then ≤20 Reads prioritizing recent (by mtime, last 90 days)
   - Purpose: narrative evidence of confirmed incidents with file:line citations
   - If no post-mortem convention exists at `{project_path}`, look for equivalents: `{project_path}/retro/`, `{project_path}/retros/`, `{project_path}/drift-cleanup/`, `{project_path}/docs/incidents/`, or RDR §Lessons sections. Name the substitute in §Sources consulted.
   - **Sanity check after Glob**: if your results contain files from a project other than `{project}` (wrong topic area, wrong language, wrong repository naming), STOP and report a path-resolution failure. Do not silently proceed with wrong-project evidence.

2. **T2 `rdr_process` collection**
   - Tools: `mcp__plugin_conexus_nexus__memory_get(project="rdr_process", title="")` lists the titles, then `mcp__plugin_conexus_nexus__memory_get(project="rdr_process", title=...)` for matching entries
   - Also: `mcp__plugin_conexus_nexus__memory_search(project="rdr_process", query="{project}")`
   - Budget: 1 list + ≤5 gets
   - Purpose: prior audit findings for `{project}`, cross-project incident filings, accumulated pattern evidence

3. **PRE-GATHERED TRANSCRIPT EXCERPTS** (optional — pre-filled by the main session before your dispatch)

```
{transcript_excerpts}
```

If the block above is empty, transcript mining was skipped or found no high-signal transcripts. Do NOT attempt to glob `~/.claude/projects/*` yourself — that tree is not efficiently scannable from a subagent context, and the main session is authoritative on transcript selection. Proceed with post-mortems as the primary evidence source.

4. **`{project}` RDR directory** (only if post-mortem evidence is inconclusive)
   - Path: `{project_path}/docs/rdr/*.md`
   - Budget: ≤5 reads, only when a post-mortem hints at a gap but lacks specifics
   - Purpose: compare §Problem Statement to what shipped when a post-mortem is absent or thin

## Indicator patterns (use as search hints, not auto-classifiers)

These are signals to look harder — not grounds for classification on their own. A regex-only audit would drown in false positives because "deferred" appears in every healthy rescoping discussion. Use semantic judgment.

- `reopen` / `reopened` / `had to revert`
- `dim(ension)? mismatch` / `type mismatch` / `shape mismatch`
- `integration bead broken` / `follow-up` / `follow-on`
- `building blocks` near `done`
- `deferred` near `to next RDR` / `to follow-up`
- drift classifications already present in post-mortem frontmatter
- `workaround` near `not the right fix` / `latent` / `design mistake`
- `critic caught` / `substantive critic` / `post-close finding`

Key semantic distinction: **healthy re-scoping** (plan updated, scope stated clearly) vs. **silent scope reduction** (close claims original scope, artifact is missing it). Only the latter counts.

## Code-verification gate (MANDATORY for non-CLEAN verdicts)

**Before classifying any RDR as PARTIAL or SCOPE-REDUCED, you MUST spot-check the codebase.** This gate exists because unchecked success-criteria checkboxes are NOT reliable evidence of non-implementation. Code frequently ships with documentation lagging — the checkbox may be unchecked while the feature is fully implemented.

**Procedure** (budget: ≤3 Grep calls per RDR under review):

1. Extract the key deliverables from the RDR's success criteria or gap descriptions — function names, module names, config keys, CLI commands, or other greppable identifiers.
2. Grep `{project_path}/src/` (or the project's source root) for those identifiers.
3. If the Grep finds the implementation, the unchecked checkbox is a **documentation gap**, not a scope gap. Classify as CLEAN with a note: "success criteria unchecked but code ships the feature at `<file:line>`."
4. If the Grep finds nothing, the gap is real. Proceed with PARTIAL or SCOPE-REDUCED classification.

**Rationale**: Two independent audit runs (2026-04-11) classified RDR-056 as SCOPE-REDUCED based on 3/4 unchecked success criteria. Codebase verification showed all 4 were implemented (`probe_hit_rate` in t3.py, `search_clusterer.py`, `distance_threshold` in config.py, `hnsw:search_ef` tuning in doctor.py). The false positive was detected only by manual code inspection after the audit. This gate prevents that class of error.

**Budget impact**: adds ≤3 Grep calls per non-CLEAN candidate. For a typical audit sampling 5-8 RDRs with 1-3 non-CLEAN candidates, this is 3-9 additional tool calls — well within the ≤25 total budget if the auditor stops early on CLEAN cases (which require no code verification).

## Output format (REQUIRED)

Emit the sections below in the exact order shown. Structural absence of any section fails the audit's validity criterion. Do not collapse sections, do not rename them.

### Sources consulted
- Enumerate every file and T2 entry actually read (not just attempted)
- Mark any attempted source that was empty, missing, or unreadable
- Include glob patterns tried and their result counts
- If `{project_path}` was `(unresolved)`, say so explicitly

### Sampling caveats
- Files NOT covered (out-of-window, skipped for budget, unreadable)
- Date range actually sampled vs. target window
- Transcript coverage (pre-gathered block used, skipped, or empty)
- Known biases in the evidence set (e.g., project convention of writing post-mortems only for "interesting" incidents, under-reporting of shipped-silently cases)
- Tool-access limitations encountered
- Path-resolution status (absolute path available, or file-based layer skipped)

### Confirmed incidents in window

For each confirmed incident, produce a block with these fields:
- **RDR ID + title + date**
- **What was planned**: one sentence, quoted or paraphrased from §Problem Statement
- **What shipped**: one sentence describing the actual artifact surface
- **The gap**: one sentence naming what was silently dropped
- **Drift category**: exactly one of `unwiring` | `dim-mismatch` | `deferred-integration` | `other`. For `other`, name the sub-pattern.
- **Caught by** (if caught): `substantive-critic` | `composition-probe` | `dim-contracts` | `user` | `post-hoc` | `not-caught`
- **Source citation**: `file:line` or T2 id
- **Code-verification result** (required for PARTIAL and SCOPE-REDUCED only): which identifiers were grepped, what was found or not found, and the file:line of the implementation if it exists. Omit for CLEAN verdicts.
- **Cognitive mechanism** (optional, only if a pre-gathered transcript excerpt is directly relevant): a short quoted turn showing the decision point

If ZERO incidents are found after a reasonable evidence pass, emit in place of the incident list:

```
VERDICT: INCONCLUSIVE

<one paragraph justifying INCONCLUSIVE rather than FALSIFIED, tied explicitly to the sampling caveats>
```

### Frequency estimate
- Rate: `<numerator> incidents / <denominator> <unit>` (e.g., `4 incidents / 90 days` or `1.3 per month`)
- **Confidence**: `HIGH` | `MEDIUM` | `LOW` (pick one, do not hedge)
- Confidence rationale: 1-2 sentences tying the level to sampling caveats — e.g., "HIGH: post-mortems are exhaustive and honest for the window" vs. "LOW: only 3 post-mortems in window, small denominator".

### Drift-category distribution
- Count per category: `unwiring: N`, `dim-mismatch: N`, `deferred-integration: N`, `other: N`
- One-sentence observation on which sub-pattern dominates this project's evidence (or "insufficient incidents to observe dominance")

### Near-misses (optional)
- Research-phase catches, pre-close critic catches, or runtime probe catches that prevented an incident from landing
- Positive evidence for which interventions are working

### Recommendation
One of (pick exactly one):
- `VERIFIED` — pattern occurs at a rate justifying intervention; describe the rate
- `PARTIALLY VERIFIED` — evidence suggestive but sampling is thin
- `INCONCLUSIVE` — cannot distinguish from noise at this evidence level
- `FALSIFIED` — no evidence of the pattern in the window AND sampling is thorough enough to support the negative

Brief rationale: 2-3 sentences tied to the evidence.

## Budget

Target ≤25 tool calls total. Glob post-mortem filenames before Reading individual files; prefer full-list-then-filter over speculative Reads. If the evidence is obvious after 10 calls, stop and emit the verdict — thoroughness beyond that point does not improve signal quality. The code-verification gate adds ≤3 Grep calls per non-CLEAN candidate — budget accordingly.

## Method notes
- LLM classification over regex — use judgment, not pattern-matching
- Cite `file:line` or T2 id for every incident claim
- Label evidence type: cognitive mechanism (transcript quote) vs. post-hoc narrative (post-mortem prose) — both valid, should be distinguished
- Distinguish **healthy rescoping** (plan updated, new scope stated) from **silent scope reduction** (close claims original, artifact missing it). Only the latter counts.
- If the project has no post-mortem convention, name the substitute source in §Sources consulted and proceed — do not fail the audit for missing convention.
- Every file-access call uses an absolute path via `{project_path}`; if you find yourself about to Glob a relative path, STOP and re-read the §IMPORTANT — path handling warning.
- **Unchecked success criteria are NOT evidence of non-implementation.** Code ships with documentation lagging. Before any non-CLEAN verdict, run the code-verification gate (§Code-verification gate). A false positive SCOPE-REDUCED verdict is worse than a missed PARTIAL — it erodes trust in the audit loop itself.
