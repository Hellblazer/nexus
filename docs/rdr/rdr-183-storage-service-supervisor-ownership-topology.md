---
title: "Storage-Service Supervisor Ownership Topology: Eliminate Session-Teardown Churn and launchd/Autostart Races"
id: RDR-183
type: Architecture
status: draft
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
  tier's copy. Whatever ownership model wins must be expressed there.
- RDR-175's decision stands: the OS init system is the single process
  WATCHDOG (no in-process respawn layers). This RDR decides the single
  spawn AUTHORITY, which RDR-175 deliberately did not.
- Client-visible availability during transitions is bounded by the
  defect-1 mitigation (client re-resolve + retry once); this RDR's bar
  is eliminating the routine churn, not papering over it.

## Decision Space (candidates, not mutually exclusive)

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

(To be filled during rdr-research: reproduce the session-teardown TERM
attribution — process-group membership of a client-autostarted
supervisor vs the plist's; enumerate every current spawn call site;
launchd kickstart semantics/latency; systemd equivalents; container/CI
topologies without an OS unit.)

## Decision

(Open — draft.)
