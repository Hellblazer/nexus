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


## Awaiting the next release (pinned: v7.7.0)

- `conexus/hooks/scripts/t2_prefix_scan.py` — nexus-8fvp2: T2 context injection repointed from the dead SQLite memory.db to the engine's T2 HTTP endpoint (stdlib-only; env > config.yml > lease precedence; two-arm loud freshness assert; per-namespace isolation, 5-namespace cap, 8s budget). Until the next release ships, installed sessions still run the frozen SQLite scan.
- `conexus/agents/_shared/CONTEXT_PROTOCOL.md` — nexus-j9lbk: store_put calling convention gains `agent="<role>"` attribution (mirrors memory_put).
- `conexus/agents/architect-planner.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/code-review-expert.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/codebase-deep-analyzer.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/debugger.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/deep-analyst.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/deep-research-synthesizer.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/developer.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/strategic-planner.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/substantive-critic.md` — nexus-j9lbk store_put attribution convention.
- `conexus/agents/test-validator.md` — nexus-j9lbk store_put attribution convention.
- `conexus/skills/catalog/SKILL.md` — nexus-j9lbk store_put attribution convention.
- `conexus/skills/nexus/SKILL.md` — nexus-j9lbk store_put attribution convention.
- `conexus/skills/nexus/reference.md` — nexus-j9lbk store_put attribution convention.
