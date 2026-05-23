---
title: "RDR-105 Manual Shakeout Checklist"
related-rdr: RDR-105
related-bead: nexus-2ze8
created: 2026-05-07
---

# RDR-105 Manual Shakeout Checklist

This is the operator-facing playbook for the P3.1 sandbox shakeout
(bead `nexus-2ze8`). Phase 3 of RDR-105 flipped the
`NX_T1_NEW_DISCOVERY` default to on, so every Claude Code session
launched from `develop` now runs the new four-branch hybrid-discovery
code path. The shakeout walks the operator through the six T1 cases
the RDR enumerates, plus log monitoring, to confirm the new path
behaves correctly under realistic usage before P4 deletes the
legacy session-record / watchdog / reconcile machinery.

The automated coverage in `tests/test_t1_discovery.py` (47 unit + 3
integration cases including the RF-3 ten-parallel stress test)
exercises the discovery primitives, the constructor, the lifespan
generator, and the dispatcher. What it cannot do is run an
interactive Claude Code session with sub-agents and live MCP. That
is the gap this checklist closes.

## Prerequisites

Run from a clean shell on `develop` after PR #583 has merged.

```bash
# 1. The binary you'll run must be post-P3.
nx --version              # confirm matches the develop tip
git -C ~/git/nexus log -1 --oneline   # should include "RDR-105 P3 flip"

# 2. Snapshot the sessions/ directory and watchdog log so a final
# diff makes any unexpected write visible.
ls ~/.config/nexus/sessions/ > /tmp/sessions-before.txt
cp ~/.config/nexus/logs/watchdog.log /tmp/watchdog-before.log 2>/dev/null \
  || touch /tmp/watchdog-before.log

# 3. Verify the env is clean (production default-on takes effect when
# the variable is absent).
env | grep -E '^(NX_T1_|NEXUS_SKIP_T1=)' || echo "(no overrides)"

# 4. Tail the watchdog log in a side terminal so any new event is
# visible while you exercise the cases.
tail -F ~/.config/nexus/logs/watchdog.log
```

## Case 1: top-level MCP spawn

Open Claude Code in any project. Trigger any MCP tool that touches
T1 (a `nx scratch put`, an `mcp__plugin_conexus_nexus__scratch` call, or
just let the session-scratch flow run normally).

**Verify**

* `~/.config/nexus/t1_addr.<claude_pid>` exists. Find it with
  `ls ~/.config/nexus/t1_addr.*`. Each running Claude Code session
  has exactly one. `cat` it and confirm the contents are
  `host:port\n` (single line, two fields).
* `ls ~/.config/nexus/sessions/` shows zero NEW files vs.
  `/tmp/sessions-before.txt`. The new lifespan does not write
  session records.

## Case 2: Agent-tool sub-agents (in-process)

Inside the running Claude session, dispatch a sub-agent through the
Task tool. Any agent works; `/conexus:research` or `/conexus:analyze` are
typical.

**Verify**

* No additional `t1_addr.*` file appears. Agent-tool sub-agents share
  the parent's MCP via tool dispatch and do not spawn a separate T1
  instance.

## Case 5: Bash-tool sibling discovery via the address file

From a fresh shell terminal (NOT through the Claude UI), run the CLI
as a sibling process:

```bash
nx scratch put "shakeout test entry"
nx scratch list
```

**Verify**

* The entry appears in the list. The CLI walked the PPID chain to
  the immediate `claude` ancestor, read `t1_addr.<claude_pid>`, and
  connected to the same chroma the MCP server owns.
* From inside the same Claude conversation, run a scratch search
  for the entry and confirm it returns. The two paths see the same
  T1 instance.

## Case 6: ephemeral operator dispatch

Inside Claude, invoke any operator that dispatches `claude -p`
(`/conexus:query "what is X"`, `/conexus:analyze`, `nx_answer` via the MCP
tool, etc.).

**Verify**

* The operator returns a result successfully. The subprocess MCP
  inherits `NX_T1_ISOLATED=1` from `_build_dispatch_env(ephemeral=True)`
  and constructs a per-process `EphemeralClient` (Path C). No
  cross-process T1 sharing.

## Cases 3 and 4: share_t1 and owned `claude -p` (smoke only)

**Caveat.** No production caller currently uses `share_t1=True` or
`owned` mode. `claude_dispatch` always sets `ephemeral=True`. The
shapes exist for future wiring but are not on the hot path. A full
end-to-end test requires a wrapper script and is deferred to P3
follow-up if needed.

For a quick smoke check, run from the repo root:

```bash
cd ~/git/nexus
uv run python -c "
from nexus.operators.dispatch import _build_dispatch_env
print('share_t1:', sorted(k for k in _build_dispatch_env(share_t1=True, parent_session_id='shakeout').items() if 'T1' in k[0] or 'SKIP' in k[0]))
print('owned   :', sorted(k for k in _build_dispatch_env(parent_session_id='shakeout').items() if 'T1' in k[0] or 'SKIP' in k[0]))
"
```

**Verify**

* `share_t1` output contains `NX_T1_HOST` and `NX_T1_PORT` matching
  the address in `t1_addr.<claude_pid>`.
* `owned` output strips `NX_T1_HOST` / `NX_T1_PORT` /
  `NX_T1_ISOLATED` / `NEXUS_SKIP_T1` so the receiving subprocess MCP
  spawns its own chroma.

## Wrap-up

Close Claude Code (clean exit; either `/exit` or terminal close).

```bash
# 1. Address file cleanup. After a clean exit no t1_addr files
# should remain for the closed session's PID.
ls ~/.config/nexus/t1_addr.* 2>/dev/null
# Each remaining file should belong to a still-running Claude.
# Cross-check with `ps`: pgrep -f "^claude" | sort

# 2. Sessions directory unchanged.
diff /tmp/sessions-before.txt <(ls ~/.config/nexus/sessions/)
# Expect empty diff. Pre-existing legacy files (from before the
# upgrade) are untouched; P5 will provide a one-time post-upgrade
# sweep.

# 3. Watchdog log diff. The new lifespan does not spawn the
# watchdog, so the log should be byte-identical.
diff /tmp/watchdog-before.log ~/.config/nexus/logs/watchdog.log
# Expect empty diff. ANY new chroma_died_externally event is a
# regression and must be filed as a bug bead before merging P4.
```

## Pass criteria

- [ ] Cases 1, 2, 5, 6 all completed without errors.
- [ ] `t1_addr.<claude_pid>` was written at session start and
      unlinked at session end.
- [ ] Zero new entries in `~/.config/nexus/logs/watchdog.log`.
- [ ] Zero new files in `~/.config/nexus/sessions/`.
- [ ] (Optional) Cases 3 and 4 env-builder smoke matched the
      expected shape.

If every item passes, close `nexus-2ze8` with a one-line summary,
then close the parent `nexus-xf5r`. P4 (`nexus-jnx7`) is the next
critical-path bead and deletes the legacy machinery.

If any item fails, open a bug bead under epic `nexus-d73b`, leave
`nexus-xf5r` and `nexus-2ze8` open, and do not proceed to P4 until
the bug is closed.

## Why these cases?

The six cases come from RDR-105 §"The six T1 cases":

1. Top-level MCP, owns the session.
2. Agent-tool sub-agent, same process as parent.
3. `claude -p` shared (`share_t1=True`).
4. `claude -p` owned (default).
5. `claude -p` ephemeral (`NX_T1_ISOLATED=1`).
6. Bash-tool / shell `nx scratch` / hooks.

The exit criterion in the RDR's §"Migration plan" Phase 3 is "T1
round-trip works across all six cases" plus the 10-parallel stress
test (already shipped as `TestE2EParallelStress` in
`tests/test_t1_discovery.py`, automated in CI).

## Related artifacts

* RDR: `docs/rdr/rdr-105-t1-chroma-architecture-env-passdown.md`
* Epic: bead `nexus-d73b`
* P3 parent: bead `nexus-xf5r`
* P3.1 (this checklist): bead `nexus-2ze8`
* P3.2 (automated stress): bead `nexus-1q88` (closed)
* PRs: #581 (P1 spike), #582 (P2 productionize), #583 (P3 default
  flip), all merged into `develop`.
* Triggering issue: GH #579 (still open until P4 deletes the
  legacy machinery).
