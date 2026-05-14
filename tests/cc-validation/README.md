# cc-validation harness

Scenario-driven Claude Code feature-validation rig. Each scenario in `scenarios/` is a bash script that writes a settings.json + .mcp.json, launches `claude` in a tmux pane, sends a prompt, and asserts on captured pane output and side-effect log files.

Companion external doc: [gist `4a7d73…`](https://gist.github.com/Hellblazer/4a7d73baa4409e02af7af222023b8b9d) — the generalised pattern, kept current with this directory's findings.

## Quick start

```bash
./tests/e2e/auth-login.sh                  # cache Claude Code OAuth creds (Keychain)
./tests/cc-validation/runner.sh            # run every scenario in order
./tests/cc-validation/runner.sh --scenario 03   # one scenario only
KEEP_TEST_HOME=1 ./tests/cc-validation/runner.sh --scenario 13   # leave TEST_HOME intact for inspection
```

## Layout

```
tests/cc-validation/
├── runner.sh                 # set up isolated $HOME, start tmux, run scenarios in order
├── fixtures/
│   └── stub_server.py        # FastMCP stub — exposes `ping`, `record`, `emit_inject_json`
├── scenarios/                # NN_<slug>.sh — see naming conventions below
└── run-ca8-spike.sh          # standalone RDR-111 CA-8 spike (release-sandbox tmux pattern)

tests/e2e/
├── lib.sh                    # tmux primitives: send_keys / capture / poll_for / claude_start /
│                             #   claude_prompt / claude_wait / claude_exit / pass / fail / …
├── auth-login.sh             # macOS Keychain → tests/e2e/.claude-auth/.credentials.json
├── sandbox.sh                # bare isolated $HOME for manual exploration (no install)
└── release-sandbox.sh        # full wheel install + sandbox (used for high-fidelity probes)
```

## Gotchas (CC 2.1.x — discovered 2026-05-14)

Three traps that silently nullify scenarios. Symptom is always the same: scenario "runs" but assertions can't observe anything because the MCP tool isn't loaded, the hook never fires, or the redirect quietly fails.

### 1. Stub MCP server's Python needs `mcp` installed

`fixtures/stub_server.py` imports `mcp.server.fastmcp`. Bare `python3` on a fresh host lacks it; the server crashes on import, Claude Code records "MCP server failed to start" and registers zero tools. Subsequent `mcp__stub__*` invocations get rejected with no warning anywhere your scenario can see.

**Fix:** point the `.mcp.json` `command` at a Python that has `mcp`:

```json
{ "mcpServers": {
    "stub": { "type": "stdio",
              "command": "/Users/you/.local/share/uv/tools/conexus/bin/python3",
              "args": ["…/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "/abs/stub.log" } } } }
```

The `conexus` tool venv path is reliable on dev machines that have run `scripts/reinstall-tool.sh`. Fallback: `$REPO_ROOT/.venv/bin/python` for editable installs.

### 2. `mcpServers` in `settings.json` is silently ignored

Up to some recent CC version, putting MCP server config under a `"mcpServers"` key in `settings.json` worked. In **2.1.x it does not** — the key is ignored without warning. The server config must live in `.mcp.json` at the workspace root, AND `settings.json.permissions.allow` must whitelist the MCP tool prefix:

```json
{ "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "default" } }
```

Without `permissions.allow`, even with `--dangerously-skip-permissions`, the first session encounters an approval prompt that `claude_start`'s auto-handlers don't dismiss.

For full automation across the per-project state-tracking dance, pass the config on the command line:

```bash
claude --dangerously-skip-permissions \
       --mcp-config "$TEST_HOME/.mcp.json" \
       --strict-mcp-config
```

`--strict-mcp-config` discards every other MCP source (`.claude.json` projects state, user state) and uses only the file you named. Sidesteps the per-project `enabledMcpjsonServers` initialisation that CC otherwise rewrites at session start.

### 3. `PreToolUse` command hooks do NOT inherit the harness's environment

Hook scripts of the shape:

```bash
#!/usr/bin/env bash
INPUT=$(cat)
echo "[$(date +%s%N)] HOOK_FIRED: $INPUT" >> "$HOOK_LOG"
python3 -c '…'  # emit valid JSON for permissionDecision
```

fail silently when CC invokes them: `$HOOK_LOG` is unset in the hook subprocess, the redirect becomes `>> ""` (bash: "No such file or directory"), and bash exits 0 because the next statement (the Python emitting JSON) succeeds. CC reads the valid JSON and accepts the hook's permission decision — so **observability dies, harness reports success**.

**Fix:** bake the absolute log path into the script via an unquoted heredoc:

```bash
cat > "$TEST_HOME/.claude/hook_allow.sh" <<EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s%N)] HOOK_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}))'
EOF
```

The `\$` defers runtime expansions; the bare `$HOOK_LOG` expands at write-time. Result: the generated script has the literal path hardcoded.

## Working scenario template

After the three fixes above, a scenario that reliably exercises MCP-tool hooks is:

```bash
HOOK_LOG="$TEST_HOME/hook.log"
STUB_LOG="$TEST_HOME/stub.log"
STUB_PYTHON="$HOME/.local/share/uv/tools/conexus/bin/python3"

mkdir -p "$TEST_HOME/.claude"

cat > "$TEST_HOME/.claude/hook_allow.sh" <<EOF
#!/usr/bin/env bash
INPUT=\$(cat)
echo "[\$(date +%s%N)] HOOK_FIRED: \$INPUT" >> "$HOOK_LOG"
python3 -c 'import json; print(json.dumps({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}))'
EOF
chmod +x "$TEST_HOME/.claude/hook_allow.sh"

cat > "$TEST_HOME/.claude/settings.json" <<EOF
{ "skipDangerousModePermissionPrompt": true,
  "permissions": { "allow": ["mcp__stub__*"], "defaultMode": "default" },
  "hooks": { "PreToolUse": [
    { "matcher": "mcp__stub__.*",
      "hooks": [{ "type": "command",
                   "command": "bash $TEST_HOME/.claude/hook_allow.sh" }] }
  ] } }
EOF

cat > "$TEST_HOME/.mcp.json" <<EOF
{ "mcpServers": {
    "stub": { "type": "stdio",
              "command": "$STUB_PYTHON",
              "args": ["$REPO_ROOT/tests/cc-validation/fixtures/stub_server.py"],
              "env": { "STUB_LOG": "$STUB_LOG" } } } }
EOF

: > "$HOOK_LOG"
: > "$STUB_LOG"
send_keys "cd $TEST_HOME" Enter
sleep 0.3
claude_start
claude_prompt "Call mcp__stub__ping. Reply DONE."
claude_wait 60

grep -q HOOK_FIRED "$HOOK_LOG" && pass "hook fired" || fail "hook did not fire"
grep -q '"tool": "ping"' "$STUB_LOG" && pass "tool ran" || fail "tool did not run"
```

## Reference spike: CA-8 (PreToolUse multi-hook ordering)

`run-ca8-spike.sh` is the gold-standard reference for "scenario that actually works" — written 2026-05-14 after rediscovering the three gotchas above the hard way. Result (CC 2.1.141): PreToolUse multi-hook semantics are **allow-wins, order-independent**. See T2 `nexus_rdr/111-research-CA-8-spike` and RDR-111 §Step 2 for the architectural implication.

## Naming + sequencing

- Scenarios are numbered `NN_<slug>.sh` and run in lex order. The runner filters with `--scenario <NN>` (matches the leading digits only — `--scenario 13` runs every file whose number is 13).
- Each scenario sources `lib.sh` (via the runner) and calls one or more `scenario "label"` blocks, each ending in `pass "…"` / `fail "…"` / `skip "…"`.
- Setup happens at scenario top; teardown is the next scenario's `reset_scenario_state` (wipes `.claude/{settings.json,.mcp.json,agents/,skills/}` plus `HOOK_LOG`/`STUB_LOG`).
- Keep scenarios self-contained — they share `$TEST_HOME` but not state, by design.

## When NOT to use this harness

- **Unit tests for hook scripts** — invoke the script directly with a fixture stdin file.
- **MCP server behaviour in isolation** — `python3 fixtures/stub_server.py` standalone, drive it with `mcp` SDK calls.
- **Anything that doesn't need a real Claude Code session** — pytest is faster.

This harness earns its complexity when the test needs CC's own behaviour (slash-command parsing, agent invocation, skill triggering, hook chains, MCP tool routing) inside a real interactive session.
