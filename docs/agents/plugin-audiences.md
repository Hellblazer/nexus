# Plugin audiences: who a hook, a rule or a line of injected text is for

The conexus plugin ships to every Claude Code user, and it also carries the rules nexus's own multi-agent development runs on. This page is the model that keeps the two apart. Plan of record: `~/.local/state/nexus/plans/plugin-portability-plan.md` (owner rulings D1-D13 in T2 `nexus/sam-ruling-plugin-portability-plan-2026-11-25`).

## The audiences

| Audience | Fires when | Predicate |
|---|---|---|
| EVERY-USER | always (the product), sometimes with a data gate on the user's own content (`.beads/`, an RDR directory, a board entry) | none, or the data gate |
| NEXUS-REPO | the payload's checkout carries a tracked nexus marker, whoever runs it | `nexus_repo(payload)` |
| SAM-WIDE | the OS account has the maintainer flag, or the checkout carries the marker | `maintainer(payload)` |
| REMOVE | never | n/a |

The markers are `scripts/agent-worktree-preflight.sh` (kept accepted for two client releases after the second shipped) and `.nexus/dev-process.json`. The flag is `<account home>/.config/nexus/maintainer-profile`.

## The helper

`_audience.py` exists twice, byte for byte: `conexus/hooks/scripts/_audience.py` (the plugin's command hooks, stdlib only) and `src/nexus/hooks/_audience.py` (`nx-hook` and the MCP server). `tests/hooks/test_audience_parity.py` checks the bytes and drives both copies over `tests/fixtures/audience_corpus.json`. A second test, marked `lint`, drives the helper at the newest release tag that carries one (an older plugin paired with this checkout) over the same corpus. It needs tags, which only the `test-lint` job has (it checks out at depth 0; develop's `test` shards and the Windows job are depth 1 with none), so elsewhere it skips, and it fails when `CI` or `NX_REQUIRE_PREV_RELEASE_COPY=1` is set and no tag is readable. Change one copy and copy it over the other.

Invariants, each of which is a test:

1. **Tightening only.** A signal adds rules or text. With every signal absent a session is the EVERY-USER baseline. The repository marker can select a stricter mode and never a looser one.
2. **Nothing a repository controls loosens anything.** The one loosening, D2's wider auto-approve list, keys on the account flag alone.
3. **No environment variable removes a rule.** The account home comes from the OS account database (`pwd`, or the profile API on Windows), never `HOME`, `USERPROFILE` or `NEXUS_CONFIG_DIR`. The one test seam is the in-process attribute `_audience._ACCOUNT_HOME_OVERRIDE`.
4. **The cwd is the payload's `cwd`, then `CLAUDE_PROJECT_DIR`,** never the process cwd (uv starts the hook scripts in the plugin root).
5. **Failure goes in the safe direction.** `maintainer()` and `nexus_repo()` tighten, so any error answers True and a broken helper restores today's rule. `account_flag()` and `remote_test_hosts()` loosen, so any error answers False and the empty set. A missing passwd entry is "no flag", not an error.

## Declaring a rule: `conexus/hooks/audience.yaml`

Every `hooks.json` registration, every module that carries text or a predicate, every injected constant and every rule that is not a constant has an entry, with `audience`, `gate`, `evidence` (the ledger row, the incident or the decision of record), `target_state` and `phase`. A registration is keyed on event plus argv: 23 registrations, 22 distinct argv, because auto-approve is registered for PreToolUse and PermissionRequest with one argv.

`tests/test_plugin_audience_lint.py` (checker: `tests/_audience_manifest.py`) fails on:

- a registration with no entry, or an entry for one that is gone;
- an unknown value, or an audience that disagrees with its gate;
- a declared constant that does not exist, and a module-level text value of 40 characters or more (below) that nobody classified as injected or not injected;
- a gated entry (`live: true`) in a module that never calls its predicate;
- an audience import that is not inside a `try`, or whose fallback widens a permission (T9);
- a predicate called at import time, or outside a wrapped body. The accepted wrappers are `_lib.run_hook`, `_io.never_fail` (directly, through a verb the dispatcher wraps, or through an `mcp` hook tool), a standalone `main()` with a `try`/`except`, and a local `try`/`except` around the call.

The census reads module-level values: a string, a `+` or f-string concatenation, a list, tuple, set or dict that holds a long string, the arguments and the receiver of a call (a `Row(...)` table, `"...".format(x)`), a conditional expression, and a tuple-unpack assignment, leaving out `re.compile` and `frozenset`. **What it cannot see:** text written or joined inside a function (function-local text of any length), text read from a file, and injected text shorter than 40 characters that nobody declared. Those are held by review and by the behaviour leg, which runs the hooks and reads what they print.

A row whose end state is not wired yet is `live: false` and names the phase that wires it. The behaviour leg's allowlist may name only those rows, so an item still to come cannot hide a live leak. Phase 3 (nexus-sby49.4) wired every row: none is `live: false` now.

## The behaviour leg

`tests/e2e/lib/plugin_behaviour.py`, run by `tests/e2e/plugin-out-of-repo-gate.sh` (static pass, `--all`, `--installed X.Y.Z`, and the release battery's `plugins` leg), drives each registration's real argv under payloads of the shape Claude Code sends, in a scratch user repository, a marked repository and a flagged account, in two postures: P0 (no `nx` on PATH, the nx-launcher and shim entries must exit 0 and print nothing) and P2 (the checkout's `nx`, or with `--nx installed` the installed tool's). It asserts that a user's outputs carry no bead id, RDR id, `bd`, owner name, peer name, lab host, `develop/main` or `Hellblazer/nexus`, that the only denies are the ones the fixture expects (D1's list, the credential read), and that every live SAM-WIDE and NEXUS-REPO constant appears where its audience says and nowhere else. `plugin-behaviour-allowlist.txt` is empty (Phase 3 shipped when it was) and the pins in `tests/test_plugin_behaviour_leg.py` keep it so. The leg also asserts, per scenario, words the output must carry or must not (the credential redirect, the stop guard's CI text, the compaction text's board and beads words), that the six destructive or other-party tools are silent for everyone but the flagged account, and that a Stop hook leaves nothing in a config directory that did not exist. A fifth fixture, `plain-beads` (a user's repository with a `.beads/` directory), runs the compaction events only, with the `bd` word allowed there because its data gate is on.

## Adding a rule or a line of text

1. Decide the audience. A personal memory rule is SAM-WIDE, a rule about nexus's own process is NEXUS-REPO, and EVERY-USER needs a product decision of record. `conexus/skills/writing-nx-skills/promotion.md` has the same step for a memory promotion.
2. Gate it with the predicate that matches, inside the hook's existing wrapper, with the guarded import and its fallback (copy one of the existing modules).
3. Add its entry to `audience.yaml` and, if it is text, a case the behaviour leg reaches.
4. Write the test in the fixture convention: a bare `git init` is a user's repo, and nexus's behaviour is asserted only with the marker or the flag (`tests/AGENTS.md`).
5. If it changes a file under the plugin's behavioural surface, declare it in `conexus/PENDING_RELEASE.md`.
