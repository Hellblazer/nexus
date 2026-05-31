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
approval gate and the `~/.claude.json` per-project state dance. **This is
implemented**: `runner.sh`'s `claude_start` wrapper computes
`--mcp-config $TEST_HOME/.mcp.json --strict-mcp-config` whenever a scenario
declares a `.mcp.json`, and normalizes the launcher python (below). Verified
2026-05-31: scenarios 03 and 05 went red→green. Once the server is genuinely
connected this way, a `type: mcp_tool` **SubagentStart hook** DOES fire and
inject `additionalContext` interactively (03 passes) — the earlier "doesn't
inject" observation was purely the server never connecting, not a hook gap.
(User-scope `mcpServers` in `~/.claude.json` also connects — `claude mcp list`
shows `✓ Connected` — but `--mcp-config` is cleaner and gate-free.)

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

**This applies to agent-frontmatter inline `mcpServers` too — and the
`--mcp-config` wrapper does NOT reach them.** The wrapper normalizes
`python3`→venv only for `$TEST_HOME/.mcp.json`. A bare `command: python3` in an
agent's frontmatter resolves to whatever `python3` is on PATH (often homebrew,
no `mcp`), so the inline server crashes on import and the tool silently never
loads — and because PATH resolution varies, it presents as flakiness, not a
clean failure (this was scenario 14a's root cause). Pin agent-frontmatter
inline servers to `$REPO_ROOT/.venv/bin/python` directly.

**Diagnostic — stub startup markers.** `fixtures/stub_server.py` logs an
`{"event":"process_launched","python":...}` line before importing `mcp`, then
either `mcp_import_failed` (interpreter lacks `mcp`) or `mcp_imported_ok`. Grep
`STUB_LOG` for `"event"` to tell apart the three cases: no marker = the server
was never spawned by CC; `mcp_import_failed` = spawned with the wrong python;
`mcp_imported_ok` = healthy. This is how 14a's bare-`python3` crash was found.

### Deferred MCP tools — the regime MUST account for this

In interactive Claude Code, `mcp__*` tools are **deferred**: they do NOT appear
in the tool list and are NOT callable until `ToolSearch` loads their schema. The
TUI shows it explicitly ("I'll load the tool schema first, then call it"). This
has three consequences every MCP scenario must respect, and which the original
scenarios did NOT (root-caused 2026-05-31 — it was the cause of scenario 16's
mis-labelled "allow-rule anomaly" and scenario 14a's flakiness):

1. **The first call after launch races schema discovery.** A bare
   `"Call mcp__stub__X"` issued immediately can land `tool_ran=0` because the
   model tries to call before the schema is loaded. A fixed `sleep` does NOT fix
   it (a 12s settle didn't); a WARMUP TURN does — issue a throwaway "list your
   mcp__ tools" prompt first to force schema load, then make the measured call.
   See scenario 16.
2. **Self-listed inventory is unreliable.** Asking an agent to write its tool
   inventory and grepping for `mcp__stub__` gives false negatives: a deferred
   tool isn't listed until loaded. Assert on the forensic call landing in
   `STUB_LOG` (the tool actually ran), or treat "listed OR called" as proof of
   load. Never treat absence-from-inventory as "server didn't load". See 14a.
3. **Subagents must load the schema too.** An agent that calls an MCP tool
   should be instructed to load the deferred schema first; otherwise its first
   call races the same way. See 14a's agent prompt.

**Convention for any new MCP scenario:** warmup (list tools) before the measured
call, instruct subagents to load the schema first, and assert on `STUB_LOG`
forensics, not self-listed inventory. NOTE (2026-05-31): scenarios 05 and 07
still use bare direct calls without a warmup — they pass on model auto-load but
carry latent deferred-tool flakiness and should adopt the warmup convention.

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

## Test validity — the vacuous-pass trap (read before adding a scenario)

The 2026-05-31 MCP-connection fixes exposed that several scenarios were passing
for the WRONG reason: a negative assertion ("tool rejected" / "hook never fires"
/ "wildcard did not match") passed because the MCP server never connected, not
because the behavior under test occurred. Such a scenario would stay green with
the entire MCP stack broken. Two rules came out of the rework:

1. **A negative conclusion needs a positive signal.** Don't infer "X did not
   happen" from absence. Require an explicit token (the subagent emits
   `NO-MARKER` / `NESTED-SPAWN-BLOCKED`) or a connected-but-denied state. See
   scenarios 01 and 17 for the canonical shape.
2. **Assert on forensic side-effects, not model output.** A tool actually
   running (`STUB_LOG`), a file written (`agent_tools.txt`) is deterministic. The
   model quoting a sentence, echoing a count, or self-reporting its tool list is
   NOT — those produce flaky pass/fails. When you must use the interactive path,
   ask for a fixed confirmation TOKEN, not a verbatim quote (scenario 18), and
   require the precondition tool-run before trusting a hook/permission verdict
   (scenarios 07, 16).

Reworked for validity 2026-05-31: 06, 07, 11, 14b, 16, 18, 19A, 23. Verified
valid as-is: 01, 17 (explicit-token), 02–05/12/13/14a/14c/15/20–22/24–26
(forensic/positive). Latent (flagged, not yet fixed): 09/10 branch-1 token grep
matches the user's own turn-1 message in scrollback.

## Known real findings (NOT harness bugs — do not mask)

The harness correctly surfaces these. A green here would be the bug:

- **06** — infix wildcard `mcp__plugin_*__*` MATCHES across the `__` boundary
  (contradicts the older `-p` finding).
- **07** — under `skipDangerousModePermissionPrompt` the PermissionRequest gate
  is bypassed entirely (tool auto-runs both with and without an allow rule, the
  hook never fires) — so a PermissionRequest auto-approver is redundant.
- **11** — inline-agent `mcpServers` are NOT scoped to the subagent: both parent
  and subagent can call the stub (forensic, via parent call-attempt).
- **14b** — plugin-shipped agents' inline `mcpServers` do NOT load (vs 14a
  project-level, which do). Passes as a characterization of the known limitation.

## Honest non-deterministic failures (do not paper over)

These two fail correctly when a precondition is not met, rather than fabricate a
green:

- **16** — ANOMALY under investigation: in `--permission-mode=auto` the MCP tool
  runs WITHOUT an allow rule (`tool_ran_b=1`) but NOT with one (`tool_ran_a=0`),
  consistently across runs; a 12s MCP settle did not change it, so it is not a
  connection-timing flake. The `tool_ran` guard refuses to assess the hook gate
  when the tool never ran. Real finding, needs a dedicated investigation.
- **14b** — depends on the model actually dispatching the plugin-shipped agent;
  when it doesn't (`agent_ran=0`) the run is honestly indeterminate, not a pass.

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
