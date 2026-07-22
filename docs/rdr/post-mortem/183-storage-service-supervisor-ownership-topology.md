# Post-Mortem: RDR-183 — Storage-Service Supervisor Ownership Topology

**Closed:** 2026-07-22 (from `draft` — never gated, never accepted)
**Disposition:** dissolved/demoted — founding hypothesis refuted by the RDR's own
research; the dominant field defect shipped as an ordinary fix; residual scope is
bead-sized. Closure investigation: T2 `nexus/rdr183-closure-investigation-2026-07-22`.

## What the RDR set out to decide

A supervisor spawn-authority topology (candidates 0-3) to end two GH #1405
defect classes: session-teardown ownership churn producing client-visible
unresolvable-endpoint failures (defect-1), and launchd/autostart respawn races
(defect-3).

## Why it dissolves instead of completing

- **The TERM-attribution hypothesis was refuted.** The client spawn already
  uses `start_new_session=True` (`commands/daemon.py` `ensure_storage_supervisor`),
  so session-teardown pgroup TERM was not the mechanism.
- **Candidate 0 was the dominant fix and shipped as a plain defect fix**:
  version-gating `_cycle_storage_service_to_current` (`95014020`, nexus-f0pmd,
  6.11.0) killed the ~20/day `stop_requested` churn.
- **The client-visible gap closed independently**: lease-gap re-resolve + retry
  across the RefreshableHttpStoreMixin family (`89455bc1`, nexus-7dsgp, verified
  live in 6.10.2), MCP never-crash-at-startup (`80f914a5`), t2 stray-LaunchAgent
  cleanup (nexus-c0vby, `7ebd3287`).
- The RDR's remaining purpose — the topology decision — was explicitly gated on
  re-measuring residual severity after candidate 0. Post-fix telemetry never
  re-escalated; the decision is intentionally NOT made. The RDR can be iterated
  if telemetry re-escalates.

## Residuals (filed before this close, per the close protocol)

- **nexus-6bmph** — defect-3 proper: launchd steady-state respawn churn
  (`KeepAlive=true` + `ThrottleInterval=30` vs `exit_if_process_unowned` exit 0)
  PLUS the service-unit stray/mode-mismatch cleanup (the c0vby sibling); carries
  live 2026-07-22 evidence (a cloud-mode box crash-looping `com.nexus.service`
  every ~30s on `pg_credentials not found`, 810 log lines in a morning).
- **nexus-3z8a7** — HttpTokenStore/HttpScratchStore construction-time resolve
  gating (promised in the RDR's Constraints, never filed).
- The 2026-07-19 nondeterministic lease-absence observation stays dispositioned
  on GH #1405 (instrumented `302aef6a`, watch-for-recurrence).

## Lessons

- An RDR whose research disproves its own premise should dissolve promptly
  rather than sit `draft` holding an open GH issue hostage — the issue's real
  remaining scope was one bead, invisible behind "tracked in RDR-183".
- Ownership/lifecycle fixes keep proving out the RDR-149 rule: they land in the
  shared registry/supervisor primitives, not in a new topology layer.
