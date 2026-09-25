---
title: "Harness Credentials Never Leave the Keychain: an Automation Token, Passed by Environment"
id: RDR-219
type: Architecture
status: accepted
priority: high
author: Sam
reviewed-by: self
created: 2026-09-25
accepted_date: 2026-09-25
related_issues: [nexus-6konb.15]
related_rdrs: [RDR-215, RDR-079]
---

# RDR-219: Harness Credentials Never Leave the Keychain

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template.

## Problem Statement

Test harnesses that drive a real Claude Code session need a credential. Today
every one of them uses the operator's own interactive login: the OAuth
credential Claude Code keeps in the macOS keychain item
`Claude Code-credentials`. That credential holds a refresh token, which Claude
Code rotates, so any running copy can mint a new live token. The harnesses copy
it into `.credentials.json` files in isolated home directories, containers and
remote test hosts, and many of those copies are never deleted.

On 2026-09-25 a subagent running the nexus-6konb.15 MVV printed the credential
into its own transcript through a redirect mistake. A search the same day found
it in that transcript and in 35 files on disk: 34 from four earlier sessions'
harness runs, plus the persistent repo snapshot. Rotating the login fixes that one leak. It does not stop the next
one, because the practice that produces the copies is unchanged.

### Enumerated gaps to close

#### Gap 1: harnesses write the credential to files, and copies are left behind

Nine distinct paths leave a copy on disk (Research Findings, "Where copies are
left"). Some are by design (a persistent snapshot in the repo tree at mode
0644, a persistent sandbox home), some are gaps (a killed run skips its
cleanup trap, a `--keep` flag skips it on purpose, the connection-race ladder
never deletes its per-run homes), and some are manual (a README recipe that
writes to `/tmp`, hand transfers to the Windows test host). Cleanup discipline
cannot close this: a SIGKILL or a crash always skips it.

#### Gap 2: nothing stops an agent printing the credential

The shared picker writes the credential JSON to stdout for callers to capture.
Any agent can also read the keychain item directly with `security
find-generic-password ... -w`, or `cat` a credential file. One mistaken
redirect puts a live token into a transcript, which is how the 2026-09-25 leak
happened. The single-source lint scans only `.sh` and `.py` under `tests/` and
`scripts/`, and nothing guards what an agent types.

#### Gap 3: the harness identity is the operator's own login

Because harnesses use the interactive login, a leak exposes the operator's
account, and there is no way to revoke the harness's access alone. The coupling
also breaks runs: during the 6konb.15 MVV the operator's keychain credential
rotated and revoked the copy the harness had picked, which failed a session
mid-run with a 401.

Revocation (Phase 0): a `setup-token` token is listed on claude.ai Settings,
Claude Code, with its own revoke control, so the harness token can be revoked
without the operator's login (T2 `nexus_rdr/219-research-8`; seen by the
operator, not exercised).

#### Gap 4: moving a credential to a test host is manual and the guidance contradicts itself

No script moves a credential to the Windows test host (qwentescence) or
removes it afterwards. One project memory authorizes placing it and says not
to scrub it; another says to scrub it when done. The transfer passes through
temporary files on the Windows side, and an earlier round's directory on that
host was never cleaned.

## Relationship to Prior RDRs

Searched the RDR corpus for `credential`, `OAuth`, `keychain` and
`.credentials.json`.

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-215 | Precedent (records the problem, does not address it) | Its veh77 round 4 notes record a credential picked on the Mac, copied to qwentescence, and removed by hand, with round 1's directory left. Evidence for Gap 4. |
| RDR-079 | Checked, no overlap | Operator dispatch runs `claude -p` as the operator in the operator's own home, using the operator's own login in place. Nothing is copied, so it is out of scope. |

The single picker (`tests/e2e/lib/claude_credentials.py`, commit 997f6de77,
nexus-galkv.19) and its lint are the current design of record. It fixed which
keychain item is read. This RDR changes which credential is used and how it
travels.

## Context

### Background

The harnesses exist because unit tests cannot show that a hook fires, a channel
notification arrives or a plugin loads in a real Claude Code session. They run
a real `claude` in a clean environment (`env -i`, an isolated `HOME`) so the
operator's settings and plugins do not leak into the result. A clean home has
no keychain access and no credential, so the harnesses copy one in.

### Technical Environment

Claude Code 2.1.28x on macOS, where the interactive login lives in the login
keychain; Linux containers under Docker; WSL2 Ubuntu on qwentescence reached
over ssh. Harnesses: `tests/cc-validation/runner.sh`, `tests/e2e/run.sh`,
`tests/e2e/release-sandbox.sh` and `sandbox.sh`, `tests/e2e/rdr208-mvv`,
`tests/e2e/hook-surface-shakeout`, `tests/e2e/migration-rehearsal` (`--fullstack`,
`--shakeout-e2e`), `tests/cc-validation/connection-race-ladder/run_ladder.py`,
and ad hoc MVV scripts in agent scratchpads.

## Research Findings

### Investigation

A read-only inventory of the repository (2026-09-25) found 26 sites that read,
copy, write or print a Claude credential, plus the manual transfer recorded in
project memory and RDR-215. A search of the local disk the same day found
credential-shaped strings in 34 files under `/private/tmp` and `/tmp`, in the
persistent repo snapshot, and in one subagent transcript (35 files in total).

The documentation for Claude Code's automation authentication was read for the
alternatives to a copied login.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Claude Code authentication | Docs only | `claude setup-token` produces a one-year OAuth token for Pro/Max/Team/Enterprise plans, read from `CLAUDE_CODE_OAUTH_TOKEN`; it takes precedence over keychain credentials and works in interactive sessions unless `/login` is run; it does not work in bare mode. `ANTHROPIC_API_KEY` works in all modes. `apiKeyHelper` runs a command for a key every 5 minutes; its interaction with subscription login is not documented. |

### Key Discoveries

- **Documented:** the picker writes nothing itself; it prints the credential
  JSON to stdout (`claude_credentials.py:148`). Every copy is made by a caller.
- **Documented:** where copies are left. The nine mechanisms:
  1. the persistent snapshot `tests/e2e/.claude-auth/.credentials.json`
     (gitignored, mode 0644), written by `auth-login.sh` and refreshed on every
     cc-validation run (`runner.sh:251`);
  2. the persistent sandbox home `~/nexus-sandbox/.claude/.credentials.json`,
     which the live CLI then rotates in place, plus a plaintext
     `ANTHROPIC_API_KEY` in `~/nexus-sandbox/activate`;
  3. SIGKILL, a crash or power loss during a harness whose EXIT trap would
     have removed its copy; `mktemp` stages are never revisited;
  4. `hook-surface-shakeout/run.sh --keep`, which skips the trap;
  5. `run_ladder.py`, which writes one credential file per run and never
     deletes run directories;
  6. the `tests/cc-validation/README.md` probe recipe, which writes to
     `/tmp/mcp-probe` with an unscoped keychain read and no cleanup;
  7. the manual qwentescence transfer, including Windows-side temp files and
     `.bak` backups;
  8. refresh-token rotation: any writable copy used by a live `claude`
     becomes a new live token wherever it sits;
  9. a plaintext `ANTHROPIC_API_KEY` written into `.env.test`.
- **Documented:** `CLAUDE_CODE_OAUTH_TOKEN` is an environment variable, so a
  harness can pass it to a child process, a container (`docker run -e`) or a
  remote command without writing any file.
- **Assumed:** a `setup-token` token is independent of the interactive login
  and survives the operator's `/logout` and `/login`. Not documented.
- **Assumed:** a single token can be revoked without revoking the login. Not
  documented; revocation may be account-wide.

### Critical Assumptions

- [x] A1: an interactive Claude Code session under tmux, with an isolated `HOME`
  (no keychain) and the harness flags (`--dangerously-skip-permissions`,
  `--dangerously-load-development-channels`, `--mcp-config`,
  `--plugin-dir`), authenticates from `CLAUDE_CODE_OAUTH_TOKEN` alone.
  — **Status**: Verified (T2 `nexus_rdr/219-research-9`) — **Method**: Spike
- [x] A2: the same holds inside the Linux containers the harnesses use, with the
  token passed as `docker run -e`. — **Status**: Verified (T2
  `nexus_rdr/219-research-10`) — **Method**: Spike
- [x] A3: the same holds on qwentescence's WSL2 Ubuntu with the token passed in
  the environment of the ssh command. — **Status**: Verified (T2
  `nexus_rdr/219-research-11`) — **Method**: Spike
  Tested shape, disclosed: the token was not placed in the ssh command's
  environment. It travelled on the ssh channel's stdin and was read and exported
  inside the remote user's shell, the transport this RDR's Technical Design
  already specifies for `--remote`. The assumption as worded was not tested
  literally; the shape the design uses was.
- [~] A4: the token keeps working after the operator runs `/logout` and `/login`
  on the Mac. — **Status**: Partly verified (T2 `nexus_rdr/219-research-12`)
  — **Method**: Spike
  Tested shape, disclosed: the token kept working after an operator re-login
  that followed its creation. That re-login is not confirmed to have been an
  explicit `/logout` then `/login`, and the operator chose not to test it
  further. Day 2 Operations carries the remedy if a later `/logout` does break
  it.

Phase 0 outcome (2026-09-25): A1 to A3 pass and A4 passes in the tested shape
(partly verified, above), so no launch shape uses the file fallback in Failure
Modes. Claude Code wrote the environment token to no file in
any run, so rule 2 holds (T2 `nexus_rdr/219-research-13`). A2, A3 and A4 were
checked in the isolated `HOME` only (A2's container is removed with the run;
A4 is the same host and shape as A1); the first A1 runs also searched only the
`HOME`; a rerun of A1 also searched `/private/tmp`,
the operator's per-user temp directory under `/private/var/folders` and
`/private/var/tmp`, before and after the run, and the only new match was the
Claude Code binary itself, byte-identical to the installed one. The `oauthAccount`
seed in `.claude.json` is not needed: a `.claude.json` holding only
`{"hasCompletedOnboarding":true}` authenticated in all three shapes (T2
`nexus_rdr/219-research-14`). A negative control, the same isolated home with
no token, recorded "Not logged in · Run /login" in the status bar and answered
the prompt with "Not logged in · Please run /login" (captured on the second
attempt; the first scripted control exited at the trust dialog and captured
nothing, T2 `nexus_rdr/219-research-9`), so the operator's own login does not
reach an isolated `HOME`.

## Proposed Solution

### Approach

Three rules:

1. **Harnesses never use the interactive login.** They use a dedicated
   automation token from `claude setup-token`, kept in its own keychain item
   (`nexus-automation-oauth-token`). The item `Claude Code-credentials` is read
   by nothing in this repository.
2. **The token travels in the environment, never in a file.** One helper sets
   `CLAUDE_CODE_OAUTH_TOKEN` in a child process's environment and runs the
   child. The token never passes through a caller's shell variable, stdout or
   a file, so there is nothing to leave behind and nothing to print. (The
   caller is the local code that invokes the helper; on `--remote`, the
   helper's own remote side reads the token from stdin into the environment it
   then execs.)
3. **Guards, not discipline.** A plugin PreToolUse hook denies commands that
   would print a keychain credential. A lint forbids credential files and
   keychain reads anywhere outside the helper, markdown included. A janitor
   check fails when a credential-shaped file exists under the known roots.

### Technical Design

**The helper** (`tests/e2e/lib/claude_credentials.py`):

```text
claude_credentials.py run [--remote HOST] -- <command> [args...]
    Reads the automation token from the keychain item, exits non-zero naming the
    remedy if it is absent or expired, and execs <command> with
    CLAUDE_CODE_OAUTH_TOKEN set in its environment. Prints nothing else.
    With --remote, runs <command> over ssh with the token in the remote
    command's environment, sent on the ssh channel's stdin, never on argv.
claude_credentials.py status
    Reports whether the automation token is present and its age. Prints no
    token material.
```

`pick` and any mode that prints credential material to stdout are removed.
Harnesses launch their `claude` (or the tmux server, container or remote shell
that will launch it) under `run --`.

Transport rules the Phase 0 spike established (T2 `nexus_rdr/219-spike-script`):
- The token never appears on any process's argument list. `env -i
  CLAUDE_CODE_OAUTH_TOKEN=<value> ...` puts it on `env`'s argv, so `run` sets it
  in the environment it passes to `exec`, never as an argument.
- tmux sessions take their environment from the tmux server, so a harness
  starts a private tmux server (`tmux -L <name>`) under `run --`, never a
  session on a shared server.
- `docker run -e CLAUDE_CODE_OAUTH_TOKEN` (no value) keeps the token off docker's
  argv, but `docker inspect` can read it while the container exists, so
  containers run with `--rm`.
- On qwentescence the ssh endpoint is PowerShell, which splits a command at `|`
  even inside double quotes, so `--remote` ships the remote script and the token
  on the ssh channel's stdin and enters WSL with `wsl -d Ubuntu -u nexus`.
- A launched session's trust and bypass-permissions dialogs default to exit, so
  a harness pre-seeds them or selects the proceed option.

**Migration of the 26 sites.** Each site from the inventory moves to `run --`
and drops its `.credentials.json` write. The persistent snapshot and
`auth-login.sh` are deleted. `~/nexus-sandbox` stops receiving a
credential. `run_ladder.py`'s `--cred-cmd`/`--cred-file` become the
environment pass-through. Plaintext `ANTHROPIC_API_KEY` writes into
`.env.test` and `activate` are removed; a harness that needs an API key uses
the same environment pass-through.

**The plugin guard.** A conexus PreToolUse hook on Bash denies, with a message
naming the helper:
- `security find-generic-password` or `security dump-keychain -d` naming either
  credential item;
- reading a `.credentials.json` file to stdout (`cat`, `less`, `head`, `tail`,
  `jq`, `python -c` open/print shapes);
- printing `CLAUDE_CODE_OAUTH_TOKEN` (`echo $CLAUDE_CODE_OAUTH_TOKEN`, `env`,
  `printenv`, `set` piped to a file or displayed).

It ships as a self-contained stdlib plugin script with no `nexus` import,
declared directly under the PreToolUse Bash matcher in `conexus/hooks/hooks.json`,
as `routing/subagent_git_write_requires_orchestrator.py` is. It depends on no
CLI verb, so an older installed CLI cannot change its behaviour. It protects
every conexus user, not only this repository.

**The lint.** `tests/test_claude_credentials_single_source_lint.py` widens to
all tracked text files, `.md` included, and forbids outside the helper:
writing a `.credentials.json`, reading either keychain item, and assigning
`CLAUDE_CODE_OAUTH_TOKEN` a literal.

**The janitor.** A check that scans `$TMPDIR`, the agent scratchpad roots, the
repository tree and `~/nexus-sandbox` for files containing the credential
pattern and names each one, as a release-battery leg that fails on any find.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Credential helper | `tests/e2e/lib/claude_credentials.py` | Extend: add `run`, `status`; remove `pick`'s stdout mode |
| Single-source lint | `tests/test_claude_credentials_single_source_lint.py` | Extend: scope and forbidden shapes |
| Plugin guard | `conexus/hooks/hooks.json`, `routing/subagent_git_write_requires_orchestrator.py` (precedent) | Extend: one new PreToolUse Bash entry |
| Janitor | release battery (`tests/e2e/release-battery.sh`) | Extend: one new leg |
| Persistent snapshot | `tests/e2e/auth-login.sh`, `.claude-auth/` | Replace: deleted |

### Decision Rationale

Removing the copy removes the whole class: a copy that never exists cannot be
left behind by a crash, a `--keep` or a forgotten step. A separate token
separates the harness's identity from the operator's, so a leak or a rotation
affects one and not the other. The guard and the lint cover the two ways the
rules could be broken later: an agent's command and a new harness.

## Alternatives Considered

### Alternative 1: keep copying the login, but clean up reliably

**Description**: every harness writes its copy under one `mktemp` root with a
trap, plus a janitor that deletes leftovers.

**Pros**: no change to how sessions authenticate.

**Cons**: a SIGKILL or crash still leaves a copy until the janitor runs; the
copy is still the operator's rotating login; the printing risk is unchanged.

**Reason for rejection**: it narrows Gap 1 and closes none of the others.

### Alternative 2: `apiKeyHelper`

**Description**: settings name a command that reads the secret store at run
time, so nothing is written.

**Cons**: documented for API keys; its interaction with subscription login is
not documented, and it does not apply in bare mode.

**Reason for rejection**: it relies on undocumented behaviour.

### Briefly Rejected

- **An Anthropic API key for every harness**: bills per token instead of the
  subscription, and is still a long-lived secret; available through the same
  environment pass-through for any harness that needs it.

## Trade-offs

### Consequences

- Positive: no credential file exists for a harness run, so there is nothing
  to clean up and nothing a crash can leave.
- Positive: the operator's `/logout` and `/login`, or a leak of the
  automation token, affect only one of the two identities.
- Negative: one more secret to create (once a year) and store.
- Negative: an agent can no longer inspect a credential, even when debugging
  auth; `status` is the supported view.

### Risks and Mitigations

- **Risk**: every fresh isolated `HOME` makes Claude Code self-install about
  222 MB on first launch, and a container or WSL session may auto-update
  mid-run, so two runs can use different versions (T2
  `nexus_rdr/219-research-13`).
  **Mitigation**: Phase 2 harnesses share one installed Claude Code binary
  across isolated homes (for example a read-only mount of
  `.local/share/claude/versions`, which holds no secret) and disable the
  auto-update, so a run's version is chosen, not discovered.
- **Risk**: A1 fails for interactive sessions.
  **Mitigation**: the fallback below; the environment path still serves
  containers and `-p` runs.
- **Risk**: the guard's patterns miss a printing shape.
  **Mitigation**: the janitor and the lint catch files; the guard is one layer,
  not the only one.
- **Risk**: the token expires after a year and harnesses fail.
  **Mitigation**: `status` reports the age; the doctor or battery warns at 30
  days to expiry.

### Failure Modes

- Token absent or expired: `run` exits non-zero naming `claude setup-token`
  and the keychain item. Loud, never a silent unauthenticated session.
- A1 fails: interactive harnesses write the automation token only, into a
  mode-0700 `mktemp` directory whose trap is set before the write. Containers
  mount it read-only, so a live session cannot rotate it. The janitor still
  catches a leftover.
- Guard false positive on a legitimate command: the message names `status`
  and the helper.

## Implementation Plan

### Prerequisites

- [x] Critical Assumptions A1 to A4 settled by the Phase 0 spike (A4 partly verified, accepted by the operator).
- [x] The operator has run `claude setup-token` and stored the token in the
  keychain item `nexus-automation-oauth-token`.

### Minimum Viable Validation

With the automation token in its own keychain item and no
`Claude Code-credentials` read anywhere: the cc-validation runner, an
rdr208-mvv container run and one interactive tmux session each authenticate
through `run --`; after each, the janitor finds zero credential files under
the known roots on this Mac, and on qwentescence an ssh `find` over the
remote home and temporary directories finds none after the remote run; and an
agent session's attempt to run `security
find-generic-password -s "Claude Code-credentials" -w` is denied by the plugin
guard.

### Phase 0: Spike

#### Step 1: Settle A1 to A4

One cc-validation scenario, run with the operator's automation token, records
one observation per assumption: an isolated-HOME interactive tmux session (A1),
a harness container (A2), a qwentescence WSL2 session over ssh (A3), and a
harness run after the operator's `/logout` and `/login` (A4). It also records
what the account settings offer for revoking one token alone.

#### Step 2: Choose the path for any failed assumption

A failed A1, A2 or A3 moves that launch shape to the fallback in Failure Modes
(the automation token in a mode-0700 `mktemp` directory, trap set before the
write). A failed A4 is recorded as a Day 2 constraint on the operator. The
choices are written into this RDR before Phase 1 starts.

### Phase 1: The helper and the automation identity

#### Step 1: `run` and `status`

Add `run [--remote HOST] -- <command>` and `status` to the helper, with tests
for an absent, an expired and a present token, and a test that the helper
itself prints no token material.

#### Step 2: Retire `pick`'s stdout mode

Keep `pick` until the last caller migrates (Phase 2), then remove it together
with its stdout output.

### Phase 2: Migrate the harnesses

#### Step 1: One harness per commit

Each inventory site moves to `run --` and drops its `.credentials.json` write.
Each commit is proved by that harness's own run.

#### Step 2: Delete the persistent copies

Delete `auth-login.sh` and `tests/e2e/.claude-auth/` outright (the
`oauthAccount` seed is not needed, T2 `nexus_rdr/219-research-14`), the sandbox
credential, and the plaintext API-key writes, and rewrite the README probe
recipe.

### Phase 3: Guards

#### Step 1: The plugin PreToolUse guard

Add the guard as a self-contained stdlib plugin script, declared directly under
the PreToolUse Bash matcher in `conexus/hooks/hooks.json` and listed in
`conexus/PENDING_RELEASE.md`, with a positive control for each denied shape.

#### Step 2: The widened lint and the janitor leg

Widen the single-source lint to all tracked text files, and add the janitor
leg to the release battery, each with a positive control that fires on a
planted violation. Run the Minimum Viable Validation.

### Phase 4: Records and cleanup

#### Step 1: Local cleanup (done 2026-09-25)

The 35 credential files on this Mac were deleted and the transcript redacted on
2026-09-25, before this RDR's implementation (T2 `nexus_rdr/219-research-2`).

#### Step 2: Remote cleanup and records

Delete the copies on qwentescence by explicit path, and correct the
contradictory project memories and the stale cc-validation notes.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| Keychain item `nexus-automation-oauth-token` | N/A | `claude_credentials.py status` | `security delete-generic-password` (operator) removes the local copy; a leaked token is revoked on claude.ai Settings, Claude Code | `status` and the janitor | None: regenerate with `claude setup-token` |
| Automation token after an operator `/logout` | N/A | a harness run fails to authenticate | N/A | the next harness run | Regenerate with `claude setup-token` and store it again (A4 is only partly verified) |

## Test Plan

- **Scenario**: token present — **Verify**: `run -- env` shows
  `CLAUDE_CODE_OAUTH_TOKEN` set in the child and nothing on the helper's own
  stdout.
- **Scenario**: token absent or expired — **Verify**: non-zero exit naming the
  remedy; the child never runs.
- **Scenario**: a harness killed with SIGKILL mid-run — **Verify**: the janitor
  finds nothing afterwards.
- **Scenario**: the plugin guard — **Verify**: each forbidden shape is denied,
  and `claude_credentials.py status` and `run --` are allowed.
- **Scenario**: the lint — **Verify**: a planted `.credentials.json` write in a
  `.md` recipe and in a `.sh` file each fail it.
- **Scenario**: the operator's `/logout` and `/login` — **Verify**: the next
  harness run still authenticates (A4).

## Validation

### Testing Strategy

The Minimum Viable Validation above, run on this Mac and on qwentescence. The
janitor's roots are local to this Mac, so the qwentescence leg is checked by
the ssh `find` the Minimum Viable Validation names, not by the janitor.

### Performance Expectations

N/A.

## Finalization Gate

### Contradiction Check

No contradictions found between the research findings and the proposed
solution. One tension is stated and resolved: the rule "no credential file"
and the fallback that writes one for a launch shape where A1, A2 or A3 fails.
The fallback applies only to a shape Phase 0 shows cannot use the environment,
holds only the automation token (never the interactive login), and is caught by
the janitor if left behind.

### Assumption Verification

At gate time (before Phase 0), A1 to A4 and per-token revocation were unverified
(T2 `nexus_rdr/219-research-4` and `-5`, both assumed, docs only). Phase 0 has
since settled them; the current status is in Critical Assumptions above (A1 to
A3 verified, A4 partly verified) and revocation under Gap 3. No other
assumption carries the design: the inventory and the disk scan are recorded
evidence (`219-research-1` and `-2`).

### Scope Verification

The Minimum Viable Validation is in scope and runs in Phase 3.

### Cross-Cutting Concerns

- **Secret/credential lifecycle**: generation by the operator with `claude
  setup-token`; storage in its own keychain item; one-year rotation with a
  30-day warning; override by `ANTHROPIC_API_KEY` through the same helper.
- **Incremental adoption**: harnesses migrate one at a time; `pick` is removed
  only after the last caller.
- **Deployment model**: the guard ships in the conexus plugin as a plugin-surface
  change with no CLI dependency, listed in `conexus/PENDING_RELEASE.md`, so a
  plugin-only cut or a client release carries it.
- Others: N/A.

### Proportionality

Right-sized for a change that touches every harness that drives a real Claude
Code session, adds a plugin hook that every conexus user receives, and handles
a secret. The inventory tables are kept because the migration works through
them site by site. Nothing is designed for launch shapes not in the inventory.

## References

- `tests/e2e/lib/claude_credentials.py`, `tests/test_claude_credentials_single_source_lint.py`
- The 2026-09-25 credential inventory (26 sites, 9 mechanisms), from this
  RDR's research pass
- T2 `nexus_rdr/6konb15-mvv-2026-09-25` (the mid-run 401)
- T2 `nexus_rdr/219-research-2` (the transcript leak and the disk scan)
- Claude Code documentation: authentication, `claude setup-token`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `apiKeyHelper`

## Revision History

### 2026-09-25 — Created

Drafted at Sam's direction after the credential leak in the nexus-6konb.15 MVV:
fix the practice that makes copies before cleaning up the copies. Sam chose an
RDR over an epic, and the conexus plugin as the guard's home.
- 2026-09-25: Gate round 1 — BLOCKED (1 Critical, 2 Significant, 1 ship-blocker(s)); commit `36217ae7f`; critique `nexus_rdr/219-gate-critique-2026-09-25-r1`.
- 2026-09-25: Gate round 2 — PASSED (0 Critical, 2 Significant, 0 ship-blocker(s)); commit `a50c35f27`; critique `nexus_rdr/219-gate-critique-2026-09-25-r2`.
- 2026-09-25: Gate round 2 Significants fixed before accept in `e833d41a4` (fix check `nexus_rdr/219-fix-check-e833d41a4`, PASS).
- 2026-09-25: Accepted by Sam.
- 2026-09-25: Phase 0 outcome recorded (nexus-wauo1.2): A1 to A4 verified, no fallback shape, no disk write, seed not needed, revocation per token; spike transport rules added to Technical Design.
- 2026-09-25: Phase 0 critique fixes (nexus-wauo1.4): accepted A3/A4 wording restored with disclosed tested shapes (A4 partly verified), negative control and wider disk check re-run and cited, gate section pointed at the Phase 0 outcome, self-install cost and Day 2 revocation added.
