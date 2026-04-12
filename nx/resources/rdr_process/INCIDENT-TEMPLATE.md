<!--
rdr_process incident filing template (RDR-067 Phase 3)

Use this template to file a cross-project silent-scope-reduction incident into
the `rdr_process` T2 collection. Filings from different projects aggregate into
the `/nx:rdr-audit` subagent's evidence base and let future audits see
historical patterns beyond any single project's post-mortem corpus.

## How to file

1. Copy this template into a new T2 entry:
   `mcp__plugin_nx_nexus__memory_put(project="rdr_process", title="<project>-incident-<slug>", ttl=0, tags="rdr-audit,incident,<project>,<drift_class>", content=<filled-in template>)`

2. Title convention: `<project>-incident-<short-slug>` where slug is a stable
   handle for the incident (e.g. `art-incident-073-dialog-grounding-dropped`).
   Titles are permanent — pick a slug that reads well in a list.

3. Fill in every frontmatter field and every section below. Empty sections
   make the filing unusable for aggregation. If a field genuinely does not
   apply, write `N/A` with a one-line justification rather than deleting.

4. Keep the prose tight. Each section is 1-3 sentences unless the mechanism
   or lessons genuinely require more. Audit subagents read these cold, so
   every sentence should carry weight.

## What belongs vs what doesn't

**Belongs**: silent scope reductions where the close claimed the original
scope delivered but the artifact shipped with a material subset missing.

**Does NOT belong**:
- Healthy rescoping: plan updated, new scope stated clearly, §Lessons
  acknowledges the change. Note in the originating RDR, not here.
- Generic bugs caught in review/testing with no scope gap.
- Process friction that did not result in a delivery gap.

See `nx/skills/rdr-audit/SKILL.md` for the failure-mode definition and
sub-pattern taxonomy (unwiring / dim-mismatch / deferred-integration / other).
-->

---
project: <project-name>
rdr: <rdr-id>
incident_date: <YYYY-MM-DD>
drift_class: <unwiring | dim-mismatch | deferred-integration | other>
caught_by: <substantive-critic | composition-probe | dim-contracts | user | post-hoc>
outcome: <reopened | partial | shipped-silently>
---

# Incident: <RDR-ID> <short-title>

## What was meant to be delivered

<!--
One sentence, quoted or paraphrased from the RDR §Problem Statement. This is
the reference point for "silent scope reduction" — the commitment the close
elided or under-delivered.
-->

## What was actually delivered

<!--
One sentence describing the actual artifact surface at close time. Be factual
and specific: "N files landed implementing X; the dispatcher in Y.java was
updated to call Z; the integration bead for wiring into the production path
was not closed."
-->

## The gap

<!--
One sentence naming what was silently dropped. The gap is the delta between
§What was meant and §What was actually. It should be crisp enough that a
reader comparing the §Problem Statement to the close artifact would point to
the same thing.
-->

## Decision point (transcript citation if available)

<!--
If the decision to drop scope is visible in a session transcript, cite the
session ID and line number and paste a short quoted turn (≤5 lines). The
canonical shape is a retcon: the agent reclassifying a failure as a plan
error to preserve prior work's validity.

If no transcript evidence is available, write "post-hoc narrative only" and
continue. Not every incident has live transcript evidence — that is OK.
-->

## Mechanism

<!--
Which sub-pattern and why. Pick one:

- **unwiring** — components exist but aren't wired into the composition/production path (dead islands)
- **dim-mismatch** — a structural/type/shape incompatibility is papered over with a workaround that silently disables the original intent
- **deferred-integration** — "building blocks only" declared done; integration step marked follow-on and then forgotten
- **other** — structurally dead feature, test-tests-nothing substitution, resonance/verification guard quietly removed (name the sub-pattern explicitly)

Include 1-2 sentences of concrete mechanism: which component was not called,
which dim mismatch surfaced where, which bead was left open, etc.
-->

## What caught it

<!--
Pick one and be specific about the invocation context:

- `substantive-critic` — pre-close or post-close? Dispatched manually or via gate?
- `composition-probe` — which probe? At which bead boundary?
- `dim-contracts` — in enrichment? At runtime?
- `user` — called it out at close time, or during implementation review?
- `post-hoc` — only visible after-the-fact, in a subsequent audit or retrospective
- `not-caught` — shipped silently and has not been caught as of this filing

If the incident was caught and remediated, note both the catch mechanism and
the remediation outcome.
-->

## Cost (beads reopened, PRs reverted, wall time lost)

<!--
Quantify the damage. Examples:
- "2 beads reopened, ~430 LOC change in same-day reopen, ~4 hours wall time"
- "1 PR reverted, follow-up RDR filed, 2 days until fix PR landed"
- "0 beads reopened — incident was caught pre-close; estimated 1 hour rework"

Zero-cost incidents (caught before delivery) still belong here — they are
positive evidence that a prevention intervention worked.
-->

## Lessons (for the process, not the project)

<!--
What does this incident teach about the RDR process itself? Project-specific
technical lessons belong in the originating RDR's §Lessons section. This
section captures only the cross-project process-level takeaway — the kind of
thing that informs future prevention intervention design.

Examples:
- "Close-time problem-statement replay would have caught this before the
  close commit; RDR-065 Step 1.75 prevents recurrence"
- "Integration bead boundary was unclear in the plan; enrichment template
  should require explicit wiring checklists for composition RDRs"
- "The retcon reframing happened in a single session turn — external check
  with fresh context (substantive-critic) is the only reliable detector"

1-3 sentences. If the lesson is already captured in an existing RDR or
process doc, cite it here rather than restating.
-->
