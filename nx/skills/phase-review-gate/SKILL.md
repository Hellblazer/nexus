---
name: phase-review-gate
description: Use when a phase boundary is being closed — cross-walk RDR §Approach against closing beads to block silent scope reduction before it is discovered mid-implementation
effort: low
---

# Phase Review Gate Skill

Enforces the §Approach cross-walk at every phase-review boundary. Before a phase can be declared closed, every numbered item in the RDR's §Approach section must have an evidence pointer (closing bead ID or explicit deferral acknowledgement).

This is the phase-level analogue of the RDR-065 Problem Statement Replay gate: the RDR-065 gate verifies problem gaps are addressed at close time; this gate verifies approach items are addressed at each phase boundary.

**Root cause it prevents**: silent scope reduction discovered mid-implementation. RDR-112 Phase 1 (nexus-52lb, 2026-05-15) shipped T2-only work; §Approach item 2 (T3 daemon) was silently dropped. Discovered three closed phases later when a bead acceptance criterion (`mcp_infra.get_t3() -> T3Client`) could not compile. Cost: 2-3 days of replanning. The gate would have blocked the close in 15 minutes.

## When This Skill Activates

- User says "phase review gate", "cross-walk the approach", "close phase N"
- User invokes `/nx:phase-review-gate`
- A phase-review bead (title contains "P{N}.review" or "Phase {N} gate") is about to close
- Any time phases are being closed and the closer wants to verify no work was silently dropped

## Input

- **RDR ID** (required) — e.g., `112`
- **Phase number** (required) — `--phase 1`
- **Evidence** (Pass 2 only) — `--evidence 'Item1=nexus-abc1,Item2=nexus-xyz2,Item3=none'`

## Two-Pass Contract

### Pass 1 — Enumerate (no --evidence supplied)

The preamble script reads §Approach from the RDR file, parses all numbered items, and prints them as a table. It instructs the reviewer to provide an evidence pointer per item and re-invoke with `--evidence`.

Output format:
```
| # | Label | Evidence needed |
|---|-------|-----------------|
| Item1 | T2 service | (provide bead-id or `none`) |
| Item2 | T3 service | (provide bead-id or `none`) |
...
```

### Pass 2 — Validate (--evidence supplied)

The preamble validates that every enumerated item has a non-empty evidence pointer. Evidence values:
- **Bead ID** (`nexus-abc1`): the closing bead whose acceptance criteria cover this approach item.
- **`none`**: explicit deferral — the reviewer acknowledges this item is not in scope for this phase. Use this for items like "T1 stays put" (no work needed) or genuinely deferred sub-work.

**BLOCKED**: any item missing from --evidence, or with an empty value, causes the gate to emit BLOCKED and exit. The missing items are named explicitly.

**PASSED**: all items covered. The gate emits the evidence table and a T1 scratch marker tagged `phase-review-passed,rdr-NNN,phase-N`.

## Evidence Format

```
Item1=nexus-61x6,Item2=nexus-t3xx,Item3=nexus-7ejx,Item4=none,Item5=nexus-lint1
```

- Keys are `ItemN` (case-insensitive, numeric suffix required)
- Values are bead IDs or `none`
- Comma-separated, no spaces required around `=` or `,`

## Worked Example — RDR-112 Phase 1

RDR-112 has 9 §Approach items. Phase 1 closed beads covered items 1, 3, 6, 7, 8, 9. Items 2 (T3 service) and 5 (storage boundary lint) had no closing beads.

**Pass 1 invocation:**
```
/nx:phase-review-gate 112 --phase 1
```

**Pass 1 output** (abridged):
```
| Item1 | T2 service | (provide bead-id or `none`) |
| Item2 | T3 service | (provide bead-id or `none`) |
...
```

**Pass 2 invocation (with the nexus-52lb gap — would BLOCK):**
```
/nx:phase-review-gate 112 --phase 1 --evidence 'Item1=nexus-61x6,Item3=nexus-7ejx,...'
```

Output:
```
> BLOCKED — Phase 1 cross-walk incomplete.
> 2 of 9 approach items have no evidence pointer.

### Missing Evidence
- Item2 (T3 service): no evidence pointer supplied
- Item5 (Any future persistent store): no evidence pointer supplied
```

**Pass 2 invocation (complete — would PASS):**
```
/nx:phase-review-gate 112 --phase 1 --evidence 'Item1=nexus-61x6,Item2=nexus-t3xx,...,Item9=nexus-w0et'
```

## Relationship to Other Gates

| Gate | Scope | When |
|------|-------|------|
| `/nx:rdr-gate` | Whole RDR structure + assumptions + AI critique | Before RDR acceptance |
| `/nx:rdr-close --reason implemented` | Problem Statement gaps (file:line pointers) | At RDR close time |
| `/nx:phase-review-gate` | §Approach items (bead pointers) | At each phase boundary |

Phase review gate is NOT a substitute for rdr-close's Problem Statement Replay. Both gates run at their respective lifecycle points.

## When to Invoke

Invoke before closing any phase-review bead — especially when:
- A phase spans multiple beads and the review gate bead has a title like "Phase N review gate" or "P{N}.review"
- The implementation plan changed during the phase (the most common time for silent drops)
- A phase was paused and resumed weeks later

## Limitations

- The gate validates **coverage**, not **correctness**: `Item2=nexus-t3xx` passes even if nexus-t3xx's acceptance criteria are weak. The reviewer must verify the evidence manually.
- The gate parses `N. **Label**: ...` formatted items. §Approach sections with non-standard numbering (e.g. roman numerals, lettered items) are not parsed. Use standard `1.` numbering in §Approach.
- `none` is a valid evidence value. Over-use of `none` (e.g. `none` for every item) defeats the purpose; the reviewer is accountable for each `none` they supply.

## Success Criteria

- [ ] Pass 1: all §Approach items enumerated, re-invoke instruction emitted
- [ ] Pass 2: BLOCKED when any item has no evidence pointer; PASSED when all covered
- [ ] BLOCKED output names the specific missing items
- [ ] PASSED output shows evidence table and T1 scratch marker
- [ ] Regression: RDR-112 Phase 1 with nexus-52lb evidence set returns BLOCKED
