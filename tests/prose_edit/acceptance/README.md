# prose-edit acceptance runs

Headless Claude Code runs of the real `prose-edit` skill and `line-editor` agent, read-only against T2.

| File | Use |
| --- | --- |
| `run-scenario.sh NAME PROMPT` | One restricted session. Writes `NAME.jsonl`, `NAME.err`, `NAME.rc` under `$OUT_DIR`. |
| `run-canaries.sh` | The tool-restriction canary, the T2-failure canary, unmapped, stdin, stdin without genre. |
| `verdicts.py OUT_DIR` | Verdict and compliance counts per run. Exit 1 on any failure. |
| `fake_nx_unavailable.py` | `PROSE_EDIT_NX` stand-in that reports T2 unavailable. |

```bash
export TMPDIR=/Volumes/SanHell/tmp/nx.noindex   # any directory outside the repo
export CLAUDE_BIN=/path/to/claude                # default: claude on PATH (an alias does not count)
OUT_DIR=/tmp/prose-edit-acceptance tests/prose_edit/acceptance/run-canaries.sh
python3 tests/prose_edit/acceptance/verdicts.py /tmp/prose-edit-acceptance
```

For a scenario, call `OUT_DIR=... run-scenario.sh NAME "/prose-edit <args>"` with at most 8 running at once. More made the cloud edge answer 503. The fixtures are in `../fixtures/`; name runs `xanadu-1`, `refrain-2`, `qa-3` and so on so that `verdicts.py` finds their document.

## The restriction

The runner uses `--permission-mode dontAsk`, allows `Bash` only for `brief.py` and `memory.py`, plus `Read`, `Write` and `Agent`, and denies `Bash(nx:*)`, `Bash(uv:*)`, `Bash(bd:*)` and `Bash(curl:*)`. `--allowedTools` only pre-approves and `--disallowedTools` refuses, so `dontAsk` is what turns everything else into a denial. The `canary-nx` run proves it: the model is told to run `nx --version` and the call must come back denied.

A denied call in a scenario is a failure of the skill's instructions, not an incident. `verdicts.py` counts it under `denied`.

## What the counts cover, and what they do not

They cover tool use (denials, off-list commands, `nx` attempts), Grep scope, Reads of `docs/rdr`, edits to short closing lines, words inserted by an edit, and queries that say "no twin". They do not judge tricolons, meaning changes or content cuts. Read the edits for those.
