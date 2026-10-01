# prose-edit acceptance runs

Headless Claude Code runs of the real `prose-edit` skill and `line-editor` agent, read-only against T2.

| File | Use |
| --- | --- |
| `run-scenario.sh NAME PROMPT` | One restricted session. Writes `NAME.jsonl`, `NAME.err`, `NAME.rc` under `$OUT_DIR`. |
| `run-canaries.sh` | The tool-restriction canary, the T2-failure canary, unmapped, stdin, stdin without genre. |
| `verdicts.py OUT_DIR` | Verdict and compliance counts per run. Exit 1 on any failure. |
| `run-review.sh` | The review-loop scenarios (nexus-ger02.4): reject then run again, list and remove a rejection then run again, a stdin run. Several turns each, answers through `RESUME_FROM`; the skill echoes an answer (`apply --dry-run`) and waits, so each answer turn is followed by a confirmation turn (`a2c`, `a4c`, `b4c`, `c2c`). The script sets `PROSE_EDIT_TEST=1`, which `PROSE_EDIT_OPEN` needs. Writes to T2 under `PROSE_EDIT_PROJECT_PREFIX` (default `zzprose024_`). |
| `review_verdicts.py OUT_DIR` | Verdicts for those runs from what the skill's scripts printed. `VACUOUS` means the editor never proposed the edit the check needs: run it again. |
| `fake_nx_unavailable.py` | `PROSE_EDIT_NX` stand-in that reports T2 unavailable. |

```bash
export TMPDIR=/Volumes/SanHell/tmp/nx.noindex   # any directory outside the repo
export CLAUDE_BIN=/path/to/claude                # default: claude on PATH (an alias does not count)
OUT_DIR=/tmp/prose-edit-acceptance tests/prose_edit/acceptance/run-canaries.sh
python3 tests/prose_edit/acceptance/verdicts.py /tmp/prose-edit-acceptance
```

`RESUME_FROM=<earlier name> run-scenario.sh NAME "<the author's answer>"` continues that run's session, which is how a scenario answers the question the skill ends its turn with. Delete the `zzprose024_*` T2 projects after a review run.

For a scenario, call `OUT_DIR=... run-scenario.sh NAME "/prose-edit <args>"` with at most 8 running at once. More made the cloud edge answer 503. The fixtures are in `../fixtures/`; name runs `xanadu-1`, `refrain-2`, `qa-3` and so on so that `verdicts.py` finds their document.

## The restriction

The runner uses `--permission-mode dontAsk`, allows `Bash` only for `brief.py`, `memory.py` and `review.py`, plus `Read`, `Write` and `Agent`, and denies `Bash(nx:*)`, `Bash(uv:*)`, `Bash(bd:*)` and `Bash(curl:*)`. `--allowedTools` only pre-approves and `--disallowedTools` refuses, so `dontAsk` is what turns everything else into a denial. The `canary-nx` run proves it: the model is told to run `nx --version` and the call must come back denied.

A denied call in a scenario is a failure of the skill's instructions, not an incident. `verdicts.py` counts it under `denied`.

## What the counts cover, and what they do not

They cover tool use (denials, off-list commands, `nx` attempts), Grep scope, Reads of `docs/rdr`, edits to short closing lines, words inserted by an edit, and queries that say "no twin". They do not judge tricolons, meaning changes or content cuts. Read the edits for those.
