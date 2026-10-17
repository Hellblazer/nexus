# Pending release: plugin changes that are NOT live yet

`.claude-plugin/marketplace.json` pins `plugins[].source.ref` to an immutable
release tag. Claude Code loads this plugin's hooks, commands, skills, and agents
from **that tag**, not from your working tree. So every change below is merged
on `develop` and **inert in every running session** until the next release ships
and users install it.

This file is the acknowledgement ledger for that gap. It exists because the gap
is otherwise invisible: on 2026-07-25 a subagent ran `git stash -u` in a shared
tree and the guard that covers exactly that verb did not fire, because the
coverage had landed hours earlier and the installed plugin was still `v6.18.1`.
Three guards had been merged, closed as "mechanized", and were protecting
nothing.

**Rules, enforced by `tests/test_plugin_release_drift_ledger.py`:**

- Every file under the behavioural surface that differs from the pinned tag MUST
  be listed here. Adding a guard without declaring it fails the suite.
- When a release ships and the pin advances, drift goes to zero and this list
  MUST be emptied. A stale entry also fails the suite, so the ledger cannot
  quietly become fiction.
- Do NOT "fix" a failure by deleting entries. The entry is the honest statement
  that the thing is not yet live.

**Do not use this to justify skipping a release.** If a guard matters enough to
mechanize, it matters enough to ship.

**Deferring a straddling entry (nexus-2x3qy).** A plugin cut (`scripts/
cut_plugin_release.py`) refuses when a ledger entry's bead also touches wheel
content (`src/`, `conexus/plans/`, `conexus/daemon/`, `mcpb/`, `dt/`) the
wholesale import cannot hold back on a per-entry basis. The only fix is moving
that entry under `## Deferred to the next client release` below: the cut then
holds the entry's channel path(s) back from itself too (restored to the base
branch's own content) so the whole bead ships together, in one piece, at the
next client release. A deferred entry is exempt from the release-window
"ledger must be empty" rule above -- still declared there is correct, not
stale -- and stays exactly where it is until moved back deliberately.

---



## Awaiting the next release or plugin cut (pinned: v7.82.0)

ONE PATH PER BULLET, on the bullet's FIRST line.

- 6.18.1 upgrade path measured (nexus-6g218): `conexus/skills/upgrade/SKILL.md` says aiming the 6.18.1 pin at a managed endpoint failed in every case measured (it stops before writing against a current engine; on the last staging-era build a same-model store with no aspects was reported verified while its pointers were discarded), replacing "unmeasured".
- `conexus/hooks/scripts/version_lockstep_hook.py` (nexus-ehg1y): with a missing or stale `cli_lockstep_marker` (a hand install never writes one; a pip or dev-tree install cannot have the action rewrite it) the lockstep hook runs `nx --version` (3 s bound) and treats a CLI not older than the plugin as in lockstep, by the action's own `satisfies` rule, so a matching CLI no longer gets a false nudge and an upgrade dispatch. A marker equal to the plugin's version still decides alone, with no subprocess.
- `conexus/launch/nx_setup.py` (nexus-ehg1y): a docstring in `_record_lockstep` now names what the lockstep hook does when the marker is missing; no behaviour change.

- A hook ask with no human present is a measured deny (nexus-mcfaj): `conexus/hooks/scripts/routing/_lib.py` documents, in the `ask_envelope` docstring and the module's ask paragraph, what a PreToolUse `ask` does in `claude -p` (default, dontAsk, auto, and a background subagent) at Claude Code 2.1.302: a deny whose reason the model reads, never a hang, never a classifier approval. Docstring only; no behaviour change.

- Description without angle brackets (nexus-0nskc): `conexus/skills/mailbox/SKILL.md` writes the address placeholder as ADDRESS in its description, since Claude Desktop's plugin sync rejects angle brackets there.

- Description without angle brackets (nexus-0nskc): `conexus/skills/tumbler-footnotes/SKILL.md` writes the tumbler placeholder as TUMBLER in its description, since Claude Desktop's plugin sync rejects angle brackets there.

- Description without angle brackets (nexus-0nskc): `conexus/agents/code-review-expert.md` asks to be briefed with a "Ship when ..." line stating the acceptance bar, in place of the angle-bracket placeholder Claude Desktop's plugin sync rejects in descriptions.

- Description without angle brackets (nexus-0nskc): `conexus/agents/substantive-critic.md` asks to be briefed with a "Ship when ..." line stating the acceptance bar, in place of the angle-bracket placeholder Claude Desktop's plugin sync rejects in descriptions.

- Description without angle brackets (nexus-0nskc): `conexus/commands/continuation.md` writes the path placeholder as PATH in its description, since Claude Desktop's plugin sync rejects angle brackets there.

- sequential-thinking survives hostile stdio (nexus-rwv5i): `conexus/mcp/sequential_thinking.py` answers input that is not UTF-8 (each line is decoded strictly, so a bad byte is a parse error with a null id, never a guess), nested past the recursion limit, an integer too long to parse or too big for a float, or a lone surrogate or NaN in an id with a JSON-RPC error and goes on, leaves quietly when the client stops reading (at the write and again at interpreter exit) or when stdin or stdout is closed at start, and writes nothing to stderr in any of them. An unexpected handler error is -32603 naming only the exception class. Replies are ASCII-escaped JSON. Carries over the fixes the RDR-228 stand-in review made (nexus-sv2wx.6).
- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` (nexus-7mgnd, nexus-0l6zh): the subagent git-write guard stops denying three literal read-only shapes, `git merge-base` / `git merge-tree`, the whole tokens `--no-commit-id` and `--abbrev-commit`, and a hook file path such as `.git/hooks/post-commit`, but only in a command that cannot rebuild text (no `$`, backtick or process substitution, no redirect to a file, no pipe into anything but a read-only filter, no shell or interpreter word); every other verb glued to a path (`git log feature/x-add-thing`, `git log docs/add.md`) still denies, and so does any verb the shell could build at runtime (`git $(basename /commit)`, `... | xargs git`, `$g-commit`). In a shared checkout it now also denies `gh pr checkout` and `gh co`, `gh repo sync`, `gh pr merge` / `gh pr close` with `-d` or `--delete-branch`, `gh issue develop --checkout`, `hub sync` and any git verb run through `hub` as a command (a bare `hub` word that is an argument or directory no longer arms it); the deny message now says the tool call's cwd decides, not a `cd` or `git -C` inside the command. A provable linked worktree stays exempt.
- the setup stand-in reads stdin as strict UTF-8 and writes its NaN-id fallback under the dead-client handler (nexus-rwv5i): `conexus/launch/nx_standin.py` answers a line that is not UTF-8 with a parse error and a null id instead of a guess from replacement characters, and a client that stops reading while the reply is the NaN-id fallback now ends the server quietly instead of with a BrokenPipe traceback.
- one stdio loop for the two stdlib-only servers (nexus-ing7a): `conexus/launch/_stdio_loop.py` is new and holds the loop the sequential-thinking server and the setup stand-in each carried a copy of (strict per-line UTF-8, parse and internal errors, the NaN-id fallback, the dead-client exit, closed streams); each server now supplies only its handler. It sits beside the stand-in, and both servers find it from their own file location. Ships with the two entries below; neither works without it.
- sequential-thinking uses the shared loop (nexus-ing7a): `conexus/mcp/sequential_thinking.py` adds the launch directory to its import path from its own location and hands its handler to `_stdio_loop`, so it needs that module in the same plugin tree; its replies are unchanged.
- the setup stand-in uses the shared loop (nexus-ing7a): `conexus/launch/nx_standin.py` hands its handler to `_stdio_loop`, and an unexpected handler error is now `-32603` naming the exception class, as the sequential-thinking server already answered it.


## Deferred to the next client release

- A missing git no longer fails setup (nexus-36qyh): `conexus/launch/nx_setup.py` records the failing rows of the main sweep of `nx doctor` (it printed `  ✗ label` and the filter wanted `[✗]`), ends a run `done` when the only failing row is the one labelled exactly `git` (the `git hooks (...)` rows stay failures), and reads a recorded `doctor-failed` over `git` alone as `done`, so it no longer outranks "the service is not running". Deferred because the same bead makes the `git` row of `nx doctor` a warning in `src/nexus/health.py`.

- Setup's git-only verify path needs a finished sweep (nexus-0ky29): `conexus/launch/nx_setup.py` takes the git-only path (a missing git does not fail setup) only when `nx doctor` exited 1 (CLIs up to v7.20.0) or 2 (from v7.21.0) and printed its fatal summary footer after the rows, as well as naming `git` alone; a CLI that prints the git row and then dies (another exit code, a traceback, no footer, a timeout) ends `doctor-failed`, and its record carries an added row saying the sweep did not finish so the status view and `nx doctor`'s setup row do not read it as `done`. Deferred with the nexus-36qyh entry above, whose git-only path it narrows.
