# Tuplespace Environment Variables (RDR-110)

Reference for the environment variables the tuple space consumer surface
honours. The `nx tuplespace` CLI, the MCP tool surface, and the
session-start banner all read these.

## NX_STORAGE_MODE

Values: `direct`, `daemon`. **Default: `daemon`** (since the
2026-05-17 cutover, RDR-112 P6.3 / nexus-507q). An unset env
resolves to `daemon`.

When the resolved mode is `daemon`, the daemon owns `tuples.db`
and runs the single SQLite writer. The CLI then refuses mutating
subcommands (`out`, `read`, `take`, `ack`, `nack`, `stats`) rather
than opening a competing connection. Read-only introspection
(`list-subspaces`, `show-schema`) remains available because it only
loads the registry from YAML.

In direct mode (operator sets `NX_STORAGE_MODE=direct` explicitly),
the CLI opens the SQLite file directly. Direct mode remains
available indefinitely as the debug fallback for hosts that don't
run the daemon.

Resolution lives in `nexus.db.default_storage_mode()` and
`nexus.db.is_daemon_mode()`; all CLI commands, the MCP server, the
hook bridge, and the doctor checks route through these helpers
post-cutover.

## NX_TUPLES_DB

Path override for `tuples.db`. Default is
`<nexus_dir>/tuples.db`, where `nexus_dir` resolves from
the config file (`~/.config/nexus` by default).

Useful in tests to redirect to `tmp_path / "tuples.db"`. Honoured by the
CLI; the MCP server uses the config-derived path only.

## NX_TUPLESPACE_BUILTIN_DIR

Path override for the bundled subspace YAML directory. Default is the
repo's `nx/tuplespace/builtin/`. Tests use this to inject a fixture
directory with stripped-down schemas.

## NX_T1_HOST, NX_T1_PORT, NX_T1_ISOLATED

T1 chroma server addressing. The tuple space does not consume T1
directly, but the MCP tool layer that wraps it shares the same process,
so misconfiguration here can starve the MCP server before tuple tools
load. `NX_T1_ISOLATED=1` skips the per-session chroma spawn entirely.

`NEXUS_SKIP_T1` is honoured as a deprecated alias for `NX_T1_ISOLATED`
through the 4.27 to 4.28 cycle (RDR-105).

## NX_BRIDGE_DISABLE

Disables the hook-to-tuple bridge (RDR-111). When set to any truthy
value, Claude Code hook events stop landing in the `events/hook`
subspace. The bridge is the upstream feed for several event subscribers;
with it disabled, those subscribers see no input and may appear stuck.

Falsy tokens (bridge runs): unset, `""`, `"0"`, `"false"`, `"False"`.
Any other non-empty value (`"1"`, `"true"`, `"yes"`, ...) disables the
bridge. The gate sits after the `CLAUDECODE` check in `emit()`, so it
covers both the daemon-routed and direct-fallback paths. Hook protocol
responses (e.g. PermissionRequest transparent-allow) are not silenced —
disabling tuple emission must not break the user's tool flow.

## NX_HOOK_DEBUG, NX_TIMEOUT, BD_TIMEOUT

Diagnostics for the session-start hook (`nx/hooks/scripts/session_start_hook.py`).
`NX_HOOK_DEBUG=1` writes per-step traces to stderr. `NX_TIMEOUT` caps
the `nx` subprocess calls (including the tuplespace banner read) at
the given seconds; `BD_TIMEOUT` caps `bd` calls. Defaults are 10 s and
5 s.

## Quick reference

| Variable | Default | Effect |
|---|---|---|
| `NX_STORAGE_MODE` | `daemon` | `direct` opts back into the legacy in-process opens (post-cutover, nexus-507q) |
| `NX_TUPLES_DB` | `<nexus_dir>/tuples.db` | path override |
| `NX_TUPLESPACE_BUILTIN_DIR` | `nx/tuplespace/builtin/` | subspace YAML directory |
| `NX_T1_ISOLATED` | unset | skip T1 chroma session spawn |
| `NX_BRIDGE_DISABLE` | unset | disables the hook-to-tuple bridge |
| `NX_HOOK_DEBUG` | `0` | session-start hook debug trace |
| `NX_TIMEOUT` | `10` | `nx` subprocess timeout for the session hook |
