---
title: "Unified nx init and Service-Supervisor Lifecycle: Collapse the Install to One Command and Close the Reboot-Persistence Gap"
id: RDR-174
type: Architecture
status: accepted
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-28
accepted_date: 2026-06-28
related_issues: []
related_rdrs: [RDR-144, RDR-152, RDR-155, RDR-157, RDR-158, RDR-160, RDR-161, RDR-165, RDR-166]
supersedes: []
related_tests: [tests/test_init_cmd.py, tests/test_daemon_cmd.py]
---

# RDR-174: Unified nx init and Service-Supervisor Lifecycle

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

The 6.0 install has accreted into a multi-step gauntlet whose steps no longer
match the post-RDR-152/155/158 reality. A new user following the README runs:
`uv tool install conexus` then `/plugin marketplace add` + `/plugin install`,
then `nx init --service`, then `nx daemon t2 install --autostart`, then
`nx doctor`, then `nx index`. Two of those steps are wrong or unnecessary for
the default 6.0 service topology, and the command surface presents two
overlapping "init" mental models.

#### Gap 1: Two init mental models for one outcome

`nx init` (RDR-144) is the local-embedder picker; `nx init --service` (RDR-157/
161) is the heavy service-provisioning path. Since T3 serving now routes
exclusively through the PG + pgvector + Java service (RDR-155 P4a), plain
`nx init` no longer produces a usable T3 backend on its own — the embedder choice
is moot in service mode, where RDR-160 forces bge-768. The user must know to pass
`--service`. There is no single "make my stack ready" command.

#### Gap 2: The T2-daemon step is vestigial in the default install

The hard default for every T2/T1 domain store flipped SQLITE → SERVICE
(`src/nexus/db/storage_mode.py`, "RDR-152 nexus-fjwxh"). In a default 6.0 service
install, T2 (memory, plans, catalog, telemetry, chash, aspects) routes through the
same PG service as T3. The local SQLite single-writer T2 daemon
(`nx daemon t2 install --autostart`) is therefore **not needed** unless a store is
explicitly opted back to SQLITE — yet the README quick-start still lists it as a
required step. RDR-158 (accepted) makes the PG service the *only* T2 path, which
removes the SQLITE rollback entirely; the daemon step becomes dead weight.

#### Gap 3: The storage-service supervisor has no reboot-persistence

The `nx daemon service` group exposes `start` / `install-binary` / `stop` /
`status` but, unlike `nx daemon t2` and `nx daemon t3`, has **no
`install --autostart`** that writes a launchd/systemd unit. `nx init --service`
starts a persistent supervisor for the session, but nothing brings the service
back after a reboot. For the substrate that now serves *every* tier (T2 and T3),
this is a silent durability gap: the user reboots and their stack is down with no
autostart to recover it. The README's autostart step targets the wrong (vestigial)
daemon while the daemon that actually needs persistence has no autostart at all.

## Decision

(To be locked at gate.) Draft direction:

1. **Unify `nx init` into one mode-detecting command.**
   - **Managed mode** (`NX_SERVICE_URL` / `service_url` present): ensure
     `service_url` + `service_token` are set (prompt if missing, reusing the
     RDR-166 managed-onboarding wizard), probe the service for reachability +
     version, then stop. No local provisioning.
   - **Local mode** (no service URL): run the existing
     `provision_and_start_service()` sequence (provision PG → lock bge-768 +
     fetch its ONNX → acquire the signed native binary → start the persistent
     supervisor). This is already the `nx init --service` body; `nx init` calls
     it by default.
2. **Add reboot-persistence for the service supervisor** (Gap 3): a
   `nx daemon service install --autostart` that writes the OS unit, on the same
   `service_registry.py` / installer substrate as t2/t3 (RDR-149). `nx init`
   offers to register it: **prompt, default yes**; `--yes` accepts
   non-interactively, `--no-autostart` skips. No silent system-unit write.
3. **Drop the vestigial T2-daemon step** from the install path and the README.
   In the default (all-SERVICE) config, `nx init` does not register the SQLite
   T2 daemon. (Pending RDR-158 completion the SQLite path may still exist as a
   rollback; if so, the T2 daemon stays available as an explicit opt-in command,
   never an install step.)
4. **Deprecate `--service`** with a notice: it still works (back-compat for
   docs, muscle memory, and `nx guided-upgrade`, which shares
   `provision_and_start_service()`), but prints "now the default; flag is
   deprecated."
5. **Trim the README quick-start** to the genuinely-required core:
   `uv tool install conexus` → `nx init` → `nx index`. `nx doctor` and
   `nx search` become optional "verify / try it" lines.

Target end state: `uv tool install conexus && nx init && nx index`.

## Approach (numbered, for phase-review cross-walk)

1. **Mode detection in `nx init`** with explicit precedence (gate-locked,
   critic SIG-1): `NX_LOCAL` is orthogonal and wins over `service_url`.
   `NX_LOCAL=1` → force local; `NX_LOCAL=0` → force managed; otherwise dispatch
   on `get_credential("service_url")` (env `NX_SERVICE_URL` or config.yml) — NOT
   `is_local_mode()`, which is `service_url`-blind across 57 callers (RF-5). This
   preserves the migration/rollback-rehearsal pattern (`NX_LOCAL=1 nx init` with a
   stale `service_url` in config still provisions local). Tests: each precedence
   branch; managed vs local dispatch. Also **remove the `_auto_service`
   side-channel** (`init.py:641-644`): the new default (plain local `nx init`
   always provisions) subsumes the `NX_STORAGE_BACKEND=service` re-provision path,
   so that silent second path is dead weight (RF-1 risk, OBS-2).
2. **Managed path**: fold the RDR-166 credential wizard + `nx service probe`
   into `nx init` when managed. Test: missing-creds prompt; probe success/failure
   exit codes.
3. **Local path**: `nx init` (no flag) calls `provision_and_start_service()`,
   which forces bge-768 (RDR-160). **Embedder-picker disposition** (critic
   OBS-1): RDR-144's interactive 384-vs-768 picker (`init.py:678-720`) is no
   longer reached on the service path. Since T3 serving is service-only, decide at
   accept whether to (a) remove the picker as deferred cleanup, or (b) keep it
   reachable solely behind an explicit non-service escape hatch. Default
   recommendation: remove it (no non-service local T3 path survives RDR-155/158);
   `--embedder` stays only as the bge/minilm selector the service embedder step
   already honors. Do not leave it as silent dead code. Test: local dispatch
   reaches provisioning (mock the heavy steps).
4. **Service-supervisor autostart**: new `nx daemon service install --autostart`
   on the RDR-149 installer substrate; `nx init` prompts (default yes) / `--yes` /
   `--no-autostart`. Two service-specific deltas, **re-scoped 2026-06-28 after
   implementation-time verification** (decision `nexus-423yt.1`; evidence T2
   `nexus/rdr-174-p2-section4-finding`). The original gate text (critic SIG-2 /
   SIG-3) assumed an external Postgres to order against; the codebase shows the
   supervisor self-manages an nx-owned PG cluster, so both deltas are lighter than
   the gate text:
   - **PG boot-ordering — VERIFIED NO-OP.** The supervisor STARTS its own
     nx-owned PG cluster as step 1 of startup (`storage_service_daemon.py`
     `_start_locked` → `_ensure_pg_running` → `_start_cluster`), with boot-safe
     binary discovery from the config dir (`pg_provision.discover_pg_binaries`, no
     provisioning-time env required). There is NO external `postgresql.service`
     cluster in any mode (host-binaries mode still starts nx's own PG_DATA/port).
     `After=postgresql.service` would order against a unit that does not manage
     nx's cluster, and `Wants=` fails on hosts without distro PG; a macOS PG
     readiness wrapper would poll a PG nothing external starts. Both wrong. The
     only real boot need is `After=network.target` (already present in
     `nexus-service.service`). Honest implementation: a template comment recording
     that the supervisor owns its PG lifecycle, plus a boot-robustness regression
     test asserting the supervisor self-starts PG regardless of env. (`nexus-exfns`)
   - **Supervisor handoff (no double-spawn) — lean on the existing lease.**
     `ensure_storage_supervisor` is already lease-gated via `service_registry`
     (RDR-149 TTL/election; P2.1 critic OBS-2): once the autostart unit's
     `RunAtLoad`/`--now` start publishes a lease, a later `ensure_storage_supervisor`
     discovers it and exits, so there is no double-spawn today. Scope is therefore
     a verify-existing-arbitration integration test (exactly ONE lease after
     install-with-autostart) plus a `nx init` handoff-poll branch (poll for the
     lease after install instead of spawning a second supervisor). Do NOT add a
     parallel arbiter — the RDR-149 lifecycle gate (`test_lifecycle_gate.py`) bans
     a bespoke lock/election outside `service_registry`. (`nexus-1brzs`)
   Tests: boot-robustness (supervisor self-starts PG regardless of env); prompt
   honored; skip honored; no double-supervisor (exactly one lease after install).

   > **SUPERSEDED 2026-06-28 by RDR-175 (Phase 1 Step 3).** The
   > supervisor-handoff and autostart-ordering text in this item (the original
   > P2.3/P2.4 scope) is superseded by RDR-175 "OS-Init as the Single Process
   > Watchdog". RDR-175 Phase 1 retires the in-process respawn mechanism (the
   > verified double-spawn root cause) and adds heal-on-next-use hardening for
   > the no-autostart path. The decide-autostart-first init ordering rework is
   > DEFERRED to follow-up bead `nexus-shkww` (RDR-174 P2.4, the autostart
   > prompt in `nx init` it would reorder, was never implemented). The
   > PG-boot-ordering no-op
   > finding above (`nexus-exfns`) is unaffected and still stands. This RDR keeps
   > `status: accepted`; only this in-body breadcrumb is added so the gate-locked
   > §4 handoff text is not left silently stale. See
   > `docs/rdr/rdr-175-os-init-single-process-watchdog.md`.
5. **`--service` deprecation notice** (still functional). Test: notice emitted,
   behavior unchanged.
6. **Remove the T2-daemon step** from `nx init` and the README; confirm default
   config needs no SQLite T2 daemon. Test: default init does not write a T2 unit.
7. **README + getting-started + cli-reference** updated to the collapsed flow and
   the new `nx daemon service install --autostart`.

## Alternatives Considered

- **README-only fix** (drop the vestigial T2 step, trim quick-start): closes
  Gap 2's symptom but leaves Gaps 1 and 3 (two init models, no reboot
  persistence). Rejected as half-measure; kept as the fallback if the lifecycle
  work must wait.
- **Keep `nx init` / `nx init --service` split**: lower blast radius but
  perpetuates the two-mental-models confusion that prompted this RDR.
- **Auto-register autostart silently** (no prompt): simplest UX but writes a
  system unit without consent; rejected per the brainstorming decision.

## Open Questions

1. **RESOLVED (RF-5).** `is_local_mode()` is service-URL-blind (57 call sites).
   Scope: `nx init` dispatches on `get_credential("service_url")`, NOT
   `is_local_mode()`. The global `is_local_mode()` blindness is filed as a
   separate bead, out of scope for this RDR.
2. **OPEN — RDR-158 interaction**: RDR-174 only *demotes* `nx daemon t2 install`
   from the install path (needs no gate; default config never uses it). Full
   *deletion* of `nx daemon t2 *` is RDR-158 P4 (two-release window). Confirm at
   gate that demotion-now / delete-at-P4 is the agreed sequence.
3. **RESOLVED (RF-3).** Reboot-persistence is a direct launchd/systemd unit via
   `installer.py` (`_render_for_service` + service constants + templates), same
   substrate as t2/t3. The supervisor's `start --foreground` contract is already
   unit-amenable. No registry-relaunch indirection needed.
4. **RESOLVED (RF-7).** Not world-blocked. The luxe6 boundary blocks releasing,
   not implementing; RDR-174 touches only init CLI flow, a new OS unit, a
   deprecation notice, and docs (release-prep scope). Implement-now.

## Consequences

- **Positive**: install collapses to three commands; the documented steps match
  the actual default topology; the substrate that serves every tier finally
  survives a reboot.
- **Negative / risk**: `nx init` becomes a higher-traffic command with a
  behavioral change; needs strong tests for mode dispatch and the autostart
  prompt. Touches the service lifecycle (RDR-152 territory) — coordinate with the
  cutover state.

## Research findings

Codebase-deep-analyzer pass, 2026-06-28, file:line-grounded.

**RF-1 — Current `nx init` dispatch** (`src/nexus/commands/init.py`). `init_cmd`
at line 627; options `--embedder` (603), `--yes` (609), `--service` (613-626).
Dispatch: `_auto_service` side-channel (641-644) fires service provisioning when
`NX_STORAGE_BACKEND` contains "service" even without `--service`; `if
provision_service or _auto_service:` (645-668) calls `provision_and_start_service`,
cloud returns `None` early (661-664); plain `nx init` cloud-mode (672-676) prints
Voyage lines and returns; plain `nx init` local-mode (678-720) runs the embedder
picker only — **no PG/service start**, so T3 is unusable after plain `nx init`.
Confirms Gap 1. RISK: `_auto_service` becomes dead weight once plain `nx init`
always provisions in local mode — remove it, don't leave a silent second path.

**RF-2 — `provision_and_start_service()`** (`init.py:567-599`) is the shared
local-init body: `_provision_postgres_step` → `is_local_mode` early-return for
cloud (585) → `_provision_service_embedder_step` (bge-768 lock + ONNX fetch,
fail-loud) → `_ensure_service_binary_step` (acquire signed binary) →
`_start_service_step` (`ensure_storage_supervisor`). Reused by guided-upgrade
(`migration/guided_upgrade.py:417-420`, `_default_serve`). Every step idempotent
→ safe to call from a default `nx init`.

**RF-3 — No service autostart; substrate to reuse** (`commands/daemon.py`).
`nx daemon service` has only start (1567) / install-binary (1649) / stop (1738) /
status (1938) — **no `install --autostart`**. Gap 3 verified. T2's
`install --autostart` (1405-1455) wraps `nexus.daemon.installer.install_autostart`
(`installer.py:136-212`) using shared `_autostart_install_dir` /
`_render_template` / `_resolve_nx_bin` / `_activate_cmd`. The supervisor runs via
`nx daemon service start --foreground` (1618-1624) → `run_storage_supervisor`,
blocking until SIGTERM — identical contract to `t3 start --foreground`, so it is
fully amenable to a launchd/systemd unit. A new service autostart should add:
`_render_for_service()` in installer.py (mirror `_render_for_t2` at 93-115),
constants `_SERVICE_PLIST_NAME`/`_SERVICE_LAUNCHD_LABEL` (mirror 57-59), and
templates `com.nexus.service.plist` + `nexus-service.service` in
`_resources/daemon/`. Go through installer.py (in-process callable), not the T3
inline pattern (tech debt).

**RF-4 — T2 default = SERVICE** (`db/storage_mode.py:122-180`).
`storage_backend_for` hard-defaults SERVICE for all stores except `t1` (which
returns SQLITE as a "local marker" but routes to Chroma `T1Database`, not
`memory.db`). In default config (no `NX_STORAGE_BACKEND*`), **no T2 store resolves
to SQLite**, so `nx daemon t2 install` is fully vestigial — only reachable via an
explicit `NX_STORAGE_BACKEND[_<store>]=sqlite` opt-out. Confirms Gap 2.

**RF-5 — Mode detection: use `get_credential("service_url")`, not
`is_local_mode()`** (`config.py:562-578`). `is_local_mode()` keys only on
chroma+voyage creds and ignores `NX_SERVICE_URL`/`service_url`, so a managed user
is mis-detected as local (would wrongly hit the embedder picker / local
provisioning). It has **57 call sites** — do NOT mass-patch it for this RDR.
`get_credential("service_url")` (reads `NX_SERVICE_URL` env or config.yml
`service_url`) is the correct, scoped dispatch key for `init_cmd`. The
`is_local_mode()` guard inside `provision_and_start_service` (585) stays as
defense-in-depth; the managed path must exit before reaching it. (Open Question 1
resolved: scope the fix to init, file the global `is_local_mode()` question
separately — already a bead.)

**RF-6 — RDR-158 interaction** (`docs/rdr/rdr-158-*`, status accepted). PG service
becomes the only T2 backend; SQLite source deleted in P4 (same N+1 window as
RDR-155 P4b). P3 (remove `=sqlite`, hard-error) is luxe6-gated; P4 (deletion) is
two-release-window-gated. **Demoting** the T2-daemon step from the install path
needs no gate (default config already never uses it); **deleting** `nx daemon t2 *`
waits for RDR-158 P4. The two moves are independent — RDR-174 does the demotion
now.

**RF-7 — NOT world-blocked.** The luxe6 boundary blocks *releasing*, not
*implementing* (RDR-173's service-substrate arc was built on this branch under the
same freeze). RDR-174 touches only init CLI flow, a new OS-unit template + install
subcommand, a `--service` deprecation notice, and docs — no T3 cutover, no SQLite
deletion, no schema/migration. It is release-prep scope, squarely buildable on the
6.0.0 branch. (Open Question 4 resolved: implement-now, not post-cutover.)

**Net effect on the draft:** all three gaps confirmed; Open Questions 1 and 4
resolved (scoped service_url dispatch; not world-blocked). Remaining for gate:
Open Question 2 (sequence T2-daemon *deletion* vs RDR-158 P4 — RDR-174 only
demotes) and Open Question 3 (autostart ownership — RF-3 answers it: a direct
launchd/systemd unit via installer.py, same as t2/t3). New risk to carry: remove
the `_auto_service` side-channel when unifying.
