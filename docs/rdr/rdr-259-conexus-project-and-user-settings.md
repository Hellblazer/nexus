---
title: "conexus project and user settings: replacing the person-keyed audience model"
id: RDR-259
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: pending
created: 2026-11-25
accepted_date:
related_issues: [nexus-sby49, nexus-sby49.3, nexus-sby49.4, nexus-5y47c]
related: [RDR-184, RDR-219, RDR-143, RDR-201, RDR-245, RDR-253]
---

# RDR-259: conexus project and user settings

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.
> Prose: see REGISTER.md beside this template. Write for a smart reader who
> may not know the jargon; define terms on first use; simplified, never
> simplistic.

Terms. **conexus** is the Claude Code plugin this repository ships; **nx** is
its command-line client, published on PyPI as `conexus`. A **hook** is a
program Claude Code runs at a fixed point (before a tool call, when a
subagent starts, when a turn stops) and whose output can allow, deny, or add
text. A **command hook** is a hook Claude Code runs as a separate process;
the plugin's command hooks are Python scripts that may use only the Python
standard library, because they must work when nx is not installed. The
**payload** is the JSON object Claude Code hands a hook; its `cwd` field is
the directory the session or subagent is working in. A **subagent** is a
Claude instance a session dispatches through the Agent tool. A **linked
worktree** is a second working directory of the same git repository
(`git worktree add`); the **primary checkout** is the original one. The
**subagent git guard** is the PreToolUse hook that stops a subagent from
writing git state in a checkout another agent is using; its **full** (strict)
form denies index-writing and work-destroying git verbs in the shared tree,
and its **seatbelt** form denies only a short list of unrecoverable ones
(decision D1 of the portability plan). An **advice row** is one entry of the
destructive-action advice hook's table (a pattern kill, a whole-disk scan); it
warns by default. **Telemetry** here means two records the hooks can send to
the user's own nexus engine: `routing_events` (each guard deny or advice,
with a fragment of the command) and the **dispatch record** (each finished
subagent's type and how many T1 notes it wrote). **T1** is session scratch.
**P0** is a machine with the plugin and no nx; **P2** is a full install.

Origin. The plugin portability work (epic nexus-sby49) built an "audience"
model: each hook rule fired for every user, for "the nexus repo", or for
"Sam", decided by a tracked marker file in the checkout and a flag file in
the OS account's home. On 2026-11-25 Sam rejected the person half of that
model: "you are confusing the dev process with nexus specic things apparently
and vice versa". His frame: the development process is product for every
user, and nexus specifics are leaks. He then ruled nine decisions, one at a
time (T2 `nexus/sam-ruling-audience-model-settings-not-identity-2026-11-25`),
which set the direction this RDR specifies. He called the result foundational
for users and for nexus and asked for careful, long-range decisions.

## Problem Statement

The plugin decides what a session gets by asking who the user is and whether
the checkout is nexus. That question has no good answer for a product. It
withholds the plugin's own process from the people the plugin is for, it
gives a different product to the same person on different machines, and it
gives nexus contributors "Sam's" rules for being in Sam's repository. The
rulings replace it with two ordinary settings files any user or project can
write. What is not yet decided is everything that makes a settings file a
durable contract: where the files live, their schema and how it grows, how a
project value and a user value combine for each key, what a broken file does,
how a user finds out the files exist and what they currently say, how the
change reaches users who have neither file, and what keeps a repository from
using its file to weaken a protection.

### Enumerated gaps to close

#### Gap 1: Behaviour depends on who the user is

`account_flag()` (the file `<account home>/.config/nexus/maintainer-profile`)
and `maintainer()` (that flag OR the repo marker) gate the full git guard,
four SubagentStart texts, a SessionStart sentence, the T1 nudge for Claude
Code's built-in agents, both telemetry records, an auto-approve widening, and
the compaction text (census rows 1-12 and 18 of the analysis, Research 1).
Ruling 1 removes both predicates. Nothing yet says what replaces each use, or
what stops the person signal from coming back.

#### Gap 2: No settings contract exists

The rework branch reads `.nexus/project.json` and
`<account home>/.config/nexus/conexus.json` (Research 3), but the keys, types,
defaults, size limits, version field, unknown-key and deprecated-key handling
were chosen in passing by the implementer and are recorded nowhere a user can
read. A settings file is a public contract the moment one user writes one;
changing it later breaks their file silently.

#### Gap 3: Precedence and "tighten-only" are undefined per key

The rulings say a project file is tighten-only and telemetry is user-only.
They do not say, key by key, which file may set it, how the two values
combine, and what "tighter" means for a list, a text, or a branch name.

#### Gap 4: A broken file fails silently

On the rework branch a malformed file, a wrong type, a misspelled key or a
value a project may not set is ignored without a word anywhere (developer
item 2, Research 4). A user who writes `"subagent-git": "strict"` believes the
full guard protects them and it does not.

#### Gap 5: Users cannot discover or inspect the settings

There is no command that shows the effective settings for a directory and
where each value came from, and no page a new user reads first.

#### Gap 6: Migration and compatibility are unplanned

8.5.7 users have no files; Sam's own sessions depend on the flag; old
plugins meet new checkouts and the reverse; two legacy marker files must be
accepted for a window and then removed; and the Phase 2 gating on `develop`
(unreleased) must never ship.

#### Gap 7: The relationship to Claude Code's own configuration is unsettled

Claude Code has project and user `settings.json`, permission rules,
`CLAUDE.md`, and plugin `userConfig`. The design has to say why the plugin
keeps its own files instead of using those, and the docs left two questions
open: which `.claude/settings.json` a worktree-isolated subagent runs under,
and whether a permission `ask` rule survives the plugin's PermissionRequest
auto-approve. The spikes answered both (Research 10). The second answer is a
defect in every shipped release: the plugin's PermissionRequest allow
silently overrides a user's `ask` rule for any conexus tool. A further spike
shows the PermissionRequest answer is not needed for the auto-approval
itself: an allow given on PreToolUse alone approves a tool that has no rule,
and leaves an `ask` or `deny` rule in force (Research 11, record 33).

#### Gap 8: A repository could use its file to weaken a protection

A tracked file is written by whoever controls the repository, and a
subagent can edit the working-tree copy. Nothing yet enforces "no setting a
repository controls may loosen anything", or lists the loosenings that
remain and why each is acceptable.

#### Gap 9: The guard fix and the settings rework collide

The subagent git guard over-match fix (nexus-5y47c, reviewed and confirmed
at 86a5fa0ff1) chooses full guard or seatbelt through `maintainer()`, and
the rework replaces that line. The merge order and the shape of the gate on
the new reader are undecided.

## Relationship to Prior RDRs

Searched `docs/rdr/` titles and T2 for "audience", "maintainer", "plugin
portability", "settings", "git guard" and "credential".

| Prior RDR | Relationship | What it means for this one |
| --- | --- | --- |
| RDR-184 (orchestration protocol hardening, closed) | Origin | Created the subagent git guard and scoped itself to "the nexus project". Its incidents (a subagent commit in the shared tree, a reviewer's `checkout --` wiping uncommitted fixes) can happen to any multi-agent user, so the scope statement no longer holds; the guard becomes a product feature with a setting. |
| RDR-219 (harness credentials never leave the keychain, closed) | Precedent | Its credential deny stays for every user. Only the redirect text naming nexus's wrapper becomes a project setting (`credential_helper`). |
| RDR-143 (plugin and CLI version lockstep, closed) | Precedent | It upgrades the CLI in a detached process, so lockstep arrives at the next session ("Shape B achieves **next-session** lockstep, not within-session lockstep", rdr-143:70), and it skips editable installs ("skipping editable / dev installs", rdr-143:70). Mixed-version sessions are therefore normal for one session after any upgrade and indefinite on an editable install (record 36); Decision 7 plans for both. |
| RDR-215 (plugin hooks as nx verbs, closed) | Precedent | It moved the hooks behind `nx-hook` verbs reached through `nx_hook_shim.py`, whose reason is that "a plugin can update before its CLI does" (`nx_hook_shim.py:5-6`). The auto-approve hook is one of those verbs (`auto_approve.py:3`), so a change to its answers ships with the CLI (record 36). |
| RDR-023 (agent tool permissions audit) and RDR-035 (plugin agent MCP tool access) | Origin | The PermissionRequest auto-approver dates from their period (commit 240731431b, 2026-04-05), when RDR-035 recorded "GitHub #21460 - PreToolUse hooks not enforced on subagent tool calls" (rdr-035:20). The PreToolUse registration was added later, 2026-08-28 (91c0cddebc), because "PermissionRequest fires only when a permission prompt would be shown" and auto mode never consults it. The 2026-11-26 spike shows the PreToolUse allow now reaches subagent calls (record 33), so the reason for the PermissionRequest answer has expired (Decision 9). |
| RDR-242 (sandboxed Lua runtime) | Adjacent | Its S5 made a project `.claude/settings.json` env value, `NX_PROCEDURES_REPO_ROOT=1`, the switch that turns on a repository's own `.nexus/procedures`; nexus turns it on that way (rdr-242:1344, record 40). That is an environment switch a repository controls, outside conexus hook settings. Decision 9 lists it among the loosenings that remain, with Sam's ruling as its reason; this RDR does not move it. |
| RDR-201 (closed vocabularies as checked tables, closed) | Precedent | The settings keys and their allowed values are a closed vocabulary. Decision 2 keeps them in one checked table that the docs, the JSON Schema files and the readers are tested against. |
| RDR-245 (structured asks on the threads board) | Adjacent | Its answer frame names "Sam" (developer item 16). Decision 12 treats that as a leak with a contract attached and leaves the switch to Sam. |
| RDR-253 (session identity, accepted) | Adjacent, no overlap | RDR-253's "identity" is a long-lived session seat, unrelated to the person signal removed here. Nothing in this RDR reads an identity, and no setting is keyed on one. |

## Context

### Background

The portability audit (`~/.local/state/nexus/plans/plugin-portability-analysis.md`)
sorted items written for nexus development into one class and gated the whole
class. That class held both generic process (the git guard, the T1 nudge, the
brief check, the Stop wake guard) and the nexus words inside it (bead ids,
`develop`, `nx ci`, the lab hosts, Sam's name). The plan then wrote down the
rule "for every SAM-WIDE row, the evidence is that the incident corpus came
from Sam's sessions", which reads "the bug happened to Sam" as "only Sam needs
the fix" (analysis section 2). Phase 1 shipped in 8.5.7 with only the git
guard gated; Phase 2 (more gates) is on `develop`, unreleased; Phase 3 is on
the branch `feature/nexus-sby49.4-plugin-portability-p3`, where the rework to
settings is in progress at WIP commit d6a0b258b0.

### Technical Environment

- The plugin's command hooks run under a bare Python interpreter with the
  standard library only (`conexus/hooks/scripts/`). JSON parses there; YAML
  does not.
- `src/nexus/hooks/` holds the nx-side hooks (`nx-hook`, the MCP server's
  hook tools), which keep a byte-identical copy of the helper module
  `_audience.py`; `tests/hooks/test_audience_parity.py` checks the bytes.
- nx's own configuration is `~/.config/nexus/config.yml` (YAML, holds
  credentials, relocatable with the `NEXUS_CONFIG_DIR` environment variable,
  `src/nexus/config.py:836-853`) plus a per-repo `.nexus.yml` that is
  gitignored by default (`docs/configuration.md`, "Config Hierarchy").
- Claude Code facts used below are in Research 6, each with its doc page.

## Research Findings

### Investigation

Read: the analysis (`~/.local/state/nexus/plans/audience-model-analysis.md`),
the plan of record (`~/.local/state/nexus/plans/plugin-portability-plan.md`),
Sam's rulings (T2 above), the current helper
(`conexus/hooks/scripts/_audience.py` on develop at 870bf29e70), the manifest
(`conexus/hooks/audience.yaml`), `docs/agents/plugin-audiences.md`, the
rework's readers and call sites at d6a0b258b0 (worktree
`nexus-wt/halcyon-sby49-p3`), the implementer's 22 open choices (T1
`de007438`, tag `rdr-259-inputs`), the guard fix at 86a5fa0ff1 and its
confirmation record (T2 `nexus/confirm-86a5fa0ff1-nexus-5y47c-2026-11-26-r2`),
and the current Claude Code docs (Research 6).

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| Claude Code permissions and hooks | Docs (code.claude.com, fetched 2026-11-25) | Research 6 and 10. A PreToolUse allow does not bypass `ask` or `deny` rules (docs); a PermissionRequest allow does answer an `ask` rule's prompt (spike, record 26). |
| Claude Code plugin `userConfig` | Docs | User or managed scope only; values exported to hooks as environment variables. |
| Python `json`, `os`, `difflib` | Stdlib | Available in every interpreter the hooks run under. |

### Key Discoveries

**Research 1 (Documented, source reading). What the person signal gates.**
The analysis census, section 1, lists 23 items. Under `maintainer()`: the
full git guard (`routing/subagent_git_write_requires_orchestrator.py`, `body()`),
the SubagentStart Git row and search-scope row and brief-check questions 1, 2,
4 and 5 (`src/nexus/hooks/subagent_start.py`), the SessionStart dispatch
sentence (`session_context.py`), the T1 nudge for agents that are not conexus
agents and the dispatch record (`subagent_stop_t1_nudge.py`), routing telemetry
(`routing/_lib.py`), and on the Phase 3 branch the compaction board text and an
advice deny mode. Under `account_flag()` alone: the auto-approve widening for
seven tools. Only the git guard gate is in a shipped release (8.5.7); the rest
is Phase 2 or Phase 3 and has reached no user.

**Research 2 (Documented). 8.5.7 users already get most withheld process.**
Because Phase 2 is unreleased, 8.5.7 users still receive the Git row, the
search-scope row, the brief check, the dispatch sentence, the T1 nudge and the
telemetry, with nexus's ids in the text (analysis section 1, "What 8.5.7 users
actually experience"). Giving these to every user is 8.5.7's behaviour minus
the nexus words, not a new behaviour.

**Research 3 (Documented, source reading of d6a0b258b0). What the rework reads.**
`project_settings(payload)` walks from the payload's `cwd` (then
`CLAUDE_PROJECT_DIR`) to the first `.git` entry and parses
`.nexus/project.json` there, keeping only `subagent_git: "strict"`, the text
keys `preflight`, `ci_wait`, `credential_helper`, and the lists
`brief_questions` and `advice_block`. A file that exists but cannot be read or
parsed answers `{"subagent_git": "strict"}`. With no `project.json`, either
legacy marker (`.nexus/dev-process.json`, `scripts/agent-worktree-preflight.sh`)
reads as strict. `user_settings()` reads `conexus.json` from the OS account
home with defaults `subagent_git: seatbelt`, `telemetry: false`,
`t1_nudge: true`, `advice_block: []`, and falls back to the defaults on any
error. The project file nexus carries on that branch sets `schema: 1`,
`subagent_git`, `preflight`, `ci_wait`, `credential_helper` and one brief
question.

**Research 4 (Documented). The rework fails silently.**
Every reader swallows every error into a default with no log line, no
message, and no inspection command (developer item 2). A misspelled key, a
wrong type, or a project value that a project may not set all vanish.

**Research 5 (Verified by reading the ref file). nexus's default branch is
not its integration branch.** `origin/HEAD` in the nexus primary checkout is
`refs/remotes/origin/main`, while work lands on `develop`. The rework's
compaction count ("commits not landed") tries `origin/HEAD` first, so in nexus
it would count everything on `develop` that is not yet on `main`. Its last
fallback, `origin/develop`, is a nexus guess built into the plugin (developer
item 15).

**Research 6 (Documented, Claude Code docs fetched 2026-11-25; pages at
https://code.claude.com/docs/en/).**

- *Permissions over hooks* (`permissions.md`, "Precedence among rules, hooks,
  and mods"): "PreToolUse hook decisions don't bypass permission rules. Claude
  Code evaluates deny and ask rules regardless of what a PreToolUse hook
  returns: a matching deny rule blocks the call, and a matching ask rule still
  prompts even when the hook returned "allow"." For PermissionRequest
  (`hooks.md`, "PermissionRequest decision control"): "The hook can't approve
  what a deny rule refuses." The docs do not say whether a PermissionRequest
  allow answers the prompt an `ask` rule raised; the spike in Research 10
  shows that it does.
- *Plugin `userConfig`* (`plugins/manifest-reference.md`, "User
  configuration", "Where values are stored"): non-sensitive values live under
  `pluginConfigs` in the user's `settings.json`; the settings reference lists
  `pluginConfigs` with scope "User or managed", so no per-project value is
  documented. Hooks see each option as the environment variable
  `CLAUDE_PLUGIN_OPTION_<KEY>`. Values are set through the `/plugin` dialog or
  `claude plugin install --config`.
- *Cloud sessions* (`settings.md`, "Settings in cloud sessions"; `desktop.md`,
  "Limits"): `~/.claude/settings.json` is not read in cloud sessions, the
  repository's shared `.claude/settings.json` is read in a single-repository
  cloud session, and plugins installed at user or local scope are not
  available there (record 39). Desktop reads the same settings files as the
  terminal.
- *Project `.claude/settings.json`* (`settings.md`): read from the session's
  primary working directory. Its `env` applies to "every session and its
  subprocesses"; Research 10 shows that this includes a plugin's command
  hooks. A project cannot
  disable one hook of a plugin, only the whole plugin (`enabledPlugins`) or
  all hooks.
- *Worktree-isolated subagents* (`worktrees.md`, "Isolate subagents with
  worktrees"; `sub-agents.md`): such a subagent takes its instruction files
  (CLAUDE.md) from the main conversation and runs its Bash commands inside its
  worktree. Which `settings.json` it reads is not stated. For a `--worktree`
  session, hook `cwd` is the worktree root and `CLAUDE_PROJECT_DIR` stays at
  the start directory (`worktrees.md`, "Hook paths don't follow the
  worktree"). Research 10 settles both questions for subagents.
- *Plugin data directory* (`manifest-reference.md`, "Environment variables"):
  `CLAUDE_PLUGIN_DATA` is `~/.claude/plugins/data/<id>/`, kept across updates
  and deleted on uninstall unless `--keep-data`.
- *Hook payload* (`hooks.md`, "Common input fields"): `cwd` is a common field;
  `agent_id` and `agent_type` are present inside a subagent. Command hooks get
  `CLAUDE_PROJECT_DIR`, `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA`.

**Research 7 (Verified by source reading).** nexus's preflight script
documents a non-zero exit (2, 3, 4, 5) for each of its four
`PREFLIGHT_FAIL_*` kinds (`scripts/agent-worktree-preflight.sh:46-52`; two of
the echo-then-exit sites read, at :81-82 and :88), so an exit-status contract
fits it without the plugin knowing the `PREFLIGHT_FAIL` convention. `nx ci
wait` reads "the CI board where the branch has a topic, otherwise GitHub's
check-runs" (`src/nexus/commands/ci_cmd.py:38`); the board and its channel
posts exist only where a project runs the board.

**Research 8 (Verified in passing).** While reading for this RDR, the
installed 8.5.7 full guard denied a read-only `git symbolic-ref --short
refs/remotes/origin/HEAD` from this subagent, a false denial of the
read-only kind nexus-5y47c targets (whether 86a5fa0ff1 admits this exact
command was not tested). The full guard is what a user opting into `strict` gets, so
that fix matters more once strict is an option for everyone (Decision 10).

**Research 9 (Verified by source reading, for the emitter only).**
`systemMessage` is a top-level hook output field the nx hook runtime already
emits (`src/nexus/_hook_runtime/_io.py:243-255`, used by
`mcp_connect_check.py`). The code alone does not show that the user sees it;
Research 10 does, for the CLI.

**Research 10 (Verified by spike, Claude Code 2.1.332 on the laptop,
2026-11-25; records `nexus_rdr/259-research-26` to `-32`).** Each spike ran
`claude` in a throwaway repository with hooks that logged every payload; the
records hold the commands and outputs.

- *A PermissionRequest allow overrides a user's `ask` rule* (record 26). With
  a project `permissions.ask` rule for a Bash command, a PermissionRequest
  hook that printed `{"decision": {"behavior": "allow"}}` let the command run
  with no prompt, in default mode and in `acceptEdits`, and interactively the
  transcript said "Allowed by PermissionRequest hook". A PreToolUse allow
  did not: PermissionRequest still fired and the call was refused, as the
  docs say. The plugin's auto-approve prints that same PermissionRequest
  answer for every tool its matcher `mcp__plugin_conexus_.*` covers
  (`src/nexus/hooks/auto_approve.py:14-16, 157-165`), so in every shipped
  release a user's `ask` rule for any conexus tool is answered silently. That
  last step is an inference; `memory_delete` itself was not run, to keep off
  live T2.
- *Project env reaches a plugin's command hooks* (record 27). A value set in
  a project's `.claude/settings.json` `env` appeared in the environment of
  both a project hook and a `--plugin-dir` plugin's hooks.
- *A worktree-isolated subagent runs under the main session's settings*
  (record 28). The primary's uncommitted `.claude/settings.json` governed every
  hook, including the subagent's own Bash call; the copy committed in the
  worktree never ran.
- *A worktree subagent's hooks see the worktree as `cwd`* (record 29), in both
  the payload and the process, while `CLAUDE_PROJECT_DIR` stays at the primary
  root. The worktree's base commit is not settled. In the spike, whose primary
  sat on a branch with no upstream, the worktree was cut from origin's default
  branch, not from the primary's `HEAD`. In nexus, halcyon's check on
  2026-11-25 of the 11 isolation worktrees under `.claude/worktrees/agent-*`
  found 8 with `HEAD` on `origin/develop` (the branch the primary tracks), none
  on `origin/main` (which is `origin/HEAD`), and the rest with agent commits on
  top. A likely reading is that the base is the primary's current branch or
  its upstream when it has one, and origin's default branch otherwise; that is
  inferred, not isolated.
- *A SessionStart `systemMessage` is shown in the interactive CLI* (record 30):
  under the banner, before any prompt, as "SessionStart:startup says: ...".
  The hook was a project hook, not a plugin hook; only `startup` was tried.
- *Under the seatbelt a subagent can edit `.nexus/project.json` and commit it*
  (record 31). Run through the real scripts at develop 870bf29e70: no conexus
  hook stopped the Edit, the Write, `git add` with `git commit`, or `git rm`
  with `git commit`; the controls showed the seatbelt and the full guard
  denying what they should. The git guard is registered for Bash only.
- *The readers cost about 60 microseconds a call warm and 1.2 ms cold*
  (record 32; Apple M4 Max, Python 3.13): `guard_mode` reading both files with
  the cwd five directories down averaged 59.5 us, and a fresh interpreter that
  imports `_audience` and calls two readers took 22.4 ms against 21.2 ms bare.
  Native Windows and the `HEAD` read path were not measured.

**Research 11 (gate round 1 evidence, 2026-11-26; records
`nexus_rdr/259-research-33` to `-40`).**

- *A PreToolUse-only allow is enough* (record 33, verified by spike, Claude
  Code 2.1.332). A hook registered only on PreToolUse that answers allow let
  a Bash command with no permission rule run with no prompt in default mode,
  in `acceptEdits` and inside a general-purpose subagent, and PermissionRequest
  never fired. With an `ask` rule for the same command the call still went to
  a prompt (refused under `claude -p`) in both modes and in the subagent, and
  with a `deny` rule it was denied. With no hook answer and no rule the call
  needed approval, as expected. Bash stood in for an MCP tool; auto mode and
  an interactive dialog were not run.
- *Why the PermissionRequest answer exists* (record 33, source reading). It
  came first, in commit 240731431b of 2026-04-05, "PermissionRequest hook
  auto-approves all nexus MCP tools", when RDR-035 recorded "PreToolUse hooks
  not enforced on subagent tool calls". The PreToolUse answer was added on
  2026-08-28 (91c0cddebc) because "under defaultMode: auto the classifier
  decides an unlisted tool first and never consults that event". Record 33
  shows the subagent limitation is gone in the current release.
- *The telemetry senders take their endpoint from the environment* (record
  34, source reading at d6a0b258b0). `routing/_lib.py` reads `NX_SERVICE_URL`
  (line 511), `NX_SERVICE_TOKEN` (522) and `NX_SERVICE_PORT` and
  `NX_SERVICE_HOST` (531-537), and takes its config directory from
  `NEXUS_CONFIG_DIR` or `NX_CONFIG_DIR` (`_endpoint_resolve.py:113`). A
  project's `env` reaches the hooks (record 27), so a repository can redirect
  routing events, and the token sent with them, to a host it names. The
  dispatch record's child process (`agent_dispatch_record.py`) was not read.
- *The guard fix and the Phase 3 branch share 11 files* (record 35, a census
  of the two three-dot diffs against `develop`), among them the guard, its
  registry, the manifest, the relay template, the orchestration skill and the
  golden replay test (a test that replays recorded real subagent commands
  through the guard and checks the verdicts did not change). That replay is
  built on the person signal: it imports `flagged_home` and `mark_nexus` and
  asserts "``maintainer()`` is true for every cwd that still exists". The fix
  is not on `develop`.
- *Plugin and CLI skew is normal* (record 36): a plugin can update before its
  CLI (`nx_hook_shim.py:5-6`), lockstep arrives at the next session and
  skips editable installs (RDR-143), and auto-approve is an `nx-hook` verb.
- *`git show HEAD:<path>` exits 128 for every failure* (record 37, spike, git
  2.53.0): an unborn `HEAD`, a commit without the file, and a directory that is
  not a repository all exit 128 with different messages; a committed file
  exits 0.
- *Two 8.5.7 facts* (record 38): the 8.5.7 helper reads
  `.nexus/dev-process.json` (`_audience.py:46-48` at v8.5.7), and 8.5.7 gives
  every user the SubagentStart Git row (`subagent_start.py:155-156`).

### Critical Assumptions

What is still assumed after the spikes:

- [ ] A SessionStart `systemMessage` from a *plugin* command hook is shown
  to the user, in Desktop as well as the CLI, and on `resume` and `clear` as
  well as `startup`. **Status**: Verified only for a project hook on
  `startup` in the CLI (Research 10). **Method**: Spike in Stage 1 for the
  plugin-hook form and the two sources; Desktop needs a hand check because it
  cannot be driven from a session. If Desktop does not show it, `nx settings
  check` and the deny-text clause (Decision 4) still tell the user.
- [ ] Which commit an isolation worktree is cut from. **Status**: Unsettled
  (Research 10: origin's default branch in the spike, the primary's tracked
  branch in nexus). **Method**: Spike. The design no longer depends on it
  (Decision 1, which copy is read).
- [ ] A PreToolUse-only allow behaves for a conexus MCP tool, and in auto
  mode, as it did for Bash in default and `acceptEdits` modes (Decision 9).
  **Status**: Verified for Bash only (record 33); MCP tools and auto mode
  inferred, not tested. **Method**: Spike in Stage 1 with the real plugin
  loaded and a harmless conexus tool, in default, `acceptEdits` and auto
  mode, with and without a user `ask` rule.
- [ ] `CLAUDE_PROJECT_DIR` reaches the nx-side hooks (the MCP server's hook
  tools), not only command hooks. **Status**: Documented for command hooks
  only. **Method**: Spike in Stage 1; where it is absent the reader uses the
  payload `cwd`, as today.
- [ ] The reader stays well under 5 ms on native Windows, and the `HEAD`
  read path stays within its two-second timeout. **Status**: Laptop measured
  (Research 10); Windows and the `HEAD` path not. **Method**: measure in
  Stage 1.

Acknowledged minor assumptions, which nothing in the design rests on:

- Plugin `userConfig` has no project scope (record 8). The design does not use
  `userConfig`, so the answer changes no decision.
- A subagent's write to the user's `conexus.json` raises a permission prompt
  in the default mode (record 23). User-file tampering by an agent is outside
  the threat model (Decision 9); the assumption only softens that risk.
- Opening a FIFO for reading blocks (record 25). This is POSIX behaviour, and
  the regular-file check makes the question moot; a test covers it.

## Proposed Solution

### Approach

Two settings files replace both predicates. A project's tracked
`.nexus/project.json` carries the project's own choices and may only tighten
or add text. A user's `<account home>/.config/nexus/conexus.json` carries the
user's choices for every repository. Every hook rule is either for every user,
or turned on by a named setting, or removed. One stdlib module reads both
files, reports every problem it finds, and is the code behind both the hooks
and the inspection command, so what a user inspects is what the hooks do. The
process texts the person signal withheld go to every user with nexus's ids
taken out; nexus's own words move to nexus's project file (for hook behaviour)
and to nexus's `CLAUDE.md`, `.claude/rules/` and project skills (for prose).

### Technical Design

Each numbered decision below answers one of the questions the brief set, with
the alternatives that were weighed. The 22 open implementation choices are
mapped to decisions in the table at the end of this section.

#### Decision 1: The files and where they live

**Project file: `.nexus/project.json`** at a checkout root, the first
directory at or above a starting path that holds a `.git` entry (a
directory, or the file a linked worktree or submodule has). A nested
repository or submodule is its own project. A path outside any repository
has no project settings. Sam ruled the name (ruling 7).

**Which copy is read.** A subagent in an isolation worktree works in a
checkout whose commit can differ from the primary's: Claude Code cut the
spike's worktree from origin's default branch and nexus's from the branch
the primary tracks, and the rule is not settled (Research 10). So the design
does not depend on the worktree's base:

- The *session root* is the checkout at or above `CLAUDE_PROJECT_DIR`, which
  stays at the primary for a worktree subagent (Research 10), or at or above
  the payload `cwd` when that variable is absent or outside a repository.
- The text and list keys (`preflight`, `ci_wait`, `credential_helper`,
  `integration_branch`, `brief_questions`) come from the session root's file
  at `HEAD` only. A working-tree edit to one of them takes effect once it is
  committed, and `nx settings show` reports the uncommitted difference. The
  reason is that any subagent can edit the working-tree file (record 31), so
  a text the plugin then shows to every dispatched agent could be planted by
  one of them (record 40). Under `strict` the planting also needs a commit,
  which the guard denies in the shared checkout; under the seatbelt a
  subagent can still commit (record 31), so the `HEAD` read narrows that
  path to a commit that shows in the history rather than closing it. The cost
  is one `git show` of the file each time a hook is about to print a project
  text: once per SubagentStart, and for the Stop guard and the credential
  guard only when they block. That cost is not measured (inferred, not read).
- The tightening keys (`subagent_git`, `advice_block`) are combined, tighter
  wins, across the session root's working-tree file, the session root's file
  at `HEAD`, and the file in the checkout the payload `cwd` sits in when that
  is a different checkout (a worktree, or an unrelated repository a subagent
  works in). Any copy can add strictness at once; only the session root's
  committed copy can remove it.

The `HEAD` read meets these cases. `git show HEAD:.nexus/project.json` exits
128 for all of the failures (record 37), so the reader tells them apart by
their cause (whether `HEAD` exists, whether the path is in its tree), never
by parsing git's message, which is locale-dependent (inferred, not read).

| Case at the session root | Tightening keys from `HEAD` | Text and list keys |
| --- | --- | --- |
| not a repository | no `HEAD` copy | none |
| unborn `HEAD` (nothing committed) | no `HEAD` copy | none |
| committed, file absent at `HEAD` | no `HEAD` copy; absence is not strict | none |
| committed file that parses | its values | its values |
| committed file that is not valid JSON, not an object, or over 64 KiB | `subagent_git` strict | none |
| git missing, the two-second timeout, any other error | `subagent_git` strict | none |

The trusted copy is therefore the session root's committed file. A
worktree's copy, on whatever commit Claude Code chose, can only add to it.

The file is tracked so it reaches every place the project's
sessions run: the primary, every worktree, a copy made for a remote test
host, Desktop and `claude -p`. Cloud sessions are a weaker case: the
repository's shared `.claude/settings.json` is read in a single-repository
cloud session, but plugins installed at user or local scope are not
available (record 39), so `project.json` applies in a cloud session only
where the plugin itself runs there, which the docs do not settle.

**User file: `<account home>/.config/nexus/conexus.json`.** The account
home comes from the OS account database (`pwd` on POSIX, the
`FOLDERID_Profile` known-folder API on Windows), never from `HOME`,
`USERPROFILE`, `XDG_CONFIG_HOME` or `NEXUS_CONFIG_DIR`. The reason is
Research 6 and record 27: a project's `.claude/settings.json` sets
environment variables that reach the plugin's hooks, so any location chosen by an environment
variable is a location a repository can choose. On Windows the path is
`%USERPROFILE%\.config\nexus\conexus.json`, the same directory nx already
uses there. The file sits beside nx's `config.yml` so a user finds both in
one place, and is named for the plugin because it configures the plugin.

**Format: JSON.** The plugin's command hooks have only the standard library,
which parses JSON and not YAML or TOML-with-comments everywhere they run.
JSON has no comments; keys beginning with `$` are reserved for annotations
(`"$schema"`, `"$comment"`) and ignored without a problem, so a user can
annotate a file and point an editor at the published JSON Schema.

Alternatives weighed for the user file:

- *Plugin `userConfig`.* Discoverable (Claude Code shows a dialog at install),
  but rejected for four reasons. Its values reach hooks as environment
  variables, and a project's `env` reaches a plugin's command hooks (record
  27), so a repository could set the same names and turn on a user-only key
  such as `telemetry` (Research 10). No project
  scope is documented for it. It lives in `~/.claude/settings.json`, which is not read in
  cloud sessions and which nx (outside Claude Code) would have to parse to
  show the effective settings. And it couples the settings contract to a
  Claude Code feature whose shape the plugin does not control.
- *nx's `~/.config/nexus/config.yml`.* One file for everything, but rejected:
  it is YAML, which the stdlib hooks cannot read; it holds credentials, so
  every hook would parse secrets on every call; it moves with
  `NEXUS_CONFIG_DIR`, an environment variable; and it is nx's file, absent on
  a P0 machine. `nx settings` (Decision 5) is where the two meet for a user.
- *`~/.claude/conexus.json`.* Rejected: Claude Code owns `~/.claude` and
  relocates it with `CLAUDE_CONFIG_DIR`, an environment variable.
- *`CLAUDE_PLUGIN_DATA`.* Rejected: deleted on uninstall, so a reinstall
  silently drops the user's settings, and its path reaches only hooks.
- *Per-repo gitignored `.nexus.yml` or `.nexus/project.local.json` for
  per-user, per-project values.* Deferred (out of scope): no key needs a
  per-user, per-project value today, and a local file inside the checkout is
  writable by every agent working there.

Alternatives weighed for the project file: the repository's
`.claude/settings.json` (rejected: it cannot carry plugin-internal behaviour without duplicating
thousands of lines of the guard, and the analysis section 3 found it reverses
the one-registration-surface rule of nexus-3h0u6); and keeping the name
`.nexus/dev-process.json` (rejected by ruling 7: it names nexus's process, not
the project's settings).

#### Decision 2: The schema

One checked table, `SETTINGS` in `_audience.py`, is the single source of
truth (RDR-201's rule for closed vocabularies). Each entry names the key,
its type, its default, which file may set it, how two values combine, its
failure value, and its size limit. The JSON Schema files
`conexus/schemas/project.schema.json` and `conexus/schemas/conexus.schema.json`
and the key tables in `docs/settings.md` are tested against `SETTINGS`
(Decision 11), so none of the three can drift. A TOML table under
`src/nexus/tables/` was considered and rejected because the plugin's stdlib
hooks on a P0 machine cannot import `nexus.tables`.

| Key | Type and limits | Default | Project | User | Combination |
| --- | --- | --- | --- | --- | --- |
| `version` | integer, 1 or more | 1 | yes | yes | per file |
| `subagent_git` | `"strict"` or `"seatbelt"` | `"seatbelt"` | yes | yes | strictest wins |
| `advice_block` | list of advice row ids | `[]` | yes | yes | union |
| `brief_questions` | list of strings, each one line of at most 300 characters; at most 8 added in total across both files | `[]` | yes | yes | append: project's, then user's, exact duplicates dropped |
| `preflight` | one line, at most 200 characters | none | yes | no | project only, from `HEAD` |
| `ci_wait` | one line, at most 200 characters; may contain the literal `<sha>` | none | yes | no | project only, from `HEAD` |
| `credential_helper` | one paragraph, at most 600 characters | none | yes | no | project only, from `HEAD` |
| `integration_branch` | a branch name (no whitespace, no `..`, no leading `-`), at most 100 characters | none | yes | no | project only, from `HEAD` |
| `telemetry` | boolean | `false` | no | yes | user only |
| `t1_nudge` | boolean | `true` | no | yes | user only |
| `remote_test_hosts` | list of ssh host aliases | `[]` | no | yes | user only |

What each key does:

- `subagent_git`: `strict` runs the full subagent git guard and adds the
  SubagentStart Git row; `seatbelt` runs the short deny list (Sam's D1 list,
  ruling 2).
- `advice_block`: the listed advice rows deny (with the existing
  `# routing-allow:` escape, which only the user may tell an agent to use)
  instead of warning (ruling 6). The valid ids are the advice table's row ids
  (`pattern_kill`, `branch_bulk_delete`, `stash_with_worktrees`,
  `bg_pipe_masks_exit`, `whole_disk_scan`, `shared_glob_delete`,
  `hook_verb_by_hand`, `running_script_edit` at d6a0b258b0); `docs/settings.md`
  lists them from the table. A row id becomes a public contract: a renamed row
  keeps its old id as an accepted alias for two client releases.
- `brief_questions`: added after the plugin's four generic implementer brief
  questions.
- `preflight`: the SubagentStart worktree row tells a dispatched agent "This
  project's `.nexus/project.json` asks: run `<preflight>` first. If it exits
  non-zero, stop and report its output." The plugin shows the command; it
  never runs it. Research 7 shows nexus's script fits the exit-status
  contract.
- `ci_wait`: the command the Stop wake guard names when a turn promises to
  wait on CI (Decision 12 on why only the command stays in the plugin). The
  plugin prints it as written; a literal `<sha>` in it is left for the model
  to fill with the commit it pushed, and nothing substitutes it.
- `credential_helper`: text the credential guard adds after its fixed
  refusal, naming the project's safe way to use a credential.
- `integration_branch`: the branch the compaction checkpoint measures "not
  yet landed" against. Without it the order is `origin/HEAD`, then
  `origin/main`, then `origin/master`, then unknown; the rework's
  `origin/develop` guess is removed (Research 5). nexus sets `"develop"`.
  The value is passed to git after `--end-of-options`, so even a value the
  validation missed cannot be read as an option.

Every project text the plugin prints (`preflight`, `ci_wait`,
`credential_helper`, `brief_questions`) is introduced as coming from the
project's `.nexus/project.json`, never in the plugin's own voice, so the model
can weigh it as project content, the same standing as the project's
`CLAUDE.md`.
- `telemetry`: sends `routing_events` and the dispatch record to the user's
  own engine; one key for both, because both answer the same question ("may
  my hooks record what my agents did?") and two keys would invite a half-on
  state nobody asked for (ruling 5).
- `t1_nudge`: the SubagentStop reminder to write findings to T1, for every
  agent type, conexus's own included. Off means off for all agents; the
  dispatch record is governed by `telemetry` alone (ruling 3).
- `remote_test_hosts`: ssh aliases whose single test command the full guard
  lets run git on the remote host. This folds the separate file
  `<account home>/.config/nexus/remote-test-hosts` into `conexus.json`, so a
  user has one file to read and one command shows all of it; the old file is
  still read, as a union, through the compatibility window (Decision 7).

**Growth.** `version` is 1 and is expected to stay 1. A key's meaning is
never changed in place; a new behaviour gets a new key name, so old files
keep their meaning and old readers ignore new keys. `version` exists so a
future incompatible change can be detected: a reader that meets a version
newer than it knows reads the keys it knows, applies the safety failure
values to keys it cannot parse, and reports "written for a newer conexus".
A deprecated key stays in `SETTINGS` with a `deprecated` note and a removal
version; it keeps working and is reported until removal, after which it is
an unknown key. The rework's `"schema": 1` is renamed `version`; it never
shipped, so no alias is kept.

**Validation and reporting.** Each read returns the effective values and a
list of problems, each with the file, the key, what was wrong, and what was
used instead. Unknown keys are reported with a "did you mean" suggestion from
`difflib` against the key names, so `subagent-git` points to `subagent_git`.
Problems are reported as Decision 4 describes; they never raise.

#### Decision 3: Precedence and what "tighten-only" means

The invariant: **no value a repository controls, whether in its project file
or in an environment variable its `.claude/settings.json` sets, can remove a
deny, remove a permission prompt, exempt anything from a guard, or cause data
to leave the machine or go to a host the repository names.** The draft
stated this for the project file alone, and it was false for telemetry: the
senders take their endpoint and token from `NX_SERVICE_*` and
`NEXUS_CONFIG_DIR` (record 34), which a repository's `env` sets (record 27).
Decision 9 pins the senders, which is what makes the invariant true. A
project may add text the model reads, because a repository already controls
its `CLAUDE.md`; the plugin never executes project text. The listed
exceptions in Decision 9 are outside this invariant by name.

Per key:

- `subagent_git`: the order is `seatbelt` < `strict`. The effective value is
  the strictest of the project copies Decision 1 names (the session root's
  working tree and `HEAD`, and the payload checkout's working tree), the
  user file, and the default. A project's `"seatbelt"` is valid and changes
  nothing; it cannot lower a user's `strict`.
- `advice_block`: union over the same project copies and the user file.
  Adding a row tightens; no file can remove an entry another file added.
- `brief_questions`: append, generic questions first, then the project's,
  then the user's; capped at 8 added questions in total, extra ones reported
  and dropped.
- `preflight`, `ci_wait`, `credential_helper`, `integration_branch`: project
  only. They are facts about the project. In the user file they are reported
  and ignored.
- `telemetry`, `t1_nudge`, `remote_test_hosts`: user only. `telemetry` and
  `remote_test_hosts` are loosenings (data leaves; a host is exempted), so a
  project can never set them. `t1_nudge` is a cost preference of the user's.
  In a project file each is reported ("a user setting; a project cannot set
  it") and ignored.

A per-user override of a project's tightening is deliberately not offered:
a user who disagrees with a project's `strict` works in a linked worktree,
which the full guard already exempts.

#### Decision 4: Failure behaviour, and how the user is told

Safety keys fail toward the protection; the rest fail to the default. The
same rule holds for both files.

| Condition | Project file | User file |
| --- | --- | --- |
| absent | no project settings | defaults |
| not a regular file, over 64 KiB, unreadable, not UTF-8, not JSON, or top level not an object | `subagent_git` strict; every other key absent | `subagent_git` strict; every other key at its default (`telemetry` false, `remote_test_hosts` empty) |
| `subagent_git` present with any other value or type | strict | strict |
| list key not a list | key empty | key empty |
| list item of the wrong type, or an unknown advice row id | item dropped | item dropped |
| text over its limit, multi-line where one line is required, or containing control characters | key absent | (project-only keys are ignored here) |
| `telemetry` not a boolean | (cannot be set) | false |
| `t1_nudge` not a boolean | (cannot be set) | true |
| unknown key | ignored | ignored |
| key in the wrong file | ignored | ignored |
| `version` newer than known | known keys read | known keys read |
| `version` not an integer, or below 1 | read as 1 | read as 1 |

Every row except "absent" is a reported problem. The regular-file check and
the size cap matter: a project could point `.nexus/project.json` at a FIFO,
and a hook that opens it would hang every tool call until its timeout.

The user-file row departs from the rework, which left a broken user file at
the seatbelt. A user writes `conexus.json` only to change something, and the
most likely change is `strict`; a broken file should not silently take away
the protection the user asked for. This is Sam's decision S2.

How the user is told, without a message on every hook call:

1. **At session start, once.** When the session's directory has a settings
   problem, a plugin-side SessionStart command hook (stdlib, so P0 users get
   it) emits one user-visible `systemMessage` and one line of context for the
   model, on `startup`, `resume` and `clear` (not `compact`). It names each
   file, the problem and the value used, and how to see more: `nx settings
   show` when the `nx` on PATH has that subcommand (the hook checks, because
   an older CLI beside a newer plugin is normal, record 36), otherwise the
   stdlib command in Decision 5 with the plugin's absolute path filled in.
2. **Where a broken file changed a decision.** When the full guard denies
   only because a file could not be read, the deny text says so: "the full
   guard applies because `<file>` could not be read: `<reason>`". This is the
   one moment the user most needs to know.
3. **In the log, once per session per problem.** The existing once-per-session
   `hook.log` note (`_note_once`) records each problem's fingerprint.
4. **On demand.** `nx settings show` and `check` list every problem;
   `nx doctor` gains a row that counts them.

No other hook prints a settings problem. A project value that is valid but
outranked (a project's `seatbelt` under a user's `strict`) is not a problem;
`show` displays it.

#### Decision 5: Discovery and inspection

`nx settings show [--cwd PATH] [--json]` prints:

- both file paths and whether each exists (and, inside a repository, whether
  the project file at `HEAD` differs from the working tree);
- for every key in `SETTINGS`, the effective value and its source: `default`,
  `user`, `project`, `project (HEAD)`, `legacy marker`, or `user
  (remote-test-hosts file)`, plus each file's own value where it differs from
  the effective one;
- the project text keys' values as the hooks will print them (`preflight`,
  `ci_wait`, `credential_helper`, `brief_questions`), with their source
  `project (HEAD)`, so a user sees every instruction a project feeds to
  dispatched agents;
- every problem, as in Decision 4;
- retired files that still exist (`maintainer-profile`, a legacy marker
  beside `project.json`) with "no effect; safe to delete after <version>";
- the plugin's version and the installed nx's version, with a warning when
  they differ: the nx-side hooks (auto-approve, SubagentStart, SubagentStop)
  follow the installed CLI, so during a skew the report can name a setting
  that some hooks do not yet implement (record 36).

`--cwd` defaults to the current directory. `--json` emits the same report
with its own `report_version` field.

`nx settings check [--cwd PATH] [--project-only]` prints only the problems
and exits 1 when there are any, so a project can lint its file in CI
(`--project-only` skips the user file, which a CI runner does not have).

Both commands import `nexus.hooks._audience`, the byte copy the hooks use,
so the report is the hooks' own reading. The plugin copy is also runnable:
`python3 <plugin root>/hooks/scripts/_audience.py show [--cwd PATH]` prints
the same report on a P0 machine.

The account home rule applies here too: `nx settings` reads the user file
from the OS account home even when `NEXUS_CONFIG_DIR` is set, because that is
the file the hooks read. The help text says so.

There is no writer in this RDR (`nx settings set`): the files are small, a
writer adds surface, and its absence costs a hand edit. The name `nx
settings` is a long-lived public command; Sam's decision S5.

#### Decision 6: Documentation

- **`docs/settings.md`** (new) is the reference: what the plugin does by
  default, the two files, every key from `SETTINGS`, the precedence rules,
  the failure table, the advice row ids, `nx settings`, and the Claude Code
  route to prompts (Decision 9).
- **`conexus/README.md`** gets a short "Settings" section near the top, the
  first thing a new user reads: the defaults suit most users; to turn on the
  full git guard or telemetry, write one of two files; `nx settings show`
  tells you what applies. It links `docs/settings.md`. The hooks table rows
  name the key that changes each hook.
- **`docs/configuration.md`** keeps describing nx's `config.yml` and
  `.nexus.yml`, adds both settings files to "File Locations", and a short
  section that says these are plugin settings, separate from nx config, and
  links `docs/settings.md`. This keeps the two easy to tell apart.
- **`docs/cli-reference.md`** documents `nx settings`.
- **`docs/agents/plugin-audiences.md`** becomes the maintainer page for the
  manifest, the lint and the behaviour leg (Decision 11), with the D8 rule
  replaced by ruling 9's wording.

#### Decision 7: Migration and compatibility

| Who | What changes |
| --- | --- |
| 8.5.7 user, no files | Guard stays the seatbelt. Keeps, now without nexus ids: the search-scope row, the four brief questions, the dispatch sentence, the T1 nudge for built-in agents. Loses: the SubagentStart Git row, which 8.5.7 shows to every user (`subagent_start.py:155-156` at v8.5.7, record 38) and which now appears only with `strict` (ruling 3); and telemetry (it stops unless they set `telemetry: true`; it only ever went to their own engine). Auto-approve still approves every allowlisted tool with no prompt (ruling 4; the D2 prompt never shipped), and a user's own `ask` or `deny` rule for a conexus tool now takes effect where 8.5.7 overrode it (Decision 9). The compaction count is measured against their default branch instead of `origin/develop`. |
| Sam | The flag file has no effect from this release. To keep today's behaviour outside nexus he writes `conexus.json` with `subagent_git: strict`, `telemetry: true` and his `remote_test_hosts`, on each machine where he wants them. Deleting `maintainer-profile` is his to do; `show` reports it as retired. |
| nexus checkout | `.nexus/project.json` sets `subagent_git: strict`, `preflight`, `ci_wait`, `credential_helper`, `integration_branch: develop` and the local-and-cloud brief question. `.nexus/dev-process.json` is deleted. The 8.5.7 helper does read it (`_audience.py:46-48` at v8.5.7, record 38), but it also reads the Phase 1 marker, which nexus keeps, so an 8.5.7 plugin still gives nexus the full guard. |
| Old plugin (8.5.7) with a new nexus checkout | 8.5.7 reads the Phase 1 marker `scripts/agent-worktree-preflight.sh`, which stays (it is nexus's preflight), so nexus keeps the full guard. A user repository's new `project.json` is ignored by the old plugin. |
| New plugin with an old checkout (an older nexus branch, a bisect) | No `project.json`, so either legacy marker reads as `subagent_git: strict`, for two client releases after the one that ships this. When `project.json` exists it alone decides and a legacy marker beside it is reported and ignored. |
| Old nx with new plugin, or the reverse | A normal state, not a short window: a plugin can update before its CLI (`nx_hook_shim.py:5-6`), RDR-143's lockstep arrives only at the next session, and it skips editable installs, where the skew can last indefinitely (record 36). During it the plugin's command hooks (the guard, the Stop guard, advice, credential) follow the settings, and the nx-side hooks (auto-approve, SubagentStart, SubagentStop, the SessionStart context) follow the installed CLI's release, which for 8.5.7 means ungated texts and a PermissionRequest answer. So the session-start notice names `nx settings show` only when the installed nx has it (Decision 4), `show` prints both versions and warns when they differ (Decision 5), and the auto-approve fix is made on both sides so either one closes it (Decision 9). No state is written that either side misreads. |
| `develop` (Phase 2, unreleased) | Its `maintainer()` gating is undone on the Phase 3 branch before release (ruling 8); a lint (Decision 11) fails while any `maintainer` or `account_flag` symbol remains, so the gated tip cannot ship. |

**Removal is tracked by a test.** `_audience.py` carries
`LEGACY_ACCEPTED_UNTIL`, a concrete version two client releases after the
shipping one, set when this ships. `tests/hooks/test_settings_legacy_window.py`
reads the plugin version from `conexus/.claude-plugin/plugin.json` and fails
once that version reaches `LEGACY_ACCEPTED_UNTIL` while the legacy markers or
the `remote-test-hosts` file are still read. Removing them, or deliberately
moving the constant, is then a visible diff (developer item 21).

#### Decision 8: Claude Code's own configuration layers

The plugin keeps its own two files and puts nothing in Claude Code's
settings. Each Claude Code layer keeps its own job:

- **Permissions** (`permissions.allow`, `ask`, `deny` in user, project and
  local `settings.json`) stay Claude Code's, and they are the one place a
  user or a project asks for a prompt. A PreToolUse allow cannot override
  them (Research 6, record 33), but a PermissionRequest allow does override
  an `ask` rule (Research 10, record 26), so the plugin stops answering on
  PermissionRequest (Decision 9).
- **`CLAUDE.md` and `.claude/rules/`** carry a project's prose. nexus's own
  prose (the incident history, `develop` as the integration branch, the push
  script, the lab hosts, the CI board) belongs there, not in plugin text.
- **Plugin `userConfig`** is not used (Decision 1).
- **Project `.claude/settings.json`** is not used for plugin settings.

The two questions the docs left open:

- *Does a worktree-isolated subagent read the worktree's
  `.claude/settings.json`?* No: it runs under the main session's settings,
  the primary's working copy (Research 10, record 28). No plugin setting
  lives there, so this changes nothing in the reader. The
  subagent's hooks do see the worktree as `cwd`, and the worktree's commit
  can differ from the primary's (record 29 and halcyon's check of nexus's
  worktrees), which is why Decision 1 reads the session root's copy of
  `project.json` and lets a worktree's copy only add to it.
- *Can plugin `userConfig` be set per project?* Not as far as the docs go:
  the only documented scope is user or managed, and the negative is inferred
  from their silence, not stated or tested (Research 6). That is one of the
  reasons Decision 1 does not use it.

Orchestrator guidance follows from record 29 and goes into the plugin's
orchestration skill: an isolation worktree may not start at the primary's
`HEAD`, so a dispatch whose work depends on a particular commit names that
commit in the brief and has the agent check it first, or the orchestrator
creates the worktree itself (`git worktree add <path> <commit>`) and points
the agent at it. nexus's preflight script with a required sha is one such
check.

#### Decision 9: Security

The invariant of Decision 3 is enforced by construction (the reader keeps
only tightening values from a project file) and by tests (Decision 11).
Further measures:

**One threat model** covers both files. The plugin's guards protect against
two things: a repository's own content weakening a protection, and a
cooperative agent that works around a block, for example by editing the file
that set it. They do not defend against an agent that deliberately attacks
them. Under that model:

- **The project file is read at `HEAD` as well as in the working tree,** at
  the session root, for the tightening keys `subagent_git` and
  `advice_block` (the tighter applies), and the text and list keys come from
  `HEAD` only (Decision 1, with its case table). A subagent working in the
  shared checkout can edit or delete the working-tree file and commit the
  change under the seatbelt; no conexus hook stops either step (Research 10,
  record 31). Without the `HEAD` read, its next Bash call would run under the
  seatbelt and could commit. A commit is exactly what the strict guard
  denies, so the `HEAD` copy cannot be changed from inside the protected
  case. For the tightening keys the cost is one `git show
  HEAD:.nexus/project.json` (two-second timeout), taken only when the working
  tree and the user file give the looser value and the decision depends on
  it (for `subagent_git`, when the strict scan would have denied the
  command); a timeout reads strict. A maintainer's own edit that lowers a
  setting, or changes a text key, takes effect after it is committed, and
  `show` says so. Sam's decision S3.
- **The user file** has no committed copy. Under the same model it relies on
  Claude Code's prompt for a write outside the project directory in the
  default permission mode, which is assumed and not tested (record 23). An
  agent that edits it on purpose is outside the model, for this file as for
  the project file.
- **Project text is prompt content and nothing else.** The plugin never
  executes `preflight`, `ci_wait` or any other project value; it shows it to
  the model, labelled as coming from the project's `.nexus/project.json`
  (Decision 2), which a repository can already do through `CLAUDE.md`.
  Length limits keep the text inside the SubagentStart byte budgets, and
  `nx settings show` lists every such text (Decision 5).
- **No setting is read from the environment.** A project's `env` reaches the
  plugin's hooks (record 27). The only environment value the reader uses is
  `CLAUDE_PROJECT_DIR`, to find the session root (Decision 1); a project
  that sets it can move only which project file is read, and that file can
  only tighten.
- **The telemetry senders are pinned.** When `telemetry` is on, the routing
  sender (`routing/_lib.py`) and the dispatch-record child
  (`agent_dispatch_record.py`) resolve the engine's address and token only
  from the service lease and `config.yml` in the config directory under the
  OS account home. They read no `NX_SERVICE_URL`, `NX_SERVICE_HOST`,
  `NX_SERVICE_PORT`, `NX_SERVICE_TOKEN`, `NEXUS_CONFIG_DIR` or
  `NX_CONFIG_DIR`, all of which a repository can set (records 27 and 34). A
  test sets each of those variables to a decoy and asserts no request goes to
  it; the lint (Decision 11) fails on an environment read in any module that
  sends telemetry. The cost is that a sandbox session which isolates itself
  with `NEXUS_CONFIG_DIR` still sends its events, when telemetry is on, to
  the engine of the account's own config directory (inferred, not tested).
- **The auto-approve hook answers on PreToolUse only.** It registers no
  PermissionRequest answer for any tool on its allowlist (the
  `mcp__plugin_conexus_.*` matcher, not only the seven destructive tools).
  The plugin removes the PermissionRequest registration from
  `conexus/hooks/hooks.json`, and the CLI's `auto_approve` returns no answer
  when the event is PermissionRequest, so an older plugin that still
  registers it, and an older CLI under a newer plugin, each lose the
  override as soon as the other side updates (record 36). The evidence is two
  spikes. A PermissionRequest allow overrides a user's `ask` rule (record
  26), which is the defect in every shipped release. A PreToolUse allow alone
  still approves a tool that has no rule, in default mode, in `acceptEdits`
  and for a subagent, and it leaves an `ask` rule prompting and a `deny`
  rule denying (record 33). The PermissionRequest answer was added in April
  2026 when PreToolUse hooks did not reach subagent calls (RDR-035); that
  limitation is gone, and the PreToolUse answer added in August 2026 covers
  auto mode (record 33). No reader of Claude Code's permission files is
  needed, so none of the sources a hook cannot read (`--settings`,
  server-managed settings, MDM profiles) matter. The MCP-tool and auto-mode
  cases are still to be spiked with the real plugin (Critical Assumptions).

Loosenings that remain, and why each is acceptable:

- **Auto-approve for every user** (ruling 4). The seven tools
  (`memory_delete`, `plan_delete`, `answer_plan_delete`,
  `memory_consolidate`, `nx_tidy`, `tuple_in`, `mailbox_send`) are
  auto-approved for everyone. This is a product decision, not a setting, so
  no repository controls it. A user gets a prompt back with an `ask` rule in
  their own Claude Code settings, and a project with one in its tracked
  `.claude/settings.json`; with no PermissionRequest answer, Claude Code
  applies both (record 33). Sam's decision S4.

  The draft's `confirm_tools` setting stays dropped. It existed because the
  `ask` route was unproven; with the PreToolUse-only answer, Claude Code's own
  rules do the same job for a user and for a project, for any tool, in the
  grammar users already know. A plugin key beside them would be a second
  vocabulary for one decision, and the two would drift.
- **`NX_PROCEDURES_REPO_ROOT=1`** in a project's `.claude/settings.json`
  `env` turns on that repository's own `.nexus/procedures` (RDR-242 S5,
  Sam's ruling of 2026-11-08, record 40). It is a switch a repository
  controls, and it is RDR-242's, not a conexus hook setting; it stays as Sam
  ruled.
- **Remote test hosts** move from the environment variable
  `NX_REMOTE_TEST_HOSTS` (removed earlier, D11) to the user file. A project
  cannot set them.
- **`NX_STOP_WAKE_GUARD=0`** stays an environment kill switch, which a
  project's `env` can set (record 27). It turns off an advisory continuation,
  not a deny, prompt or data flow, and scripted `claude -p` runs need it. It
  is outside the invariant by the invariant's own definition.
- **`# routing-allow:`** escapes are unchanged: an agent may use one only on
  the user's explicit instruction.

Out of the threat model, as stated above: an agent that deliberately edits
either file to escape a guard. This is recorded as a risk.

#### Decision 10: The subagent git guard fix (nexus-5y47c) and the merge order

The guard fix adds a segment-level admission layer (`routing/_git_admit.py`)
inside the full guard and rewrites its deny text; `body()` still chooses
full guard or seatbelt with `_maintainer(payload)`. On the new reader that
one line becomes `if _guard_mode(payload) == STRICT`, and the import block
swaps `maintainer` for `guard_mode`, with the fallback answering strict.

Merge order: **nexus-5y47c lands on `develop` first**, then the Phase 3
branch rebases onto it. The fix is reviewed and confirmed, it does not touch
the settings model, and it removes false denials of the kind Research 8 shows, which users
would otherwise meet the first time they opt into `strict`. The reverse
order would ship strict mode to users with the over-matching guard.

The collision is larger than the draft said. The two branches change 11 of
the same files (record 35): the guard and its registry, the manifest, the
pending-release ledger, the relay template, the orchestration skill and its
reference, the golden replay worker and test, and two guard test files. The
gate critique counted 14; the difference is not resolved (record 35). The
substantive conflict is the golden replay: at 86a5fa0ff1 it is built on
`flagged_home`, `mark_nexus` and `maintainer()` (record 35), which this RDR's
lint outlaws. Rebasing therefore includes re-expressing the golden replay in
the new postures (project-strict for the nexus checkout, plain for a user's
repository) so that it still proves the strict path did not move. The
prerequisite is open: the fix is not yet on `develop` (record 35).

The full guard's deny text now reaches any user who opts in (developer item
22). It keeps "orchestrator", which is the plugin's own documented term (its
orchestration skill), defined once as "the orchestrator (the session that
dispatched you)", and replaces "STRUCTURE-AGNOSTIC" with "it reads the raw
command text". The rule's internal name
`subagent_git_write_requires_orchestrator` stays, because routing events and
the registry key on it.

#### Decision 11: The test model

- **Fixtures.** Three postures, built by `tests/_audience_fixtures.py`:
  *plain* (a bare `git init`, no project file, an account home with no
  `conexus.json`), *project-strict* (`project.json` with `subagent_git:
  strict` and one advice row, say `whole_disk_scan`), and *user-strict*
  (`conexus.json` with `subagent_git: strict` and a different row, say
  `pattern_kill`). Distinct rows let a combined case prove the union
  (developer item 19). The account home is set only through the in-process
  seam `_ACCOUNT_HOME_OVERRIDE`. `tests/AGENTS.md` states the convention.
- **Reader tests**, table-driven from `tests/fixtures/audience_corpus.json`
  over both byte copies: every row of the Decision 4 failure table, the
  per-key precedence matrix (project value by user value), the `HEAD` union,
  the legacy markers, the regular-file and size checks, and the problem list
  each case produces.
- **The behaviour leg** (`tests/e2e/lib/plugin_behaviour.py`, run by the
  out-of-repo gate) drives every hook registration under the three postures
  in P0 and P2, asserts that plain output carries no nexus lexicon (bead id,
  RDR id, owner or peer name, lab host, `develop/main`, `Hellblazer/nexus`),
  that each `SETTING:<key>` text appears exactly when its key is on, and that
  the only denies are those the posture expects. "orchestrator" leaves the
  forbidden lexicon as process vocabulary. One named exception remains until
  Sam answers S6: the ask-answer frame in `mailbox_drain.py` keeps "Sam" and
  `<<<sam-words`, so the leg exempts that frame by name and the allowlist
  carries one entry for it.
- **The boundary of the lexicon rule.** It covers everything the plugin
  prints or injects and the plugin's agent, skill, command and resource
  markdown. Source comments and docstrings may cite records, rulings and
  people as provenance, because they are not shown to users or models;
  identifiers that encode the old model are renamed (Decision 12).
- **The manifest** (`conexus/hooks/audience.yaml`) uses three audiences:
  `EVERY-USER` (no gate, or a gate on the user's own content such as
  `.beads/`), `SETTING:<key>` (fires only when that key is set), and
  `REMOVE`. The module keeps the names `_audience.py` and `audience.yaml`:
  ruling 9's hot-rule line names the manifest, the cross-release parity test
  locates the old copy by that name, and "audience" now means which setting,
  if any, a rule answers to.
- **The lint** (`tests/test_plugin_audience_lint.py`) fails on: a
  registration or module-level text constant with no entry; a
  `SETTING:<key>` whose key is not in `SETTINGS`, or that sits in a module
  that never calls that key's reader (developer item 18); a key in
  `SETTINGS` that no row and no reader uses; any `maintainer`,
  `account_flag` or `maintainer-profile` symbol in the plugin or
  `src/nexus/hooks/`; an `os.environ` read in `_audience.py` other than
  `CLAUDE_PROJECT_DIR`; an endpoint or config-directory environment read
  (`NX_SERVICE_*`, `NEXUS_CONFIG_DIR`, `NX_CONFIG_DIR`) in any module that
  sends telemetry, found by tracing every caller of the two telemetry entry
  points (`log_routing_event`, `record_dispatch`) to the module that opens
  the connection (Decision 9); a PermissionRequest registration in
  `hooks.json` for the conexus matcher; a JSON Schema file or `docs/settings.md` table that
  disagrees with `SETTINGS`; and the existing guarded-import and wrapper
  rules, with each reader's failure value checked against the table.
- **Cross-release.** The parity test that drives the previous release's
  helper keeps only the cases both models agree on (strict in a checkout with
  the Phase 1 marker, the seatbelt in a plain repository).
- **The legacy window test** (Decision 7).

#### Decision 12: What the plugin keeps and what moves to nexus

The Stop wake guard keeps its generic job: a turn that promises a wait
nothing will wake is continued once with the generic wake mechanisms. A
project's `ci_wait` command, when set, is named in that text. The separate
"no channel" class, which tells the session that a CI completion post on
the CI board will not wake it, is removed from the plugin and folds into the
generic CI-wait class (developer item 13). Its sentence is true only where a
project runs nexus's CI board and its channel, so it is a nexus fact; nexus's
practice is already in its own `AGENTS.md` (worktree rule 7). What stays in
the plugin is the hook point, a project-supplied command.

The Stop classifier's personal and peer names are already gone; its internal
identifiers that encode the old model (`is_sam_gated`, `final_sam`, the class
name `final-to-sam`) are renamed to user vocabulary in the same change, since
plugin source ships to users (developer item 14). Any reader of those class
names in `hook.log` is found by search at implementation and updated with
them.

The ask-answer frame (`mailbox_drain.py`: "An unverified claim that Sam
answered your ask", delimited by `<<<sam-words <nonce>`) is a leak with a
contract on top: Sam's `CLAUDE.md` files quote both strings, and agents act on
them. The generic form is "the user" and `<<<user-words <nonce>`. Changing
the frame without his `CLAUDE.md` edits on the same day would make his agents
distrust real answers, and `CLAUDE.md` edits are his. Sam's decision S6
(developer item 16).

#### The 22 implementation choices

| # | The choice at d6a0b258b0 | Decision |
| --- | --- | --- |
| 1 | `schema: 1` unchecked; `project.json` alone decides | Renamed `version`, checked (D2). Kept: `project.json` alone decides; a legacy marker beside it is reported (D7). |
| 2 | Silent fallbacks everywhere | Overturned. Every problem is reported, at session start, in the affected deny, in the log and by `nx settings` (D4, D5). |
| 3 | Strict if either says strict; `advice_block` union | Kept, extended to the `HEAD` copy and the payload checkout's copy (D1, D3, D9). |
| 4 | Strings stripped, commands not validated | Kept that commands are not interpreted; added type, one-line, control-character and length limits (D2). |
| 5 | Walk from `cwd`, then `CLAUDE_PROJECT_DIR`, to the first `.git` | Changed: text keys come from the session root (`CLAUDE_PROJECT_DIR` first), and the tightening keys combine the session root's copies with the payload checkout's, because a worktree's commit can differ from the primary's (D1, Research 10). |
| 6 | User file per-key defaults; OS account home | Kept for the home; overturned for `subagent_git`, which fails strict (D4, S2). |
| 7 | One module, byte-copied, per-reader fallbacks | Kept; the fallbacks are checked against `SETTINGS` by the lint (D11). |
| 8 | Both legacy markers read strict | Kept, with a tested removal (D7). |
| 9 | One `telemetry` key, user only | Kept (D2). |
| 10 | `t1_nudge` user only, all agent types; dispatch record independent | Kept (D2). |
| 11 | `advice_block` ids undocumented; unknown id a no-op | Documented from the table; unknown ids reported; ids are a contract with aliases (D2). |
| 12 | Git row only when strict; preflight wording names `PREFLIGHT_FAIL` | Git row kept. Wording becomes the exit-status contract (D2, Research 7). |
| 13 | `ci_wait` command plus the board "no channel" sentence | Command kept as a project hook point; the board sentence leaves the plugin (D12). |
| 14 | Names dropped; internal identifiers kept | Identifiers renamed too (D12). |
| 15 | Default branch falls back to `origin/develop` | `integration_branch` setting added; the `develop` guess removed (D2, Research 5). |
| 16 | Mailbox frame keeps "Sam" | Sam's decision S6 (D12). |
| 17 | `remote-test-hosts` a separate file | Folded into `conexus.json`; old file read during the window (D2, D7). |
| 18 | Manifest vocabulary; SETTING row must call its reader | Kept, with more lint rules (D11). |
| 19 | Both strict fixtures block the same row; "orchestrator" leaves the lexicon | Rows made distinct; "orchestrator" kept as product vocabulary (D11). |
| 20 | Seven tools auto-approved; an `ask` rule claimed as the users' route, unverified | Refuted by the spike (record 26): the plugin's PermissionRequest allow overrides an `ask` rule. Auto-approve now answers on PreToolUse only, for every allowlisted tool, which a second spike shows still approves a tool with no rule and leaves `ask` and `deny` rules in force (record 33), so the `ask` rule is the route (D9, S4). |
| 21 | No removal test for the window | Version-keyed test (D7). |
| 22 | Strict deny text says "orchestrator" and "STRUCTURE-AGNOSTIC" | Term defined once; shouting removed (D10). |

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Settings reader | `conexus/hooks/scripts/_audience.py` and `src/nexus/hooks/_audience.py` | Extend the rework's readers: add `SETTINGS`, problems, `version`, `$` keys, the file checks, the `HEAD` read, the session-root and payload-checkout copies, `integration_branch`, `remote_test_hosts`, the `show` entry point. |
| Auto-approve on PreToolUse only | `src/nexus/hooks/auto_approve.py`, `conexus/hooks/hooks.json` | Extend: no answer on PermissionRequest; remove the PermissionRequest registration (Decision 9). |
| Telemetry endpoint | `conexus/hooks/scripts/routing/_lib.py` `_engine_endpoint`, `src/nexus/hooks/agent_dispatch_record.py` | Extend: resolve from the account's lease and `config.yml` only when sending telemetry (Decision 9). |
| Once-per-session notes | `_audience._note_once` | Reuse for problem fingerprints. |
| User-visible notice | `systemMessage` in `src/nexus/_hook_runtime/_io.py` | Reuse the field from a plugin-side stdlib SessionStart script. |
| Inspection | `src/nexus/commands/config_cmd.py` | New `settings_cmd.py` beside it; `config` stays nx's own config. |
| Guard gate | `routing/subagent_git_write_requires_orchestrator.py` `body()` | Swap the predicate after nexus-5y47c lands. |
| Remote hosts reader | `_audience.remote_test_hosts()` | Extend to read `conexus.json` and, during the window, the old file. |

### Decision Rationale

The person signal had three remaining uses (a wider auto-approve, telemetry,
strict mode in Sam's repositories), and each is a preference with a better
carrier anyone can use (analysis section 5). A tracked project file is the
one carrier that reaches every place a project's sessions run, and a user
file under the OS account home is the one location no repository can move.
Keeping both under one stdlib reader that also powers the inspection
command makes "what the hooks do" and "what the user sees" the same code. The
reporting design closes the gap the rework left: a settings file a user
cannot see working is worse than none, because it creates belief in a
protection that is not there.

## Alternatives Considered

### Alternative 1: Keep the person signal for loosenings only

**Description**: Delete `maintainer()` but keep `account_flag()` for the
loosening cases (the wider auto-approve, telemetry).

**Pros**: Sam's sessions change least.

**Cons**: Keeps a product that behaves differently by who is typing; the
loosenings it would carry have native carriers (permission rules, a user
setting).

**Reason for rejection**: Ruling 1.

### Alternative 2: Repo-keyed only (analysis M2)

**Description**: Keep `nexus_repo()`, delete the flag.

**Pros**: Small change.

**Cons**: Keeps "is this nexus" as the gate for process; every other
project, and Sam's other repositories, lose the process.

**Reason for rejection**: It fixes nothing Sam objected to (analysis
section 5).

### Alternative 3: Plugin generic with no settings (analysis M1 alone)

**Description**: Everything at one default for everyone; nexus's specifics
only in its prose files.

**Pros**: No settings surface.

**Cons**: The git guard has two sensible behaviours, and either single
default hurts one group: strict for all brings back the complaint that
users' subagents cannot commit, seatbelt for all removes nexus's protection.

**Reason for rejection**: Some process needs a choice; M3 (this RDR) adds
the minimum settings for those, with M1 for prose.

### Alternative 4: Claude Code plugin `userConfig` for user settings

Weighed in Decision 1 and rejected: environment-delivered (spoofable by a
project), user scope only, absent from cloud sessions, unreadable to nx
outside Claude Code.

### Alternative 5: Auto-approve answers on PreToolUse only (chosen)

**Description**: Remove the auto-approve hook's PermissionRequest answer, in
`hooks.json` and in the CLI's `auto_approve`, and keep the PreToolUse answer.

**Pros**:

- Fixes the shipped override of a user's `ask` rule (record 26) with no new
  code: a spike shows a PreToolUse allow approves a tool that has no rule and
  leaves `ask` and `deny` in force, in default mode, `acceptEdits` and a
  subagent (record 33).
- Claude Code applies its own rules from every source it reads, including
  the ones a hook cannot read (`--settings`, server-managed settings, MDM).

**Cons**:

- Bash stood in for an MCP tool, and auto mode was not run; both are Stage 1
  spikes. A version of Claude Code in which PreToolUse does not reach
  subagent calls (RDR-035's era) would show prompts, the safe direction.

**Reason chosen**: It removes the cause instead of compensating for it.

### Alternative 6: Read Claude Code's permission files and stay silent on a match

**Description**: The round-1 draft's remedy. Before answering allow on either
event, the hook reads `ask` and `deny` rules from the user, project, local
and managed settings files and gives no answer when one matches.

**Pros**: Works whichever event Claude Code consults.

**Cons**: A hook cannot read every source Claude Code uses: rules from
`--settings`, server-managed settings and MDM profiles are not files on the
hook's path, so a rule there would still be overridden. It must also
reproduce Claude Code's file locations (the shared file in the session's
start directory per Research 6; the local file's location as the gate
critique gives it, inferred, not read) and its rule grammar. And it treated an
unparsable file differently from Claude Code, which the gate critique says
skips an invalid file (not read in the docs here), so a broken file would
cause prompts for every conexus tool with no explanation.

**Reason for rejection**: Alternative 5 fixes the defect without any of this,
and the spike shows it is enough.

### Briefly Rejected

- **nx `config.yml` for user settings**: YAML, credentials, `NEXUS_CONFIG_DIR` (Decision 1).
- **Project `.claude/settings.json` hooks for nexus's behaviour**: cannot carry plugin-internal behaviour, reverses nexus-3h0u6 (Decision 1).
- **A `confirm_tools` setting** (the first draft): a second vocabulary beside Claude Code's `ask` rules, which Alternative 5 lets work (Decision 9).
- **A generic `notes: {rule id: text}` map instead of per-slot text keys**: lets a project splice text into any deny; per-slot keys are validated, documented and budgeted.
- **Silent fallbacks** (the rework): Gap 4.
- **A writer command now**: Decision 5.

## Trade-offs

### Consequences

- Every user gets the plugin's process by default, with no nexus words in it,
  except the ask-answer frame's "Sam" until S6 is answered.
- Sam's experience outside nexus depends on a file he writes on each
  machine, the same as anyone's.
- Two public contracts are created (two files, one command) that must be
  kept compatible indefinitely.
- A project maintainer's edit that lowers `subagent_git` waits for a commit.
- Telemetry stops for users who never asked for it; `nx telemetry` reports
  have nothing to read on those installs.

### Risks and Mitigations

- **Risk**: A PreToolUse-only allow does not approve a conexus MCP tool in
  some mode (auto mode, or a Claude Code version where PreToolUse does not
  reach subagent calls, as RDR-035 once recorded), so users meet prompts
  they did not meet before.
  **Mitigation**: The Stage 1 spike with the real plugin in default,
  `acceptEdits` and auto mode, for a main session and a subagent. The failure
  is a prompt, not a silent approval, so it fails in the safe direction.
- **Risk**: A malformed project file in a stranger's repository gives a user
  the full guard unexpectedly.
  **Mitigation**: The session-start message and the deny text both name the
  file and the reason; a linked worktree is still exempt.
- **Risk**: A subagent edits the user's `conexus.json`.
  **Mitigation**: Out of the threat model (Decision 9); we assume, without a
  test, that Claude Code prompts for writes outside the project in the
  default mode (record 23). Revisit if an incident shows it.
- **Risk**: The settings report at session start becomes noise if a problem
  persists.
  **Mitigation**: Once per session start, only when a problem exists, and
  only from one hook.
- **Risk**: The compatibility window is forgotten.
  **Mitigation**: The version-keyed test fails the build when it ends.
- **Risk**: The rework, the guard fix and the portability plan's Phase 2
  un-gating ship in one client release and interact.
  **Mitigation**: Fixed merge order (Decision 10), the golden replay
  re-expressed in the new postures, one review gate on the rebased branch
  (ruling 8), the behaviour leg in three postures.
- **Risk**: A repository redirects telemetry through the environment.
  **Mitigation**: The senders are pinned to the account's lease and
  `config.yml` (Decision 9), with a decoy-variable test and the lint.

### Failure Modes

- A broken project file: strict guard, visible at session start and in the
  deny. Recovery: fix the file; `nx settings check` exits 0.
- A broken user file: strict guard, telemetry off, nudge on, visible the
  same way.
- The reader module missing or raising: each caller's guarded import falls
  back to its failure value (strict for the guard, no telemetry, nudge on,
  empty lists), and `_note_once` records it.
- The `HEAD` read timing out: strict. Visible in `hook.log`.

## Implementation Plan

### Prerequisites

- [ ] Sam's answers to S1-S6 (below).
- [ ] nexus-5y47c on `develop` (Decision 10); open (record 35).
- [x] Stage 0 spikes recorded (records 26 to 33, and 37).

This RDR's implementation steps are called *stages* so they are not confused
with the portability plan's Phases 1 to 4 (Phase 1 shipped in 8.5.7, Phase 2
is on `develop`, Phase 3 is the rework branch). What ships when:

- The auto-approve fix (Decision 9) may ship ahead of everything else: its
  plugin half (the `hooks.json` registration) can go in a plugin-only
  release, and its CLI half (`auto_approve` ignoring PermissionRequest) ships
  with the CLI. Either closes the shipped defect alone (record 36). S4.
- Stages 1 to 3 ship together, with the portability plan's Phases 2 and 3, in
  the next client release (ruling 8).
- Stage 4 ships in the release that `LEGACY_ACCEPTED_UNTIL` names.

### Minimum Viable Validation

In a scratch repository on a P0 machine (plugin only): with no files a
dispatched subagent can `git commit` and is denied `git push --force`; after
writing `.nexus/project.json` with `{"version": 1, "subagent_git": "strict"}`
the same subagent is denied `git commit` in the primary and allowed it in a
linked worktree; after misspelling the key, the next session start shows a
user-visible message naming the file and suggesting `subagent_git`; and
`python3 <plugin root>/hooks/scripts/_audience.py show` reports each state.
The same run on a P2 machine with `nx settings show` gives identical output.

### Stage 0: Measure (no release), done 2026-11-25 and 2026-11-26

The spikes ran and are recorded as `nexus_rdr/259-research-26` to `-33` and
`-37` (Research 10 and 11): the PermissionRequest-versus-`ask` precedence
(refuted the draft's assumption), the PreToolUse-only allow (enough, and it
leaves `ask` and `deny` in force), the worktree subagent's settings and
`cwd` (settled; the worktree's base commit is not), `systemMessage` in the
CLI (settled for a project hook on `startup`), project env reaching plugin
hooks (confirmed), the seatbelt bypass, the reader's cost and the `HEAD`
read's exit codes. What they left open moves to Stage 1.

### Stage 1: The reader, the reporting and the inspection command

On the portability plan's Phase 3 branch, rebased onto nexus-5y47c.

1. Tests first (Decision 11): the corpus rows for the failure table, the
   precedence matrix, the `HEAD` reads and their case table, the
   session-root and worktree copies, the legacy window, auto-approve's
   silence on PermissionRequest, the telemetry decoy test, and the lint
   rules.
2. `SETTINGS`, the readers, the problem list, the file checks, the `HEAD`
   read, the session-root and payload-checkout copies, `integration_branch`,
   `remote_test_hosts` and the `show` entry point in `_audience.py`; byte
   copy to `src/nexus/hooks/`.
3. The guard gate swap and deny text (Decision 10), and the golden replay
   re-expressed in the new postures (project-strict for the nexus checkout,
   plain for a user's repository) in place of `flagged_home`, `mark_nexus`
   and `maintainer()` (record 35); auto-approve answers on PreToolUse only
   (the `hooks.json` registration removed, and the CLI's `auto_approve`
   silent on PermissionRequest; Decision 9); the telemetry senders pinned
   (Decision 9); compaction uses `integration_branch`; the Stop guard change
   and identifier renames (Decision 12); the provenance wording for project
   texts (Decision 2); the orchestration-skill line on worktree base commits
   (Decision 8).
4. Spikes left open by Stage 0: auto-approve with the real plugin and a
   harmless conexus MCP tool, in default, `acceptEdits` and auto mode, for a
   main session and a subagent, with and without a user `ask` rule; the
   plugin-hook form of the SessionStart `systemMessage` on `startup`,
   `resume` and `clear`; `CLAUDE_PROJECT_DIR` in the nx-side hooks; the
   reader's cost on native Windows and on the `HEAD` path. Desktop's display
   of `systemMessage` is a hand check by Sam.
5. The SessionStart problem notice (plugin-side, stdlib, checking that the
   installed nx has `nx settings`) and the deny-text clause for an
   unreadable file.
6. `nx settings show` (with both versions and the project texts) and
   `check`; the `nx doctor` row.
7. The JSON Schema files.

**Ship when**: the reader and lint tests pass on both copies; the behaviour
leg is green in plain, project-strict and user-strict, P0 and P2; the lint
finds no `maintainer`, `account_flag` or `maintainer-profile` symbol and no
environment read in a telemetry sender; the re-expressed golden replay is
green over the recorded corpus; the Minimum Viable Validation passes on the
laptop and on native Windows; the added hook time on native Windows is
measured and under 5 ms per call (the laptop's is already measured, record
32); with the real plugin loaded, a conexus tool with no rule runs with no
prompt and the same tool under a user `ask` rule prompts, in default,
`acceptEdits` and auto mode; the step 4 spikes are recorded.

### Stage 2: Un-gate and move nexus's words

1. Un-gate the portability plan's Phase 2 texts on the same branch with ids removed (ruling 3);
   remove the phase-gate incident clause, the tripwire and the "no maintainer
   flag" SessionStart line.
2. nexus's `.nexus/project.json` as in Decision 7; delete
   `.nexus/dev-process.json`.
3. The prose moves to nexus's `CLAUDE.md`, `.claude/rules/` and project
   skills (Sam's edits for `CLAUDE.md`).

**Ship when**: the behaviour leg's allowlist holds only the ask-answer frame's
entry, which S6 removes; a nexus session shows
the full guard, the preflight row, the local-and-cloud question and the CI
wait command, and a plain repository shows none of them; one review gate
(code-review-expert and substantive-critic) passes on the rebased branch.

### Stage 3: Documentation and records

1. `docs/settings.md`, the README section, `docs/configuration.md`,
   `docs/cli-reference.md`, `docs/agents/plugin-audiences.md`, `promotion.md`'s
   audience step, `tests/AGENTS.md`, the AGENTS.md hot-rule line (ruling 9).
2. A changelog entry in `changelog/unreleased/` that states the user-visible
   changes in Decision 7's table.

**Ship when**: the docs tables pass the lint against `SETTINGS`; the release
notes name the telemetry change and the two new files.

### Stage 4: End of the compatibility window

When the legacy window test fails (Decision 7), remove the legacy markers
and the `remote-test-hosts` file read, and their tests.

**Ship when**: the window test passes with the legacy tuples empty.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| `.nexus/project.json` | `nx settings show` | `nx settings show --json` | the user deletes the file | `nx settings check` | tracked in git |
| `conexus.json` | `nx settings show` | same | the user deletes the file | `nx settings check` | N/A: a small hand-written file |

### New Dependencies

None.

## Test Plan

- **Scenario**: no files, plain repository. **Verify**: seatbelt; the
  generic texts appear without nexus lexicon; no telemetry is sent.
- **Scenario**: `project.json` strict. **Verify**: full guard in the primary,
  exemption in a linked worktree, the Git row appears.
- **Scenario**: user `conexus.json` strict, project absent. **Verify**: full
  guard in every repository.
- **Scenario**: project `seatbelt`, user `strict`. **Verify**: strict;
  `show` reports the outranked project value.
- **Scenario**: working tree drops `subagent_git`, `HEAD` has strict.
  **Verify**: strict; `show` reports the difference.
- **Scenario**: each row of the failure table, for each file. **Verify**:
  the stated value, and one problem with file, key and value used.
- **Scenario**: a FIFO and a 1 MiB file at `.nexus/project.json`. **Verify**:
  the hook returns promptly and reads strict.
- **Scenario**: `telemetry: true` in a project file. **Verify**: no event is
  sent; a problem is reported.
- **Scenario**: the auto-approve verb called with a PermissionRequest payload
  for an allowlisted tool. **Verify**: no answer. `hooks.json` registers no
  PermissionRequest entry for the conexus matcher.
- **Scenario**: the real plugin loaded, a harmless conexus MCP tool, default,
  `acceptEdits` and auto mode, main session and subagent. **Verify**: with no
  rule the tool runs with no prompt; with a user or project `ask` rule it
  prompts; with a `deny` rule it is denied.
- **Scenario**: `NX_SERVICE_URL`, `NX_SERVICE_HOST`, `NX_SERVICE_PORT`,
  `NX_SERVICE_TOKEN`, `NEXUS_CONFIG_DIR` and `NX_CONFIG_DIR` each set to a
  decoy, `telemetry` on. **Verify**: no request reaches a decoy; events go to
  the account's own lease endpoint or nowhere.
- **Scenario**: a text key changed in the working tree only. **Verify**: the
  hooks print `HEAD`'s value; `show` reports the difference.
- **Scenario**: each row of the `HEAD`-read case table (Decision 1).
  **Verify**: the stated value; a file absent at `HEAD` is not strict.
- **Scenario**: a worktree subagent whose worktree commit carries a
  `project.json` without `subagent_git`, while the primary's commit says
  `strict`. **Verify**: strict, read from the session root.
- **Scenario**: `advice_block` in both files with different rows. **Verify**:
  both rows deny.
- **Scenario**: nexus checkout without `project.json` but with the Phase 1
  marker. **Verify**: strict, until `LEGACY_ACCEPTED_UNTIL`.
- **Scenario**: `integration_branch: develop` in nexus. **Verify**: the
  compaction count is measured against `origin/develop`, not `origin/main`.
- **Scenario**: session start with a misspelled key. **Verify**: one
  `systemMessage` with the suggestion, and no settings text from any other
  hook in that session.
- **Scenario**: `nx settings show` and the stdlib `show` on the same
  directory. **Verify**: identical output.

## Validation

### Testing Strategy

The reader corpus drives both byte copies through every failure row and
precedence pair; the behaviour leg runs the real registrations; the lint
holds the table, the docs, the schemas and the absence of the person signal
together. Done means the Stage 1 to 3 ship-when lines hold.

### Performance Expectations

The reader adds a directory walk, at most two small file reads, and on one
rare path a git call. On the laptop the warm cost is about 60 microseconds
and the cold cost 1.2 ms (Research 10, record 32); native Windows and the
`HEAD` path are measured in Stage 1 against the 5 ms bound.

## Finalization Gate

### Contradiction Check

The rework failed a broken user file to the seatbelt; Decision 4 fails it
strict. That is a deliberate change, put to Sam as S2. The draft's claim that
a user's `ask` rule was a route back to a prompt was refuted by the spike
(record 26). Round 1's remedy, a reader of Claude Code's permission files,
was replaced after the gate by a PreToolUse-only answer that a spike showed
is enough (record 33), and the draft's `confirm_tools` is dropped. The
draft's invariant was false for telemetry (record 34); Decision 9 pins the
senders. No other contradiction found between the research, the rulings and
the design.

### Assumption Verification

Five assumptions remain (Critical Assumptions), each with a Stage 1 spike,
measurement or hand check. The one the direction leans on is that a
PreToolUse-only allow behaves for a conexus MCP tool and in auto mode as it
did for Bash (record 33); its failure mode is an unexpected prompt, the safe
direction. The worktree base commit no longer matters to the design
(Decision 1). Three
minor assumptions are acknowledged and carry no decision.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| PreToolUse `permissionDecision: allow` vs `permissions.ask` | Claude Code | Docs Only (stated) |
| PermissionRequest `allow` vs `permissions.ask` | Claude Code | Spike, record 26 (the allow wins) |
| Project `env` reaching plugin command hooks | Claude Code | Spike, record 27 |
| Settings a worktree subagent runs under | Claude Code | Spike, record 28 (the main session's) |
| Hook payload `cwd` in a worktree subagent | Claude Code | Spike, record 29 (the worktree); base commit unsettled |
| `systemMessage` at SessionStart | Claude Code | Spike, record 30 (CLI, project hook, `startup`); plugin hook, `resume`, `clear` in Stage 1; Desktop by hand |
| PreToolUse-only allow for a conexus MCP tool, and in auto mode | Claude Code | Spike, record 33 for Bash in default and `acceptEdits`; MCP tool and auto mode in Stage 1 |

### Scope Verification

The Minimum Viable Validation is Stage 1's ship-when, on P0 and on Windows.

### Cross-Cutting Concerns

- **Versioning**: the `version` field and the key-growth rules (Decision 2).
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: Stages 1 to 3 ship in one client release with the
  plugin (ruling 8); the auto-approve fix has a plugin half and a CLI half,
  either of which closes the defect, and may ship first (Implementation Plan,
  "What ships when"); a plugin and CLI on different versions is a normal
  state (record 36).
- **IDE compatibility**: JSON Schema files let editors validate both files.
- **Incremental adoption**: no file means today's 8.5.7 behaviour minus nexus words.
- **Secret/credential lifecycle**: neither file holds secrets; the user file is kept apart from `config.yml` so the settings reader never parses credentials. The engine token the telemetry senders attach is taken only from the account's lease or `config.yml`, never from an environment variable a repository can set (Decision 9, record 34).
- **Memory management**: N/A (files capped at 64 KiB).

### Proportionality

The document is long because it fixes a public contract. The 22-row table
could move to a companion note once the gate has read it.

## Out of Scope

- A writer command (`nx settings set`, `init`).
- A plugin `userConfig` front end to the user file.
- Per-directory settings inside one repository (monorepo subprojects).
- A per-user, per-project local file.
- An organisation or managed-settings layer for conexus settings.
- The `sn` plugin in this repository registers its own auto-approver on both
  events the same way (commit 91c0cddebc); the same fix applies to it, and it
  is not part of this RDR.
- nx's own behaviour settings, which stay in `config.yml` and `.nexus.yml`.
- Configurable advice text, or new advice rows.
- Phase 4 of the portability plan (moving agent text into SubagentStart).
- Defending against an agent that deliberately edits either settings file
  (the threat model in Decision 9).

## Decisions Only Sam Can Make

- **S1. Is the user file `<account home>/.config/nexus/conexus.json`, in
  JSON?** Recommended: yes. Plugin `userConfig` can be spoofed by a
  project's environment and is absent from cloud sessions; `config.yml` is
  YAML with credentials (Decision 1).
- **S2. Should a broken user file give the strict guard, as a broken project
  file does?** Recommended: yes. A user writes the file to change something,
  most likely to ask for strict; the rework fell back to the seatbelt
  silently (Decision 4).
- **S3. Should the project file's `HEAD` copy count, so a working-tree edit
  can tighten at once but loosens only after a commit?** Recommended: yes.
  Without it a subagent can edit the file and then commit in the shared
  checkout (Decision 9).
- **S4. Should the auto-approve hook answer only on PreToolUse, removing its
  PermissionRequest answer for every allowlisted conexus tool, with no plugin
  setting beside it?** Recommended: yes, and ship it ahead of the rest of
  this RDR, in both halves (the plugin's `hooks.json` and the CLI's
  `auto_approve`), because every shipped release silently overrides a
  user's `ask` rule through that answer (record 26). A spike shows the
  PreToolUse answer alone still approves a tool that has no rule and leaves
  `ask` and `deny` rules in force (record 33), so ruling 4 (auto-approve for
  everyone) holds for every user who has written no such rule. Two things
  to know: you may see prompts you do not see today if your own settings
  hold `ask` rules for conexus tools, and the MCP-tool and auto-mode cases
  are spiked in Stage 1 before release (Decision 9).
- **S5. Is the inspection command named `nx settings` (`show`, `check`)?**
  Recommended: yes, cross-linked from `nx config --help` and `nx doctor`.
  The alternative is `nx config plugin show`, which hides it under nx's own
  config (Decision 5).
- **S6. Change the ask-answer frame to "the user" and `<<<user-words`, on the
  same day you edit your `CLAUDE.md` files to match?** Recommended: yes, as a
  separate small change after this release, so the frame and your
  instructions never disagree. Until then the frame keeps your name
  (Decision 12).

Actions only Sam takes, no decision needed: write his own `conexus.json` on
each machine where he wants strict, telemetry and remote hosts; delete
`maintainer-profile`; make the `CLAUDE.md` edits that move nexus prose.

## References

- Sam's rulings: T2 `nexus/sam-ruling-audience-model-settings-not-identity-2026-11-25`.
- Analysis: `~/.local/state/nexus/plans/audience-model-analysis.md` (T2 `nexus/audience-model-analysis-2026-11-25`).
- Plan of record: `~/.local/state/nexus/plans/plugin-portability-plan.md`; rulings T2 `nexus/sam-ruling-plugin-portability-plan-2026-11-25`.
- Implementer choices: T1 `de007438` (tag `rdr-259-inputs`).
- Rework: commit d6a0b258b0 on `feature/nexus-sby49.4-plugin-portability-p3`.
- Guard fix: 86a5fa0ff1 on `feature/nexus-5y47c-guard-overmatch`; T2 `nexus/confirm-86a5fa0ff1-nexus-5y47c-2026-11-26-r2`.
- Claude Code docs, fetched 2026-11-25: https://code.claude.com/docs/en/permissions.md, hooks.md, settings.md, settings-reference.md, worktrees.md, sub-agents.md, plugins/manifest-reference.md, plugins/components.md, desktop.md.
- Source: `conexus/hooks/scripts/_audience.py`, `conexus/hooks/audience.yaml`, `docs/agents/plugin-audiences.md`, `src/nexus/config.py:836-853`, `scripts/agent-worktree-preflight.sh`, `src/nexus/commands/ci_cmd.py`, `src/nexus/_hook_runtime/_io.py:243-255`.

## Revision History

- 2026-11-25: drafted.
- 2026-11-25: spikes recorded (records 26 to 32). Auto-approve defers to Claude Code's `ask` and `deny` rules, and `confirm_tools` is dropped; the project file is read from the session root, and a worktree's copy can only add; Critical Assumptions restated.
- 2026-11-26: Gate round 1 — BLOCKED (1 Critical, 6 Significant, 1 ship-blocker(s)); blob `46f4df2ba82a`; critique `nexus_rdr/259-gate-critique-2026-11-26-r1`.
