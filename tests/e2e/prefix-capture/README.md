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

## ab/seqthink/: uncued sequential thinking on the real repo

Measures how often the main session calls sequential thinking on its own when
the prompt is a design decision that never names the tool. Each run is one
`claude -p` session in a snapshot of this repo, crossed over arm A or C and
two guidance levels: `G1` has no user CLAUDE.md, `G2` installs the user's
global CLAUDE.md. The `prompts/` are three decision questions about real code
(T1 fallback, the paged-limit fix, the retry-loop test seam).

Two inputs are not committed and are rebuilt by hand before a batch:
`project.tar.gz`, from `git archive --format=tar.gz -o
ab/seqthink/project.tar.gz <sha>` (the 2026-10-15 runs used 005fd83ac), and
`user_claude.md`, a copy of the user's `~/.claude/CLAUDE.md`, which is
personal and stays out of the repo.

`sq_batch.sh <REPS> <ARM>:<GUIDE>:<prompt>...` is the loop on chas,
`sq_detach.sh` starts it detached through the credentials helper as for `ab/`,
and `analyse_sq.py ab/seqthink/out` reports main-session and subagent
sequential-thinking calls, the first turn that made one, ToolSearch and Agent
counts, and cost per cell. Results of the 2026-10-15 run: T2
`nexus/seqthink-uncued-real-guidance-2026-10-15`.

## ab/skillroute/: skill routing under distractor plugins (nexus-dg7i6.1)

Do conexus skills still load when other plugins' skills crowd the listing?
`claude plugin eval` cannot answer this (its sandbox loads only the plugin
under test), so this runs the `conexus/evals` case prompts as plain
`claude -p` sessions on the seeded image, graded with the cases' own
`tool_used` graders. Arms: `S0` is the image's conexus + sn with nexus tools as
shipped (8 eager); `S1` adds the beads plugin; `S2` adds every other plugin
enabled on the laptop except the LSP plugins. Extra plugins load with
`--plugin-dir` from `ab/skillroute/plugins/`, which is not committed: build it
on the laptop with `snapshot_plugins.sh` (copies from the installed plugin
cache) and copy it to the chas worktree.

`sr_batch.sh <REPS> <ARM>... [-- <case>...]` is the loop on chas (cases default
to every `conexus/evals` case), `sr_detach.sh` starts it detached through the
credentials helper, and `score_sr.py ab/skillroute/out <conexus/evals>`
reports positive-grader pass rate, zero-load rate on positive cases,
negative-grader pass rate, and the skill-listing size of each arm's first
request (an arm S1 or S2 whose listing did not grow over S0 is flagged
vacuous). The 2026-10-16 smoke run listed 76, 104 and 135 entries for S0, S1
and S2.

Arms `D` and `N` swap the conexus tree instead of adding plugins: the
installed conexus is switched off in the container's settings and a tree is
loaded with `--plugin-dir`, so the listing holds exactly one conexus. Each arm
loads `plugins/<arm>/conexus`, which `export_conexus.sh <arm> <commit>`
extracts from a commit fetched on chas: `N` the narrowed conexus of
nexus-dg7i6.2, `D` that commit's parent, so the two differ by exactly the
narrowing diff. The MCP servers of either tree hand off to
the image's installed `nx`, so the nexus tools match S0. `score_sr.py` counts
each arm's conexus entries, flags a listing name that appears twice, and takes
markers `ARM:+name` / `ARM:-name` that must hold in every run of that arm, for
example `D:+conexus:setup S0:-conexus:setup` (`setup` is a command develop has
and the image's 7.79.0 does not).

Any capital letter other than `S` is a tree arm loading
`plugins/<arm>/conexus` (`export_conexus.sh <arm> <commit>`), and it reads
the case prompts from that tree's `evals/`. Adding `T` (`DT`, `ET`) runs each
case's own scaffold, `evals/<case>/fixture.sh`, in an empty project
directory, the way `claude plugin eval --scaffold` does (nexus-dg7i6.5/.7);
a case without one runs on the plain project. The session's final project
is copied to `out/.../repo` so file-target graders can read it.
`score_sr.py` grades `tool_used` and `regex` graders and lists `llm`
graders as ungraded, on a separate line, so a pass count never mixes the
two.
