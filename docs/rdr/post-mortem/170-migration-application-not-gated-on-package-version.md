# Post-Mortem: RDR-170 T2 Migration Application Must Not Gate on Package Version

## RDR Summary

`apply_pending`'s upper bound (`introduced <= current_version`) silently dropped any
registered migration whose `introduced` exceeded a frozen package version. On `develop`
(pinned at 5.10.6) the registered `slcn7` step (`introduced=5.10.7`) was dormant. The RDR
diagnosed the root cause — the `MIGRATIONS` registry ships in the same wheel as the runner,
so the upper bound could only ever mis-fire on a frozen/ahead-of-release branch — and chose
to drop the upper bound (lower-bound-only gating) over a policy-only or sequence-number
alternative.

## Implementation Status

**Implemented.** Shipped to `develop` (merge `7c737fee`, fix `38883b63`). Full suite green
(11374 passed). Stacked review (code-review-expert + substantive-critic) returned 0 Critical.
Single tracking bead `nexus-j25po` closed.

---

## Implementation vs. Plan

### What Was Implemented as Planned

- `apply_pending` lower-bound-only filter (drop `m_ver <= current_t`).
- Registry-aware canonical version `expected_t2_schema_version() = max(package, registry_max)`.
- Upper bound dropped in the two other pending-filter sites named in the Approach: `nx upgrade
  --dry-run` (`pending_t2`/`pending_t3`) and `nx doctor --check-schema`.
- `upgrade._current_version` + `bootstrap_schema` migration target + cold-start fast path
  routed through `expected_t2_schema_version()`.
- `slcn7` re-validated on a copy of the live `develop` DB (no dup roots, idempotent, partial
  unique index) before merge.
- AGENTS.md contract note; frozen-branch runner regression + floor tripwire tests.

### What Diverged from the Plan

- **The canonical-version redefinition was NOT in the original RDR.** The first draft
  explicitly stated `expected_t2_schema_version()` was "a separate concern … left
  package-version-based. This RDR does not touch it," and proposed a `max(current_version,
  highest-applied)` stamp. The finalization **gate caught a Critical**: that stamp writes
  `5.10.7` to the daemon's `_nexus_version` row while the client's `expected_t2_schema_version()`
  still returned the package `5.10.6`, tripping `T2SchemaVersionMismatchError` in the
  RDR-120 P3b handshake → **daemon unreachable on develop**. The fix (registry-aware
  canonical version) was folded in and the RDR re-gated PASSED.
- **Two additional upper-bound filter sites were missed by the first Approach.** The Approach
  named one site; code-review found a third (`nx doctor --check-schema`) and the cold-start
  fast path reading `_pkg_version` directly. Both folded in before merge.

### What Was Added Beyond the Plan

- `clean_registry` fixture isolating the `TestApplyPending` bookkeeping tests from the real
  registry's catalog-absent defer steps (which now run under lower-bound gating and suppress
  the stamp on a catalog-less DB).
- Registry-slice monkeypatches for the RDR-108 je0b/OBS1 tests to reproduce the old
  upper-bound scoping faithfully.
- `test_context` change: distinct collections, because `slcn7` now forbids duplicate root
  topics (the test had depended on `slcn7` being dormant).

### What Was Planned but Not Implemented (at first) — caught by review

- **Frozen-branch CLI-output tests** for `--check-schema` and `--dry-run` were specified in
  Approach step 3 but omitted from the first implementation pass. The **substantive-critic
  caught the silent scope reduction**; they were added (`TestRDR170FrozenBranchReporting`).

---

## Drift Classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| **Missing failure mode** | 1 | stamp value feeds the cross-process handshake; collision not anticipated in the first draft | Yes — trace every reader of the value being changed |
| **Missing cross-cutting concern** | 1 | the upper bound was duplicated across 3 filter sites + the fast path; first Approach named 1 | Yes — `grep` the predicate before scoping |
| **Internal contradiction** | 1 | first draft declared `expected_t2_schema_version()` out of scope while changing the stamp it feeds | Yes — a stamp change cannot be out-of-scope from the version it stamps |

### Pattern References

The "silent scope reduction at implementation, caught by the critic" instance is the same
class the stacked-reviewer discipline exists to catch (see global `feedback_exhaustive_surface_audit`
and `feedback_phase_closeout_scope_audit`): the RDR named the test, the first implementation
pass dropped it, the critic restored it.

---

## RDR Quality Assessment

### What the RDR Got Right

- The **root-cause framing** ("the registry is the authority, not the package string; an
  upper bound can only ever mis-fire on a frozen branch") was correct and is the durable
  insight future maintainers need.
- The **alternatives analysis** correctly rejected re-stamping lower (cannot serve both
  develop-now and released-user-upgrade) and sequence-numbers (disproportionate).
- The **caller audit** ("no production path passes a non-package target") was exhaustive and
  held up.

### What the RDR Missed

- That changing the stamp value forces a change to `expected_t2_schema_version()` because the
  same value feeds the client↔daemon handshake. Declaring it out of scope was an internal
  contradiction the gate caught.
- The full count of upper-bound filter sites (3, not 1) and the cold-start fast path.

### What the RDR Over-specified

- Nothing material. The Approach was small and mostly accurate; the divergences were
  additions the gate/review surfaced, not over-specified code that went unused.

---

## Key Takeaways for RDR Process Improvement

1. **A change to a value cannot be scoped narrower than the set of its readers.** The first
   draft tried to change the version stamp while declaring `expected_t2_schema_version()` "out
   of scope" — but the stamp IS that value, read by the handshake and the fast path. Before
   declaring a value "untouched," enumerate every reader; if any reads the thing you're
   changing, it is in scope.
2. **`grep` the exact predicate being changed before writing the Approach.** The upper bound
   `<= current_t` lived in three filter sites + a fast path; the first Approach named one.
   An inverse-grep of the predicate at authoring time would have listed all four.
3. **The two-round gate earned its cost.** Round 1 BLOCKED on a Critical (handshake collision)
   that green tests would never have shown — it only manifests cross-process, on a frozen
   branch, after the dormant migration runs. The gate is the cheapest place to catch a
   "looks-correct, breaks-the-daemon" change.
4. **The critic catches implementation-vs-RDR scope reduction the code reviewer does not.**
   code-review-expert approved with docstring nits; the substantive-critic found that an
   RDR-mandated test (frozen-branch CLI output) had been silently dropped from the
   implementation. Run both; never substitute one for the other.
