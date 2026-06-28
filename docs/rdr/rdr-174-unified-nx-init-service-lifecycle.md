---
title: "Unified nx init and Service-Supervisor Lifecycle: Collapse the Install to One Command and Close the Reboot-Persistence Gap"
id: RDR-174
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-28
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

1. **Mode detection in `nx init`** keyed on service-URL presence (NOT
   `is_local_mode()`, which has the open `service_url`-blind bug — see Open
   Questions). Tests: managed path vs local path dispatch.
2. **Managed path**: fold the RDR-166 credential wizard + `nx service probe`
   into `nx init` when managed. Test: missing-creds prompt; probe success/failure
   exit codes.
3. **Local path**: `nx init` (no flag) calls `provision_and_start_service()`.
   Test: local dispatch reaches provisioning (mock the heavy steps).
4. **Service-supervisor autostart**: new `nx daemon service install --autostart`
   on the RDR-149 installer substrate; `nx init` prompts (default yes) / `--yes` /
   `--no-autostart`. Tests: unit written; prompt honored; skip honored.
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

1. **`is_local_mode()` is service-URL-blind** (filed bead, this session): a
   managed user with `service_url` but no chroma/voyage key is mis-detected as
   local across ~15 call sites. `nx init` must use an authoritative
   service-mode gate (`storage_mode()` / `NX_SERVICE_URL` presence), and this RDR
   should decide whether to fix `is_local_mode()` itself or formally deprecate it
   in favor of `storage_mode()`/`is_vector_service_mode()`.
2. **RDR-158 interaction**: if the SQLite T2 backend is fully retired, the T2
   daemon commands (`nx daemon t2 *`) may be removable entirely, not merely
   demoted from the install path. Sequence against RDR-158.
3. **Reboot-persistence mechanism**: does the service supervisor get a direct
   launchd/systemd unit, or does an existing always-on daemon (e.g. the
   service-registry lease owner) relaunch it? Decide the ownership before
   implementing Gap 3.
4. **World-blocked?** This touches the RDR-152/155 service substrate. Confirm
   whether the implementation is pre- or post-cutover; if post-cutover, this RDR
   is design-now / implement-after-cutover like RDR-173's daemon arc.

## Consequences

- **Positive**: install collapses to three commands; the documented steps match
  the actual default topology; the substrate that serves every tier finally
  survives a reboot.
- **Negative / risk**: `nx init` becomes a higher-traffic command with a
  behavioral change; needs strong tests for mode dispatch and the autostart
  prompt. Touches the service lifecycle (RDR-152 territory) — coordinate with the
  cutover state.

## Research findings

(To be added during `/conexus:rdr-research`. Codebase evidence already gathered
this session: `storage_mode.py` default-SERVICE flip; `nx daemon service` lacks
`install --autostart`; `provision_and_start_service()` is the shared local-init
body reused by guided-upgrade; `is_local_mode()` service-URL-blindness.)
