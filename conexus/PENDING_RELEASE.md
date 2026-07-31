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

## Awaiting the next release (pinned: v6.18.1)

- `conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py` —
  nexus-ays2l: the guard was blocking the harmless git verbs and permitting the
  destructive ones. Until this ships, subagents in a shared tree can still run
  `checkout` / `restore` / `stash` / `clean` / `reset` / `rm`. This is the exact
  gap that let a `git stash -u` through on 2026-07-25.
- `conexus/hooks/scripts/routing/git_add_all_redirects_to_explicit_paths.py` —
  nexus-vduer: now carries a SECOND check, denying `git push` whose effective
  target is `main`. Until this ships, direct-push-to-main is a memory-only
  control, which is what failed on 2026-07-23.
- `conexus/hooks/scripts/routing/registry.yaml` — nexus-vduer: registry entry for
  the consolidated two-check hook above.
- `conexus/hooks/scripts/expectations.sh` — nexus-mk3tw: the Gap-1 guard reported
  `undeclared=0` while recognising none of the dispatches it saw. Until this
  ships, that false-clean persists.
- `conexus/commands/continuation.md` — nexus-mk3tw: documents that
  `expectations_undeclared`'s exit code is now load-bearing (exit 1 + BLINDSPOT).
- `conexus/skills/orchestration/SKILL.md` — nexus-mk3tw: same, for the
  orchestration skill's census step.
- `conexus/skills/orchestration/SKILL.md` — context-economy routing: adds a
  second routing axis (shape, not only task type) carrying the distill-early
  heuristic and the bulk-read-to-subagent rule. Until this ships, routing
  guidance remains task-type-only and says nothing about context cost, so the
  26-tool-calls shape stays unaddressed at the skill layer.
- `conexus/skills/plan-first/SKILL.md` — nexus-0yrjr: adds the cue-to-`bindings`
  table so an agent routes "search RDRs for X" into
  `bindings={"content_type": "rdr"}`. Until this ships, `nx_answer`'s new
  `bindings` parameter is discoverable only by an agent that reads the tool
  schema directly, so the type-scoped builtins (`type-scoped-search`,
  `find-by-author`) stay effectively unreachable — the practical half of the
  reachability fix is inert even once the wheel ships.
- `conexus/hooks/hooks.json` — nexus-i711w: the SessionStart hook that ran
  `nx daemon t2 ensure-running` on EVERY session start, for every plugin user,
  silenced by `|| true`. The verb is deleted with the T2 daemon, so until this
  ships that hook keeps firing a command that no longer exists — harmless only
  because of the `|| true` that hid it in the first place.
- `conexus/hooks/scripts/rdr_hook.py` — nexus-i711w (nrxs9 final review
  Critical-1): the RDR-collection resolver imported the DELETED local
  `Catalog`, so a broad except silently forced every session onto the
  path-derived fallback name. Repointed to the service catalog's
  `collection_for_repo`. Until this ships, sessions on the pinned tag keep
  using the fallback — same behaviour as before the fix, no new breakage.
- `conexus/agents/_shared/ERROR_HANDLING.md` — nexus-cg13x: the TTL rule said
  "Use `ttl=0` for permanent entries". That is true only because `memory_put`
  coerces it; in the store, `0` means EXPIRE IMMEDIATELY (`expire()` selects
  `WHERE ttl IS NOT NULL` and computes `effective_ttl = ttl * (1 +
  log(access_count + 1))`, so a stored `0` is swept on the next pass — only
  NULL is excluded). The corrected rule states that permanent is NULL, and
  warns not to carry "0 means permanent" outside `memory_put`, naming the ETL
  import endpoints as where it is still false. Until this ships, agents on the
  pinned tag keep reading the old rule — harmless for MCP callers, who are
  coerced either way, and the engine-side coercion that closes it for direct
  POSTers is itself waiting on the next engine tag.
