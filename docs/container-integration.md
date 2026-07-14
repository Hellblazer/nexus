# Container integration

Running nexus from inside a container — Docker dev container, Claude
Cowork VM, CI agent, multi-agent Co-Work session — and sharing T1 / T2 /
T3 state with the host CLI Claude and any other nexus consumers.

This page assumes a 6.x install on the host.

> **Post-RDR-152 topology — there is one service to reach.** All three
> storage tiers now serve through the native **nexus-service** (Java +
> Postgres 17 + pgvector): T3 vectors, the T2 domain stores
> (notes/plans/taxonomy/telemetry/aspects/catalog), and T1 scratch. The
> storage backend hard-defaults to `service`
> (`src/nexus/db/storage_mode.py`); the old SQLite-T2-daemon-over-socket
> model (RDR-120: `NX_T2_ADDR`, Unix-socket mounts, `nx daemon t2`
> transport guidance) is an explicit opt-out kept only as a rollback
> path — see [Legacy (SQLite backend)](#legacy-sqlite-backend) at the
> end. If you followed the pre-6.x version of this page, the entire
> T2-daemon plumbing is gone from the happy path.

## TL;DR

The integration model has **one rule**: every nexus consumer talks to
the same nexus-service. A container reaches the host's service over
HTTP by setting exactly two environment variables:

```
NX_SERVICE_URL=http://<host-reachable-address>:<port>
NX_SERVICE_TOKEN=<bearer>
```

Every storage client — the ten T2 domain stores, the catalog client,
the T1 scratch store, and the T3 vector client — resolves its endpoint
through the same resolver (`src/nexus/db/service_endpoint.py` /
`http_vector_client._resolve_endpoint`): `NX_SERVICE_URL` +
`NX_SERVICE_TOKEN` env first, then the supervisor's lease file, then
fail loud. Inside a container there is no lease file, so the env pair
is required — and sufficient. Embedding is server-side, so the
container needs no Voyage/model credentials.

Picking a path:

| Host platform | Container runtime | Use |
|---|---|---|
| any | **Claude Cowork** | SDK transport (zero config; see § Cowork below) |
| any | any, host uses a **managed endpoint** (`service_url` is `https://…`) | Point the container at the same URL + a tenant token; no host bridging at all |
| macOS | Docker Desktop | `NX_SERVICE_URL=http://host.docker.internal:<port>` (Desktop's proxy reaches host loopback) |
| Windows | Docker Desktop (WSL2) | same as macOS — `host.docker.internal` |
| Linux | Docker / Podman, host-network | `--network=host` + `NX_SERVICE_URL=http://127.0.0.1:<port>` |
| Linux | Docker / Podman, default-bridge | `--add-host=host.docker.internal:host-gateway` + non-loopback service bind (`NX_SERVICE_BIND`) **or** a socat forward |

## Prerequisites on the host

One service, one start command:

```
nx daemon service start        # one-shot supervisor start
```

For a durable always-running service (recommended for any host that
runs Claude Code regularly):

```
nx init --service              # provisions PG cluster + credentials + persistent supervisor
nx daemon service install --autostart   # LaunchAgent / systemd user unit
```

Confirm it and read the port:

```
$ nx daemon service status --json
{
  "host": "127.0.0.1",
  "port": 49731,          ← dynamic; the container needs this
  "pid": 12345,
  "health": "ok",
  ...
}
```

> **The port is dynamic and changes on every supervisor (re)start.**
> The supervisor allocates a fresh free port each time it starts and
> republishes it in its lease (`~/.config/nexus/storage_service_addr.<uid>`).
> Host-side clients rediscover the lease automatically; a container
> pinned to a literal port does **not** — after a host service restart
> the container gets connection-refused until you refresh its
> `NX_SERVICE_URL`. If restarts are frequent, front the service with a
> stable-port forwarder (see the socat pattern below) or use a managed
> endpoint.

### Getting a token for the container

Two options, in order of preference:

1. **Issue a tenant-bound token** (revocable, auditable, least
   privilege):

   ```bash
   nx service token issue --tenant default --label my-dev-container
   # Token (shown once — store it now):
   # <bearer>
   ```

   Revoke it later with `nx service token revoke <hash-prefix>`;
   rotate with `nx service token rotate --tenant default` (zero
   downtime — old tokens stay valid through a grace window).

2. **Reuse the root bearer** the supervisor was provisioned with —
   persisted by `nx init --service` in
   `~/.config/nexus/pg_credentials`:

   ```bash
   TOKEN=$(grep '^NX_SERVICE_TOKEN=' ~/.config/nexus/pg_credentials | cut -d= -f2)
   ```

   Fine for a throwaway local container; prefer option 1 for anything
   longer-lived.

Host-side snippet reused by every recipe below:

```bash
SVC_PORT=$(nx daemon service status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["port"])')
TOKEN=<from one of the two options above>
```

## Path A: macOS / Windows Docker Desktop

```bash
docker run --rm \
    -e NX_SERVICE_URL=http://host.docker.internal:$SVC_PORT \
    -e NX_SERVICE_TOKEN=$TOKEN \
    <image-with-conexus>
```

Docker Desktop's `host.docker.internal` DNS name is proxied by a
process running natively on the host, so it reaches services bound to
the host's `127.0.0.1` — the nexus-service's default loopback-only
bind works unchanged. `--network=host` does NOT work on macOS Docker
Desktop (the container's `127.0.0.1` stays in the VM's own
namespace); use the DNS name.

## Path B: Linux host-network

```bash
docker run --rm --network=host \
    -e NX_SERVICE_URL=http://127.0.0.1:$SVC_PORT \
    -e NX_SERVICE_TOKEN=$TOKEN \
    <image>
```

The container shares the host's network namespace, so the service's
default loopback bind is directly reachable. Simplest path on Linux,
at the cost of the container's network isolation.

## Path C: Linux default-bridge

On native Linux, `--add-host=host.docker.internal:host-gateway` maps
the DNS name to the docker bridge gateway (typically `172.17.0.1`) —
which does **not** reach a service bound only to `127.0.0.1`. Two
ways to close the gap:

**C1 — bind the service beyond loopback.** The Java service honours
`NX_SERVICE_BIND` (passed through by the supervisor via env
inheritance):

```bash
# Host: restart the supervisor with a non-loopback bind
nx daemon service stop
NX_SERVICE_BIND=0.0.0.0 nx daemon service start
SVC_PORT=$(nx daemon service status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["port"])')

# Container
docker run --rm \
    --add-host=host.docker.internal:host-gateway \
    -e NX_SERVICE_URL=http://host.docker.internal:$SVC_PORT \
    -e NX_SERVICE_TOKEN=$TOKEN \
    <image>
```

**Security note:** the service has no external TLS (a forward proxy
terminates TLS in production deployments), so a non-loopback bind
exposes a token-authed but plaintext service — plus the
unauthenticated `/health` and `/version` endpoints — to the network.
The service logs a loud warning when it starts this way
(`service_bind_non_loopback`). Intended only for trusted container
networking (e.g. a firewalled dev box); prefer C2 on shared networks.

**C2 — keep the loopback bind, forward with socat.** The service
stays loopback-only; a host-side forwarder bridges the bridge:

```bash
# Host: forward the bridge gateway to the service's loopback port
socat TCP-LISTEN:9999,fork,bind=172.17.0.1 TCP:127.0.0.1:$SVC_PORT

# Container
docker run --rm \
    -e NX_SERVICE_URL=http://172.17.0.1:9999 \
    -e NX_SERVICE_TOKEN=$TOKEN \
    <image>
```

The forwarder also solves the dynamic-port problem: the container
pins the stable forwarded port (`9999`) and only the host-side socat
target needs updating after a service restart. The same shape works
for cross-host access (CI runners reaching a shared service): an SSH
tunnel or VPN that turns cross-host traffic into a loopback
connection at the service end.

## Managed endpoint (cloud mode)

When the host is configured against a managed nexus-service
(`nx config set service_url https://…` + `service_token`, RDR-166),
there is nothing to bridge: the endpoint is network-reachable from
anywhere. Give the container the same pair:

```bash
docker run --rm \
    -e NX_SERVICE_URL=https://api.example-nexus.com \
    -e NX_SERVICE_TOKEN=$TENANT_TOKEN \
    <image>
```

`NX_SERVICE_URL` is used verbatim — scheme included — so `https`
survives end-to-end. An explicitly set `NX_SERVICE_URL` is
authoritative: it is never silently rebound to a locally discovered
supervisor lease.

## Claude Cowork (special case)

Claude Cowork runs a Linux VM via Apple's Virtualization Framework on
macOS. The VM has a **strict network allowlist** — only
`api.anthropic.com`, `pypi.org`, and `registry.npmjs.org` are
reachable. The HTTP paths above **do not apply** inside the VM: it
cannot reach `127.0.0.1` on the host, `host.docker.internal`, or your
LAN.

**The integration model for Cowork is the MCP SDK transport, not the
network.**

What works:

1. Install the conexus plugin in your Claude Code installation:
   `/plugin install conexus@nexus-plugins`. The plugin registers
   `nx-mcp` as an MCP server in your Claude Desktop config.
2. Start the host service: `nx daemon service start`.
3. Open a Cowork session. Claude Desktop passes the configured MCP
   servers into the VM via `--mcp-config` with `"type": "sdk"` — the
   MCP server itself stays running **on the host**; the VM agent's
   tool calls are bridged back through the Anthropic SDK channel, not
   through the network.
4. Inside the VM, the Cowork agent has
   `mcp__plugin_conexus_nexus__memory_put`,
   `mcp__plugin_conexus_nexus__search`, etc. Every call round-trips:
   VM agent → SDK bridge → host nx-mcp → host nexus-service → shared
   state.

State is shared bidirectionally with the host CLI (which routes
through the same service) and any other Cowork sessions or dev
containers pointed at the same service.

You cannot run `nx memory put` from the **shell** inside a Cowork VM
and expect it to reach the host's service — the VM has no network
path to it. Use the MCP tools from the agent, not the CLI from the
shell.

## T1 scratch across containers

T1 scratch is service-backed too (Postgres FTS via `HttpScratchStore`,
RDR-152), but it is **session-scoped**: entries belong to a T1
session id. The RDR-105 sub-agent contract governs who shares a
session:

- **Agent-tool sub-agents** (in-process Task dispatches) share T1 with
  their parent via the parent's MCP scratch tool. No container
  concern.
- **A live MCP session** exports `NX_T1_SESSION` (and
  `NX_T1_SESSION_ID`) to the subprocesses it dispatches; a subprocess
  that inherits them joins the parent's scratch session.
- **A bare CLI in a container** (no inherited session, no host lease
  visible) mints its own persisted CLI-dedicated session id — its
  scratch entries live in the shared service but under its own
  session, invisible to the host session's `nx scratch list`.
- **`NX_T1_ISOLATED=1`** forces an in-process ephemeral scratch
  (ChromaDB `EphemeralClient`), touching the service not at all. This
  is the documented escape hatch when a process must not (or cannot)
  mint a session.

Rule of thumb unchanged from RDR-105: **cross-process findings go to
T2** (`nx memory put` / `memory_put`), which is shared by everything
pointed at the same service. T1 is per-session working memory.

`NX_NEXUS_TENANT` stamps the tenant on scratch requests (default
`default`); for the other stores the tenant is bound to the bearer
token itself (RDR-005 bound tokens).

## Environment variable reference

| Variable | Effect | Default |
|---|---|---|
| `NX_SERVICE_URL` | full base URL of the nexus-service, used verbatim (scheme preserved) — the one knob a container normally needs, honoured by T1/T2/T3/catalog clients alike | supervisor lease (`storage_service_addr.<uid>`), unreachable from a container |
| `NX_SERVICE_TOKEN` | bearer token (required with `NX_SERVICE_URL`) | lease / `pg_credentials`, host-side only |
| `NX_SERVICE_HOST` / `NX_SERVICE_PORT` | host + port halves (always `http`); alternative to `NX_SERVICE_URL` for local-supervisor setups | lease |
| `NX_SERVICE_BIND` | **host-side, read by the Java service**: bind address override (e.g. `0.0.0.0`) for container hosting; non-loopback logs a security warning | `127.0.0.1` |
| `NX_STORAGE_BACKEND` / `NX_STORAGE_BACKEND_<STORE>` | `service` (default) or `sqlite` (legacy opt-out; see below). Per-store overrides global. Invalid values fail loud | `service` |
| `NX_T1_SESSION` / `NX_T1_SESSION_ID` | join an existing T1 scratch session (exported by a live MCP to its subprocesses) | CLI mints its own dedicated session |
| `NX_T1_ISOLATED` | `1` = in-process ephemeral scratch, no service contact | unset |
| `NX_NEXUS_TENANT` | tenant stamped on T1 scratch requests | `default` |
| `NEXUS_CONFIG_DIR` | relocate the entire config/data footprint (lease files, logs, credentials) | `~/.config/nexus` |

`NX_SERVICE_URL` wins over the `NX_SERVICE_HOST`/`NX_SERVICE_PORT`
halves; each env half independently overrides the lease. When nothing
resolves, clients **fail loud** with a message naming the fix — there
is no silent localhost:8080 fallback.

## Diagnostic recipes

### "Is the container reaching the host service?"

```bash
# Inside the container
nx daemon service status         # exits non-zero in-container (no lease) — that's expected; use the round-trip:
nx memory put -p _diag -t ping "test" --ttl=1d
nx memory get -p _diag -t ping   # should round-trip
```

```bash
# On the host
nx memory get -p _diag -t ping   # must see the container's write
```

If the host can't see what the container just wrote, check that the
container's `NX_SERVICE_URL` / `NX_SERVICE_TOKEN` are actually set
and point at the right host address and current port.

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `T2 storage service unavailable: nexus-service endpoint is not resolvable (NX_STORAGE_BACKEND=service)` | Container has neither env pair nor a lease | Set `NX_SERVICE_URL` + `NX_SERVICE_TOKEN` |
| HTTP 401 | Wrong / revoked / rotated-out token | Reissue: `nx service token issue --tenant default` |
| Connection refused | Service down — or the host supervisor restarted and allocated a **new port** | `nx daemon service status --json` on the host; refresh the container's `NX_SERVICE_URL`, or use the socat stable-port pattern |
| Works from macOS container, refused from Linux bridge container | Service bound to loopback; bridge gateway can't reach it | Path C: `NX_SERVICE_BIND` or socat forward |
| HTTP 422 on `voyage-*` collections | Host service started without a Voyage key (local ONNX embedding mode) | Provide `VOYAGE_API_KEY` on the host and restart the service, or use local-mode collections |
| Cowork agent's `mcp_*` tool returns error | Host service down or conexus plugin not enabled | `nx daemon service start`; `/plugin list` to confirm the plugin is active |

### Version skew

`nx daemon service status` reports the running service's
`service_release_version` and warns when it drifts from the installed
binary. For the Python side, pin the same `conexus` version in the
container image as on the host so client and service expectations
match:

```dockerfile
FROM python:3.13-slim
RUN pip install --no-cache-dir conexus==<host nx --version>
```

## Legacy (SQLite backend)

> **Everything in this section applies only when
> `NX_STORAGE_BACKEND=sqlite` is explicitly set** (globally or
> per-store). That is the RDR-152 rollback opt-out, not the default —
> the ETL copies data to Postgres and never deletes from SQLite until
> the Phase-4 decommission, so the flag remains a pure routing switch.
> New setups should not start here.

In SQLite mode the T2 domain stores (notes/plans/taxonomy/…) live in
a SQLite + FTS5 database behind the single-writer **T2 daemon**
(RDR-120/128), and containers must reach that daemon rather than the
HTTP service. The transport still exists in the code
(`src/nexus/daemon/t2_daemon.py` / `t2_client.py` /
`discovery.py`):

- **Start / inspect on the host:** `nx daemon t2 start`,
  `nx daemon t2 status` (prints `uds_path`, `tcp_host`,
  `tcp_port` — the TCP port is dynamic), `nx daemon t2 install
  --autostart` for a LaunchAgent / systemd unit. The conexus plugin's
  SessionStart hook still runs `nx daemon t2 ensure-running --quiet`
  on every Claude session start.
- **Container env:** `NX_T2_SOCK` (UDS path) is checked first, then
  `NX_T2_ADDR` (TCP `host:port`), then the discovery file
  `~/.config/nexus/t2_addr.<uid>`. Set one or the other, not both,
  **and** set `NX_STORAGE_BACKEND=sqlite` so the CLI routes through
  `T2Client` instead of the HTTP stores.
- **TCP path** (works everywhere): same host-reachability rules as
  Path A/B/C above — `host.docker.internal:<port>` on Docker Desktop,
  `--add-host=host.docker.internal:host-gateway` on Linux bridge,
  `127.0.0.1:<port>` with `--network=host`.
- **UDS mount** (native Linux Docker only):
  `-v ~/.config/nexus/sockets:/host-nexus-sockets -e
  NX_T2_SOCK=/host-nexus-sockets/t2.sock` plus
  `--user $(id -u):$(id -g)` — the socket is mode `0o600`, so a UID
  mismatch fails `EACCES` (errno 13). Docker Desktop on macOS /
  Windows cannot pass `connect()` across the VM file-sharing boundary
  (`ENOTSUP`, errno 95); use TCP there.
- **Loopback only:** the daemon binds `127.0.0.1` exclusively and has
  no network auth model (RDR-120 §Out of scope). Non-loopback access
  goes through a host-side forwarder (socat / SSH tunnel), never a
  daemon bind change.
- **Version handshake:** the client handshakes on first connect;
  `T2SchemaVersionMismatchError` means the container's `conexus`
  version differs from the host daemon's — pin matching versions and
  restart the daemon (`nx daemon t2 stop && nx daemon t2 start`).
- **`T2DaemonNotReachableError`** from the CLI means the container
  has no path to the daemon: verify `NX_T2_ADDR` / `NX_T2_SOCK` and
  the daemon's current dynamic port.

T3 never had a daemon transport in this mode either — the retired
`NX_T3_ADDR` ChromaDB daemon address is dead; T3 has served through
the nexus-service since 6.0.

## Related

- `docs/architecture.md` § Storage tiers — the one-service convergence diagram
- `docs/rdr/rdr-152-postgres-java-storage-service.md` — T2/T1 onto the Postgres service; SQLite daemon retirement
- `docs/rdr/rdr-155-pgvector-t3-consolidation.md` — T3 onto pgvector via the service
- `docs/rdr/rdr-149-unified-service-registry-substrate.md` — the shared lease/discovery lifecycle substrate
- `docs/rdr/rdr-120-storage-substrate-split.md` — the legacy SQLite daemon design (historical)
- `src/nexus/db/service_endpoint.py` — the endpoint resolver every HTTP storage client routes through
