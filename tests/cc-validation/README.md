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
| Every scenario reports `tool_ran=0` / "MCP connection issue" although the run printed a green `[auth]` line | the provisioned credential is a token-less keychain husk; the pane says `Login expired · Please run /login` | [Auth](#auth) |
| MCP tool "not available", `STUB_LOG` empty | project `.mcp.json` never connects + stub python lacks `mcp` | [MCP servers](#mcp-servers-the-big-one) |
| `can't find pane: cc-val`, suite aborts mid-run | bare `tmux` instead of `_tmux` (wrong socket) | [tmux isolation](#tmux-isolation) |
| Hook "runs" but writes nothing | PreToolUse hooks don't inherit harness env | [Hooks](#hooks) |
| New `nx` subcommand: `No such command` | cc-val uses on-PATH `nx`, not repo source | [on-PATH nx](#on-path-nx) |
| A scenario that installs the real plugin by writing `installed_plugins.json` sees no plugin hooks, agents or servers | Claude Code 2.1.302: `claude plugin list` reports `failed to load: Marketplace nexus-plugins not found`; never write one: call `cc_plugin_require` (runner.sh), which installs through a local-directory marketplace and fails the scenario unless `claude plugin list` shows the plugin enabled at its declared version | [Installing a plugin](#installing-a-plugin-in-a-scenario) |
| A scenario asserts a plugin hook ran and the transcript shows nothing | the transcript records a hook only when its stdout or stderr is non-empty; read the debug log with `cc_hook_ran` / `cc_mcp_hook_ran` | [Installing a plugin](#installing-a-plugin-in-a-scenario) |
| A probe passes the moment the prompt is sent | the pane shows the prompt, so a marker or reply token written whole in the prompt matches the grep; spell it in two pieces to be joined | [Installing a plugin](#installing-a-plugin-in-a-scenario) |

## Auth

No credential file, ever (RDR-219). `runner.sh` starts its own private tmux
SERVER under `tests/e2e/lib/claude_credentials.py run --` (`_cred_tool()`
wraps it), which execs the `new-session` call with `CLAUDE_CODE_OAUTH_TOKEN`
set in the process's own environment — the harness's dedicated automation
token, from keychain item `nexus-automation-oauth-token` (created once with
`claude setup-token`), never the operator's own interactive login
(`Claude Code-credentials`). A tmux session inherits its environment from the
SERVER, not from whatever later asks for the session, so every pane spawned
afterward — including the one that launches `claude` — already carries the
token.

Check the automation token's health any time with:

```bash
python3 tests/e2e/lib/claude_credentials.py status
```

which reports present/absent, creation date, and days to expiry, and never
prints the token itself. If it's absent or expired, `claude setup-token`
mints a fresh one; store it in keychain item `nexus-automation-oauth-token`
under your own account.

A well-written scenario is still **fail-closed**: it asserts the *presence*
of an expected marker, so an auth failure lands in the `else` branch (a real
fail), never a vacuous pass. Keep it that way.

### History: the operator's interactive login used to leak in (closed, RDR-219)

Before RDR-219, `runner.sh` provisioned from the operator's own interactive
Keychain item (`Claude Code-credentials`) via a bare, unscoped `security
find-generic-password` and cached the result at
`tests/e2e/.claude-auth/.credentials.json`. More than one keychain item can
carry that service name — an `acct="unknown"` husk (`accessToken ""`,
`refreshToken ""`, `expiresAt 0`) alongside the account the live CLI actually
refreshes — and the bare lookup could silently select the husk. Every
scenario then ran against a **logged-out** session: scenarios 16 and 28
reported `hook_fired=0 tool_ran=0` and their guards blamed the MCP
connection, while the pane had said the real thing the whole time:

```
⏺ Login expired · Please run /login          # in the transcript
                        Not logged in · Run /login   # in the status bar
```

Worse, `provision_credentials` then overwrote the harness's own fallback
snapshot with the husk, destroying its own only alternative credential
source.

RDR-219 closed the class rather than the symptom: the harness uses its own
automation token instead of the operator's login, and never writes a
credential to disk at all, so there is no snapshot left to overwrite and no
husk to choose between. The shared helper this section used to describe,
`tests/e2e/lib/claude_credentials.py` (nexus-galkv.19), now carries only
`run` and `status` for the automation token; its husk-avoiding picker over
the operator's login was removed once the last harness migrated
(nexus-wauo1.18).

**Lesson (same class as the 2026-05-31 trio below): a successful FETCH is not
a valid CREDENTIAL.** An `[auth] ...` line on stderr is provenance, not
verification — never read it as "auth is fine".

### Watching a run: `CC_VAL_DEBUG_CAPTURE=1`

```bash
CC_VAL_DEBUG_CAPTURE=1 ./tests/cc-validation/runner.sh --scenario 16
```

Polls `capture-pane` every 2s into `$CC_VAL_DEBUG_DIR` (default
`$TMPDIR/cc-val-debug`), alongside per-tick copies of `STUB_LOG` and
`HOOK_LOG`. Snapshots live outside `$TEST_HOME`, so the EXIT trap's `rm -rf`
cannot eat them, and the poller is killed at cleanup. Sampling DURING the run
is the only option — the TUI's alternate screen is gone once claude exits, so
a post-mortem capture shows nothing. This is the instrument that found the
`Login expired` line above; reach for it before hypothesising.

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

Use `$REPO_ROOT/.venv/bin/python`, never bare `python3`.

A managed install works too, but resolve the pointer first —
`"$(readlink ~/.local/share/nexus/tools/current)"/bin/python3`, never
`~/.local/share/nexus/tools/current/bin/python3` itself. CPython looks for
`pyvenv.cfg` next to the executable **as invoked**, before resolving symlinks,
so going through `current` puts that component into `sys.prefix` and the next
install flips the pointer out from under a running process (nexus-q3xrx; the
shims exist to make that unconstructible, and `python` is deliberately never
shimmed). The old text here named `~/.local/share/uv/tools/conexus/bin/python3`,
which is the pre-generation layout (nexus-utpuw.19).

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
forensics, not self-listed inventory. The direct-call scenarios (05, 07, 16) all
carry the warmup turn as of 2026-05-31; a new MCP scenario must too.

### Fast verification (no tmux, seconds)

`claude mcp list` does a live connection check. Use it to validate MCP config
before spending minutes on an interactive run:

```bash
REPO="$PWD"
PROBE=/tmp/mcp-probe; mkdir -p "$PROBE/proj"
cat > "$PROBE/.claude.json" <<EOF
{ "hasCompletedOnboarding": true,
  "mcpServers": { "stub": { "type":"stdio",
    "command":"$PWD/.venv/bin/python",
    "args":["$PWD/tests/cc-validation/fixtures/stub_server.py"],
    "env":{"STUB_LOG":"/tmp/mcp-probe-stub.log","STUB_NAME":"stub"} } } }
EOF
# No credential file, ever (RDR-219): launch under claude_credentials.py
# run --, which execs `claude` with the harness's own automation token
# (CLAUDE_CODE_OAUTH_TOKEN) already in its environment.
(cd "$PROBE/proj" && HOME="$PROBE" python3 "$REPO/tests/e2e/lib/claude_credentials.py" run -- claude mcp list)   # -> stub: ... - ✓ Connected
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
3. **Keep scenarios isolated — reset must clean what scenarios write.** A
   scenario's `$TEST_HOME/.mcp.json` (workspace root) must be removed by
   `reset_scenario_state`, or the `--mcp-config` wrapper feeds a stale server to
   the NEXT scenario's parent (this manufactured a false "inline mcpServers
   leaked to parent" in 11). If an isolated `--scenario N` result disagrees with
   the full-suite result, suspect cross-scenario state first. Reproduce fast with
   a comma-list: `runner.sh --scenario "05,07,11"` (scenario numbers are
   zero-padded — use `05`, not `5`; the match is exact on the `NN_` prefix).

Reworked for validity 2026-05-31: 05, 06, 07, 09, 10, 11, 14a, 14b, 16, 18, 19A,
23. Verified valid as-is: 01, 17 (explicit-token),
02/03/04/12/13/14c/15/20–22/24–26 (forensic/positive). (nexus-qsmc5 found 01-04, 11, 15, 17,
21, 24 and 25 were NOT valid: a token or marker written whole in the prompt or command
body, or a verdict read off the pane; and 05-07 ran under the bypass flag. Reworked.) (09/10's branch-1 token
grep used to match the user's own turn-1 prompt in scrollback — fixed by
requiring a compound `INHERIT-SEEN:<token>` / `ENVV-SEEN:<token>` that only the
fork can emit.)

## Known real findings (NOT harness bugs) and dated observations

The harness surfaces these. They are two kinds, and only the first kind is a stable
finding.

### Observations that change with the Claude Code version

Scenarios 06, 07, 33 and 34 ask permission questions whose answers have flipped, or may flip, across Claude
Code releases (and between a bypass-flag launch and an enforced one). Each is an
OBSERVATION of one build, recorded with its date, never a fact about the product.
Re-measure on every pin bump (see "Bumping the Claude Code pin"); nothing may be
deleted, or relied on, on the strength of what is written here.

- **06** — does the allow rule `mcp__plugin_*__*` match a tool named
  `mcp__plugin_xxx_yyy__record`? Observed on 2.1.302 on 2026-10-18: it did not match;
  the call stopped at a permission prompt exactly as a non-matching rule's call does, in
  the same mode, with the tool not run. The 2026-05-31 entry ("MATCHES") was read under
  `--dangerously-skip-permissions`, where every call runs, and was no reading. This has
  flipped across Claude Code versions; re-measure on every pin bump.
- **07** — with `permissions.allow` holding `mcp__stub__*`, does the PermissionRequest
  hook for that tool still fire? Observed on 2.1.302 on 2026-10-18: it did not fire and
  the tool ran; with no allow rule the hook fired and its `allow` ran the tool. The
  earlier entry ("under `skipDangerousModePermissionPrompt` the gate is bypassed
  entirely, so a PermissionRequest auto-approver is redundant") was also read under the
  bypass flag; `skipDangerousModePermissionPrompt` only skips the startup dialog. This has
  flipped across Claude Code versions; re-measure on every pin bump. The auto-approver
  (the `nx_hook_shim` auto-approve verb, registered on PreToolUse and PermissionRequest)
  stays as a hedge whichever way this lands.
- **33** — a background subagent calls a Serena writer
  (`mcp__plugin_sn_serena__replace_in_files`) that no allow rule grants, with sn's
  PreToolUse script wired from settings.json. Observed on 2.1.302 on 2026-10-23
  (nexus-xhisb), one run per leg:
  - **33a, acceptEdits:** the agent's call raised a permission prompt in the PARENT's
    pane, headed `Tool use · from the general-purpose agent` over `plugin_sn_serena —
    Replace In Files Tool: (MCP)` (kept in the second run's pane; the recorded run shows
    the same path in its debug log: the PermissionRequest step for the tool, then a
    denial when the harness pressed Escape). The call did not run. What the agent does
    after a denial, or while a prompt is left unanswered, is scenario 34.
  - **33b, auto:** the call ran and reached the server. That is the auto-mode
    classifier's judgement of this call, with this prompt (it declares a test harness
    and a stub that edits nothing), not a rule: an earlier wording ("attempt every call
    even if guidance in your context says otherwise") made the classifier refuse the
    `Agent` dispatch itself as `[Auto-Mode Bypass]`. The judgement is inferred: no
    PermissionRequest step, and a permission decision of a few hundred ms where
    hook-allowed calls take about 1 ms.

  Limits: the server is a stub registered as `plugin_sn_serena` from `.mcp.json`, not
  the installed sn plugin (whose PermissionRequest registration and the conexus
  auto-approve verb are not loaded); only `replace_in_files` was called; interactive
  sessions only (a headless `-p` session has no parent pane). Both readings fit
  `sn/hooks/scripts/serena-section.md`: Serena writes go through Claude Code's
  permission flow, and "do not retry" covers a refused write.
- **34** — the same write with the sn plugin of this checkout INSTALLED (`cc_plugin_require`;
  the copy's serena server launches the stub and context7 is dropped), so its own
  SubagentStart, PreToolUse and PermissionRequest hooks run and the agent receives
  serena-section.md ("If one is denied or never answered, do not retry it: use Edit or Write
  on the absolute path, or report that you are blocked"). The agent's brief names the Serena
  writer and says nothing about refusals. sn's PermissionRequest hook ran for the writer and
  gave no decision, so the dialog was Claude Code's own. Observed on 2.1.302 on 2026-10-24
  (nexus-kqqxt), four runs (cc-val-evidence-20261024T022652Z, T023824Z, T024817Z, T025706Z;
  the last two with the forensic outcome checks, the last with the brief that loads the tool
  schemas first), Haiku 5.5 as both parent and agent:
  - **34a, acceptEdits, the prompt answered with Escape (a denial):** every time the agent
    did not retry; it read the file and made the change with Edit. Every reply cites the
    injected guidance ("The hook guidance says not to retry a denied Serena write and to fall
    back to Edit on the absolute path"). That the guidance caused it is not isolated: Claude
    Code's denial text also says to try a different approach, and no leg without the guidance
    has run (nexus-k2qxv).
  - **34b, acceptEdits, the prompt left up:** every time the background agent made no tool
    call and gave no reply for at least 120 s (124 s by transcript timestamps in the first
    run) while the parent's dialog stood: its call stays pending until the dialog is answered.
    Once it was denied the agent fell back to Edit as in 34a.
  - **34c, auto, three fresh sessions per run:** the agent's write ran in every sample whose
    write was the agent's own. In the third run one agent called `find_symbol` with no
    arguments (its schema not loaded), handed back, and the parent made the write itself; the
    scenario gave that run no verdict, and the brief now has the agent load both schemas with
    ToolSearch and the parent leave the work to it.

  Limits: Haiku 5.5 only; acceptEdits, where the fallback Edit is allowed without a prompt,
  so the fall-back itself was never prompted; only `replace_in_files`, whose dialog shows
  the stub's description ("logs and claims success"), which the 33b review showed can sway
  the classifier; the conexus plugin (and its auto-approve verb) not installed alongside sn;
  interactive sessions only. The trigger the bead set for one more clause in
  serena-section.md (a background write shown to block on the parent's prompt) was met; no
  clause was added, because a call pending on the dialog leaves the agent nothing to act on
  until it is answered, and the dialog is in the parent's pane, where the user sees it.

Verdict lines from 06, 07, 33 and 34 say `observed on Claude Code <version>`, report what they saw without
asserting a direction, and compare it with `docs/tables/cc-version-answers.toml` (a
checked RDR-201 table; `lib/version_answers.py` reads it): the recorded answer for that
version passes; a different answer FAILS "answer changed since <version>"; a version with
no row prints `INFO: record this version's answer` and passes. The control legs (a rule
that cannot match must stop at a prompt; sub-run B must reach a gate) still FAIL a session
that does not enforce permissions.

### Bumping the Claude Code pin

`ARG CLAUDE_CODE_VERSION` in `container/Dockerfile` pins the build. A bump re-runs
scenarios 06, 07, 33 and 34 and updates `docs/tables/cc-version-answers.toml` (add the new
version's rows, or read why a recorded answer changed before touching it). A bump with no
re-run leaves the table silent about the new build, so these scenarios are what announce it:
an unrecorded version prints the INFO line, a recorded one whose answer flipped FAILS, and so does a
run that cannot read its Claude Code version at all ("unknown").
Run them on every bump.

### Findings that are not version-sensitive

- **11** — inline-agent `mcpServers` ARE scoped to the subagent (confirmed), the
  documented behavior ("the subagent gets the tools; the parent conversation does
  not"). The inline server spawns at subagent dispatch (stub startup markers show
  no spawn before it); the parent's pre-dispatch probe call does not land
  (`parent_called_stub=0`). A transient full-suite "leak" reading was a HARNESS
  ISOLATION BUG, not a scoping failure: `reset_scenario_state` cleaned
  `.claude/.mcp.json` but scenarios write `$TEST_HOME/.mcp.json` (workspace
  root), so a stale project `.mcp.json` from a prior scenario (05/07) was fed to
  scenario 11's PARENT via `--mcp-config`, giving it the stub. Fixed by removing
  `$TEST_HOME/.mcp.json` in `reset_scenario_state`; 11 then reports SCOPED even
  when run right after stub-using scenarios. (An even earlier "not scoped" claim
  was a separate artifact of the bare-`python3` crash + model self-report.)
- **14b** — plugin-shipped agents and `mcpServers`. Claude Code's docs say `hooks`,
  `mcpServers` and `permissionMode` are not supported on plugin-shipped agents. Since
  nexus-spwp2 the fake plugin is really installed (`cc_plugin_require`), so the agent is
  registered; since nexus-qsmc5 the verdict rests on the stub's own `process_launched`
  marker (did Claude Code spawn the server for the plugin agent), not on the deferred
  tool's schema. It PASSes when the stub was spawned, or when it was not and 14a (the
  positive control: the same stub, project-level agent) shows the stub loads here; it SKIPs
  only when `agent14b` did not run or the control did not load. Like 06/07 this is a
  reading of one build.

## Root causes resolved 2026-05-31 (recorded so they are not re-derived)

Three failures looked like real CC anomalies/flakiness but were harness issues,
each root-caused by *measuring* (stub startup markers, before/after snapshots)
rather than hypothesising — twice the first hypothesis was wrong:

- **16 was NOT an "allow-rule anomaly".** The `tool_ran_a=0` / `tool_ran_b=1`
  split was the deferred-tool first-call race (the pane showed "I'll load the
  tool schema first"). A 12s settle didn't fix it; a warmup turn does. Fixed,
  passes deterministically.
- **14a was NOT "deferred-loading / spawn flakiness".** The inline-agent server
  used bare `command: python3` → homebrew python without `mcp` → crashed on
  import (`mcp_import_failed` in `STUB_LOG`). PATH-dependent python resolution
  made it look intermittent. Fixed by pinning to `.venv/bin/python`.
- **11 was NOT "inline mcpServers leak to the parent".** That reading came from
  the bare-python3 era + the parent's unreliable self-reported tool list. The
  startup markers prove the inline server spawns only at dispatch and the
  parent's pre-dispatch call never lands: scoping works (see Known findings).

Lesson encoded in the convention above: assert on forensic side-effects, and
when a result is surprising, add a deterministic probe before believing a
behavioural hypothesis.

## Root cause resolved 2026-08-28 (nexus-qs1g6)

- **16 and 28 at CC 2.1.251 were NOT a prompt-delivery regression.** All four
  sub-runs reported `hook_fired=0 tool_ran=0`, and the first two hypotheses
  (a new startup dialog eating keystrokes; deferred MCP tools not loading) were
  both wrong. A `CC_VAL_DEBUG_CAPTURE=1` run settled it in one pass: the TUI
  reached input-ready, `claude_prompt`'s `load-buffer`/`paste-buffer` + Enter
  delivered BOTH prompts verbatim, the stub server spawned — and the model
  answered `Login expired · Please run /login`. The provisioned credential was
  a token-less keychain husk (see [Auth](#auth)). With the credential picker
  fixed, both scenarios produce non-vacuous verdicts: 16 passes with
  `tool_ran=1` in both sub-runs, and 28 passes with `28a allow → tool_ran=1`,
  `28b deny → tool_ran=0`, hook fired and server connected in both.
- Two latent harness bugs found in the same pass, both in the `claude_start_auto`
  copies in scenarios 16 and 28: the readiness pattern `auto.*on` matches the
  ECHOED COMMAND LINE (`...=auto --mcp-config … --strict-mcp-con`) and `❯ `
  matches an ordinary shell prompt, so the loop broke on its first poll no
  matter what Claude was doing — only the `sleep 8` + `sleep 5` were holding it
  together. Both now anchor on the status bar's literal `auto mode on`.

## Patterns from `~/git/recording-rig`

`recording-rig` is a mature Claude-Code-driving harness (spec-driven, tmux +
desktop backends). Two of its patterns are already ported; two remain.

**Ported (implemented 2026-05-31):**
- **Trust pre-seed instead of poll-and-press-Enter.** `runner.sh`'s
  `claude_start` wrapper writes `hasTrustDialogAccepted` +
  `hasCompletedProjectOnboarding` into `$TEST_HOME/.claude.json` for the project
  paths before launch, so the trust dialog never fires.
- **Deterministic config injection via flags.** The wrapper launches with
  `--mcp-config $TEST_HOME/.mcp.json --strict-mcp-config` (via `CLAUDE_EXTRA_ARGS`
  in `lib.sh`) when a scenario declares a `.mcp.json`, rather than relying on
  `~/.claude.json` discovery and its broken approval gate.

**Still worth porting:**
- **Stop-hook sentinel for turn-completion.** Instead of scraping the TUI for
  "Simmering…"/"esc to interrupt" (our `claude_wait`), the rig has a `Stop`
  hook write `/tmp/$SESSION.turn-end` and blocks on its mtime going quiet
  (`lib/sentinels.sh::sentinel_wait_idle`). Deterministic, immune to TUI
  rendering changes. This is the single biggest robustness upgrade remaining.
- **Capture the pane ID (`%N`) and target it** instead of `-t <session>`, so
  later splits/active-pane shifts can't misdirect keystrokes.

## Headless hook `ask` (nexus-mcfaj), measured on chas

`hook-ask-headless/` is a sibling harness (like `connection-race-ladder/`) for
what a PreToolUse `permissionDecision: "ask"` does when no human can answer:
real `claude -p` sessions in the `nexus-cc-val` container on chas, never on
the laptop. `hook-ask-headless/run.sh` stages the harness, runs the matrix under
`claude_credentials.py run --remote chas-test -- lab-run -c lab-s -- ...`, and
`summarize.py` prints one verdict per run (`RAN`, `DENIED`, `HUNG`,
`NOT-EXERCISED`). Result at Claude Code 2.1.302: an ask is a DENY in default,
`dontAsk` and `auto` mode and for a background subagent; nothing hung; the
auto-mode classifier never approved past it. The reason travels back to the
model in the tool result. Recorded runs: `hook-ask-headless/evidence/`. The
table and its limits live in `ask_envelope`'s docstring
(`conexus/hooks/scripts/routing/_lib.py`).

Two traps this harness hit, for the next probe of its kind: an untrusted
workspace silently ignores the project's `permissions.allow` entries (pre-seed
`hasTrustDialogAccepted` for the cwd), and a read-only probe command such as
`echo` is auto-allowed in every mode, so it cannot show that an ask changed
anything (use `touch` plus an allow rule).

## Installing a plugin in a scenario

(nexus-spwp2.) A scenario that needs a plugin loaded calls the shared helpers in
`runner.sh`; it never writes `installed_plugins.json` or `enabledPlugins` itself
(`tests/test_cc_validation_plugin_install.py` fails the build on either).

```bash
cat > "$TEST_HOME/.claude/settings.json" <<EOF ... EOF       # settings FIRST: the install merges enabledPlugins into them
if cc_plugin_require "$REPO_ROOT/conexus" conexus nexus-plugins; then
    claude_start                                              # adds --debug-file when a plugin is installed
    ...probe...
    cc_hook_ran "SubagentStart" "$CC_PLUGIN_ID"               # command hook: debug log says it finished with status 0
    cc_mcp_hook_ran "$CC_PLUGIN_ID" "hook_subagent_start"     # mcp_tool hook: THAT plugin's call, and no mcp_tool hook error
    cc_hook_context_injected "SubagentStart" "subagent_start.py"   # Claude Code accepted non-empty additionalContext from it
    claude_exit
fi
cc_plugin_uninstall_all                                       # keeps evidence, restores the empty registry
```

`cc_plugin_require` copies the plugin tree into a synthesized local-directory
marketplace, runs `claude plugin marketplace add` and `claude plugin install
--scope user`, and passes only when `claude plugin list` shows
`<plugin>@<marketplace>` enabled at the version its `plugin.json` declares. When
it does not, the scenario FAILS (never skips) and the list and install output are
printed. `cc_plugin_install` is the same without the `fail`, for scenario 32,
which routes a not-loaded plugin into its own verdict ladder. One plugin per
scenario part: an install replaces all plugin state.

Evidence (plugin list/install output, debug log, settings, registry, transcripts)
goes to `$CC_VAL_EVIDENCE_DIR/<scenario>` when the container mounted one.

Census (2026-10-17, 30 scenarios; 31 with 33, nexus-xhisb; 32 with 34, nexus-kqqxt): plugin-dependent were
12, 13b, 14b, 18, 32 and 34; the other 26 install nothing. The container carries what a loaded plugin needs:
the Nexus CLI built from the staged source tree, not fetched from PyPI by version
(`nx-mcp`, `nx-hook`; without it the plugin loads and then its `mcp_tool` hooks
fail with "Nexus is not installed"),
the `sn` plugin tree, and the `mcp` package for the stub server.

**Probes must not match their own prompt.** The pane shows the prompt, and the
subagent sees it too, so a marker or reply token written whole in the prompt
satisfies the grep at once. Scenarios 01-04, 11, 12, 13, 15, 17 and 18 (and the
slash-command bodies of 21, 24 and 25) spell tokens and probe phrases as pieces
("the word FOUND immediately followed by -13B") and grep the joined form;
`tests/test_cc_validation_plugin_install.py` (`PROBES`) fails if any of their
prompts, or for 17/21/24/25 their skill or command bodies, holds a probe whole. A
fixed phrase can still be answered from the prompt, so scenario 12 asks for the
per-dispatch `Claimant id` the hook emits and compares the subagent's reply with
the id in the debug log. The reply is read from the subagent's own transcript
(`cc_subagent_replies`; the main session's own is `cc_session_replies`), never the
pane: the main agent paraphrases the reply, redacts agent ids and may dispute it,
and the pane also prints a hook's own output (seen live). Scenario 17 reads what the
subagents DID, not what they wrote (`cc_nested_dispatch_facts`, `lib/agent_facts.py`):
CAN needs a Task/Agent call whose result came back without `is_error` AND the nested
token in a transcript that made no such call (the leaf agent's, never the fork's);
CANNOT needs a `ToolSearch` call and no Task/Agent call in the fork's transcript. The token
must sit in the transcript of the agent the dispatch STARTED, by id: the Agent result names it
(`agentId: <id>`) and the leaf's transcript is `agent-<id>.jsonl`; a token in any other
transcript, or a result that names no agent, fails (nexus-ijate). Every result observed
on chas so far is the async shape that names the id; a sync or a fork-type result (a fork's
"Fork started" names none) would read as a false red until a live run shows its shape. Scenarios
01 and 15 (plain stdout) read the subagent's answer AND the debug log, which must agree
(`cc_injection_reading`, `lib/debug_log.py`): NONE passes only with the hook's
finished-with-status-0 line and no accepted `additionalContext`, VISIBLE only with both; 15
withholds the marker's last three characters from its prompt, as 01-04 do. 15e is the negative
control (a hook that never runs must not read as NONE); 15f is the positive control (nexus-ijate):
a hook that injects JSON `additionalContext` must read VISIBLE, so the VISIBLE branch has a live
leg (15a-d all read NONE). Run it with `container/run.sh --scenario 15` on chas. Scenarios 21, 24
and 25 read the expanded slash command out of the main session's transcript
(`cc_session_user_text`, `cc_command_render_check`): the user turn Claude Code sent to the
model must carry the injection's own output, whatever the model then says about it; 21b is
the negative control (an injection that prints nothing must FAIL the same check).
`reset_scenario_state` and scenario 14's parts clear the transcripts (`cc_reset_transcripts`)
so one scenario never reads another's. Scenario 18 gates on the size of the context the
plugin's hook delivered (`cc_hook_context_is_whole`: the debug log's
`provided additionalContext (N chars)` must equal the lengths of the two section
files), because the reply tokens cannot tell a partial delivery from a whole one.
`cc_hook_context_injected` and `cc_hook_context_chars` are scoped to a plugin id.

**Permission scenarios launch enforced (05-07).** `claude_start` passes
`--dangerously-skip-permissions`, under which every call runs whatever
`settings.json` says. A scenario about the permission system launches with
`claude_start_enforced` (no bypass flag) and carries a control in the same mode:
05 and 06 run a leg whose allow rule does not match the tool, which must stop at a
"Do you want to proceed?" prompt without running it (`cc_allow_rule_verdict`); 07's
no-allow sub-run must reach the PermissionRequest hook or the prompt
(`cc_preempt_verdict`). A control that does not prompt fails the scenario: the
session is not enforcing permissions. The census test fails for any scenario that
pins `defaultMode` default/auto/plan and still launches through `claude_start`.

**Scenario 14 waits for the agent, and reads the spawn.** `claude_wait` watches only
the main session's spinner and the main agent does not spin while a subagent
works, so 14 used to read its files, and end the session, mid-task (the agents'
transcripts ended "[Request interrupted by user]"). It now waits for each agent's
own `14X_DONE` reply. Scenario 14b no longer depends on whether the agent loaded
and called the deferred tool: it passes on the stub's own `process_launched` marker
(Claude Code spawned the server for the plugin agent), or, with no spawn, on
14a's positive control that the same stub loads project-level; otherwise SKIP.

## Running in a container (and scenario 32's positive control)

Claude Code runs only in a container on a Linux test host (chas), never on a
laptop. `container/run.sh` stages the tracked harness plus the conexus plugin
of the tree (and says which staged files are untracked or differ from HEAD), builds a toolchain image (Claude Code, tmux, uv), and runs a
scenario inside it; `runner.sh` detects `NX_CC_VAL_IN_CONTAINER=1` and takes
the token from the container's environment instead of the keychain.

```bash
python3 tests/e2e/lib/claude_credentials.py run --remote chas-test -- \
    ~/src/nexus-wt/<ref>/tests/cc-validation/container/run.sh --scenario 32
```

The helper sends the automation token over the ssh channel's stdin and the
script passes it to `docker run` as a bare `-e CLAUDE_CODE_OAUTH_TOKEN`. Do not
put the script behind `lab-run`: the container is a child of dockerd, not of the
wrapped command, so the wrapper's cgroup grant neither caps nor even sees it
(`docker run` therefore carries its own `--memory 4g --cpus 2 --pids-limit`),
and the token reaches chas only over the ssh channel's stdin, which a wrapper in
between would not hand on. The image pins Claude Code (`ARG
CLAUDE_CODE_VERSION` in the Dockerfile, updater off); `run.sh` passes the pin to
the scenario, which refuses a verdict on any other build, and `claude --version`
is written into each evidence directory. Evidence (hook log, pane, transcripts, per
session) lands in `$NX_CC_VAL_EVIDENCE` (default `$TMPDIR/cc-val-evidence-<stamp>`).

**Scenario 32 has a positive control** (nexus-ueztp). It runs two sessions: 32a
installs the current plugin and passes when the permission prompt reaches the
pane; 32b installs the conexus plugin of git tag `v7.64.1` (the
GHSA-mc84-6gjq-vm2p plugin, whose routing guards emit a blanket `allow`),
extracted with `git archive` and version-checked, and is DECLARED to fail that
same check: it passes only when the prompt is skipped, the command runs, and
the old scripts' own stdout carries the `permissionDecision`. A control that
shows the prompt anyway is a finding, not a flake: it means the real
`hooks.json` entries do not govern the call here, so 32a's pass proves
nothing. `tests/test_cc_validation_scenario32_control.py` pins the wiring.

Two traps the first container runs found, both now handled in the scenario:
the hand-written `installed_plugins.json` did not load the plugin (see the
triage table), and in `defaultMode: auto` the classifier approves the harmless
`echo` itself so neither plugin shows a prompt. Sessions run in `defaultMode:
default`; `--s32-mode auto` restores the old mode, where 32b reports NOT
INFORMATIVE. 32b also requires Claude Code's own transcript to show the
v7.64.1 `hooks.json` entries emitting `permissionDecision: allow` (the allow
shape, not any decision).

**Proof the guards ran.** The transcript records a hook only when its stdout or
stderr is non-empty, so the current plugin's silent guards leave no record
there (a first run read that absence as ambiguous; a diagnosis run on 2026-10-17
showed the hooks do run, `uv` included). Each session therefore runs with
`claude --debug-file`, which logs every hook, and the scenario requires both
real guards to show there as finished under the plugin, plus a marker hook
appended to the same `PreToolUse`/`Bash` matcher group of the installed copy.
Missing either, a wrong Claude Code build, or an unloaded plugin each fail the
leg whatever the prompt did. The ladder is `_s32_verdict`; its truth table is
driven from `tests/test_cc_validation_scenario32_control.py`.

## Layout

```
runner.sh                       # setup (isolated HOME, tmux server under run --), scenario loop
scenarios/NN_*.sh               # one scenario each; source lib.sh helpers
container/                      # Dockerfile + run.sh: how Claude Code runs (chas, in docker)
fixtures/stub_server.py         # FastMCP stub (needs an interpreter with `mcp`)
../e2e/lib.sh                   # _tmux, claude_start, claude_prompt, claude_wait, capture
../e2e/lib/claude_credentials.py # shared credential helper (run/status); no file, ever (RDR-219)
lib/                            # stdlib-only readers the verdicts call: version_answers.py (the 06/07
                                # table), agent_facts.py (17), debug_log.py (01, 15)
../../docs/tables/cc-version-answers.toml  # the version -> observed answer table for 06/07
```

Companion gist (older notes): `4a7d73baa4409e02af7af222023b8b9d`.
