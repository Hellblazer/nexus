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
keeps its own files instead of using those, and it rests on two questions
the docs do not settle: whether a worktree-isolated subagent reads the
worktree's `.claude/settings.json`, and whether a permission `ask` rule
survives the plugin's PermissionRequest auto-approve.

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
| RDR-143 (plugin and CLI version lockstep, closed) | Precedent | The plugin and nx are kept on one version; mixed-version windows are transient, which bounds the compatibility cases in Decision 7. |
| RDR-201 (closed vocabularies as checked tables, closed) | Precedent | The settings keys and their allowed values are a closed vocabulary. Decision 2 keeps them in one checked table that the docs, the JSON Schema files and the readers are tested against. |
| RDR-245 (structured asks on the threads board) | Adjacent | Its answer frame names "Sam" (developer item 16). Decision 9 treats that as a leak with a contract attached and leaves the switch to Sam. |
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
| Claude Code permissions and hooks | Docs (code.claude.com, fetched 2026-11-25) | Research 6. A PreToolUse allow does not bypass `ask` or `deny` rules; a PermissionRequest allow against an `ask` rule is not settled. |
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
  what a deny rule refuses." Whether a PermissionRequest allow answers the
  prompt an `ask` rule raised is not stated.
- *Plugin `userConfig`* (`plugins/manifest-reference.md`, "User
  configuration", "Where values are stored"): non-sensitive values live under
  `pluginConfigs` in the user's `settings.json`; the settings reference lists
  `pluginConfigs` with scope "User or managed", so no per-project value is
  documented. Hooks see each option as the environment variable
  `CLAUDE_PLUGIN_OPTION_<KEY>`. Values are set through the `/plugin` dialog or
  `claude plugin install --config`.
- *Cloud sessions* (`settings.md`, "Settings in cloud sessions"; `desktop.md`,
  "Limits"): `~/.claude/settings.json` is not read in cloud sessions, and
  plugins installed at user or local scope are not available there. Desktop
  reads the same settings files as the terminal.
- *Project `.claude/settings.json`* (`settings.md`): read from the session's
  primary working directory. Its `env` applies to "every session and its
  subprocesses", which by inference includes command hooks. A project cannot
  disable one hook of a plugin, only the whole plugin (`enabledPlugins`) or
  all hooks.
- *Worktree-isolated subagents* (`worktrees.md`, "Isolate subagents with
  worktrees"; `sub-agents.md`): such a subagent takes its instruction files
  (CLAUDE.md) from the main conversation and runs its Bash commands inside its
  worktree. Which `settings.json` it reads is not stated. For a `--worktree`
  session, hook `cwd` is the worktree root and `CLAUDE_PROJECT_DIR` stays at
  the start directory (`worktrees.md`, "Hook paths don't follow the
  worktree"); by inference a worktree subagent's hooks see the same split.
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
`mcp_connect_check.py`). The code does not say the user sees it; that is an
assumption below.

### Critical Assumptions

- [ ] A PermissionRequest hook's `allow` does not answer a prompt raised by
  the user's `permissions.ask` rule. **Status**: Unverified. **Method**: Docs
  only, and the docs are silent. Decision 9 does not depend on it: the
  `confirm_tools` setting gives users a prompt either way. The spike in
  Phase 0 decides what the docs page tells users.
- [ ] A worktree-isolated subagent's hook payload carries the worktree as
  `cwd`. **Status**: Unverified (inference from `worktrees.md`). **Method**:
  Spike. If it carries the main root instead, the subagent reads the primary's
  `project.json`, which is the same tracked content on the same commit; the
  design holds, and the spike result goes into `docs/settings.md`.
- [ ] A SessionStart command hook's `systemMessage` is shown to the user in
  the CLI and in Desktop. **Status**: Unverified in both; the existing
  `mcp_connect_check` use shows only that the field is written. **Method**:
  Spike.
- [ ] Reading two small JSON files and walking up to `.git` adds under 5 ms
  to a hook call. **Status**: Unverified. **Method**: measure in Phase 1 on
  the laptop and on Windows; the git guard already stats and shells to git on
  many calls.

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

**Project file: `.nexus/project.json`** at the checkout root, which is the
first directory at or above the payload's `cwd` (then `CLAUDE_PROJECT_DIR`)
that holds a `.git` entry (a directory, or the file a linked worktree or
submodule has). A nested repository or submodule is its own project. A linked
worktree reads its own copy, which is the tracked file on that worktree's
branch. A `cwd` outside any repository has no project settings. Sam ruled the
name (ruling 7). The file is tracked so it reaches every place the project's
sessions run: the primary, every worktree, a copy made for a remote test
host, Desktop and `claude -p`. Cloud sessions are a weaker case: a
repository's `.claude/settings.json` hooks and env are ignored there, and
plugins installed at user or local scope are not available, so the file
applies in a cloud session only where the plugin itself runs there (not
settled by the docs; Research 6).

**User file: `<account home>/.config/nexus/conexus.json`.** The account
home comes from the OS account database (`pwd` on POSIX, the
`FOLDERID_Profile` known-folder API on Windows), never from `HOME`,
`USERPROFILE`, `XDG_CONFIG_HOME` or `NEXUS_CONFIG_DIR`. The reason is
Research 6: a project's `.claude/settings.json` can set environment
variables for every subprocess, so any location chosen by an environment
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
  variables, and a project's `env` can set the same names, so if project env
  reaches command hooks (assumed, not measured) a user-only key such as
  `telemetry` could be turned on by a repository (Research 6). No project
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
`.claude/settings.json` (rejected: hooks from it do not reach cloud
sessions, it cannot carry plugin-internal behaviour without duplicating
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
| `confirm_tools` | list of conexus MCP tool names | `[]` | yes | yes | union |
| `brief_questions` | list of up to 8 strings, each one line of at most 300 characters | `[]` | yes | yes | append: project's, then user's, exact duplicates dropped |
| `preflight` | one line, at most 200 characters | none | yes | no | project only |
| `ci_wait` | one line, at most 200 characters; `<sha>` is the only placeholder | none | yes | no | project only |
| `credential_helper` | one paragraph, at most 600 characters | none | yes | no | project only |
| `integration_branch` | a branch name (no whitespace, no `..`, no leading `-`), at most 100 characters | none | yes | no | project only |
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
- `confirm_tools`: the listed tools are left to Claude Code's normal
  permission prompt instead of being auto-approved (Decision 9).
- `brief_questions`: added after the plugin's four generic implementer brief
  questions.
- `preflight`: the SubagentStart worktree row tells a dispatched agent "Run
  `<preflight>` first. If it exits non-zero, stop and report its output." The
  plugin shows the command; it never runs it. Research 7 shows nexus's script
  fits the exit-status contract.
- `ci_wait`: the command the Stop wake guard names when a turn promises to
  wait on CI (Decision 12 on why only the command stays in the plugin).
- `credential_helper`: text the credential guard adds after its fixed
  refusal, naming the project's safe way to use a credential.
- `integration_branch`: the branch the compaction checkpoint measures "not
  yet landed" against. Without it the order is `origin/HEAD`, then
  `origin/main`, then `origin/master`, then unknown; the rework's
  `origin/develop` guess is removed (Research 5). nexus sets `"develop"`.
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

The invariant: **no value a project file holds can remove a deny, remove a
permission prompt, exempt anything from a guard, or cause data to leave the
machine.** A project may add text the model reads, because a repository
already controls its `CLAUDE.md`; the plugin never executes project text.

Per key:

- `subagent_git`: the order is `seatbelt` < `strict`. The effective value is
  the strictest of the project's working-tree file, the project's file at
  `HEAD` (Decision 8), the user file, and the default. A project's
  `"seatbelt"` is valid and changes nothing; it cannot lower a user's
  `strict`.
- `advice_block` and `confirm_tools`: union. Adding a row or a tool tightens;
  no file can remove an entry another file added.
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
| list item of the wrong type, an unknown row id, an unknown tool | item dropped | item dropped |
| text over its limit, multi-line where one line is required, or containing control characters | key absent | (project-only keys are ignored here) |
| `telemetry` not a boolean | (cannot be set) | false |
| `t1_nudge` not a boolean | (cannot be set) | true |
| unknown key | ignored | ignored |
| key in the wrong file | ignored | ignored |
| `version` newer than known | known keys read | known keys read |

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
   show` when nx is on PATH, otherwise the stdlib command in Decision 5 with
   the plugin's absolute path filled in.
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
- every problem, as in Decision 4;
- retired files that still exist (`maintainer-profile`, a legacy marker
  beside `project.json`) with "no effect; safe to delete after <version>".

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
| 8.5.7 user, no files | Guard stays the seatbelt. Keeps, now without nexus ids: the search-scope row, the four brief questions, the dispatch sentence, the T1 nudge for built-in agents. Loses: telemetry (it stops unless they set `telemetry: true`; it only ever went to their own engine). Auto-approve is unchanged (ruling 4; the D2 prompt never shipped). The compaction count is measured against their default branch instead of `origin/develop`. |
| Sam | The flag file has no effect from this release. To keep today's behaviour outside nexus he writes `conexus.json` with `subagent_git: strict`, `telemetry: true` and his `remote_test_hosts`, on each machine where he wants them. Deleting `maintainer-profile` is his to do; `show` reports it as retired. |
| nexus checkout | `.nexus/project.json` sets `subagent_git: strict`, `preflight`, `ci_wait`, `credential_helper`, `integration_branch: develop` and the local-and-cloud brief question. `.nexus/dev-process.json` is deleted (it never shipped in a release). |
| Old plugin (8.5.7) with a new nexus checkout | 8.5.7 reads the Phase 1 marker `scripts/agent-worktree-preflight.sh`, which stays (it is nexus's preflight), so nexus keeps the full guard. A user repository's new `project.json` is ignored by the old plugin. |
| New plugin with an old checkout (an older nexus branch, a bisect) | No `project.json`, so either legacy marker reads as `subagent_git: strict`, for two client releases after the one that ships this. When `project.json` exists it alone decides and a legacy marker beside it is reported and ignored. |
| Old nx with new plugin, or the reverse | RDR-143's lockstep keeps the window short. During it the plugin's command hooks (the guard, the Stop guard, advice, credential) follow the settings, and an old nx-side hook follows its own release's rules, which for 8.5.7 means ungated texts. No state is written that either side misreads. |
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

- **Permissions** (`permissions.allow`, `ask`, `deny` in user or project
  `settings.json`) stay Claude Code's. The plugin's auto-approve cannot
  override a user's `ask` or `deny` through PreToolUse (Research 6); the
  PermissionRequest case is open and handled by Decision 9.
- **`CLAUDE.md` and `.claude/rules/`** carry a project's prose. nexus's own
  prose (the incident history, `develop` as the integration branch, the push
  script, the lab hosts, the CI board) belongs there, not in plugin text.
- **Plugin `userConfig`** is not used (Decision 1).
- **Project `.claude/settings.json`** is not used for plugin settings.

The two open questions, bounded:

- *Does a worktree-isolated subagent read the worktree's
  `.claude/settings.json`?* The docs do not say (Research 6). It does not
  matter here, because no plugin setting lives in `.claude/settings.json`;
  the plugin reads `project.json` from the payload's `cwd`. The Phase 0 spike
  records which `cwd` a worktree subagent's hook sees.
- *Can plugin `userConfig` be set per project?* Not as far as the docs go:
  the only documented scope is user or managed, and the negative is inferred
  from their silence, not stated or tested (Research 6). That is one of the reasons Decision 1 does
  not use it.

#### Decision 9: Security

The invariant of Decision 3 is enforced by construction (the reader keeps
only tightening values from a project file) and by tests (Decision 11).
Further measures:

- **The project file is read at `HEAD` as well as in the working tree** for
  `subagent_git`, and the stricter of the two applies. A subagent working in
  the shared checkout can edit or delete the working-tree file; without this,
  its next Bash call would run under the seatbelt and could commit. A commit
  is exactly what the strict guard denies, so the `HEAD` copy cannot be
  changed from inside the protected case. The cost is one `git show
  HEAD:.nexus/project.json` (two-second timeout), taken only when the
  working tree and user file give the seatbelt and the strict scan would
  have denied the command; a timeout reads strict. A maintainer's own edit
  that lowers the setting takes effect after it is committed, and `show`
  says so. Sam's decision S3.
- **Project text is prompt content and nothing else.** The plugin never
  executes `preflight`, `ci_wait` or any other project value; it shows it to
  the model, which a repository can already do through `CLAUDE.md`. Length
  limits keep the text inside the SubagentStart byte budgets.
- **No setting is read from the environment.** The only environment value
  the reader uses is `CLAUDE_PROJECT_DIR`, as a fallback for the `cwd`.

Loosenings that remain, and why each is acceptable:

- **Auto-approve for every user** (ruling 4). The seven tools
  (`memory_delete`, `plan_delete`, `answer_plan_delete`,
  `memory_consolidate`, `nx_tidy`, `tuple_in`, `mailbox_send`) are
  auto-approved for everyone. This is a product decision, not a setting, so
  no repository controls it. A user gets a prompt back two ways: a
  `permissions.ask` rule in their Claude Code settings, which a PreToolUse
  allow cannot bypass (Research 6), and `confirm_tools`, which makes the
  plugin's hook give no answer for those tools on both PreToolUse and
  PermissionRequest, so Claude Code's own prompt appears whatever the
  unsettled PermissionRequest precedence turns out to be. `confirm_tools` is
  a tightening, so a project may set it too. Sam's decision S4.
- **Remote test hosts** move from the environment variable
  `NX_REMOTE_TEST_HOSTS` (removed earlier, D11) to the user file. A project
  cannot set them.
- **`NX_STOP_WAKE_GUARD=0`** stays an environment kill switch, which a
  project's `env` may be able to set. It turns off an advisory continuation,
  not a deny, prompt or data flow, and scripted `claude -p` runs need it. It
  is outside the invariant by the invariant's own definition.
- **`# routing-allow:`** escapes are unchanged: an agent may use one only on
  the user's explicit instruction.

Out of the threat model: an agent that deliberately edits the user's own
`conexus.json` to escape the guard. The guard prevents accidents by
cooperative agents, and we assume (not checked in the docs or tested) that
writing outside the project directory raises Claude Code's own prompt in the
default permission mode. This is recorded as a risk.

#### Decision 10: The subagent git guard fix (nexus-5y47c) and the merge order

The guard fix adds a segment-level admission layer (`routing/_git_admit.py`)
inside the full guard and rewrites its deny text; `body()` still chooses
full guard or seatbelt with `_maintainer(payload)`. On the new reader that
one line becomes `if _guard_mode(payload) == STRICT`, and the import block
swaps `maintainer` for `guard_mode`, with the fallback answering strict.

Merge order: **nexus-5y47c lands on `develop` first**, then the Phase 3
branch rebases onto it. The fix is reviewed and confirmed, it does not touch
the settings model, and it removes false denials of the kind Research 8 shows, which users
would otherwise meet the first time they opt into `strict`. Rebasing the
rework onto it is a conflict in two places (the gate line and the deny
text), both small. The reverse order would ship strict mode to users with the
over-matching guard, and would make the fix's golden replay rebase onto a
moving settings model.

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
  forbidden lexicon as process vocabulary.
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
  `CLAUDE_PROJECT_DIR`; a JSON Schema file or `docs/settings.md` table that
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
| 3 | Strict if either says strict; `advice_block` union | Kept, extended to the `HEAD` copy (D3, D9). |
| 4 | Strings stripped, commands not validated | Kept that commands are not interpreted; added type, one-line, control-character and length limits (D2). |
| 5 | Walk from `cwd`, then `CLAUDE_PROJECT_DIR`, to the first `.git` | Kept (D1). |
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
| 20 | Seven tools auto-approved; an `ask` rule claimed as the users' route, unverified | PreToolUse half verified in the docs; PermissionRequest half unsettled, so `confirm_tools` added (D9, S4). |
| 21 | No removal test for the window | Version-keyed test (D7). |
| 22 | Strict deny text says "orchestrator" and "STRUCTURE-AGNOSTIC" | Term defined once; shouting removed (D10). |

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Settings reader | `conexus/hooks/scripts/_audience.py` and `src/nexus/hooks/_audience.py` | Extend the rework's readers: add `SETTINGS`, problems, `version`, `$` keys, the file checks, the `HEAD` read, `confirm_tools`, `integration_branch`, `remote_test_hosts`, the `show` entry point. |
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

### Briefly Rejected

- **nx `config.yml` for user settings**: YAML, credentials, `NEXUS_CONFIG_DIR` (Decision 1).
- **Project `.claude/settings.json` hooks for nexus's behaviour**: cannot carry plugin-internal behaviour, dropped in cloud, reverses nexus-3h0u6 (Decision 1).
- **A generic `notes: {rule id: text}` map instead of per-slot text keys**: lets a project splice text into any deny; per-slot keys are validated, documented and budgeted.
- **Silent fallbacks** (the rework): Gap 4.
- **A writer command now**: Decision 5.

## Trade-offs

### Consequences

- Every user gets the plugin's process by default, with no nexus words in it.
- Sam's experience outside nexus depends on a file he writes on each
  machine, the same as anyone's.
- Two public contracts are created (two files, one command) that must be
  kept compatible indefinitely.
- A project maintainer's edit that lowers `subagent_git` waits for a commit.
- Telemetry stops for users who never asked for it; `nx telemetry` reports
  have nothing to read on those installs.

### Risks and Mitigations

- **Risk**: The PermissionRequest hook's allow answers a user's `ask` rule,
  so the documented route to prompts does not work.
  **Mitigation**: `confirm_tools` works regardless; Phase 0 spike decides what
  the docs promise.
- **Risk**: A malformed project file in a stranger's repository gives a user
  the full guard unexpectedly.
  **Mitigation**: The session-start message and the deny text both name the
  file and the reason; a linked worktree is still exempt.
- **Risk**: A subagent edits the user's `conexus.json`.
  **Mitigation**: Out of the threat model (Decision 9); Claude Code prompts
  for writes outside the project in the default mode. Revisit if an incident
  shows it.
- **Risk**: The settings report at session start becomes noise if a problem
  persists.
  **Mitigation**: Once per session start, only when a problem exists, and
  only from one hook.
- **Risk**: The compatibility window is forgotten.
  **Mitigation**: The version-keyed test fails the build when it ends.
- **Risk**: The rework, the guard fix and Phase 2's un-gating ship in one
  client release and interact.
  **Mitigation**: Fixed merge order (Decision 10), one review gate on the
  rebased branch (ruling 8), the behaviour leg in three postures.

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
- [ ] nexus-5y47c on `develop` (Decision 10).
- [ ] Phase 0 spikes recorded.

### Minimum Viable Validation

In a scratch repository on a P0 machine (plugin only): with no files a
dispatched subagent can `git commit` and is denied `git push --force`; after
writing `.nexus/project.json` with `{"version": 1, "subagent_git": "strict"}`
the same subagent is denied `git commit` in the primary and allowed it in a
linked worktree; after misspelling the key, the next session start shows a
user-visible message naming the file and suggesting `subagent_git`; and
`python3 <plugin root>/hooks/scripts/_audience.py show` reports each state.
The same run on a P2 machine with `nx settings show` gives identical output.

### Phase 0: Measure (no release)

1. Spike: a user `permissions.ask` rule for `mcp__plugin_conexus_nexus__memory_delete`,
   with the plugin's PreToolUse and PermissionRequest auto-approve active, in
   default and in `acceptEdits` mode. Record whether a prompt appears.
2. Spike: a worktree-isolated subagent's Bash PreToolUse payload: record `cwd`
   and `CLAUDE_PROJECT_DIR`.
3. Spike: a SessionStart command hook's `systemMessage` in the CLI and in
   Desktop: record whether the user sees it.
4. Spike: whether a project `.claude/settings.json` `env` value reaches a
   plugin command hook (the plan's unrecorded Phase 0 item).

**Ship when**: all four results are recorded in T2 with the commands used,
and `docs/settings.md` drafts match them.

### Phase 1: The reader, the reporting and the inspection command

On the Phase 3 branch, rebased onto nexus-5y47c.

1. Tests first (Decision 11): the corpus rows for the failure table, the
   precedence matrix, the `HEAD` union, the legacy window, and the lint rules.
2. `SETTINGS`, the readers, the problem list, the file checks, the `HEAD`
   read, `confirm_tools`, `integration_branch`, `remote_test_hosts` and the
   `show` entry point in `_audience.py`; byte copy to `src/nexus/hooks/`.
3. The guard gate swap and deny text (Decision 10); auto-approve honours
   `confirm_tools`; compaction uses `integration_branch`; the Stop guard
   change and identifier renames (Decision 12); the preflight wording.
4. The SessionStart problem notice (plugin-side, stdlib) and the deny-text
   clause for an unreadable file.
5. `nx settings show` and `check`; the `nx doctor` row.
6. The JSON Schema files.

**Ship when**: the reader and lint tests pass on both copies; the behaviour
leg is green in plain, project-strict and user-strict, P0 and P2; the lint
finds no `maintainer`, `account_flag` or `maintainer-profile` symbol; the
Minimum Viable Validation passes on the laptop and on native Windows; the
added hook time is measured and under 5 ms per call.

### Phase 2: Un-gate and move nexus's words

1. Un-gate Phase 2's texts on the same branch with ids removed (ruling 3);
   remove the phase-gate incident clause, the tripwire and the "no maintainer
   flag" SessionStart line.
2. nexus's `.nexus/project.json` as in Decision 7; delete
   `.nexus/dev-process.json`.
3. The prose moves to nexus's `CLAUDE.md`, `.claude/rules/` and project
   skills (Sam's edits for `CLAUDE.md`).

**Ship when**: the behaviour leg's allowlist is empty; a nexus session shows
the full guard, the preflight row, the local-and-cloud question and the CI
wait command, and a plain repository shows none of them; one review gate
(code-review-expert and substantive-critic) passes on the rebased branch.

### Phase 3: Documentation and records

1. `docs/settings.md`, the README section, `docs/configuration.md`,
   `docs/cli-reference.md`, `docs/agents/plugin-audiences.md`, `promotion.md`'s
   audience step, `tests/AGENTS.md`, the AGENTS.md hot-rule line (ruling 9).
2. A changelog entry in `changelog/unreleased/` that states the user-visible
   changes in Decision 7's table.

**Ship when**: the docs tables pass the lint against `SETTINGS`; the release
notes name the telemetry change and the two new files.

### Phase 4: End of the compatibility window

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
- **Scenario**: `confirm_tools: ["memory_delete"]`. **Verify**: the
  auto-approve hook gives no decision for that tool on either event.
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
together. Done means the Phase 1 to 3 ship-when lines hold.

### Performance Expectations

The reader adds a directory walk, at most two small file reads, and on one
rare path a git call. Phase 1 measures it; no target beyond the 5 ms bound
is claimed.

## Finalization Gate

### Contradiction Check

The rework failed a broken user file to the seatbelt; Decision 4 fails it
strict. That is a deliberate change, put to Sam as S2. No other contradiction
found between the research, the rulings and the design.

### Assumption Verification

Four assumptions are unverified and each has a Phase 0 spike or a Phase 1
measurement. None is load-bearing for the direction: `confirm_tools` removes
the dependency on the PermissionRequest question, and the `cwd` question
changes only which copy of the same tracked file is read.

#### API Verification

| API Call | Library | Verification |
| --- | --- | --- |
| PreToolUse `permissionDecision: allow` vs `permissions.ask` | Claude Code | Docs Only (stated) |
| PermissionRequest `allow` vs `permissions.ask` | Claude Code | Spike (Phase 0) |
| Hook payload `cwd` in a worktree subagent | Claude Code | Spike (Phase 0) |
| `systemMessage` at SessionStart | Claude Code | Spike (Phase 0), CLI and Desktop |

### Scope Verification

The Minimum Viable Validation is Phase 1's ship-when, on P0 and on Windows.

### Cross-Cutting Concerns

- **Versioning**: the `version` field and the key-growth rules (Decision 2).
- **Build tool compatibility**: N/A.
- **Licensing**: N/A.
- **Deployment model**: plugin and client release together (ruling 8).
- **IDE compatibility**: JSON Schema files let editors validate both files.
- **Incremental adoption**: no file means today's 8.5.7 behaviour minus nexus words.
- **Secret/credential lifecycle**: neither file holds secrets; the user file is kept apart from `config.yml` so hooks never parse credentials.
- **Memory management**: N/A (files capped at 64 KiB).

### Proportionality

The document is long because it fixes a public contract. The 22-row table
could move to a companion note once the gate has read it.

## Out of Scope

- A writer command (`nx settings set`, `init`).
- A plugin `userConfig` front end to the user file.
- Per-directory settings inside one repository (monorepo subprojects).
- A per-user, per-project local file.
- An organisation or managed-settings layer.
- nx's own behaviour settings, which stay in `config.yml` and `.nexus.yml`.
- Configurable advice text, or new advice rows.
- Phase 4 of the portability plan (moving agent text into SubagentStart).
- Defending against an agent that deliberately edits the user file.

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
- **S4. Add `confirm_tools`, a setting that hands chosen tools back to Claude
  Code's prompt, settable by a user or a project?** Recommended: yes. It is
  the only route back to a prompt for the seven auto-approved tools that does
  not depend on an undocumented precedence (Decision 9).
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
