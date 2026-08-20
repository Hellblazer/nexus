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

---


## Awaiting the next release (pinned: v7.12.0)

- `conexus/skills/using-nx-skills/SKILL.md` — nexus-h33x8.5: one pointer
  sentence added, noting SessionStart now emits only a condensed
  imperative + trigger conditions (wheel-delivered via
  `src/nexus/session_start_guidance.py`, live already) and that this
  file remains the full routing menu, read in full when `Skill` is
  invoked. No routing content changed.
- `conexus/hooks/scripts/session_start_hook.py` — nexus-h33x8.5 fix-pass:
  Ready Beads cap tightened (10 lines/500 chars -> 5 lines/160 chars +
  overflow-count line), nx Capabilities framing prose condensed (every
  distinct backtick-quoted token preserved), both factored into pure
  `_render_ready_beads`/`_build_capabilities_block` functions. Closes the
  VERIFICATION 1 combined-SessionStart-budget gap the critic flagged.
- `conexus/hooks/scripts/t2_prefix_scan.py` — nexus-h33x8.5 fix-pass:
  render caps tightened (`_HARD_CAP` 15->8, `_SNIPPET_LIMIT` 5->3,
  `_TITLE_LIMIT` 8->5, snippet `max_chars` 120->70). The fetch/budget/
  freshness machinery is unchanged — only render density shrank.
- `conexus/hooks/scripts/routing/_lib.py` — nexus-h33x8.3 fix-pass
  (Sam-directed, 2026-08-20): `log_routing_event` now rotates
  `routing_log.jsonl` to `routing_log.jsonl.1` via atomic rename
  (`os.replace`, clobbering any prior `.1`) once it exceeds a 1 MiB byte
  cap, BEFORE appending — never a read-modify-write trim, which would
  race the many concurrent routing-hook processes that append to this
  file. Rotation failure (including the expected concurrent-rotation
  `FileNotFoundError` race) never blocks the append. All other behavior
  unchanged, including the pre-existing `Path.home()` hardcoding this
  fix-pass deliberately left alone.
- `sn/hooks/scripts/routing/_lib.py` — same change, kept byte-identical
  to the `conexus/` copy per `test_lib_copies_are_byte_identical`.
