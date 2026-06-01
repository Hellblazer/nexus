# RDR-140 Post-Mortem: T2 Daemon Supervisor & Ownership Model

**Closed:** 2026-05-31 · **Status:** implemented · **Epic:** nexus-7nxc2 · **Tracking:** nexus-13bcq (#1041)

## Outcome

Shipped all four phases to `develop` (PRs #1045 P1+P2, #1050 P3, #1051 P4). The spawn-race / SQLite-lock thrash that produced ~535 `t2_daemon_crashed`/day under multiple concurrent stacks (#1041) is resolved: K racing `ensure-running` stacks now converge to exactly one daemon with zero crashes and zero healthy-peer reaps.

- **P1 (Gap 1 + Gap 4):** spawn-lock loser quiet-attaches (typed `T2SpawnLockLost`, exit 0, never opens `T2Database` — A1) with a self-healing discovery re-assert; migration cold-start fast-path elides the writer-lock when already at `current_version` + WAL (A3 lock-free reads).
- **P2 (Gap 3):** single-flight election flock around discover→spawn in `ensure-running`; only the holder cold-spawns, waiters re-discover and attach. Waiter budget derived dynamically from the worst-case hold so it can't time out into a thundering herd.
- **P3 (Gap 2, invariant-touching):** ownership/version-aware reap — a healthy current-version addr-file peer is waited-then-forced (never coexisted with); stale/unreachable/open-fd peers reap immediately. RDR-128/129 single-writer backstop preserved.
- **P4 (Gap 5):** `status` surfaces `restarts_in_window`; a bounded crash-loop guard stops `ensure-running` respawns after N failures in a window, logging once.

## What the gates caught (the value of the process)

1. **P3 single-writer Critical.** The stacked review caught that the original "spare a healthy peer" design returned from the reap without aborting `start()`, so a spared peer could become a second writer. Reasoning it through revealed the spare-and-coexist premise was wrong: a healthy-current peer met *after* we hold the spawn lock is necessarily mid-shutdown. Resolved (user-chosen) to **wait-then-force, never coexist** — the reap never returns while the peer is alive. The critic re-review then caught a residual (SIGKILL returning before confirming death), also fixed. Neither was visible from green tests.

2. **P4 false documentation claim.** The critic caught that the cli-reference claimed the crash-loop guard bounds the launchd/systemd `KeepAlive` loop — but those run `nx daemon t2 start` directly and never consult the guard's sentinel. Corrected to accurate scope (the guard bounds the `ensure-running`-driven path, the dominant MCP-boot churn).

3. **Two near-vacuous tests** were caught and hardened: a status assertion that matched the pytest `tmp_path` (which embeds the test name) instead of a real field; and a SIGKILL-confirmation assertion that checked stub state rather than the bounded poll.

## Design evolution worth remembering

- **There is no assumable single supervisor.** nexus is invoked from N independent ephemeral process trees; the contention is leader-election among uncoordinated peers over a single-writer SQLite file. `fcntl` flock is the coordinator-free floor; launchd/systemd is the optional coordinator-ful upgrade. RDR-128/129/140 are the cost of making the floor correct.
- **Where "don't kill a healthy peer" actually lives:** in `ensure-running`'s version-aware pre-discovery (P2) + the spawn lock — not in the post-lock reap. The reap is a single-writer backstop, not a place to defer to peers.

## Deferred follow-ups (filed + enriched)

- **nexus-cis97** (P2) — extend the guard to the launchd/systemd `start` path (Option A; behavioral, likely its own RDR).
- **nexus-whl8n** (P3) — guard robustness: atomic R-M-W under election-timeout, NTP step-back, window boundary.
- **nexus-w82up** (P3) — flapping daemon defeats reset-on-reachability; consecutive-failures model.
- **nexus-safw1** (P3) — real-process version-skew harness scenario (deterministic unit-test substitute shipped; may close wont-fix at audit).

None are gates. The MEDIUM/LOW items are documented trade-offs; the single-writer invariant is not implicated by any.

## Critical Assumptions — final

A1 (loser never opens a second writer), A2 (discovery token carries version/identity for ownership-aware reap, no new persisted state), A3 (migration fast-path lock-free reads), A4 (single-flight converges without deadlock; flock auto-releases on holder death) — all VERIFIED and held through implementation.
