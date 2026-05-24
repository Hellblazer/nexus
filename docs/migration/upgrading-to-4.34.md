# Upgrading to conexus 4.34.x — RDR-120 storage substrate split

**TL;DR — one new command after upgrading:**

```bash
uv tool update conexus
nx daemon t2 install --autostart       # NEW — register T2 daemon at login
# (local-mode T3 only)
nx daemon t3 install --autostart       # NEW — register T3 daemon at login
```

If you use the conexus plugin in Claude Code, the SessionStart hook
auto-spawns the daemon on every session start. The `install --autostart`
command above is still recommended if you also use `nx` directly from
the shell, because it survives across reboots independent of Claude
Code lifecycle.

That's it for the user-facing upgrade. The rest of this page explains
**why** the change happened and what it means for multi-process and
container scenarios.

## What changed

Pre-4.34.0, every nexus consumer (CLI, MCP server, dev containers,
multiple Claude Code sessions) opened its own SQLite connection to the
T2 database and relied on SQLite's WAL to arbitrate writes. This worked
for single-host single-process usage but had real failure modes:

- Two Claude Code sessions racing on the same T2 → SQLite busy retries,
  occasional SQLITE_BUSY escapes under load.
- A dev container with conexus installed couldn't share state with the
  host — each side opened a different SQLite file.
- Claude Cowork agents had no path to host state at all.

RDR-120 split storage into daemon processes:

- **T2 daemon** (`nx daemon t2`): one process owning all SQLite writes
  to memory.db. Every consumer talks to it via UDS (Linux) or 127.0.0.1
  TCP loopback (macOS Docker Desktop / Linux default-bridge / cross-
  process on the same host).
- **T3 daemon** (`nx daemon t3`, local mode only): wraps the ChromaDB
  server lifecycle so multiple consumers share the same vector store.
  Cloud-mode T3 already used HTTP transport; no change there.
- Both daemons publish their address via discovery files at
  `~/.config/nexus/t2_addr.<uid>` / `t3_addr.<uid>`. The client honours
  `NX_T2_ADDR` / `NX_T2_SOCK` / `NX_T3_ADDR` env-var overrides for
  container scenarios.

## What you'll see if you skip the upgrade step

If you upgrade conexus but don't run `nx daemon t2 install --autostart`,
the very next CLI command (or MCP tool call from Claude Code) will
fail with:

```
T2DaemonNotReachableError: No t2 daemon discovery resolved.
Tried env-var (NX_T2_ADDR) and discovery file (~/.config/nexus/t2_addr.<uid>).
Start with: `nx daemon t2 start`.
```

The recovery is exactly what the error message says:

```bash
nx daemon t2 ensure-running             # one-shot spawn (idempotent)
# OR for durable autostart:
nx daemon t2 install --autostart
```

Both options work; the `install --autostart` path is the recommended
one because it survives reboots and crashes (`KeepAlive=true` on
macOS LaunchAgent, `Restart=on-failure` on Linux systemd user-unit).

## Container and Claude Cowork users

The substrate split is what makes container ↔ host state sharing
actually work. Before 4.34.x, you got two copies of state; after,
you get one arbitrated copy.

- **Dev container with conexus installed** (e.g., `python:3.13-slim
  + pip install conexus==4.34.2`): set `NX_T2_ADDR=host.docker.internal:<port>`
  (read the port from `nx daemon t2 status` on the host) and CLI / MCP
  calls round-trip through the host daemon. Symmetric state with host
  CLI Claude.

- **Claude Cowork**: zero config. Cowork's MCP-SDK transport bridges
  the host's `nx-mcp` server into the VM, so agents inside Cowork call
  MCP tools that round-trip through host's daemons. Same state as host
  CLI Claude + dev containers + multiple Cowork sessions.

See [Container Integration](../container-integration.md) for the full
operator-facing matrix (TCP / UDS / SDK-bridge per platform).

## Version compatibility

Conexus 4.34.x clients refuse to talk to mismatched daemons via the
**schema-version handshake** (RDR-120 P3b). A 4.34.0 client against a
4.33.x daemon gets:

```
T2SchemaVersionMismatchError: Client version X does not match daemon Y.
Restart the daemon: `nx daemon t2 stop && nx daemon t2 start`.
```

After `uv tool update conexus`, **always restart the daemon** so it
picks up the new binary. The LaunchAgent / systemd unit will respawn
the daemon at the new version automatically when stopped:

```bash
launchctl kickstart -k gui/$(id -u)/com.nexus.t2     # macOS
# or
systemctl --user restart nexus-t2.service             # Linux
```

## Empirical validation

The substrate has been stress-tested under realistic load before this
upgrade went GA:

- 10,000 monotonic-contiguous writes across 10 parallel cross-process
  workers (scenario 1).
- 7,000 mixed put/get/search/delete ops across host CLI + simulated
  container clients (scenario 2).
- 53,793 pre-kill writes durable across `kill -9 + restart` (scenario 3).
- Schema-handshake mismatch handled correctly under load (scenario 4).
- Catalog rebuild + concurrent T2 writers; catalog projection
  bit-equal to source (scenario 5).

Receipts in T2 under `nexus_rdr / 120-stress-*-receipt`. See RDR-120
§Phase 6 for the matrix details.

## Rollback

If you need to roll back to a pre-4.34 version, the schema is
backward-compatible (no on-disk format change — only the access
arbitration changed). Stop the daemon, uninstall the autostart unit,
and downgrade:

```bash
nx daemon t2 stop
nx daemon t2 uninstall --autostart
uv tool install conexus==4.33.1 --force
```

Your existing T2 / T3 / catalog data is preserved across the
downgrade. Note that 4.33.x and earlier did NOT have multi-process
arbitration, so post-rollback you regain the original WAL-race
behaviour.
