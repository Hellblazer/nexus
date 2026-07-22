---
title: "Storage-Service Supervisor Ownership Topology: Eliminate Session-Teardown Churn and launchd/Autostart Races"
id: RDR-183
type: Architecture
status: closed
closed_date: 2026-07-22
priority: high
author: Hal Hildebrand
reviewed-by: ""
created: 2026-07-15
related_issues: []
related_rdrs: [RDR-149, RDR-152, RDR-174, RDR-175]
supersedes: []
related_tests: [tests/daemon/test_rdr149_lifecycle_conformance.py, tests/daemon/test_storage_service_daemon.py]
---

# RDR-183: Storage-Service Supervisor Ownership Topology

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

GH #1405 (the 6.10.1 shakeout on a real service-mode macOS box) exposed a
supervisor OWNERSHIP hole left between RDR-174 (launchd/systemd autostart
units) and RDR-175 (OS-init as the single process watchdog): two distinct
spawn authorities — the launchd unit and the client autostart path — can
each legitimately produce the owning supervisor, and the two produce
supervisors with DIFFERENT lifecycle characteristics.

Field evidence (2026-07-15, one box, one day):

- **20 `stop_requested=True` supervisor exits**, 6 of them inside one
  5-minute window at a Claude session start. The owning supervisor was
  client-autostarted (argv carries `--config-dir`; the `com.nexus.service`
  plist's does not) and orphaned to PPID 1. Working hypothesis:
  client-autostarted supervisors sit in the parent session's process
  group and receive SIGTERM at session teardown.
- Each stop respawns on a **new port** with a ~5–10s gap before
  `storage_service_lease_published`. MCP calls landing in the gap
  hard-fail with `nexus-service endpoint is not resolvable` (client-side
  retry-and-re-resolve mitigation tracked separately — the GH #1405
  defect-1 mitigation bead — but the churn itself remains).
- **Permanent launchd spawn churn**: with a client-autostarted supervisor
  holding the lease, the launchd instance probes, logs
  `already_running_healthy`, and exits; `KeepAlive=true` +
  `ThrottleInterval=30` makes launchd respawn it every 30s, forever.
- **Start races**: during ownership gaps the launchd instance races the
  client autostart; losers write `Failed to start Postgres on port
  50608` / `exited with code 1 before /health became ready` into
  `~/Library/Logs/nexus-service.err` (4 entries that day).

## Constraints

- **RDR-149 substrate rule** (AGENTS.md hot rule): daemon-lifecycle
  changes land in the shared primitive (`src/nexus/daemon/service_registry.py`)
  plus the conformance suite
  (`tests/daemon/test_rdr149_lifecycle_conformance.py`) — never one
  tier's copy. Research nuance (finding 2): the lease/election/heartbeat
  *primitive* is genuinely unified there (zero spawn calls in
  service_registry.py), but spawn *authority* is today scattered across
  `commands/daemon.py`, `commands/upgrade.py`, `commands/init.py`,
  `upgrade_finish.py`, and both OS-unit templates — unifying it is this
  RDR's open decision, not an already-satisfied precondition. Whatever
  ownership model wins must be expressed in the shared substrate.
- RDR-175's decision stands: the OS init system is the single process
  WATCHDOG (no in-process respawn layers). This RDR decides the single
  spawn AUTHORITY, which RDR-175 deliberately did not.
- Client-visible availability during transitions is bounded by the
  defect-1 mitigation (nexus-7dsgp: bounded-wait re-resolve,
  evidence-gated on prior in-process lease success) for the T3 vector
  client and the T2 `RefreshableHttpStoreMixin` store family (memory,
  plan, aspect-queue, taxonomy, telemetry, chash, centroid,
  document-aspects/highlights, catalog) — covering the reported
  search/store_get/memory_delete failure modes. `HttpTokenStore` and
  `HttpScratchStore` construction remain ungated at construction time
  (their connection-refused retry legs are covered; their first-resolve
  is not) — lower-exposure by construction pattern (session-scoped /
  self-healing background loop) but not yet closed by the same
  mechanism (follow-on bead). This RDR's bar is eliminating the routine
  churn, not papering over it.

## Decision Space (candidates, not mutually exclusive)

0. **Version-gate `_cycle_storage_service_to_current`** to match its T2
   sibling `_cycle_daemon_to_current` (research finding 2: the dominant
   churn source is the SessionStart hook's `nx upgrade --auto`
   unconditionally cycling a live, already-current supervisor — not
   session teardown at all). Small, evidence-shaped, independently
   shippable; kills the 20/day `stop_requested` class on its own.
   Candidates 1–3 then address the RESIDUAL dual-authority problems
   (launchd 30s `already_running_healthy` loop, start races), whose
   severity should be re-measured after this lands.

1. **Detach client-autostarted supervisors** into their own session /
   process group (`setsid` / `start_new_session=True` at spawn) so
   session teardown cannot TERM them. Smallest change; leaves TWO spawn
   authorities and the launchd churn/races in place.
2. **launchd/systemd becomes the sole spawn authority.** Client
   autostart stops spawning supervisors entirely; when the service is
   absent it kicks the OS unit (`launchctl kickstart` / `systemctl
   start`) and waits on the lease. Eliminates the dual-authority class
   (churn, races, plist-vs-argv config divergence) at the cost of
   requiring the unit to be installed and correct — needs a fallback
   story for boxes without the unit (dev checkouts, containers, CI).
3. **Lease handoff / same-port takeover**: the retiring supervisor keeps
   serving until the successor's lease is published (or hands the
   listening socket over), closing the 5–10s unresolvable window
   entirely. Heaviest; orthogonal to who spawns; likely only worth it if
   candidates 1/2 leave a measurable gap.

Likely shape: 2 as the primary decision with 1's detachment applied to
whatever residual non-launchd spawn paths must survive (containers/CI),
and 3 deferred unless post-fix telemetry still shows gaps.

## Success Criteria

- Zero routine `stop_requested` supervisor exits at session transitions
  on a lived-in box (upgrade-finish cycling remains the sanctioned stop).
- Zero steady-state launchd respawn churn (no `already_running_healthy`
  exit loop).
- Zero pg-start race errors in `nexus-service.err` during transitions.
- Conformance-suite coverage for the chosen ownership model (spawn
  authority, teardown immunity, race behavior) per the RDR-149 rule.
- The `--package-upgrade` rehearsal leg still passes (convergence cycles
  the service through whatever the new ownership path is).

## Research

Two research passes, 2026-07-16. Structured findings in T2:
`nexus_rdr/183-research-1` (RQ3–RQ5, OS-init semantics) and
`nexus_rdr/183-research-2` (RQ1–RQ2, code attribution), with full
file:line / source detail in the `-detail` twins.

### RQ1 — TERM attribution: the working hypothesis is REFUTED

The problem statement's pgroup-teardown hypothesis does not survive the
code. `ensure_storage_supervisor` (`src/nexus/commands/daemon.py:1648`)
already spawns with `start_new_session=True` (`daemon.py:1733`) — the
client-autostarted supervisor is session/pgroup-isolated from birth.
The actual churn mechanism is a deliberate, direct-PID
`os.kill(pid, SIGTERM)` (`storage_service_daemon.py:1270`) fired by
`_cycle_storage_service_to_current` (`upgrade.py:226-276`), which
**unconditionally** stop+starts any live supervisor — no version check,
unlike its T2 sibling `_cycle_daemon_to_current` (`upgrade.py:170-189`)
— and runs from `nx upgrade --auto`'s `finally:` block on **every**
SessionStart hook firing (`conexus/hooks/hooks.json:9`, matcher
`startup|resume|clear|compact`). This fully accounts for the 6-in-5-min
clustering and the 20/day `stop_requested` exits.

### RQ2 — Spawn call sites (full table in 183-research-2-detail)

One Popen spawn path (`ensure_storage_supervisor`; callers `nx init
--service`, `nx daemon service start` non-foreground); two OS units
exec'ing `--foreground` directly (launchd plist without `--config-dir`,
`KeepAlive=true`/`ThrottleInterval=30`; systemd with
`SuccessExitStatus=143`); two unconditional cyclers (`upgrade.py`,
`upgrade_finish.py:583`); rehearsal harnesses. Platform asymmetry:
systemd treats graceful SIGTERM as success (no respawn churn on Linux);
macOS `KeepAlive=true` respawns unconditionally — the permanent
`already_running_healthy` loop is expected launchd behavior.

### RQ3–RQ5 — OS-init semantics (183-research-1)

launchd: `kickstart` starts a stopped job immediately but requires the
plist bootstrapped (defensive `bootstrap` before `kickstart`); `-k`
kill-restarts a running job; plain-kickstart-on-running and
ThrottleInterval-bypass are UNDOCUMENTED (verification items 1–2).
systemd: `start` no-ops on running (asymmetry to note);
`StartLimitBurst` lockout has no launchd equivalent — verify
`StartLimitIntervalSec=0` landed per RDR-175 Gap-4 (item 4); user units
need lingering or they re-import session-lifetime coupling → system-unit
vs user-unit sub-decision. Fallback: `start_new_session=True` immunity
to POSIX pgroup teardown is confirmed (setsid(2)); macOS
login-session-sweep immunity is unverified (item 3). Prior art for
direct-child fallback: zonkyio/embedded-postgres; no external precedent
for the kick-else-spawn hybrid.

### Open verification items (for the gate)

1. Plain `kickstart` (no `-k`) behavior on an already-running job.
2. Whether `kickstart` bypasses `ThrottleInterval`.
3. setsid immunity to macOS *login-session* (not pgroup) teardown.
4. `StartLimitIntervalSec=0` present in the shipped systemd unit?

## Decision

(Open — draft.)
