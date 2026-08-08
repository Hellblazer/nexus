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


## Awaiting the next release (pinned: v7.4.0)

- `conexus/hooks/hooks.json` — nexus-h33x8.4: the SessionStart
  `cat $CLAUDE_PLUGIN_ROOT/skills/using-nx-skills/SKILL.md` entry is
  REMOVED. The guidance imperative it delivered is now emitted by
  `nx hook session-start` instead (Tier B — already-registered
  hooks.json entry, PyPI wheel; see `src/nexus/session_start_guidance.py`
  for the moved, byte-identical-on-landing content). This is the
  highest-leverage change in the epic: it collapses guidance-iteration
  latency from plugin-release cadence to PyPI-release/local-reinstall
  cadence for every future edit, permanently — but only once THIS
  hooks.json edit itself goes live at the next release. Until then, a
  session running under the currently-pinned plugin still carries the
  legacy `cat` entry, so `nx hook session-start`'s own emission is
  self-suppressing during that window
  (`session_start_guidance.legacy_cat_channel_active` reads the
  INSTALLED plugin's own `hooks/hooks.json` via `$CLAUDE_PLUGIN_ROOT` and
  detects the still-registered legacy entry) — installed sessions get
  the imperative exactly once (from the legacy `cat` entry, unchanged
  content) rather than twice. At the next plugin release this entry
  disappears from the pinned copy too, the suppression gate opens
  permanently, and `nx hook session-start` becomes the sole channel.
  nexus-h33x8.4's own TIER PROOF verification (change one word in the
  wheel, reinstall, confirm it appears with no plugin release) is
  deliberately NOT expected to pass until AFTER that release ships —
  see the bead for why closing on merge would repeat the 2026-07-25
  ledger-blindness incident this file exists to prevent.
  Post-release verification one-liner:
  `nx hook session-start | grep -c 'Using Conexus Skills'` (expect 1).
  Release-note obligation (critic [21867] observation): a machine whose
  WHEEL predates h33x8.4 gets NEITHER channel for ~one session after
  this release activates (the pinned cat entry is gone and the old
  wheel has no emitter); self-healing via the same SessionStart block's
  `nx upgrade --auto` + the lockstep hook — state this in the release
  notes rather than letting it read as a regression report.
- `conexus/skills/writing-nx-skills/SKILL.md` — nexus-h33x8.4 companion
  doc edit (rode the same commit, declared late — caught by the drift
  ledger at the 8nlj4 batch push): the "Updating using-nx-skills"
  section now explains the SessionStart-delivery split — the injected
  text is `GUIDANCE_IMPERATIVE` in `src/nexus/session_start_guidance.py`
  (wheel, Tier B), so editing the skill's SKILL.md alone no longer
  changes what a fresh session sees; keep both in sync by hand until
  nexus-h33x8.5 diverges them deliberately. Doc-only; no behavior.
- `conexus/hooks/scripts/version_lockstep_action.py` — the detached
  auto-upgrade now writes an always-on audit line per swap attempt
  (`lockstep_upgrade_started` / `lockstep_upgrade_result`) to
  `~/.config/nexus/lockstep.log` (nexus-otnvr item 4: the hook's output
  was routed to DEVNULL and its debug() gated behind NX_HOOK_DEBUG, so
  the venv swap was invisible — it raced the MCP server boot at the
  7.4.0 reinstall with no trace). Installed sessions keep the silent
  swap until the next plugin release.
- `conexus/hooks/scripts/expectations.sh` + `conexus/hooks/scripts/subagent-stop.sh`
  — nexus-7z7rj + nexus-plycy (two-round fix, same day). Round 1
  (nexus-7z7rj): the `expectations_owes_report` credit read-decide-append
  is now gated strictly `if [[ -n "$held" ]]`, closing the double-spend
  the 4b8sz lock-budget knob only narrowed (CI red 2026-08-08, PR #1445
  run 31244456036, blocked=5 vs credit=4 at NX_EXPECT_LOCK_TRIES=200).
  Round 2 (nexus-plycy, substantive-critic Critical on round 1 before it
  shipped): round 1's fixed exhaustion default ("does not owe", never
  consult the ledger) was itself unsafe combined with the pre-existing
  60s stale-lock reap — an orphaned lock holder could silence the
  owes-report guard SESSION-WIDE for up to a minute (silent miss, not
  the guard's usual safe-direction over-block). Round 2 ships the
  contract actually live going forward:
    - the owes-report lockdir is now PER AGENT_TYPE
      (`<file>.owes.<type>.lock`, `:` encoded to `__`), not
      session-wide, so exhaustion can only mean same-type contention or
      a same-type stale lock;
    - the fixed default on exhaustion is now BLOCK (owes), not pass —
      the safe direction is restorable now that cross-type
      false-accusation risk is gone — but the CONSUMED ledger row is
      still never written on an unverified decision (no credit is ever
      spent without holding the lock);
    - every exhaustion-forced block is DISCLOSED, never silent: the
      JSON `reason` names the lock-contention cause and the BLOCKED
      row's ledger entry carries an optional 4th "cause" field
      (`lock-exhausted`) so it's distinguishable from a genuine,
      credit-verified block on audit.
  Residual, honest and bounded: a stale SAME-type lock still over-blocks
  stops of that one type for up to the existing ~60s reap window before
  self-healing — the accepted price, always disclosed, never silent.
  Also adds the test-only `NX_EXPECT_LOCK_HOLD_DELAY_S` contention seam.
  Installed sessions keep the pre-round-1 racy-unlocked-fallback
  behavior (rare, load-dependent, undisclosed over-block of the
  owes-report SubagentStop guard) until the next plugin release.
