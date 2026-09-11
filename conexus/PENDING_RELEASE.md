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



## Awaiting the next release or plugin cut (pinned: v7.41.1)

- `conexus/skills/mailbox/SKILL.md`: the send line says the nonce is
  REQUIRED for `mailbox/<address>` (id_from keys+nonce), refused as a
  SchemaViolation without it, and never echoed on the read wire.
  bead: nexus-e00lh
- `conexus/skills/phase-review-gate/SKILL.md`: nexus-w5gma — the
  Limitations section now names all three phase-structure layouts the
  gate's parser recognises, including the new `### Phase N` / `#### Step
  N` headings under `## Implementation Plan` (the RDR template's own
  placement, RDR-205/RDR-204's actual shape), which the prior text did
  not mention at all.
  bead: nexus-w5gma
- `conexus/hooks/scripts/_endpoint_resolve.py` `conexus/hooks/scripts/tuple_ledger_project.py` `conexus/hooks/scripts/t2_prefix_scan.py` `conexus/hooks/scripts/routing/_lib.py`:
  nexus-aginu — new shared stdlib-only endpoint/credential resolver.
  ``t2_prefix_scan.py``, ``routing/_lib.py``, and ``tuple_ledger_project.py``
  each carried an independent hand-rolled mirror of
  ``nexus.db.service_endpoint.resolve_service_endpoint``'s precedence — a
  drift between those copies is exactly how nexus-0zsmg happened. All three
  now import ``_endpoint_resolve.py`` for config-dir resolution, lease
  reads, ``config.yml`` credential parsing, data-token-lease matching, and
  the base-URL precedence; their private copies are deleted. Two fixes ride
  along: the ``config.yml`` scanner now strips a trailing inline
  ``# comment`` (real YAML does; the old mirrors did not), and the
  ``NX_SERVICE_HOST``/``PORT`` env leg now fills a missing HOST from a live
  local supervisor lease before defaulting to ``127.0.0.1`` (matching
  ``resolve_service_config``'s per-field lease merge). Also: a SubagentStop
  ``report`` payload with no ``agent_id`` (the harness firing a stop for
  something this ledger has no tracked agent for — measured at ~250
  occurrences per session) now projects nothing, silently, instead of
  logging an identical non-actionable line every time.
  bead: nexus-aginu

- `conexus/hooks/scripts/tuple_ledger_project.py` `conexus/hooks/scripts/subagent-start-tuple-async.sh`:
  nexus-em75s.42 — projector residuals from the Phase 2 review. The POST
  now runs through a no-redirect, no-proxy ``urllib`` opener (an internal
  fixed engine URL must never follow a 3xx or route through an ambient
  ``http_proxy``/``https_proxy``) and the whole call is bounded to
  ``_POST_TIMEOUT_S`` wall-clock time via a joined daemon thread, not just
  each individual socket operation. ``subagent-start-tuple-async.sh``'s
  stale "curl" wording is corrected (the POST has used stdlib ``urllib``
  since nexus-em75s.12). A new CA-4 pin (a listening-but-never-accepting
  socket) exercises the wrapper's slow path for the first time — the
  existing three latency tests only ever hit an instantly-refused port.
  bead: nexus-em75s.42
- `conexus/hooks/scripts/expectations.sh`: nexus-zn9op (production half) —
  `expectations_census`'s space-backed `nx tuple list --prefix ledger/`
  call is now bounded by a portable wall-clock deadline
  (`_expectations_run_bounded`, no `timeout`(1) dependency — macOS ships
  no GNU `timeout`), default 20s, overridable via
  `NX_EXPECT_CENSUS_NX_TIMEOUT_S`. On expiry the census reports
  `SPACE_FALLBACK\treason=nx tuple list exceeded <N>s` and returns rather
  than hanging the live orchestrator session that calls it directly.
  bead: nexus-zn9op
