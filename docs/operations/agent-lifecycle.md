# Agent Lifecycle & Operations

The one operator-facing map of how the nexus agent is installed, runs, upgrades,
and is removed. It is a navigator, not a design record: each section links out to
the authoritative RDR for rationale rather than restating it.

"The agent" here is the local nexus stack: the `nx` CLI, the T2 daemon
(notes/plans/taxonomy over SQLite), and the T3 storage service (the native
`nexus-service` binary over Postgres 17 + pgvector). Since RDR-155 P4a, T3 serves
exclusively through that service stack in both local and cloud mode.

## State model

```
  uninstalled ──────nx init──────▶ installed ──provision PG+pgvector──▶ provisioned
       ▲                                                                        │
       │                                                                (supervisor start)
       │                                                                        ▼
       │                                  ┌──────────── nx upgrade ─────────────┐
       │                                  ▼                                     │
       └──────── nx uninstall ◀──────── running ───────────────────────────▶ upgrading
                                                                                │
                                                          (ladder rungs converge, back to running)
```

- **uninstalled** — no autostart unit, no service binary, no provisioned PG.
- **installed** — the `nexus-service` binary is fetched + positioned; `nx` resolvable.
- **provisioned** — Postgres 17 + pgvector provisioned, schema migrated (Liquibase), embedder wired (bge-768 local, or Voyage in cloud mode).
- **running** — the supervisor publishes a `storage_service` lease; T2 + T3 serve.
- **upgrading** — a transient state during `nx upgrade`, while the ladder walks its pending rungs (T2 schema, the Chroma → service substrate move, chunk identity, embedder era).

The running-state machinery (lease publish/heartbeat/relinquish, single-writer
discovery, version-skew) is the shared service-registry primitive
(`src/nexus/daemon/service_registry.py`); its invariants are owned by
[RDR-149](../rdr/rdr-149-unified-service-registry-substrate.md) and enforced by
`tests/daemon/test_lifecycle_gate.py`.

## Coverage matrix (lifecycle surface → authority)

| Stage | CLI / surface | Authority |
|-------|---------------|-----------|
| Install | `nx init` (collapsed flow), `nx daemon service install-binary <tag>`, `nx daemon service install --autostart` | RDR-157 (distribution), RDR-161 (native-only), RDR-174 (collapsed init + autostart), RDR-175 (OS-init watchdog) |
| Provision | bundled PG17 + pgvector, Liquibase migrate, bge-768 ONNX fetch | RDR-155 (pgvector substrate), RDR-160 (bge-768 embedder) |
| Run | T2 daemon + T3 `nexus-service`; lease lifecycle | RDR-149 (daemon lifecycle), RDR-152 (endpoint discovery) |
| Upgrade | `nx upgrade` — the single trigger for every data transition; `nx doctor` reports pending rungs | RDR-185 (the ladder), RDR-159 (the inherited ETL engine), RDR-162 (cross-model) |
| Uninstall | `nx uninstall` (CLI), `daemon_uninstall` (MCP) | RDR-165 (this RDR) |

## Install

Greenfield local first-run (no prior install):

```bash
nx daemon service install-binary engine-service-vX.Y.Z   # fetch the cosign-verified native binary + relocatable PG+pgvector bundle
nx init                                                   # provision Postgres, fetch the bge-768 ONNX, start the service, offer autostart
nx doctor                                                 # verify: dependencies, service /health, embedding mode
```

Pick `engine-service-vX.Y.Z` from the
[engine-service releases](https://github.com/Hellblazer/nexus/releases) (newest
`engine-service-v*` tag, an explicit pin, no `latest`). `nx init` provisions the
local service backend by default (the deprecated `nx init --service` flag still
works) and offers to register the OS autostart unit (RDR-174 decide-first;
`--yes` accepts, `--no-autostart` declines). It is idempotent (safe to re-run).
It embeds with bge-768 locally; in the managed-cloud deployment, embeddings run
server-side via Voyage with `NX_VOYAGE_API_KEY` plumbed from the nexus credential
chain. See
[getting-started.md](../getting-started.md#first-time-setup-the-storage-backend) for the
full walkthrough and [container-integration.md](../container-integration.md) for
reaching the service from a container.

**Managed (no local stack):** to use the hosted service instead of running the
stack yourself, see [managed-onboarding.md](../managed-onboarding.md) — point `nx`
at the endpoint with an operator-provisioned token; no install, no provision.

## Upgrade

- **`nx upgrade`** — the single trigger (RDR-185). It converges the stateless
  preconditions (package, engine, process, and provisioning — standing up the
  service stack when a legacy footprint needs one to migrate into), then walks
  the ladder: every pending rung, in order, each detecting → converging →
  verifying before completion is recorded. Idempotent and resumable; runs
  `--auto` (T2 only, no engine install) on session start via the plugin hook.
  See [cli-reference.md](../cli-reference.md#nx-upgrade).
- **`nx doctor`** — reports pending rungs read-only, plus era debt (pre-RDR-108
  chunk ids) from the release that ships the detector rather than on migration
  day.

The Chroma → service move is one of those rungs, not a separate journey: detect
footprint, migrate (T2, then each T3 leg with its catalog ref-remap), validate,
unlock — copy-not-move, with the Chroma source left byte-untouched as the
rollback origin (RDR-176). Cross-model collections (e.g. legacy minilm) are
re-embedded into the target model (RDR-162); same-model voyage collections are
copied verbatim, skipping the billed re-embed (RDR-166). Legacy (pre-RDR-108)
chunk ids are recomputed on the wire from the chunk text — no re-index, no
source files needed (RDR-185). The engine below it is RDR-159's, inherited; the
`nx guided-upgrade` / `nx migrate-to-service` verbs that used to front it are
demoted internal primitives. See [migration-runbook.md](../migration-runbook.md)
for the operator's manual order of operations.

When to upgrade: whenever you update the code — `nx upgrade` converges whatever
that install actually needs, whether that is one schema migration or a five-year
gap. Rollback: the Chroma source is never modified; a blocked migration leaves
reads degraded-loud and offers rollback, never auto-invoked.

## Uninstall

`nx uninstall` is the complete, discoverable teardown (RDR-165). It is **dry-run
by default** (preview only; `--yes` to act) and auto-detects both install shapes,
each branch a no-op when its target is absent:

```bash
nx uninstall                       # DRY RUN: show what would be removed
nx uninstall --yes                 # perform the teardown
nx uninstall --yes --remove-data   # ALSO wipe the local data dir (notes + index)
```

- **Local service present:** stops the engine-service + Postgres stack
  (`nx daemon service stop --with-pg`), stops the T2 daemon, removes the OS
  autostart unit, clears the first-run marker. With `--remove-data`, also wipes
  the local data dir (irreversible; a shallow-path guard refuses a misconfigured
  `NEXUS_CONFIG_DIR`).
- **Managed-only client:** clears the managed endpoint config
  (`service_url` + `service_token`) from `config.yml`. Skips service-stop (no
  local service) and never touches the remote tenant's data. If
  `NX_SERVICE_URL` / `NX_SERVICE_TOKEN` are exported in your **shell**, `nx`
  clears `config.yml` and warns you to `unset` the export (it cannot unset the
  parent shell).

`--remove-data` is local-only: it never deletes a managed/remote tenant's data.
The service-stop routes through the shared lease primitive (RDR-149); `nx
uninstall` orchestrates existing lifecycle commands rather than duplicating them.
The `daemon_uninstall` MCP tool remains for in-chat teardown of the local daemon.

## See also

- [getting-started.md](../getting-started.md) — install + first-run detail
- [managed-onboarding.md](../managed-onboarding.md) — the hosted-service journey
- [migration-runbook.md](../migration-runbook.md) — Chroma → service migration
- [cli-reference.md](../cli-reference.md) — every command + flag
- RDRs: 165 (this lifecycle), 185 (the upgrade ladder), 155 (substrate), 157/161 (distribution), 149 (daemon lifecycle), 159 (the inherited guided-upgrade ETL engine), 162 (cross-model upgrade), 166 (managed journeys)
