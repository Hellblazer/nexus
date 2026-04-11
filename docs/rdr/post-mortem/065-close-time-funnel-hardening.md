# Post-Mortem: RDR-065 Close-Time Funnel Hardening Against Silent Scope Reduction

## RDR Summary

RDR-065 added four close-time gates to defend the RDR lifecycle against
silent scope reduction under LLM-driven composition pressure: a Problem
Statement replay that forces per-gap `file:line` commitment; a
divergence-language advisory hook on post-mortem writes; a `bd create`
commitment-metadata enforcer that fires during an active RDR close; and
a template scaffold change so new RDRs enumerate gaps out of the box.
The recursive self-validation (Steps 6a/6b/6c) used RDR-065 itself as
the first subject of the gate.

## Implementation Status

Implemented. All 10 epic beads closed. 3.8.2 shipped; `nx --version`
reports 3.8.2 post-reinstall. The Problem Statement replay, both
hooks, the SKILL.md Step 1.5 text, and the template scaffold are live.
The recursive self-validation passed: 6a synthetic injection caught
all three failure modes, 6b found one LOOSE verdict (Q1 pointer shape)
which was fixed and re-reviewed STRICT, and 6c is this post-mortem
itself — written by the close skill with the hooks and preamble live.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- Two-pass Problem Statement Replay preamble with Pass 1 enumeration and
  Pass 2 pointer validation, `sys.exit(0)` hard blocks, T1 scratch
  `rdr-close-active` marker.
- ID-based grandfathering (`rdr_id_int < 65`) — not date-based, per CA-4
  and the RC-1 implementer note.
- Divergence-language PostToolUse hook with the locked Rev 4 8-pattern
  regex bank, markdown header/table-row pre-filter, advisory-only
  `permissionDecision: allow`.
- `bd create` enforcement branch extending the existing PreToolUse Bash
  hook, with audit log at `/tmp/nexus-rdr065-bd-create-audit.log` (RC-3
  fix — NOT `.beads/`).
- RDR template `### Enumerated gaps to close` subsection with `#### Gap N:`
  placeholders and the documented `^#### Gap \d+:` regex.
- SKILL.md `### Step 1.5: Problem Statement Replay` with the verbatim
  structural-not-semantic framing prompt.
- Plugin release 3.8.2 bundling all working-tree changes, smoke-tested
  via scaffold substitution.

### What Diverged from the Plan

- **Q1 pointer shape missing** (nexus-lets.9 6b finding, fixed in-bead):
  The RDR planned pointer validation as "file existence check." The
  initial implementation did `partition(':')[0]` and checked `exists()`,
  which silently accepted bare filenames like `Gap1=README.md`. An agent
  could have gamed the gate by naming any existing file with no line
  number. The independent review found this, nexus-lets.3 was reopened,
  `(not sep)` and `re.match(r'^\d+', line_part)` guards were added, and
  nexus-lets.8 and nexus-lets.9 were re-run on the fixed preamble.

- **Gap heading regex loosened from literal spec**: The bead spec called
  for `r'^#### Gap (\d+):'`. The implementation shipped
  `r'^#### Gap (\d+)([^\n:]*):\s*(.*)$'`. The looser form permits
  parenthetical qualifiers between the number and the colon, which was
  necessary to capture RDR-065's own `#### Gap 4 (prerequisite for
  Gap 1):` heading. The strict regex would have enumerated 3 gaps on
  RDR-065 instead of 4 and silently missed the prerequisite gap. The
  deviation is documented in the commit message and preamble comment.

- **T1 scratch tag scheme**: The bead spec for nexus-lets.3 said
  `--tags rdr-close-active`. The implementation uses
  `--tags rdr-close-active,rdr-NNN` so the Gap 3 hook can read the
  active RDR id via `nx scratch list | grep` instead of the unreliable
  `nx scratch search` (which does semantic ranking, not exact-tag
  match). Discovered during nexus-lets.7 testing.

### Existing Infrastructure Reused Instead of New Code

- **`pre_close_verification_hook.sh`** — the Gap 3 enforcement branch
  extends this existing PreToolUse hook rather than creating a new hook
  per the bead guidance. Kept behavioral parity for the existing
  `bd close|done` flow.
- **`nx scratch` T1 store** — used as the active-close marker store
  instead of a new sidecar file, leveraging the session-scoped
  `chromadb.EphemeralClient` already set up by the plugin session
  infrastructure.
- **`memory_put` / `memory_get` T2 API** — used for validation and
  review T2 artifacts (6a/6b-failed/6b-passed entries), not a custom
  log format.

### What Was Added Beyond the Plan

- **Audit log stdout-discipline** for the divergence hook: `nx scratch
  put` was calling `2>/dev/null` only; the `Stored: <id>` stdout was
  leaking into the hook's JSON output and breaking JSON parsing. Fixed
  to `>/dev/null 2>&1` during nexus-lets.6 testing. Not anticipated by
  the bead spec but necessary for hook correctness.
- **Shape-guard error messages** in the Pass 2 validator differentiate
  between "missing colon" and "no digit after colon" — more informative
  than a single "invalid shape" catch-all.

### What Was Planned but Not Implemented

Nothing — all 10 beads closed, all acceptance criteria met.

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Unvalidated assumption** | 0 | | |
| **Framework API detail** | 1 | `nx scratch search` is semantic, not exact-tag | Yes — spike (but only discovered mid-implementation) |
| **Missing failure mode** | 1 | Q1 pointer shape gap accepting bare filenames | Yes — source search on the validator pseudocode |
| **Missing Day 2 operation** | 0 | | |
| **Deferred critical constraint** | 0 | | |
| **Over-specified code** | 0 | | |
| **Under-specified architecture** | 1 | Gap heading regex too strict for natural authoring (Gap 4 parenthetical) | Yes — corpus audit |
| **Scope underestimation** | 0 | | |
| **Internal contradiction** | 0 | | |
| **Missing cross-cutting concern** | 0 | | |

### Pattern References

No cross-RDR pattern (single instance per category). The "Missing
failure mode" example is the clearest generalizable drift: validator
pseudocode that reads naturally but silently accepts degenerate inputs.

### Drift Category Notes

- **Framework API detail — `nx scratch search` behavior**: the bead
  pseudocode assumed `nx scratch search "rdr-close-active"` would
  return entries with that tag. Actual behavior is semantic ranking
  over content and tags — unreliable for exact-tag lookup. Caught
  during nexus-lets.7 manual testing when advisory fired on no-
  active-close scenarios.

- **Missing failure mode — Q1 pointer shape**: the bead pseudocode
  for Pass 2 validation read as `file_part = pointers[gap_key]
  .partition(':')[0]` followed by `(repo/file_part).exists()`. Every
  reader (including the implementer and the initial plan audit)
  missed that `partition(':')` is non-failing for strings without a
  colon — it returns `(whole_string, '', '')`. An agent supplying
  `Gap1=README.md` would pass. Caught by independent substantive-critic
  review with attack matrix.

- **Under-specified architecture — gap regex strictness**: the bead
  spec's literal `^#### Gap (\d+):` didn't contemplate parenthetical
  qualifiers. The file being validated (RDR-065 itself) used one. Any
  corpus audit of existing `#### Gap` headings in the RDR tree would
  have surfaced the convention before the spec froze.

---

## RDR Quality Assessment

### What the RDR Got Right

- **Layered gate design**: the four Gaps decompose cleanly along the
  lifecycle — author-time scaffold (Gap 4), close-time replay (Gap 1),
  write-time advisory (Gap 2), create-time enforcement (Gap 3). No
  overlap, no redundant enforcement.
- **Recursive self-validation as MVV**: using RDR-065 as its own first
  subject caught a real failure mode (Q1) that a paper review would not
  have. The triple of 6a/6b/6c gave three independent checks:
  synthetic injection, code review, end-to-end run.
- **ID-based grandfathering** (RC-1 correction): picking the numeric
  cutoff over a date-based one eliminated an entire class of edge cases.
  CA-4's corpus audit (0/65 pre-065 RDRs use `#### Gap N:` format)
  locked the cutoff precisely where it needed to be.
- **Hard constraints as named entities**: HA-1 through HA-5 and CA-1
  through CA-6 acted as load-bearing anchors during gate rounds and
  during implementation — every diff could be checked against a named
  constraint.

### What the RDR Missed

- **Validator pseudocode review**: Pass 2's pointer validator was read
  as English by the plan-auditor, the gate, and the initial implementer.
  No one ran the attack matrix against it. The substantive-critic agent
  did — and found Q1. Future RDRs with validation logic should have a
  "describe inputs that should fail, confirm each fails" checklist in
  the audit.
- **`nx scratch search` vs `list` semantics**: the bead spec assumed
  search was exact-tag. No spike on that behavior before freezing.
- **Gap regex corpus audit**: the literal regex didn't match the one
  example the RDR itself provided. A `grep '^#### Gap' docs/rdr/*.md`
  during planning would have caught the parenthetical convention.

### What the RDR Over-specified

- **Code samples rewritten**: some of the audit log JSON builder in
  nexus-lets.7 was rewritten using `python3 -c 'import json,sys; ...'`
  for escaping; the bead's inline sed-based approach was fragile.
  Minor.
- **Deferred feature code unused**: none.
- **Config/schema never implemented**: none.
- **Performance targets unvalidated**: none.
- **Alternative analysis disproportionate**: none.

---

## Key Takeaways for RDR Process Improvement

1. **Run adversarial inputs against validator pseudocode during plan
   audit**: when a bead spec contains `if not X.exists()` or similar
   shape checks, the auditor should enumerate at least three degenerate
   inputs (empty string, missing separator, wrong shape) and walk each
   through the pseudocode. The 6b review caught Q1 because it built an
   attack matrix; the earlier plan audit did not.

2. **Spike externalised API semantics before freezing the spec**: the
   `nx scratch search` assumption was wrong and wasted a cycle during
   nexus-lets.7 testing. Any bead that makes a claim about a CLI,
   library, or service behavior should either cite a source-search
   verification or contain a spike line ("verified against commit X,
   behavior: Y").

3. **Audit the corpus your regex targets before freezing the regex**:
   RDR-065's Gap 4 heading used a parenthetical qualifier that the
   literal bead regex missed. A one-line `grep '^#### Gap' docs/rdr/*.md`
   during CA-4's corpus audit would have surfaced the convention.

4. **Treat recursive self-validation as a mandatory phase, not a
   flourish**: 6a + 6b + 6c caught failures that no amount of paper
   review would have surfaced. Future "process hardening" RDRs should
   bake the three-step self-validation into the implementation plan
   from the start, not as an optional extra.

5. **Divergence hooks SHOULD fire on their own post-mortems**: during
   the 6c writeup, the Gap 2 hook is expected to flag "deferred",
   "workaround", "limitation", "divergence", and "follow-up RDR" in
   this text. Those are true positives — this post-mortem documents
   real drift. The advisory-only design (never hard-block) is correct:
   the closer reads the advisory, confirms each hit is acknowledged,
   and proceeds. A hard-block here would prevent honest post-mortems.
