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
  uninstalled ──nx init --service──▶ installed ──provision PG+pgvector──▶ provisioned
       ▲                                                                        │
       │                                                                (supervisor start)
       │                                                                        ▼
       │                                  ┌──── nx upgrade / guided-upgrade ────┐
       │                                  ▼                                     │
       └──────── nx uninstall ◀──────── running ───────────────────────────▶ upgrading
                                                                                │
                                                          (migration/migrations apply, back to running)
```

- **uninstalled** — no autostart unit, no service binary, no provisioned PG.
- **installed** — the `nexus-service` binary is fetched + positioned; `nx` resolvable.
- **provisioned** — Postgres 17 + pgvector provisioned, schema migrated (Liquibase), embedder wired (bge-768 local, or Voyage in cloud mode).
- **running** — the supervisor publishes a `storage_service` lease; T2 + T3 serve.
- **upgrading** — a transient state during `nx upgrade` (schema/T3 migrations) or `nx guided-upgrade` (Chroma → service migration).

The running-state machinery (lease publish/heartbeat/relinquish, single-writer
discovery, version-skew) is the shared service-registry primitive
(`src/nexus/daemon/service_registry.py`); its invariants are owned by
[RDR-149](../rdr/rdr-149-unified-service-registry-substrate.md) and enforced by
`tests/daemon/test_lifecycle_gate.py`.

## Coverage matrix (lifecycle surface → authority)

| Stage | CLI / surface | Authority |
|-------|---------------|-----------|
| Install | `nx init --service`, `nx daemon service install-binary <tag>` | RDR-157 (distribution), RDR-161 (native-only), RDR-144 (`nx init` embedder onboarding) |
| Provision | bundled PG17 + pgvector, Liquibase migrate, bge-768 ONNX fetch | RDR-155 (pgvector substrate), RDR-160 (bge-768 embedder) |
| Run | T2 daemon + T3 `nexus-service`; lease lifecycle | RDR-149 (daemon lifecycle), RDR-152 (endpoint discovery) |
| Upgrade | `nx upgrade` (migrations), `nx guided-upgrade` (Chroma→service) | RDR-159 (guided upgrade), RDR-162 (cross-model) |
| Uninstall | `nx uninstall` (CLI), `daemon_uninstall` (MCP) | RDR-165 (this RDR) |

## Install

Greenfield local first-run (no prior install):

```bash
nx daemon service install-binary engine-service-vX.Y.Z   # fetch the cosign-verified native binary + relocatable PG+pgvector bundle
nx init --service                                         # provision Postgres, fetch the bge-768 ONNX, start the supervisor
nx doctor                                                 # verify: dependencies, service /health, embedding mode
```

Pick `engine-service-vX.Y.Z` from the
[engine-service releases](https://github.com/Hellblazer/nexus/releases) (newest
`engine-service-v*` tag, an explicit pin, no `latest`). `nx init --service` is
idempotent (safe to re-run). It embeds with bge-768 locally; in the managed-cloud
deployment, embeddings run server-side via Voyage with `NX_VOYAGE_API_KEY`
plumbed from the nexus credential chain. See
[getting-started.md](../getting-started.md#first-time-setup-the-t2-daemon) for the
full walkthrough and [container-integration.md](../container-integration.md) for
reaching the service from a container.

**Managed (no local stack):** to use the hosted service instead of running the
stack yourself, see [managed-onboarding.md](../managed-onboarding.md) — point `nx`
at the endpoint with an operator-provisioned token; no install, no provision.

## Upgrade

- **`nx upgrade`** — applies pending T2 + T3 schema/migration steps. Idempotent;
  runs `--auto` (T2 only) on session start via the plugin hook. See
  [cli-reference.md](../cli-reference.md#nx-upgrade).
- **`nx guided-upgrade`** — the one-shot Chroma → service migration for a
  pre-6.0 install: detect footprint, provision + version-pin the service, migrate
  (T2, then each T3 leg with its catalog ref-remap), validate, unlock.
  Copy-not-move (the Chroma source is the
  rollback manifest). See [migration-runbook.md](../migration-runbook.md) and
  RDR-002 / RDR-159. Cross-model collections (e.g. legacy minilm) are re-embedded
  into the target model (RDR-162); same-model voyage collections are copied
  verbatim, skipping the billed re-embed (RDR-166).

When to upgrade: on a version bump (`nx upgrade` runs automatically for T2) or
when moving a pre-6.0 Chroma install onto the service stack (`nx guided-upgrade`,
once). Rollback: the Chroma source is never modified; a blocked migration leaves
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
- RDRs: 165 (this lifecycle), 155 (substrate), 157/161 (distribution), 149 (daemon lifecycle), 159 (guided upgrade), 162 (cross-model upgrade), 166 (managed journeys)
