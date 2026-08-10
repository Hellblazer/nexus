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


## Awaiting the next release (pinned: v7.5.0)

### Test-falsification obligation (2026-08-10)

- `conexus/agents/test-validator.md` — new MANDATORY "Falsification" section:
  every regression test gets a `FALSIFIED:` or `NOT FALSIFIED:` line, obtained
  by breaking the PRODUCTION code and watching the test go red, never by
  editing the test. Names the four shapes that shipped past green suites (a
  double accepting a call production rejects; a fixture reality cannot emit; an
  assertion on the absence of a negative; a gate whose failure path returns
  success) and requires a scanned-item count when the change under test IS a
  gate.
- `conexus/agents/code-review-expert.md` — new MANDATORY "Test Falsifiability"
  output section: each added/modified test rated CAN FAIL / CANNOT FAIL with
  the concrete production edit that would turn it red. Reviewer names the
  doubtful ones; test-validator does the actual falsification.
- `conexus/skills/test-validation/SKILL.md` — falsification added to the relay
  quality criteria and the methodology.
- `conexus/skills/code-review/SKILL.md` — falsifiability rating added to the
  relay quality criteria.

Earned 2026-08-10. In one session: a `--force` regression test survived a real
bug because a bare `MagicMock` accepted a call signature the service-mode
collection rejects; a T1 handoff mechanism that had NEVER fired in production
was covered by tests patching a process table with a `comm` value the kernel
cannot report; and an RDR phase gate was written against a doctor check that
returns `ok=True` when the engine is unreachable. Every one was caught by the
same physical act (break it, watch the detector), and every one had previously
survived being read and judged sound. 2026-08-09 produced five more instances
of the same shape and is recorded in memory as "the vacuous-verification day".

Until the pin advances, this obligation exists only for agents dispatched from
a working tree that has it — the installed plugin's reviewers do not ask for it.
