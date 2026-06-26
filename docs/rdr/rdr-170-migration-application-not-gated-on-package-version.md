---
title: "T2 Migration Application Must Not Gate on Package Version: Drop the apply_pending Upper Bound"
id: RDR-170
type: Architecture
status: closed
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-25
accepted_date: 2026-06-25
closed_date: 2026-06-25
close_reason: implemented
related_issues: [nexus-j25po]
related_rdrs: [RDR-076, RDR-120, RDR-142]
supersedes: []
related_tests: [tests/test_migrations.py, tests/test_upgrade_e2e.py, tests/db/test_migration_fast_path.py]
post_mortem: docs/rdr/post-mortem/170-migration-application-not-gated-on-package-version.md
implementation_notes: "Shipped to develop (merge 7c737fee, fix 38883b63). Drop apply_pending upper bound across 3 filter sites; expected_t2_schema_version registry-aware; slcn7 un-dormants (validated on live-db copy). Gate caught a handshake Critical (round 1 BLOCKED); stacked review 0 Critical; full suite 11374 passed."
---

# RDR-170: T2 Migration Application Must Not Gate on Package Version: Drop the apply_pending Upper Bound

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

#### Gap 1: a version-frozen branch silently drops registered migrations whose `introduced` exceeds the frozen version

`apply_pending(conn, current_version)` (`src/nexus/db/migrations.py`) executes a registered
migration step only when:

```python
m_ver > last_seen_t and m_ver <= current_t
```

where `current_version = expected_t2_schema_version()` is derived from the installed
`conexus` package version (pyproject `version`). The **upper bound** `m_ver <= current_t`
silently drops any migration whose `introduced` exceeds the package version.

This bites the moment a branch's package version is frozen below the next release — which
is exactly the state of `develop` today. `develop` is pinned at `5.10.6` and stays there
while it is unreleasable (the RDR-155 P4a release boundary; auto-memory
`project_release_boundary_p4a`). The registry already contains:

```python
Migration("5.10.7", "nexus-slcn7: merge duplicate root topics + unique (collection,label) index",
          migrate_dedup_root_topics)
```

On every `develop` build `current_t == (5,10,6)`, so `5.10.7 <= 5.10.6` is **False** and
this migration **never applies** — not on a fresh bootstrap, not on an existing DB, not in
the test suite's `apply_pending` path. The dedup + unique-index it enforces is absent on
every `develop` install and on the daemon-owned schema. The migration's *logic* is unit
-tested directly (`tests/test_migrations.py::...migrate_dedup_root_topics`), so the gap is
invisible to the suite: green tests, dormant migration.

#### Gap 2: no single semver `introduced` stamp can serve both a frozen development branch and a released-user upgrade

##### Why "just re-stamp it lower" does not work

The obvious patch — stamp the migration at the current frozen version (`5.10.6`) so it
applies on `develop` — is **wrong**, because a single semver `introduced` value cannot
serve both consumers while the branch version is frozen below the next release:

- To apply **on develop now**, the migration needs `introduced <= 5.10.6`.
- To apply **for a released user upgrading `5.10.6 → next`**, it needs `introduced = next`
  (e.g. `5.10.7`). A `5.10.6`-stamped migration never runs for someone already at `5.10.6`
  (`5.10.6 < 5.10.6` is False).

These requirements conflict precisely because `develop`'s version is frozen. No single
stamp satisfies both. The defect is structural, not a mis-stamp.

#### Gap 3: the upper bound couples schema evolution to the package version, a coupling that can only ever mis-fire

The upper bound couples *schema evolution* to the *package version string*. But the
`MIGRATIONS` registry **ships in the same wheel as the code that reads it** — a running
client can never possess a registered migration "newer than its own code." Therefore
`expected_t2_schema_version()` is, by construction, `>=` every `introduced` in that
client's registry **except** on a frozen- or mis-stamped branch. The upper bound only ever
*does* anything in exactly that pathological case, and what it does there is suppress a
migration whose implementation is present and intended to run. The gate is not protecting
against anything real; it is the bug.

The only legitimate gate is the **lower bound** `m_ver > last_seen` (do not re-run a
migration already applied to this DB). Ordering by `introduced` still gives a deterministic
sequence.

## Context

- **RDR-076** established the idempotent `apply_pending` runner and the `_nexus_version`
  gate (`cli_version` row = last-applied version).
- **RDR-120 P3b** transferred migration ownership to the T2 daemon
  (`T2Database(run_migrations=True)`); production direct-opens default
  `_DEFAULT_RUN_MIGRATIONS=False`.
- **RDR-142** is adjacent prior art: it addressed the *version-row vs actually-applied-steps*
  disagreement for deferred/gated steps. RDR-170 addresses a different disagreement — the
  *registry vs package-version* upper bound — but both stem from treating the semver row as
  the source of truth about schema state rather than the registry + per-step guards.
- Surfaced during the RDR-651 cutover F4 rollback analysis (`conexus-jya7`), 2026-06-25.

### Production call-sites audited

Every production caller passes `current_version = expected_t2_schema_version()` (the package
version):

- `nexus.db.t2._apply_pending_with_lock_retry` (daemon + bootstrap path)
- `nexus.db.t2.T2Database.bootstrap_schema`
- `nexus.commands.upgrade` (`nx upgrade`)

The **only** caller that passes a non-package target is the unit test
`tests/test_migrations.py::...test_only_runs_migrations_in_range` (`apply_pending(conn,
"2.0.0")`), which exists solely to assert the upper-bound behavior. No production feature
performs a partial / staged upgrade to an explicit intermediate target. Removing the upper
bound therefore changes no production behavior except the dormant-migration bug it fixes.

## Decision

**Drop the upper bound from `apply_pending`'s step filter.** Apply every registered
migration with `introduced > last_seen`, in registry order, regardless of the package
version. Retain `current_version` only for the post-run `_nexus_version` stamp and the
existing downgrade/pre-release guards.

```python
for m in MIGRATIONS:
    m_ver = _parse_version(m.introduced)
-   if m_ver > last_seen_t and m_ver <= current_t:
+   if m_ver > last_seen_t:
        # ... run step
```

Rationale: the registry is the authoritative set of schema changes the running code knows
about; all of them should apply to any DB that has not already seen them. This fixes both
horns at once — `develop` dormancy **and** released-user upgrades — with one change, and it
removes a gate that was only ever capable of mis-firing.

### Version-stamp semantics after the change — unify the canonical schema version

The finalization-gate critique (BLOCKED, 2026-06-25) found that a naive stamp of
`max(current_version, highest-applied introduced)` writes `5.10.7` to the daemon's
`_nexus_version.cli_version` row, which then **breaks the RDR-120 P3b client↔daemon
handshake**: `T2Client._do_handshake` (`src/nexus/daemon/t2_client.py:322,352-360`) compares
the daemon's stored row against the client's `expected_t2_schema_version()` — which returns
the package version `5.10.6` — and raises `T2SchemaVersionMismatchError` on any non-equal,
non-`"0.0.0"` daemon version. The result: the moment `migrate_dedup_root_topics` runs, the
develop daemon stamps `5.10.7` and **every client process rejects it** (daemon-down-
equivalent). The same `5.10.7 != 5.10.6` mismatch also permanently disables the RDR-140
cold-start fast path (`_cold_start_is_current_and_wal`, `t2/__init__.py:232`).

The original RDR said `expected_t2_schema_version()` was "a separate concern … left
package-version-based." The critique correctly identified that the stamp change cannot avoid
this collision, so this RDR **does** touch it — and the principled resolution is exactly the
RDR's own thesis applied one level up: **the canonical T2 schema version is the registry's
authority, not the package string.**

Define a single canonical schema version used by the stamp, the handshake, and the
cold-start fast path:

```python
def expected_t2_schema_version() -> str:
    pkg = <installed conexus package version, or "0.0.0">
    registry_max = max(m.introduced for m in MIGRATIONS)
    return _max_version(pkg, registry_max)   # highest schema this CODE knows
```

Why this is correct and minimal:

- **Same-wheel client and daemon agree by construction.** Both read the identical
  `MIGRATIONS` list, so both compute the same `registry_max` — the RDR-120 P3b "agree when
  running the same wheel" invariant is *preserved*, just sourced from the registry instead
  of the (frozen-on-develop) package string.
- **Genuine cross-wheel skew is still caught.** A truly older client (older wheel, lower
  `registry_max`) computes a lower version and correctly mismatches a newer daemon — the
  handshake keeps its real job. The critique's Option A (blanket-tolerate `daemon > client`)
  was rejected here precisely because it would *mask* that real skew.
- **The stamp simplifies.** With `current_version` now = the canonical (registry-aware)
  version, `apply_pending` stamps `current_version` as before — no `max()` special case —
  and the row never understates on-disk schema. The existing downgrade guard
  (`current_t >= last_seen_t`) and pre-release `(0,0,0)` guard remain.
- **Cold-start fast path works on develop.** `current_version` = `5.10.7` == the stamped
  row → the fast path is not broken.

This redefinition is a no-op on a released build (where `package >= registry_max`) and only
changes behavior on a frozen/ahead-of-release branch — the same surface as the core fix.

## Approach

1. **Runner change.** In `apply_pending` (`migrations.py:~2527`), drop `and m_ver <=
   current_t` from the step filter. The version-stamp write (`migrations.py:~2566`) stamps
   `current_version` unchanged (no `max()` special case is needed once `current_version` is
   the registry-aware canonical version per step 2), preserving the downgrade
   (`current_t >= last_seen_t`) and pre-release `(0,0,0)` guards.
2. **Canonical schema version (Critical fix).** Redefine `expected_t2_schema_version()`
   (`migrations.py:~2345`) to return `max(package_version, max(m.introduced for m in
   MIGRATIONS))` (add a small `_max_version` helper or inline the `_parse_version`
   comparison). This is the value the daemon stamps, the handshake compares
   (`t2_client.py:322`), and the cold-start fast path compares. **The cold-start fast path
   does not currently route through this function** — `_cold_start_is_current_and_wal`
   (`t2/__init__.py:~218`) reads `importlib.metadata.version("conexus")` directly, so it
   **must also be changed** to call `expected_t2_schema_version()`; otherwise it compares the
   stamped `5.10.7` row against the raw package `5.10.6` and the fast path stays broken on
   develop. Update `tests/db/test_migration_fast_path.py` (its `_current_version()` /
   stamp-fixture at ~32-33,81,136,186) to stamp `expected_t2_schema_version()` rather than
   `_pkg_version("conexus")`. Add a unit test asserting the function equals `registry_max`
   when the package version is frozen below it and equals the package version on a released
   build. Audit and update any existing test that asserts `expected_t2_schema_version() ==`
   the package version verbatim.
3. **Reporting-surface parity (Significant fix) — drop the upper bound everywhere it is
   duplicated.** There are **three** filter sites carrying `<= current_t`, not one. All must
   change together with the runner or the reporting lie reappears in a different surface:
   - `apply_pending` step filter (`migrations.py:~2527`) — the runner, step 1.
   - `nx upgrade --dry-run` `pending_t2` (`commands/upgrade.py:~434`) **and** `pending_t3`
     (`~443`).
   - `nx doctor --check-schema` `_run_check_schema` pending filter (`commands/doctor.py:
     ~146-150`) — drops `and _parse_version(m.introduced) <= cli_t`. Without this, doctor
     reports "Schema version: OK" while `apply_pending` is actively stamping `5.10.7`.
   Add a frozen-branch test for each surface: with a registered `introduced > package_version`
   step and `last_seen <` that step, both `--dry-run` and `--check-schema` must list it as
   pending (not "Up to date" / "OK"). Update the existing doctor/upgrade schema tests
   (`tests/test_upgrade_e2e.py`, `tests/test_phase5_integration.py`) accordingly. This closes
   the same reporting-lie surface RDR-142 addressed for deferred steps.
4. **Test updates for the runner contract.** The existing `test_version_filtering`
   (`tests/test_migrations.py:~608`, calls `apply_pending(conn, "2.0.0")` then `"4.1.2")` and
   asserts the row stops at each) encodes the **removed** upper-bound contract — after the
   change the first call applies all `introduced > last_seen` steps (and several will fire
   `MigrationRetry` → `any_skipped=True`, suppressing the stamp), so its assertions are
   invalid. Replace it with a test encoding the new contract: `apply_pending(conn, X)` applies
   all `introduced > last_seen` steps regardless of `X` and stamps the canonical version.
   Audit other intermediate-target callers (`test_idempotent` at ~635 passes `"4.1.2"` for an
   idempotency check — lower risk, but verify it still holds). Add a regression test
   reproducing the frozen-branch case directly: bootstrap a DB at `last_seen < X`, call
   `apply_pending(conn, current)` where `current < X` for some registered step `X`, and assert
   step `X` **applied** and the version row reflects it.
5. **Frozen-branch tripwire (Significant fix — must not be vacuous).** The tripwire must use
   a `current_version` *strictly below* `max(m.introduced for m in MIGRATIONS)`, otherwise it
   is trivially true and re-introducing the upper bound would not fail it. Concretely:
   `apply_pending(conn, current_version=X)` with `X < max(introduced)`, assert every step
   with `introduced > last_seen` (including those `> X`) applied. This is the precise case the
   upper bound breaks and is the test that would have caught `nexus-j25po` at author time.
6. **Monotonic-order assertion.** Add a test that `MIGRATIONS` `introduced` values are
   **non-decreasing** (`>=`, not `>` — the registry legitimately has multiple entries at the
   same version, e.g. four at `4.14.2`), so registry order matches version order (the runner
   now relies on registry order, not the version filter, for determinism).
7. **Re-validate `migrate_dedup_root_topics` (slcn7) before it un-dormants.** Confirm the
   dedup + unique-`(collection,label)`-index migration is safe against current `develop`
   data (duplicate root topics may have accumulated while its unique index was never
   created). It is already guarded + idempotent; verify on a copy of a real `develop`
   `memory.db` that it applies cleanly. This is the data-safety check the dormancy hid.
8. **AGENTS.md note.** Update `src/nexus/db/AGENTS.md` to state the contract explicitly:
   migration application is gated by the registry + per-step guards, **not** by the package
   version; `introduced` orders steps and stamps the version row, it does not authorize them.

## Consequences

**Positive**
- `develop` runs its full registered schema; no silent dormancy while the version is frozen.
- Released-user upgrades apply the same migrations with no special stamping.
- One fewer coupling between packaging and schema; the registry is the single source of truth.
- The fresh-bootstrap tripwire makes future dormancy a test failure, not a production surprise.

- The canonical schema version (`expected_t2_schema_version`) now reflects the registry, so
  the client↔daemon handshake and the cold-start fast path stay coherent on a frozen/ahead
  branch instead of breaking once an ahead-of-release migration runs. On released builds the
  value is unchanged (package version dominates).

**Negative / risk**
- Migration-runner semantics is correctness-critical. The blast radius is the entire T2
  schema-bootstrap path (daemon start, `nx upgrade`, every fresh open) **plus** the
  client↔daemon schema handshake and cold-start fast path now that
  `expected_t2_schema_version()` changes. Mitigated by: no production caller relies on the
  upper bound (audited); the canonical-version redefinition is a no-op on released builds and
  preserves the same-wheel-agree invariant; full unit + the regression/tripwire/handshake
  tests above; idempotent per-step guards already in place.
- The upper bound is duplicated across **three** surfaces — the runner, `nx upgrade
  --dry-run` (`pending_t2` + `pending_t3`), and `nx doctor --check-schema` — plus the
  cold-start fast path reads the raw package version. All must change **together** (Approach
  steps 1–3) or the reporting lie RDR-142 fought reappears in whichever surface was missed.
  Called out as a single atomic change set.
- `migrate_dedup_root_topics` un-dormants on merge — it will run on the next daemon
  start / fresh open against `develop` data. Step 7 gates this; it must pass before merge.

## Alternatives Considered

- **Policy + CI tripwire only (keep the gate).** Declare dormant-until-release the contract,
  forbid `introduced > pyproject.version` surprises in CI, document it. Rejected: leaves
  `develop` running un-migrated schema for the entire (open-ended) freeze window — the dedup
  unique-index stays absent on every dev install — and does not fix the released-user-upgrade
  horn for a migration that legitimately needs `introduced = next-release`.
- **Dev/pre-release version on develop.** Make `develop` report a higher version so future
  migrations apply. Rejected: conflicts with the pinned pyproject version that the
  marketplace parity tests and release boundary require; reintroduces the coupling rather
  than removing it.
- **Sequence-number migrations (Alembic/Django-style monotonic revision counter).** The
  correct long-term decoupling of schema evolution from package version. Rejected *for now*
  as disproportionate: a substantial refactor of the runner, version bookkeeping, and tests.
  Dropping the upper bound achieves the same practical guarantee (registry order is the
  authority) with a one-line core change. A future RDR may still adopt revision counters if
  the version row proves insufficient.
