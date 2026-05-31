# cc-validation harness

Interactive tmux sandbox that drives a **real** `claude` CLI against fixture
settings/agents/skills/hooks, with no plugin install. It exists to catch
behavior that unit tests and `claude -p` (headless) miss, because real Claude
Code resolves config, PATH, MCP servers, and permissions differently from
both.

```bash
./tests/cc-validation/runner.sh                 # full suite
./tests/cc-validation/runner.sh --scenario 03   # one scenario
tmux -L cc-val-sock attach -t cc-val            # watch a run live
```

## READ THIS FIRST when a scenario "runs but observes nothing"

Most cc-val failures are **harness/config traps, not real findings**. Walk this
list before concluding the feature under test is broken. Each trap below has
silently nullified scenarios for an hour-plus at least once.

| Symptom | Likely trap | Section |
|---|---|---|
| Every scenario fails at startup, pane shows `API Error: 401` | stale OAuth creds | [Auth](#auth) |
| MCP tool "not available", `STUB_LOG` empty | project `.mcp.json` never connects + stub python lacks `mcp` | [MCP servers](#mcp-servers-the-big-one) |
| `can't find pane: cc-val`, suite aborts mid-run | bare `tmux` instead of `_tmux` (wrong socket) | [tmux isolation](#tmux-isolation) |
| Hook "runs" but writes nothing | PreToolUse hooks don't inherit harness env | [Hooks](#hooks) |
| New `nx` subcommand: `No such command` | cc-val uses on-PATH `nx`, not repo source | [on-PATH nx](#on-path-nx) |

## Auth

The sandbox session reads `$TEST_HOME/.claude/.credentials.json` (and
`.env.test` unsets `ANTHROPIC_API_KEY` so this file is the auth source).

**OAuth access tokens are short-lived (~1h) and the refresh token rotates.** A
frozen snapshot at `tests/e2e/.claude-auth/.credentials.json` goes stale within
hours: once the live CLI refreshes, the snapshot's refresh token is invalidated
and the sandbox 401s before the model runs anything. Every scenario then
false-fails.

`runner.sh::provision_credentials` handles this: it provisions from the **live
macOS keychain** (`security find-generic-password -s 'Claude Code-credentials'`)
at runtime and refreshes the on-disk snapshot for the Linux/CI fallback. The
`tests/e2e/.claude-auth/` dir is gitignored, so live tokens are never committed.

A well-written scenario is **fail-closed**: it asserts the *presence* of an
expected marker, so a 401 lands in the `else` branch (a real fail), never a
vacuous pass. Keep it that way.

## MCP servers (the big one)

**Project-scoped `.mcp.json` servers do NOT connect in the interactive
sandbox.** Confirmed repeatedly (2026-05-31): the stub tool reports "not
available" and `STUB_LOG` stays empty (the server is never even spawned). Two
independent root causes, both must be fixed:

### 1. The approval gate is broken for project scope

- The interactive project-MCP approval prompt is effectively non-functional
  (anthropics/claude-code#9189, closed not-planned).
- The enable keys (`enableAllProjectMcpServers`, `enabledMcpjsonServers`) are
  read **only** from `~/.claude.json`, not from `.claude/settings.json` or
  `settings.local.json` (anthropics/claude-code#24657). Putting them in a
  scenario's `settings.json` is silently ignored. Even in `~/.claude.json`
  (root or per-project) they did not reliably connect the server in testing.
- Headless `claude -p --dangerously-skip-permissions` auto-connects project
  servers; interactive `claude` does not. This asymmetry is the "contradicts
  -p finding" note on scenarios 03/05/14c. **Those failures are real Claude
  Code behavior, not harness bugs.**

**The reliable, keypress-free fix is to bypass project scope entirely:** launch
with an explicit MCP config file.

```bash
claude --mcp-config "$TEST_HOME/.mcp.json" --strict-mcp-config ...
```

`--mcp-config` loads the servers directly, sidestepping the `.mcp.json`
approval gate and the `~/.claude.json` per-project state dance. (User-scope
`mcpServers` written into `~/.claude.json` also connects — verified via
`claude mcp list` showing `✓ Connected` — but `--mcp-config` is cleaner and is
the documented path. Note: even with the server connected, a `type: mcp_tool`
**SubagentStart hook** was still not observed to invoke the tool interactively
as of 2026-05-31; that remains an open question, tracked in `nexus-oay5b`.)

### 2. The stub's interpreter lacks `mcp`

`fixtures/stub_server.py` imports `mcp.server.fastmcp`. The system `python3`
(Homebrew) does **not** have it, so the stdio server crashes on import and
never registers any tools, while Claude Code records "MCP server failed to
start" and continues. Point the launcher at an interpreter that has `mcp`:

```json
{ "mcpServers": { "stub": {
    "type": "stdio",
    "command": "<repo>/.venv/bin/python",
    "args": ["<repo>/tests/cc-validation/fixtures/stub_server.py"],
    "env": { "STUB_LOG": "...", "STUB_NAME": "stub" } } } }
```

Use `$REPO_ROOT/.venv/bin/python` (or the installed-tool venv
`~/.local/share/uv/tools/conexus/bin/python3`), never bare `python3`.

### Fast verification (no tmux, seconds)

`claude mcp list` does a live connection check. Use it to validate MCP config
before spending minutes on an interactive run:

```bash
PROBE=/tmp/mcp-probe; mkdir -p "$PROBE/.claude" "$PROBE/proj"
security find-generic-password -s 'Claude Code-credentials' -w > "$PROBE/.claude/.credentials.json"
cat > "$PROBE/.claude.json" <<EOF
{ "hasCompletedOnboarding": true,
  "mcpServers": { "stub": { "type":"stdio",
    "command":"$PWD/.venv/bin/python",
    "args":["$PWD/tests/cc-validation/fixtures/stub_server.py"],
    "env":{"STUB_LOG":"/tmp/mcp-probe-stub.log","STUB_NAME":"stub"} } } }
EOF
(cd "$PROBE/proj" && HOME="$PROBE" claude mcp list)   # -> stub: ... - ✓ Connected
```

## tmux isolation

The harness runs on a **private tmux socket** (`NX_TMUX_SOCKET=cc-val-sock`) via
`lib.sh`'s `_tmux()` wrapper (`command tmux -L "$NX_TMUX_SOCKET" "$@"`). This is
a hard isolation boundary: a kill-server here can never touch the developer's
own tmux.

**Every tmux call in a scenario or helper MUST go through `_tmux`, never bare
`tmux`.** A bare `tmux send-keys -t cc-val` targets the *default* socket, finds
no `cc-val` session, errors `can't find pane: cc-val`, and `set -e` aborts the
whole suite. This bit scenario 16's custom `claude_start_auto` (fixed 2026-05-31).
Grep guard before committing a scenario:

```bash
grep -rnE '(^|[^_])\btmux ' tests/cc-validation/scenarios/   # must be empty
```

## Hooks

**PreToolUse command hooks do NOT inherit the harness env.** A hook that reads
`$HOOK_LOG` from its environment fails silently (`>> ""` -> "No such file",
but bash still exits 0 because the JSON-emitting `python3 -c` succeeds; Claude
accepts the valid JSON; observability dies). Bake the absolute log path into
the hook script via an **unquoted** heredoc (`\$` for runtime expansions,
bare `$HOOK_LOG` for write-time expansion).

**Notification's permission-prompt trigger does not fire** in recent CC: safe
tools are auto-allowed even at `permissions.defaultMode=default`. For
Notification capture use **idle-wait** (~60-90s); the captured
`notification_type` is `idle_prompt`, message `"Claude is waiting for your
input"`.

## on-PATH nx

cc-val runs the **on-PATH `nx`** (`~/.local/bin/nx`), NOT the repo source. A
scenario exercising a NEW `nx` subcommand fails with `No such command` until
you run `scripts/reinstall-tool.sh` to rebuild the shim from the working tree.
Unit tests pass regardless (they import the package directly). This PATH-vs-source
gap is exactly what cc-val exists to catch: reinstall after any subcommand edit
before re-running.

## Known characterization failures (real CC behavior, not harness bugs)

These scenarios fail because they document a real interactive-vs-`-p`
asymmetry, not because the harness is broken. Do not "fix" them by masking:

- **03 / 05 / 14c** — project `.mcp.json` servers don't connect interactively
  (see [MCP servers](#mcp-servers-the-big-one)). 03 additionally probes whether a
  `type: mcp_tool` SubagentStart hook injects; unresolved (`nexus-oay5b`).
- **11 / 14a / 14b** — inline-agent-frontmatter `mcpServers` is a separate
  mechanism from project `.mcp.json`; characterize independently.
- **01 / 06 / 07b** — assert that something does NOT happen (`-p` findings about
  stdout injection / wildcard matching / permission preemption). A pass here
  means absence, by design.

## Patterns worth adopting from `~/git/recording-rig`

`recording-rig` is a mature Claude-Code-driving harness (spec-driven, tmux +
desktop backends). Proven patterns we should port:

- **Pre-seed trust instead of poll-and-press-Enter.** The rig merges trusted
  folders into config *before* launch (`lib/trusted-folders.sh`,
  `localAgentModeTrustedFolders` for the desktop app; the CLI analogue is the
  `~/.claude.json` `projects[path].hasTrustDialogAccepted` key). Removes the
  fragile auth-screen poll loop in `claude_start` (the class scenario 16's
  custom launcher tripped on).
- **Deterministic config injection via flags.** The rig launches
  `claude --settings <file>` (and we should add `--mcp-config <file>
  --strict-mcp-config`) rather than relying on `~/.claude/settings.json`
  discovery. Explicit, reproducible, gate-free.
- **Stop-hook sentinel for turn-completion.** Instead of scraping the TUI for
  "Simmering…"/"esc to interrupt" (our `claude_wait`), the rig has a `Stop`
  hook write `/tmp/$SESSION.turn-end` and blocks on its mtime going quiet
  (`lib/sentinels.sh::sentinel_wait_idle`). Deterministic, immune to TUI
  rendering changes. This is the single biggest robustness upgrade available.
- **Capture the pane ID (`%N`) and target it** instead of `-t <session>`, so
  later splits/active-pane shifts can't misdirect keystrokes.

## Layout

```
runner.sh                       # setup (auth, isolated HOME, tmux), scenario loop
scenarios/NN_*.sh               # one scenario each; source lib.sh helpers
fixtures/stub_server.py         # FastMCP stub (needs an interpreter with `mcp`)
../e2e/lib.sh                   # _tmux, claude_start, claude_prompt, claude_wait, capture
../e2e/.claude-auth/            # gitignored; OAuth snapshot (fallback for non-macOS)
```

Companion gist (older notes): `4a7d73baa4409e02af7af222023b8b9d`.
