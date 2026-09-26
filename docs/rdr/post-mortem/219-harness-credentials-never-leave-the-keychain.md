# RDR-219 post-mortem: harness credentials never leave the keychain

## What the RDR set out to do

Test harnesses that drive a real Claude Code session copied the operator's
interactive login into `.credentials.json` files, and many copies were never
deleted. On 2026-09-25 a subagent printed that credential into its own
transcript, and 35 copies were found on disk. The RDR replaced the practice
with three rules. Harnesses use a dedicated automation token from
`claude setup-token`. The token reaches Claude only through a child
process's environment, set by one helper
(`tests/e2e/lib/claude_credentials.py run`). A print guard, a lint and a
janitor enforce both rules, so nothing depends on discipline.

## Implementation status

Implemented. Epic nexus-wauo1 has 40 children, all closed, across Phases 0
to 4 and an amendment phase, 3b. Five follow-ups were closed on 2026-09-26,
after the Phase 4 gate. Develop CI is green on 9ada0033e. At close, the
critic's verdict was `justified`, with 0 Critical and 0 Significant
findings (T2 `nexus_rdr/219-critique-scope-audit-2026-09-26`).

## What diverged

**Claude Code deletes the token from its own environment, so the harness
could not reach nx-mcp's LLM dispatch.** The plan assumed a token in
Claude's environment would reach everything Claude started. It does not.
Claude removes `CLAUDE_CODE_OAUTH_TOKEN` from its environment, so its
children and nx-mcp's `claude -p` dispatch run without it. Phase 2 found this
while migrating harnesses. The amendment (nexus-wauo1.35, Phase 3b) grants
the token to nx-mcp alone under a harness-only name, through a piped
`--mcp-config` env block. The amendment needed three critique rounds, and the
first two returned not-justified. The first draft put the grant in Claude's
environment, which gave Claude's Bash tool the token again. The second allowed
plugin-loaded harnesses before anyone had measured whether the plugin's
`mcp_tool` hooks still resolve. The grant is safe only because no
dispatched `claude -p` gets a shell tool, and
`tests/test_dispatch_grants_no_shell_tool.py` pins that.

*Drift: unvalidated assumption.* One spike, "does a child of `claude` see
the variable", would have found this in Phase 0.

**The print guard stopped denying whole-environment dumps.** For the same
reason, `env` or `printenv` in Claude's Bash tool cannot print the token, so
denying them blocked harmless commands and protected nothing. The guard now
denies keychain reads, credential-file reads, references to a protected
variable name, and reads of another process's environment.

*Drift: framework API detail.*

**The janitor fails on processes and tmux servers as well as files.** The
plan's janitor looked for credential-shaped files. The MVV found five
leftover harness processes and tmux servers that still held the token
variable. After Sam's decision, the janitor fails on those too.

*Drift: missing Day 2 operation.* "What else outlives a killed run" is a
question about the whole process tree, not only the filesystem.

**Host-side launches take the token on fd 3 (follow-up nexus-wauo1.36).**
The plan left the token in Claude's exec environment. Claude removes it only
from its own `process.env`, so a same-user `ps -E` can read it for as long as
Claude runs. That reader is outside the threat model, but the exposure was
cheap to remove. Claude Code also accepts
`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`, so host-side launches now go
through `tests/e2e/lib/claude_fd_exec.sh`. Container harnesses keep
`docker run -e`, because the container's init process holds the variable
whatever Claude does.

*Drift: framework API detail.*

**Plugin-loaded harnesses are proven for 6 of the 9 `mcp_tool` hooks
(follow-up nexus-wauo1.37).** Phase 3b allowed the grant only with
`--strict-mcp-config`. It deferred plugin-loaded harnesses until a
measurement showed their hooks still fire. The measurement shows that
naming the override entry exactly `plugin:conexus:nexus` keeps six hooks
firing identically, on Claude Code 2.1.277. The other three need a
compaction, a failed stop or a named teammate to fire, so they are
inferred, not measured. The earlier override, named plain `nexus`, broke
the tool-tier hooks and the session did not stop.

*Drift: deferred critical constraint.*

## Drift classification

| Category | Count | Examples | Preventable? |
| --- | --- | --- | --- |
| Unvalidated assumption | 1 | Token in Claude's environment reaches its children | Yes, with a spike |
| Framework API detail | 2 | Claude deletes the token from its environment; fd-3 token route | Yes, with a source search of Claude Code's environment handling |
| Missing Day 2 operation | 1 | Janitor did not check processes or tmux servers | Yes, with a janitor that checks the process tree |
| Deferred critical constraint | 1 | Plugin-loaded grant, 6 of 9 hooks measured | Partly; three hooks need rare session events |

## What the RDR got right

- Three rules, each with a mechanical check. The critic found every
  narrowing disclosed and dated, and none silent.
- The threat model is named: accidental disclosure, not a deliberate reader
  running as the same user. This kept the guard from growing into an
  unwinnable sandbox, and reviewers could check each rule against one
  sentence.
- Phase 0 spiked the token's transport and lifetime before any harness
  moved. Four of the five divergences came from Claude Code's process
  behavior, which that spike did not cover.

## What the RDR missed

- How Claude Code treats the variable once it has read it. Three of the
  five divergences come from that one fact.
- Processes as a place a credential can outlive a run.

## Residuals, stated in the RDR

- The janitor checks file contents only in named harness-output folders.
  In the wider `$TMPDIR` and scratchpad roots it checks file names only.
- A4 (the token survives the operator's `/logout` and `/login`) is only
  partly verified. Per-token revocation was seen in the UI, not exercised.
- Three of the nine `mcp_tool` hooks are inferred under the plugin
  override. Re-run the shakeout probe when Claude Code's version moves.

## Takeaways for RDR authoring

1. **Spike the platform's handling of any credential you pass it.** Test
   the direct child and the grandchild, not only whether the value arrives.
   The amendment and the guard rewrite both came from one fact that a
   five-minute spike could have found.
2. **List process-tree residue beside filesystem residue.** When a design
   depends on "nothing left behind", the janitor needs both lists from the
   start.
3. **Put the measured count in every summary line.** Reviewers found
   "every" written for 6 of 9 and "both modes" for evidence from `-p` only,
   in several places, including this RDR's Revision History at close.
