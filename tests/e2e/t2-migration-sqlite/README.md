# SQLite-T2 migration E2E (completely isolated)

A throwaway-container end-to-end test of the **SQLite T2 migration path** through
the **real wheel install** — the path the unit suite (editable install +
`EphemeralClient` + tmp SQLite) does **not** faithfully exercise. Use it after
any change to:

- `src/nexus/db/migrations.py` (`apply_pending`, `expected_t2_schema_version`,
  the step-resolver, `resolve_blocking_steps`)
- `src/nexus/commands/upgrade.py` (`nx upgrade` / `--dry-run`)
- `src/nexus/commands/doctor.py` (`nx doctor --check-schema`)

## Why this exists

`uv tool install` (the wheel path users get) resolves package data and
**version-gated migrations differently** from the editable install pytest uses.
This is the lightweight, **completely-isolated** check for that gap — it
complements, and does not overlap with, the service-mode
`tests/e2e/migration-rehearsal/` (which tests the Chroma→pgvector cutover and
does **not** touch the SQLite-T2 path).

## Isolation guarantees

Zero contact with the host:

- runs in a `--rm` `python:3.12-slim` container with **only** the develop wheel
- never reads or writes `~/.config/nexus`
- never swaps the host `uv tool` venv (the live-install hazard `reinstall-tool.sh`
  guards against, `nexus-q3xrx`)
- each scenario uses a fresh per-test `NEXUS_CONFIG_DIR` (sqlite backend, local mode)

Safe to run alongside a live install.

## Usage

```bash
tests/e2e/t2-migration-sqlite/run.sh
```

`run.sh` builds the develop wheel (`uv build --wheel`), bakes it into a clean
image (build context is this dir; the repo-root `.dockerignore` excludes
`dist/`, so the wheel is staged here as `conexus-*.whl` and cleaned on exit),
then runs the scenarios in a throwaway container. ~5–8 min cold (heavy dep
install), ~2–3 min with the image layer cached.

## Scenarios (`rehearse_t2.sh`)

1. **Clean fresh upgrade (catalog present)** — `nx upgrade` runs `apply_pending`
   to completion; `--dry-run` then reports clean; `doctor --check-schema` healthy.
2. **RDR-170 registry-aware stamped version** — the stamped `_nexus_version` is
   `max(package, registry_max)` (e.g. package `5.10.6` → stored `5.10.7`), NOT the
   package version.
3. **RDR-142 gated `--dry-run`** — a seeded high-volume-orphan state makes the
   je0b PK migration GATE; `--dry-run` reports `[BLOCKED]` + the
   `nx catalog rename-collection` and `NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD`
   remediations (not "no pending"); `doctor --check-schema` reports it too.
4. **RDR-142 deferred `--dry-run`** — a catalog-absent state DEFERS the PK
   migration; `--dry-run` reports the deferral, not "no pending".
5. ~~**nexus-3lbhb daemon bootstrap gate**~~ — RETIRED. It asserted
   `nx daemon t2 start` stayed fail-closed on a gated DB; the verb and
   `run_t2_daemon` went with the T2 daemon (nexus-i711w Stage 2 sub-stage B),
   so there is no bootstrap left to gate.

`seed_gated.py` builds the per-scenario DB state (`catalog-only`,
`gated-orphan`, `deferred`) with pure stdlib `sqlite3` — no nexus import.

## Behaviours this harness pins (and the unit suite can miss)

- A **catalog-less fresh install correctly DEFERS** the je0b PK migrations
  (`MigrationRetry`) and so **never stamps the version** (stays `0.0.0`) — the
  deferred-not-complete state RDR-142 exists to surface. The clean-stamp path
  needs a catalog present.
- The operator-facing `MigrationError` (with remediation) surfaces on **stderr**.
  (The daemon-log half of this "loud on both channels" claim retired with
  scenario 5.)

## Not (yet) in CI

Manual-run by design (heavy container build). Run it locally before merging any
of the files listed at the top. A path-filtered manual-dispatch workflow is a
reasonable future addition (mirroring `cold-install-rehearsal.yml`).
