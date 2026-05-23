# Routing Hooks (sn) — RDR-125

PreToolUse hooks shipped in the sn plugin. The framework
(`_lib.py`, `_run_python_hook.sh`) is canonical in nx and **vendored**
here byte-for-byte; `tests/test_routing_lib_drift.py` in the nexus
monorepo refuses any divergence.

## What lives here (and why)

Per RDR-125, each plugin owns the routing rules whose deny message
redirects to a tool the plugin ships. sn ships Serena MCP tools
(`mcp__plugin_sn_serena__*`); the
`grep_for_symbols_redirects_to_serena` rule lives here because its
deny message names those tools.

Installing nx without sn means this hook does not exist -- which is
the correct default. Users without Serena get no surprise denial of
their `grep` invocations.

## Files

- `_lib.py` -- vendored from nx canonical. **Do not edit in isolation.**
  Updating the framework means updating both copies atomically.
- `grep_for_symbols_redirects_to_serena.py` -- the routing rule. Uses
  the standard `sys.path.insert(0, dirname); import _lib` pattern.
- `registry.yaml` -- the sn-side registry. Schema mirrors
  `conexus/hooks/scripts/routing/registry.yaml`.

The `_run_python_hook.sh` wrapper at `sn/hooks/scripts/_run_python_hook.sh`
is also vendored from nx canonical.

## Cumulative-cap accounting (RDR-121 + RDR-125)

The 4-hook cap on PreToolUse:Bash is an **aggregate across all
installed plugins**, not per-plugin. See
`conexus/hooks/scripts/routing/README.md` for the breakdown. Adding a
fifth routing rule in any plugin requires consolidation or a budget
revision in a successor RDR.

## Ownership-rule scope

The "hook lives in the plugin that ships the redirect target" rule is
stated for single-target hooks (one `mcp__plugin_<owner>_*` tool in
the deny message). Multi-target hooks whose deny message names tools
from two or more plugins are out of scope for RDR-125; the first
author who needs that case must file a follow-on RDR.
