# Plugin audiences: what a hook, a rule or a line of injected text reaches, and what can change it

The conexus plugin ships one development process to every Claude Code user: worktree isolation, the subagent git guard, review gates, brief checks, the Stop wake guard, relay discipline. That process is the product, and it reaches every user. Nexus's own specifics (its branch names, its CI board, its lab hosts, its wrapper URL, bead and RDR ids, a person's name) are leaks, and a leak is deleted or turned into a setting with a neutral default. **Nothing in the plugin depends on who the user is.** Plan of record: `~/.local/state/nexus/plans/plugin-portability-plan.md`; the audience model was reworked by Sam's ruling in T2 `nexus/sam-ruling-audience-model-settings-not-identity-2026-11-25`, which amends the plan's D2, D6 and D10.

## The audiences

| Audience | Fires when | Reader |
|---|---|---|
| EVERY-USER | always (the product), sometimes with a data gate on the user's own content (`.beads/`, an RDR directory, a board entry found) | none, or the data gate |
| SETTING:`<key>` | the project's tracked `.nexus/project.json` or the user's `~/.config/nexus/conexus.json` sets `<key>` | the reader of that key (below) |
| REMOVE | never | n/a |

## The settings

Two files, read by the plugin's hooks: a project's tracked `.nexus/project.json` (tighten-only: a project may make the plugin stricter or add its own text, and never loosen a rule or turn telemetry on, because whoever controls a repository writes the file) and the user's `<account home>/.config/nexus/conexus.json`. The user-facing reference is [`docs/settings.md`](../settings.md): every key, how two values combine, the failure table and the advice row ids. Its key table is generated from `SETTINGS` in `_audience.py` (`uv run python -m tests._settings_doc` prints it) and `tests/test_settings_docs_lint.py` fails when the two disagree, when the page is absent, or when its advice row ids differ from `ADVICE_ROW_IDS`. The design of record is RDR-259.

The two older markers, `.nexus/dev-process.json` (the Phase 2 file) and `scripts/agent-worktree-preflight.sh` (the Phase 1 file), are gone: a checkout with no `.nexus/project.json` reads plain (the seatbelt) whatever those files say. A project that relied on `.nexus/dev-process.json` for the strict guard writes `.nexus/project.json` with `"subagent_git": "strict"`.

The default for every key is the same for everyone: the seatbelt, neutral text, no blocked advice row, no telemetry, the T1 nudge on. The seven destructive and other-party tools (`memory_delete`, `answer_plan_delete` and its old name, `memory_consolidate`, `nx_tidy`, `tuple_in`, `mailbox_send`) are auto-approved for every user; a user who wants a prompt adds an `ask` rule in their own Claude Code settings, and the plugin's approver answers on PreToolUse only, so Claude Code's own rule decides. (Depends on the PermissionRequest registration's removal, nexus-sby49.14.)

## The helper

`_audience.py` (the settings reader) exists twice, byte for byte: `conexus/hooks/scripts/_audience.py` (the plugin's command hooks, stdlib only) and `src/nexus/hooks/_audience.py` (`nx-hook` and the MCP server). `tests/hooks/test_audience_parity.py` checks the bytes and drives both copies over `tests/fixtures/audience_corpus.json`. A second test, marked `lint`, drives the helper at the newest release tag that carries one (an older plugin paired with this checkout, which still reads the two retired markers: a corpus row where it answers differently on purpose says `prev_release: false`) over the same corpus. It needs tags, which only the `test-lint` job has (it checks out at depth 0; develop's `test` shards and the Windows job are depth 1 with none), so elsewhere it skips, and it fails when `CI` or `NX_REQUIRE_PREV_RELEASE_COPY=1` is set and no tag is readable. Change one copy and copy it over the other.

Invariants, each of which is a test:

1. **Tighten-only for a project.** A project's file adds rules or text; it never loosens anything and never turns telemetry on. Every default is the EVERY-USER baseline.
2. **No environment variable moves the settings.** The account home comes from the OS account database (`pwd`, or the profile API on Windows), never `HOME`, `USERPROFILE` or `NEXUS_CONFIG_DIR`. The one test seam is the in-process attribute `_audience._ACCOUNT_HOME_OVERRIDE`; it is never read from the environment. The two telemetry senders are pinned the same way: they resolve the engine from the account's config directory only and start their child with an environment built by `_audience.telemetry_child_env()`.
3. **The cwd is the payload's `cwd`, then `CLAUDE_PROJECT_DIR`,** never the process cwd (uv starts the hook scripts in the plugin root).
4. **Failure goes in the safe direction.** `guard_mode()` answers strict on any error, so a broken helper restores the full guard; a project file that cannot be read gives the strict reading, and so does a user file that cannot be read (a user writes `conexus.json` to change something, and the likeliest change is `strict`). The readers that loosen or add nothing answer their default: `telemetry_enabled()` False, `advice_blocked()` and `remote_test_hosts()` the empty set, `project_text()` None, `project_brief_questions()` the empty list; `t1_nudge_enabled()` answers True (it adds a reminder). A missing passwd entry is "no user settings", not an error.
5. **Nothing asks who the user is.** There is no maintainer flag, no repo-is-nexus test and no person's name in the plugin's behaviour; a test asserts the helper has none.

## Declaring a rule: `conexus/hooks/audience.yaml`

Every `hooks.json` registration, every module that carries text or reads a setting, every injected constant and every rule that is not a constant has an entry, with `audience` (`EVERY-USER`, `SETTING:<key>` or `REMOVE`), `gate` (`none`, `setting` or `data`), `evidence` (the ledger row, the incident or the decision of record), `target_state` and `phase`. A registration is keyed on event plus argv: 23 registrations, 22 distinct argv, because auto-approve is registered for PreToolUse and PermissionRequest with one argv.

`tests/test_plugin_audience_lint.py` (checker: `tests/_audience_manifest.py`) fails on:

- a registration with no entry, or an entry for one that is gone;
- an unknown value (an old `SAM-WIDE` or `NEXUS-REPO` is unknown), or an audience that disagrees with its gate;
- a declared constant that does not exist, and a module-level text value of 40 characters or more (below) that nobody classified as injected or not injected;
- a `SETTING:<key>` entry (`live: true`) in a module that never calls the reader of that key;
- an audience import that is not inside a `try`, or whose fallback widens a permission (T9);
- a table in `docs/settings.md` that disagrees with `SETTINGS`: that rule is its own file, `tests/test_settings_docs_lint.py` (see [The settings](#the-settings));
- a reader called at import time, or outside a wrapped body. The accepted wrappers are `_lib.run_hook`, `_io.never_fail` (directly, through a verb the dispatcher wraps, or through an `mcp` hook tool), a standalone `main()` with a `try`/`except`, and a local `try`/`except` around the call.

The census reads module-level values: a string, a `+` or f-string concatenation, a list, tuple, set or dict that holds a long string, the arguments and the receiver of a call (a `Row(...)` table, `"...".format(x)`), a conditional expression, and a tuple-unpack assignment, leaving out `re.compile` and `frozenset`. **What it cannot see:** text written or joined inside a function (function-local text of any length), text read from a file, and injected text shorter than 40 characters that nobody declared. Those are held by review and by the behaviour leg, which runs the hooks and reads what they print.

A row whose end state is not wired yet is `live: false` and names the phase that wires it. The behaviour leg's allowlist may name only those rows, so an item still to come cannot hide a live leak. Phase 3 wired every row: none is `live: false` now.

## The behaviour leg

`tests/e2e/lib/plugin_behaviour.py`, run by `tests/e2e/plugin-out-of-repo-gate.sh` (static pass, `--all`, `--installed X.Y.Z`, and the release battery's `plugins` leg), drives each registration's real argv under payloads of the shape Claude Code sends, in a **plain** scratch repository (with a dirty-tree and a beads variant), a **project-strict** one (a repository whose tracked `.nexus/project.json` sets every project key) and a **user-strict** one (an account home whose `conexus.json` sets the user keys), in two postures: P0 (no `nx` on PATH, the nx-launcher and shim entries must exit 0 and print nothing) and P2 (the checkout's `nx`, or with `--nx installed` the installed tool's). It asserts that no fixture's output carries a bead id, RDR id, `bd` outside a beads project, owner name, peer name, lab host, `develop/main` or `Hellblazer/nexus`; that the only denies are the ones the fixture expects (the seatbelt's list, the credential read, a blocked advice row, a Stop promise); that each live `SETTING:<key>` constant appears in the output of each fixture that sets the key and in none that does not; and, per scenario, the words the output must carry or must not (the credential helper, the Stop guard's CI command, the Git, Search scope and Preflight rows, the brief questions, the compaction text's board and beads words), that all seven destructive tools are auto-approved everywhere, and that a Stop hook leaves nothing in a config directory that did not exist. `plugin-behaviour-allowlist.txt` is empty and the pins in `tests/test_plugin_behaviour_leg.py` keep it so.

## Adding a rule or a line of text

1. Decide what it is. Process every user should have is EVERY-USER, with nothing nexus-specific in its text. A behaviour that needs a choice (a stricter mode, a project's command, extra text, data leaving the machine) is a setting: pick the key it belongs to, give it a default that suits a stranger, and keep nexus's own value in nexus's `.nexus/project.json`. Never key a hook, a rule or a line of text on who the user is. `conexus/skills/writing-nx-skills/promotion.md` has the same step for a memory promotion.
2. A new key is one entry in `SETTINGS` (in both byte copies of `_audience.py`), a limit and a failure value, and a row in `docs/settings.md`: print the table with `uv run python -m tests._settings_doc` and paste it, and describe the key under the table. A key never changes meaning in place, and an advice row id is never renamed once a file can name it.
   Read the setting through `_audience`'s reader for its key, inside the hook's existing wrapper, with the guarded import and its fallback (copy one of the existing modules).
3. Add its entry to `audience.yaml` and, if it is text, a case the behaviour leg reaches.
4. Write the test in the fixture convention: **plain** by default (a bare `git init` and an account with no settings file), asserting a setting's behaviour only in a **project-strict** or **user-strict** fixture (`tests/AGENTS.md`).
5. If it changes a file under the plugin's behavioural surface, declare it in `conexus/PENDING_RELEASE.md`.
