# prefix-capture (nexus-4mptw)

Captures the exact API requests real Claude Code sends, including subagent
dispatches, through a logging pass-through proxy (`proxy.py`) on
`ANTHROPIC_BASE_URL`. Claude Code runs only on chas, in a container built from
`Dockerfile` (Claude Code 2.1.300, conexus 7.79.0 from PyPI, conexus and sn
plugins installed from this repo's marketplace, which pins release tags).

From the laptop checkout: `tests/e2e/prefix-capture/run.sh <run-name> [prompt-file]`.
Output on chas: `out/<run-name>/` with `requests.jsonl` (body + usage per request,
no auth headers), `analysis.txt` (`analyse.py`: block order, sizes, cache_control,
usage, and the first divergence byte between first-turn requests), `debug.log`,
`result.json`. `ENABLE_TOOL_SEARCH=true` is set by default because a custom base
URL otherwise turns tool search off. Nothing in `out/` should be committed.

## ab/: eager versus deferred nexus tools (nexus-ly9c2)

An A/B on how the conexus plugin's `nexus` MCP server loads its tools, on the
same proxy. Image `nexus-ab-seeded:2.1.300` is the capture image plus a local
engine made by `nx init --no-autostart`, seeded with `ab/seed.sh` (three T2
entries in project `aurora`, four knowledge notes), cleanly stopped, and the
conexus+sn plugins installed, then `docker commit`ed. Each run starts a fresh
container from it, so writes never leak between runs.

- `ab_in_container.sh`: starts the proxy and the engine, patches the installed
  conexus `.mcp.json` per `ARM` (A: `alwaysLoad: true`; B: `false`; C: key
  removed plus `arm_c_core_meta.py` appended to the installed `core.py`, which
  sets `_meta["anthropic/alwaysLoad"]` on seven core tools; develop ships eight, adding `store_put`), builds the small
  `ab/project/` repo, runs `claude -p` on one task file, records post-state.
  In 2.1.300 a server-level `alwaysLoad: false` overrides per-tool flags
  (`serverDefersAllTools`), which is why C removes the key instead.
- `ab_batch.sh <ARM> <REPS> <task>...`: the loop on chas; `ab_detach.sh` starts
  it detached. Launch from the laptop through
  `tests/e2e/lib/claude_credentials.py run --remote chas-test -- bash <wt>/tests/e2e/prefix-capture/ab/ab_detach.sh A 3 t1_memsearch ...`.
  The helper exports `ANTHROPIC_MODEL=haiku` remotely, so the model is taken
  from `AB_MODEL` (default claude-opus-5-5), never `ANTHROPIC_MODEL`.
- `score.py ab/out`: per-arm means and per-task first-tool correctness.
- `tasks/`: eight tasks aimed at one nexus tool each, three near-misses.

## ab/tuplespace/: arm C against tuplespace, the SubagentStart hook and sequential thinking

Checks that the per-tool `alwaysLoad` set shipped on develop (eight core tools,
`arm_c8_core_meta.py`) breaks nothing the deferred `nexus` tools carry, compared
with arm A, on the same seeded image. `ts_in_container.sh` has two modes:

- `single`: one `claude -p` session on a prompt from `prompts/` (`subagent`
  dispatches one subagent; `seq_cued` is a decision prompt naming the thinking
  tool).
- `tuple`: three sessions on one engine. S1 subscribes a mailbox name and
  `mailbox_send`s to an agent id and to S3's session id; S2 claims and acks the
  agent-id message with `tuple_in`/`tuple_ack`; S3 gets an uncued decision
  prompt, so the UserPromptSubmit drain hook should render its mail.

`ts_batch.sh <REPS> <ARM>:<MODE>:<name>...` is the loop on chas, `ts_detach.sh`
starts it detached through the credentials helper as for `ab/`, and
`analyse_ts.py ab/tuplespace/out` scores the subagent's conexus block,
sequential thinking in the first main and subagent `tools`, tool calls and
ToolSearch counts, engine census and the drain probe. Channel push delivery is
not covered: `claude -p` runs without channels. Results of the 2026-10-15 run:
T2 `nexus/arm-c-tuplespace-seqthink-validation-2026-10-15`.
