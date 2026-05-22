# Container integration

Running nexus from inside a container — Docker dev container, Claude
Cowork VM, CI agent, multi-agent Co-Work session — and sharing T2 /
T3 state with the host CLI Claude and any other nexus consumers.

This page assumes RDR-120 4.34.1+ is installed on the host. Earlier
releases ran every consumer against its own SQLite file and silently
diverged across containers; the daemon model in 4.34.x is what makes
shared state real.

## TL;DR

The integration model has **one rule**: every nexus consumer talks to
the host's T2 / T3 daemons. The container reaches the daemons either
via loopback TCP, a mounted Unix socket, or via Anthropic's SDK
transport (Claude Cowork). The CLI commands in 4.34.1+ honour this
automatically; tests that previously opened the SQLite file directly
have been migrated to route through `T2Client`.

Picking a path:

| Host platform | Container runtime | Use |
|---|---|---|
| any | **Claude Cowork** | SDK transport (zero config; see § Cowork below) |
| Linux | Docker / Podman, host-network | TCP loopback (`--network=host` + `NX_T2_ADDR=127.0.0.1:<port>`) |
| Linux | Docker / Podman, default-bridge | TCP via host-gateway (`--add-host=host.docker.internal:host-gateway`) |
| Linux | Docker / Podman, network-isolated | UDS mount (`--user $(id -u):$(id -g)` + `NX_T2_SOCK=...`) |
| macOS | Docker Desktop | TCP via `host.docker.internal` (UDS unsupported across the macOS↔VM kernel boundary) |
| Windows | Docker Desktop (WSL2) | TCP via `host.docker.internal` (UDS unsupported across the WSL2 boundary) |

## Prerequisites on the host

```
# Always
nx daemon t2 start

# Local-mode T3 only (cloud-mode T3 is already HTTP-served; no daemon)
nx daemon t3 start
```

Confirm the daemons:

```
$ nx daemon t2 status
T2 Daemon Status
  format_version: 1
  uds_path:       /Users/<user>/.config/nexus/sockets/t2.sock
  tcp_host:       127.0.0.1
  tcp_port:       49177       ← the dynamic port the container will reach
  pid:            12345
  daemon_version: 4.34.1
```

The TCP port is dynamic — read it via `nx daemon t2 status` or
`cat ~/.config/nexus/t2_addr.$(id -u)`. The container needs that
value.

## Path A: TCP loopback (most portable)

The T2 daemon binds `127.0.0.1:<port>` (always) and a Unix socket at
`<config-dir>/sockets/t2.sock`. For container-to-host traffic the
TCP loopback is the simplest path; it's the only one that works on
macOS / Windows Docker Desktop.

### macOS Docker Desktop

```bash
PORT=$(nx daemon t2 status | grep tcp_port | awk '{print $2}')
docker run --rm \
    -e NX_T2_ADDR=host.docker.internal:$PORT \
    -e NX_T3_ADDR=host.docker.internal:<t3_port>   # local-mode T3 only
    <image-with-conexus>
```

`--network=host` does NOT work on macOS Docker Desktop — the
container's `127.0.0.1` stays in the container's own namespace.
Docker Desktop's built-in `host.docker.internal` DNS name reaches
the macOS host's loopback through the VM gateway.

### Linux (default-bridge network)

```bash
docker run --rm \
    --add-host=host.docker.internal:host-gateway \
    -e NX_T2_ADDR=host.docker.internal:$PORT \
    <image>
```

The `--add-host=host.docker.internal:host-gateway` flag is the
documented Docker pattern that synthesizes the same DNS name the
macOS Desktop runtime provides natively.

### Linux (host-network mode)

```bash
docker run --rm --network=host \
    -e NX_T2_ADDR=127.0.0.1:$PORT \
    <image>
```

The container shares the host's network namespace. Simplest from a
networking standpoint, but the container loses network isolation;
prefer default-bridge unless you have a specific reason for
host-network.

## Path B: Unix-socket mount (Linux native)

The T2 daemon's UDS at `~/.config/nexus/sockets/t2.sock` is created
mode `0o600` owned by the daemon-process UID. Mounting it into a
container is the safer transport (kernel-level UID gate; no TCP
attack surface) but requires that the container runs as the same
UID as the daemon process and that the host kernel can pass UDS
state into the container.

**Working only on native Linux Docker.** Docker Desktop on macOS /
Windows uses a Linux VM under the hood; the macOS / Windows kernel
cannot transmit `connect()` calls across the file-sharing layer
(returns `ENOTSUP` / errno 95).

```bash
docker run --rm \
    --user $(id -u):$(id -g) \
    -v ~/.config/nexus/sockets:/host-nexus-sockets \
    -e NX_T2_SOCK=/host-nexus-sockets/t2.sock \
    <image-with-conexus>
```

`--user $(id -u):$(id -g)` is non-negotiable. Without it the
container runs as UID 0 (root) inside its user namespace, and the
socket's `0o600` permission gate fires `EACCES` (errno 13). The
CLI's error message names the fix.

T3 has no UDS path; the T3 daemon is HTTP-only (chromadb upstream
contract). Use the TCP path for T3 even when T2 goes through UDS.

## Path C: Operator-side forward (fallback)

When neither host-gateway nor UDS-mount is acceptable, an operator
can forward the host's loopback port into the container via
`socat`, `ssh -L`, or a sidecar proxy:

```bash
# socat sidecar listening on a routable address
socat TCP-LISTEN:9999,fork,bind=172.17.0.1 TCP:127.0.0.1:$PORT

# Container points at the forwarded endpoint
docker run --rm \
    -e NX_T2_ADDR=172.17.0.1:9999 \
    <image>
```

This is documented for completeness; if you're reaching for this,
prefer one of the supported paths first. Non-loopback TCP from the
daemon itself is on RDR-120's §Out of scope banlist (no auth model
exists yet) — Path C uses a host-side forwarder, not a daemon
binding change.

## Claude Cowork (special case)

Claude Cowork runs a Linux VM via Apple's Virtualization Framework
on macOS. The VM has a **strict network allowlist** — only
`api.anthropic.com`, `pypi.org`, and `registry.npmjs.org` are
reachable. The TCP loopback and UDS-mount paths above **do not
apply** to Cowork: the VM cannot reach `127.0.0.1`,
`host.docker.internal`, or mount arbitrary host paths.

**The integration model for Cowork is the MCP SDK transport, not
the network.**

What works:

1. Install the nx plugin in your Claude Code installation:
   `/plugin install nx`. The plugin registers `nx-mcp` as an MCP
   server in your Claude Desktop config.
2. Start the daemons on the host: `nx daemon t2 start` (and
   `nx daemon t3 start` if local-mode).
3. Open a Cowork session. Claude Desktop **passes the configured
   MCP servers into the VM via `--mcp-config` with
   `"type": "sdk"`** — the MCP server itself stays running on the
   host; the VM agent's tool calls are bridged back through the
   Anthropic SDK channel, not through the network.
4. Inside the VM, the Cowork agent has
   `mcp__plugin_nx_nexus__memory_put`, `mcp__plugin_nx_nexus__search`,
   etc. available. Every call round-trips: VM agent → SDK bridge →
   host nx-mcp → host T2/T3 daemons → shared state.

State is shared bidirectionally with the host CLI Claude (which
routes through the same daemons via w6txl's CLI migration) and any
other Co-Work sessions or dev containers running against the same
host daemons.

You cannot run `nx memory put` from the **shell** inside a Cowork
VM and expect it to reach the host's daemons — the VM has no
network path to them. Use the MCP tools from the agent, not the
CLI from the shell.

## Environment variable reference

| Variable | Effect | Default |
|---|---|---|
| `NX_T2_ADDR` | TCP host:port for the T2 daemon | discovery file |
| `NX_T2_SOCK` | UDS path for the T2 daemon | discovery file |
| `NX_T3_ADDR` | TCP host:port for the T3 daemon | discovery file |
| `NX_PYTEST_DAEMON_MODE` | (tests only) skip the conftest direct-mode pin | unset |

`NX_T2_ADDR` and `NX_T2_SOCK` are mutually exclusive — set one or
the other. When both env vars are unset, the T2 client falls back
to the discovery file at `~/.config/nexus/t2_addr.<uid>` (mounted
into the container if the operator chooses, otherwise unreachable).

## T1 (scratch) isolation across containers

T1 is **process-local by design** (RDR-105). Each container runs
its own per-process ephemeral chromadb backend. Cross-container T1
sharing is **not supported** — containers must use T2
(`memory_put` / `memory_get`) for any state they want visible
across processes.

This is the right model for the use cases T1 was built for
(session scratch, per-agent working memory). If two Cowork agents
need to coordinate, they coordinate through T2; T1 is for the
single-agent process's own short-term notes.

## Diagnostic recipes

### "Is the container reaching the host daemon?"

```bash
# Inside the container
nx daemon t2 status              # discovers via env var or addr file
nx memory put -p _diag -t ping "test" --ttl=1d
nx memory get -p _diag -t ping   # should round-trip
```

```bash
# On the host
nx memory get -p _diag -t ping   # must see the container's write
```

If the host can't see what the container just wrote, the container
is talking to a different (container-local) SQLite. Check that the
container's `NX_T2_ADDR` / `NX_T2_SOCK` is actually set, and that
the daemon's address matches.

### Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `T2DaemonNotReachableError` from CLI | Container has no path to daemon | Set `NX_T2_ADDR` to the host's daemon TCP port |
| `[Errno 95] Operation not supported` on UDS | macOS/Windows Docker Desktop VM-boundary limit | Switch to the TCP path |
| `[Errno 13] Permission denied` on UDS | UID mismatch | `--user $(id -u):$(id -g)` |
| `[Errno 111] Connection refused` on TCP | Daemon not running, or wrong port | `nx daemon t2 status` on the host; verify port matches `NX_T2_ADDR` |
| Container writes succeed but host can't see them | Container falling back to local SQLite | (4.34.0 only) Upgrade to 4.34.1+; verify CLI is using `t2_handle()` |
| Cowork agent's `mcp_*` tool returns error | Host daemon down or nx plugin not enabled | Start daemons; `/plugin list` to confirm nx plugin is active |

### `T2SchemaVersionMismatchError`

The CLI handshakes with the daemon on first connect (RDR-120 P3b).
A mismatch means the container's installed `conexus` version
differs from the host daemon's version:

```
T2 schema version mismatch: client built against '4.34.1',
daemon reports '4.32.0'. Re-install conexus so both sides match,
then restart the T2 daemon: `nx daemon t2 stop && nx daemon t2 start`.
```

Pin the same conexus version in your container image as on the
host:

```dockerfile
# Container image
FROM python:3.13-slim
RUN pip install --no-cache-dir conexus==4.34.1
```

```bash
# Host
nx --version    # must match the container's pinned version
```

## Why non-loopback TCP isn't a path

The T2 daemon binds 127.0.0.1 exclusively. Binding to 0.0.0.0
would let any host on the network reach the daemon — and the
substrate has **no authentication model**. RDR-120 §Out of scope
puts both "non-loopback TCP" and "network auth" on the moratorium
banlist; they would require an RDR addressing the security model
(at minimum: peer credentials, mutual TLS, or HMAC-signed frames).

If you need cross-host nexus access (CI runners reaching a
shared daemon, multi-machine teams), the right primitive is an
SSH tunnel or VPN that turns the cross-host traffic into a
loopback connection at the daemon end. That's Path C above.

## Related

- `docs/architecture.md` § Storage tiers — daemon-mediated substrate diagram
- `docs/rdr/rdr-120-storage-substrate-split.md` — the design + soak phasing
- `CHANGELOG.md` [4.34.0] / [4.34.1] — substrate split release notes
- Container MVV receipts in T2 — `120-container-mvv-tcp-receipt`,
  `120-container-mvv-uds-receipt`, `120-container-pypi-smoke-receipt`
