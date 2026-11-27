# conexus plugin settings

The conexus plugin runs one development process for every user: worktree isolation for subagents, a git guard, review gates, brief checks, advice before destructive commands. The defaults suit most users. Two small JSON files change them: a project's own file, and yours. Nothing the plugin does depends on who you are.

`nx settings show` prints what applies in the directory you are in, and where each value came from. `nx settings check` prints only the problems and exits 1 when a file has one, so a project can run it in CI.

## What the plugin does with no settings

- **Subagent git guard.** A short deny list: force pushes, `branch -D`, reflog and gc pruning, writes to hook paths and global git config, discarding uncommitted work. A subagent may commit, add, push, rebase and merge. `subagent_git: "strict"` runs the full guard instead.
- **Advice rows warn.** A pattern kill, a bulk branch delete and the other rows below advise before the command runs. `advice_block` turns a row into a deny.
- **Nothing is sent to your engine.** `telemetry` is off. The routing events the guards write and the agent-dispatch record leave the machine only when you turn it on.
- **Subagents get the generic brief.** The search-scope row, the four implementer brief questions and the T1 reminder when a named agent stops with nothing written. `t1_nudge: false` turns the reminder off.
- **The nexus tools are auto-approved**, destructive ones included (`memory_delete`, `plan_delete`, `answer_plan_delete`, `memory_consolidate`, `nx_tidy`, `tuple_in`, `mailbox_send`). To be asked first, see [Prompts](#prompts).
- **The compaction checkpoint** counts work not yet landed against the repository's default branch.

## The two files

| File | Where | Who writes it | What it can do |
| --- | --- | --- | --- |
| Project | `.nexus/project.json` at the root of a checkout (the first directory at or above the working directory that holds a `.git` entry) | the repository, tracked in git | tighten the plugin or add text to what it prints; never loosen a rule, never turn telemetry on |
| User | `.config/nexus/conexus.json` under your account home (`%USERPROFILE%\.config\nexus\conexus.json` on Windows) | you | change your own defaults, including telemetry |

A nested repository or a submodule is its own project. A path outside any repository has no project file.

The account home comes from the operating system's account database, never from `HOME`, `USERPROFILE`, `XDG_CONFIG_HOME` or `NEXUS_CONFIG_DIR`. A repository's own Claude Code settings can set environment variables that reach the plugin's hooks, so no variable may decide which file is read. This is also why `nx settings` reads your file from the account home even when `NEXUS_CONFIG_DIR` is set: it is the file the hooks read. The only environment variable the reader looks at is `CLAUDE_PROJECT_DIR`, to find the session's own checkout.

Both files are JSON. A key that starts with `$` (`"$comment"`) is ignored, so a file can carry a note. A UTF-8 byte-order mark at the start is accepted. A file over 64 KiB is not read.

Example project file:

```json
{
  "version": 1,
  "subagent_git": "strict",
  "advice_block": ["whole_disk_scan"],
  "preflight": "scripts/preflight.sh",
  "brief_questions": ["Does this change run in both local and cloud mode?"]
}
```

Example user file:

```json
{
  "version": 1,
  "subagent_git": "strict",
  "telemetry": true
}
```

## The keys

| Key | Type and limits | Default | Project | User | Combination | On a bad value |
| --- | --- | --- | --- | --- | --- | --- |
| `version` | integer, 1 or more | `1` | yes | yes | per file | `1` |
| `subagent_git` | `"strict"` or `"seatbelt"` | `"seatbelt"` | yes | yes | strictest wins | `"strict"` |
| `advice_block` | list of advice row ids | `[]` | yes | yes | union | `[]` |
| `brief_questions` | list of strings, each one line of at most 160 bytes of UTF-8; at most 3 added in total across both files | `[]` | yes | yes | append | `[]` |
| `preflight` | one line, at most 160 bytes of UTF-8 | none | yes | no | project only | absent |
| `ci_wait` | one line, at most 200 bytes of UTF-8; may contain the literal `<sha>` | none | yes | no | project only | absent |
| `credential_helper` | one line, at most 600 bytes of UTF-8 | none | yes | no | project only | absent |
| `integration_branch` | a branch name (no whitespace, no `..`, no leading `-`), at most 100 bytes | none | yes | no | project only | absent |
| `telemetry` | boolean | `false` | no | yes | user only | `false` |
| `t1_nudge` | boolean | `true` | no | yes | user only | `true` |

Every limit is in bytes of the UTF-8 encoding. The table is generated from `SETTINGS` in `conexus/hooks/scripts/_audience.py`, and `tests/test_settings_docs_lint.py` fails when it and the table in the code disagree. The last column is the value a key falls to when its value is bad (see [When a file is wrong](#when-a-file-is-wrong)).

- `version`: 1, and expected to stay 1. A key's meaning is never changed in place; a new behaviour gets a new key. A reader that meets a newer `version` reads the keys it knows and says the file was written for a newer conexus.
- `subagent_git`: `"strict"` runs the full subagent git guard, which denies index-writing and working-tree-destroying git verbs in the shared checkout, and adds the Git row to the dispatch instructions. `"seatbelt"` runs the short deny list. A subagent in its own linked worktree is exempt from the full guard. An ssh command to a host named in `.config/nexus/remote-test-hosts` (one host alias per line, or comma-separated; `#` starts a comment) is exempt too; that file is not a key and a project cannot set it.
- `advice_block`: advice row ids that deny instead of warn. The deny keeps the `# routing-allow: <reason>` escape, which an agent may use only when you told it to.
- `brief_questions`: questions added after the four generic implementer brief questions. At most 3 are added in total across both files; extra ones are reported and dropped. Exact duplicates are dropped.
- `preflight`: the dispatch instructions tell a worktree agent to run this command first and to stop and report when it exits non-zero. The plugin shows the command and never runs it.
- `ci_wait`: the command the Stop guard names when a turn promises to wait on CI. It is printed as written. A literal `<sha>` is left for the model to fill with the commit it pushed; nothing substitutes it.
- `credential_helper`: text the credential guard adds after its fixed refusal, naming the project's safe way to use a credential.
- `integration_branch`: the branch the compaction checkpoint measures "not yet landed" against. Without it the order is `origin/HEAD`, then `origin/main`, then `origin/master`. A name `origin` does not have falls back to that order, and `show` and `check` report it.
- `telemetry`: sends the routing events and the agent-dispatch record to your own engine. One key covers both: they answer the same question, whether your hooks may record what your agents did.
- `t1_nudge`: the reminder, when a named agent stops with no T1 write, to write its findings. Off means off for every agent type. The dispatch record is governed by `telemetry` alone.

### Advice row ids

The ids `advice_block` accepts. A row id is a public contract once a file names it, so a row is not renamed.

| Id | Warns about |
| --- | --- |
| `pattern_kill` | a kill by name pattern (`pkill -f`, `killall`) |
| `branch_bulk_delete` | a bulk or output-hiding `git branch -D` |
| `stash_with_worktrees` | `git stash` when the repository has more than one worktree |
| `bg_pipe_masks_exit` | a background job piped into `tail`, `head` or `grep`, which hides its exit status |
| `whole_disk_scan` | a recursive scan of the whole disk |
| `shared_glob_delete` | a glob delete under a shared temp root |
| `hook_verb_by_hand` | a hook verb run by hand without a scratch `HOME` |
| `running_script_edit` | an edit of a shell script a live shell is running |

## Who sees a text a file adds

The plugin prints text from a settings file under a label that names its source, never in its own voice, so the model weighs it as project or user content, as it would a `CLAUDE.md`:

| Key and source | Printed as |
| --- | --- |
| `preflight`, project | This project's `.nexus/project.json` asks: run `<preflight>` first. If it exits non-zero, stop and report its output. |
| `brief_questions`, project | This project's `.nexus/project.json` adds these questions: |
| `brief_questions`, user | Your `conexus.json` adds these questions: |
| `ci_wait`, project | This project's `.nexus/project.json` names its CI wait command: `<ci_wait>`. Run it as a background command; its exit is the wake. |
| `credential_helper`, project | inside the credential guard's deny, after its fixed refusal: This project's `.nexus/project.json` adds (project text, not this guard's): `<credential_helper>` |

Before any of it is printed it is cleaned: control characters removed, one line where the key is one line, cut to its limit. The plugin never executes a project text; it only shows it. `nx settings show` prints every such text beside its label, so you can read what a repository feeds to your agents.

## Precedence

| Key | How two values combine |
| --- | --- |
| `subagent_git` | The strictest of: the project file in the session's checkout (working tree, and its copy at `HEAD`), the project file in the checkout the command runs in when that is a different one, your user file, and the default. `"seatbelt"` in a project changes nothing and cannot lower your `"strict"`. |
| `advice_block` | The union over the same project copies and your user file. A file can add a row and none can remove one another added. |
| `brief_questions` | Generic questions first, then the project's, then yours. |
| `preflight`, `ci_wait`, `credential_helper`, `integration_branch` | Project only. In the user file they are reported and ignored. |
| `telemetry`, `t1_nudge` | User only. In a project file they are reported and ignored: a repository cannot turn data leaving your machine on. |

The project file is also read at `HEAD` for `subagent_git` and `advice_block` only. A subagent in the shared checkout could delete or edit the working-tree file, and the next command would then run under the short deny list; the committed copy is what the strict guard protects, because committing is what it denies. A maintainer's own edit that loosens either key takes effect once it is committed, and `show` says so. The text keys are read from the working-tree file. The `HEAD` read runs git only when the guard or the advice hook is about to decide something the committed copy could change, and a repository with no project file anywhere runs no git for settings.

A project cannot be overridden down by a user. If a project's `strict` is in your way, work in a linked worktree, which the full guard already exempts.

## When a file is wrong

A key that guards something fails toward the protection; every other key falls to its default. The same rule holds for both files.

| Condition | Project file | User file |
| --- | --- | --- |
| absent | no project settings | defaults |
| no account home (no passwd entry, as in a bare container) | not affected | not read; defaults; `show` and `check` and the first-prompt notice say so |
| a symbolic link, or any file that is not a regular file | `subagent_git` strict; every other key absent | read normally (dotfile managers link this file) |
| over 64 KiB, unreadable, not UTF-8, not JSON, or the top level is not an object | `subagent_git` strict; every other key absent | `subagent_git` strict; every other key at its default (`telemetry` false) |
| starts with a UTF-8 byte-order mark | read normally | read normally |
| `subagent_git` is any other value or type | strict | strict |
| a list key is not a list | key empty | key empty |
| a list item of the wrong type, or an unknown advice row id | item dropped | item dropped |
| text over its limit, on more than one line, or with control characters | key absent | project-only keys are ignored here |
| `telemetry` or `t1_nudge` is not a boolean | cannot be set | `telemetry` false, `t1_nudge` true |
| unknown key (the report suggests the closest name) | ignored | ignored |
| a key in the wrong file | ignored | ignored |
| `version` newer than known | known keys read | known keys read |
| `version` not an integer, or below 1 | read as 1 | read as 1 |

Every row except "absent" and the byte-order mark is a reported problem. A broken user file fails to `strict` because you write `conexus.json` to change something, and the likeliest change is `strict`: a broken file should not quietly take that away.

Where you hear about a problem:

1. On your first prompt of a session, once: one message naming each file, the problem and the value used. Text from the file in it is cleaned, quoted and cut to 60 bytes. The model gets only a count and the pointer to `nx settings show`, never text from the file.
2. When the full guard denies only because a file could not be read, the deny says so and names the file and the reason.
3. In `hook.log`, once per session per problem.
4. On demand: `nx settings show` and `nx settings check`.

`nx settings check` leaves problems in the machine out of its exit code (no account home, a remote branch missing in a shallow clone, telemetry on with no engine configured); it prints them, and `show` carries them too.

## Inspecting

```
nx settings show [--cwd PATH] [--json]
nx settings check [--cwd PATH]
```

`show` prints both file paths and whether each exists, whether the project file at `HEAD` differs from the working tree, every key's effective value and its source (`default`, `user`, `project`, `project (HEAD)`, `project (cwd checkout)`), every text the hooks print under its label, every problem, files an earlier release read that still exist (`maintainer-profile`), and the plugin's and the CLI's versions, with a warning when they differ. The nx-side hooks (auto-approve, SubagentStart, SubagentStop) follow the installed CLI, so during a version skew `show` can name a setting some hooks do not yet implement.

With no `nx` on the machine, the plugin carries the same report:

```
python3 <plugin root>/hooks/scripts/_audience.py show [--cwd PATH] [--json]
```

There is no command that writes either file. Edit them by hand. [`nx settings`](cli-reference.md#nx-settings) has the flags.

## Prompts

Prompting before a tool runs belongs to Claude Code, not to this plugin. Put an `ask` rule in your own `~/.claude/settings.json`, or a project's tracked `.claude/settings.json`, for any tool, in the grammar Claude Code documents (`permissions.ask: ["mcp__plugin_conexus_nexus__memory_delete"]`). The plugin's auto-approve hook answers only on the PreToolUse event, so a rule you write still prompts, and a `deny` rule still denies.

## What is not a setting

Remote test hosts for the full guard live in `.config/nexus/remote-test-hosts`. nx's own configuration (`config.yml`, `.nexus.yml`, credentials) is in [configuration.md](configuration.md); the plugin settings are separate and never hold a credential. The advice and guard rows are described in the plugin's [README](../conexus/README.md#hooks).
