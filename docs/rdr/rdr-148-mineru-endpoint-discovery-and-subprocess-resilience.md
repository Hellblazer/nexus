---
title: "MinerU endpoint discovery + subprocess-fallback resilience: stop formula-PDF extraction from silently degrading onto a broken in-process path"
id: RDR-148
type: Bug Fix
status: draft
priority: high
author: Hal Hildebrand
reviewed-by: self
created: 2026-06-03
accepted_date:
related_issues: [nexus-m26oq, nexus-yrlbd]
related: [RDR-044, RDR-046, RDR-147]
---

> **Scope consolidation (2026-06-24).** This RDR is the design home for the whole
> "MinerU subprocess path robustness" problem. Two previously-standalone beads were
> folded in after a relevancy review confirmed PDF extraction stays client-side
> (RDR-152 §Approach: "Chunking / extraction … Keep (client-side; feeds
> upsert-chunks)") and is not obsoleted by the PG/pgvector/native-install work:
> **`nexus-m26oq`** (per-page `-9` OOM → optional degrade-to-docling) and
> **`nexus-yrlbd`** (memory ceiling + per-page timeout + adaptive `batch//2`).
> They are the *resource-resilience* axis (Gaps 5–6 below); the original four gaps
> are the *discovery / correctness* axis. All edit the same
> `_mineru_run_subprocess` / `_extract_with_mineru` code, so they ship as one
> coherent arc rather than three fragmented edits to the same ~60 lines.

# RDR-148: MinerU endpoint discovery + subprocess-fallback resilience

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

Formula-bearing PDFs fail to extract with a misleading hard error, despite a
healthy MinerU server running. Observed (Self-RAG, arXiv 2310.11511):

```
RuntimeError: PDF "...Self-RAG..." contains formulas (detected 6) but MinerU
extraction failed: RuntimeError: MinerU subprocess exited with code 1
(pages 2-3). To bypass ... rerun with `--extractor docling`.
```

The MinerU server was running and healthy on port 53947 the entire time. The
failure is a two-layer cascade rooted in a stale endpoint and a broken fallback.

### Enumerated gaps to close

#### Gap 1: Endpoint discovery split-brain — extractor trusts stale static config, not the live pid file

The HTTP extraction path selects the MinerU endpoint from the static config
value `pdf.mineru_server_url` via `get_mineru_server_url()`
(`pdf_extractor.py:740` health check `{url}/health`, `:774` parse `{url}/file_parse`).
The live server's actual address is recorded in the pid file
(`~/.config/nexus/mineru.pid` → `{pid, port, ...}`), read by
`nexus._mineru_pid.read_pid_file()` and surfaced by `nx mineru status` — but
that pid file is **not consulted for endpoint selection**. The server binds an
ephemeral port on start; the config is not reconciled with it. **Verified**:
config held `:62485` (HTTP 000, connection refused) while the pid file and
`nx mineru status` both reported the live, healthy `:53947` (HTTP 200). The two
sources had disagreed for days.

#### Gap 2: Silent degradation — unreachable configured server falls to the subprocess path with no rediscovery and no loud failure

When the configured `/health` probe fails (because the URL is stale, not because
the server is down), extraction silently falls back to the in-process MinerU
subprocess path — the path `nx doctor` already labels "OOM-risk". It does not
first attempt pid-file rediscovery of a live server, and it does not fail loudly;
the user sees only the downstream subprocess crash, not the root "configured
endpoint unreachable" condition.

#### Gap 3: In-process subprocess fallback is broken on macOS — missing multiprocessing guard

The in-process fallback runs MinerU `do_parse` (`pdf_extractor.py:45,49`) in a
per-page-range subprocess (intentional, to isolate the OOM that accumulates
across in-process calls — `:597-600`). On macOS (spawn start method) the MinerU
formula/model-inference workers fail without an `if __name__ == "__main__":`
multiprocessing guard, exiting code 1 on the formula pages. **Verified
(corroborating)**: the failing run emitted
`resource_tracker: There appear to be 1 leaked semaphore objects to clean up at
shutdown` — a multiprocessing-without-guard signature. So even when the fallback
is correctly chosen, it cannot extract formula PDFs on darwin/arm64.

#### Gap 4: mineru-api server is spawned with stdout/stderr → DEVNULL

`nx mineru start` (`src/nexus/commands/mineru.py:199`) launches the long-lived
`mineru-api` server with both streams DEVNULL'd, so a server crash or startup
failure leaves no record — the same silent-death class nexus-ovbr7 fixed for
the storage-service/t3 daemons (2026-06-11). When this RDR's resilience work
lands, the spawn should route output through
`nexus.logging_setup.open_child_log("mineru_api", ...)` per the standing rule
in `src/nexus/daemon/AGENTS.md` ("no daemon child is ever silent"). **Status
(2026-06-24): still open** — `commands/mineru.py:199-200` still spawns with both
streams `DEVNULL`.

#### Gap 5: Single-page subprocess OOM (`-9`) fails the whole document instead of degrading (was `nexus-m26oq`)

Root-caused 2026-05-30 (arXiv:2605.13379, page 31): a single page reproducibly
OOM/jetsam-SIGKILLs (`-9`) MinerU's formula-recognition (MFR) model, even on an
idle machine with 92% RAM free, run in isolation — so it is page-content-specific
(a formula MFR cannot bound), not transient pressure. `batch_size` is already at
the 1-page floor, so the existing OOM mitigation (shrink batch) has no headroom
and `span <= 1` re-raises immediately → whole-document failure. The `-9` lands as
an *opaque* `RuntimeError` (`_mineru_run_subprocess`, current code
`pdf_extractor.py:1005-1017`), indistinguishable from any other subprocess
failure. **Evidence re-validated 2026-05-31** (post the `oa7r`/`h1jk` endpoint
fixes, in isolation) → a genuine MFR OOM, not a split-brain artifact. Proposal:
make the `-9`/SIGKILL-class exit a *catchable* memory-error type, and when a
single-page subprocess raises it, optionally degrade THAT page to docling
(formula-stripped) and continue, instead of failing the whole document. Opt-in
(`--on-formula-oom=docling|fail`, default `fail`) to preserve the
no-silent-fallback-for-formulas guarantee by default.

#### Gap 6: No memory ceiling or per-page timeout before the subprocess spawn (was `nexus-yrlbd`)

MinerU extraction relies entirely on the OS OOM killer: no `RLIMIT_AS`/cgroup
ceiling is set before spawning the worker (`pdf_extractor.py:970` `Popen`), and
there is only a batch-level 180s timeout (`:997`), no per-page timeout. A `-9`
SIGKILL then triggers a 1-page retry that can OOM again, cascading 3–5× before
resolution. Proposal: an explicit, configurable memory ceiling before spawn
(`RLIMIT_AS` via `preexec_fn`, **gated to Linux** — `RLIMIT_AS` is unreliable for
torch's large virtual arenas on darwin, which keeps the OS-OOM-killer fallback),
a per-page timeout within the batch loop, and an adaptive `batch//2` reduction
before falling to 1-page. Exceeding the ceiling must surface as the *same
catchable memory-error type* Gap 5 keys on — that shared type is the coordination
seam between the two gaps.

### Reconciliation with already-shipped fixes (2026-06-24)

The original Gaps 1–2 were partially addressed by two commits that predate this
consolidation:

- **`nexus-oa7r`** (`1de1ce69`, 2026-05-11): stopped `nx mineru start` writing the
  ephemeral bound port into the persistent config — so the config no longer *rots*
  with a dead port. But the *extractor* still selects the endpoint from
  `get_mineru_server_url()` (config) at `pdf_extractor.py:741`/`:776`, **not** the
  live pid file. Gap 1's Approach #1 (pid-file precedence in the resolver) is
  **still unimplemented**.
- **`nexus-h1jk`** (`49aa05e8`, 2026-05-11): added warn-on-fallback + a
  server-unreachable surface in `nx doctor`. This is a partial Gap 2 mitigation
  (the fallback is no longer fully silent), but the rediscover-then-fail-loud
  policy (Approach #2) is **not** in place.

Net: Gaps 1, 3, 4 are open; Gap 2 is partially mitigated; Gaps 5–6 are new. The
implementation plan below sequences all of them.

## Context

### Background

Discovered while indexing 6 RAG research papers into T3 (DT-sourced) for RDR-147.
Five extracted cleanly via the MinerU server; Self-RAG failed and only succeeded
under `--extractor docling` (formula-stripped). Root-causing the single failure
exposed the cascade above. Two complementary halves: the endpoint split-brain
(why the subprocess path ran at all) and the multiprocessing-guard crash (why the
subprocess then died) — the latter found by a `conexus:debugger` agent. Full
finding in T2 `nexus/mineru-formula-extraction-failure-root-cause-2026-06-03`.

### Technical Environment

- Platform: darwin/arm64. nexus 5.9.2.
- `src/nexus/pdf_extractor.py` — MinerU HTTP path (`get_mineru_server_url()` at
  `:740`/`:774`), in-process `do_parse` subprocess path (`:45,49,597-657`,
  OOM-retry at 1-page granularity), `read_pid_file` import (`:845-853`, used for
  lifecycle/auto-start, NOT endpoint selection).
- `src/nexus/_mineru_pid.py` — `read_pid_file()` (`:26`), `is_process_alive()`
  (`:37`), `_pid_file_path()` → `<config_dir>/mineru.pid` (`:20`).
- `src/nexus/config.py` — `get_mineru_server_url()` returns the static
  `pdf.mineru_server_url` config value.
- `src/nexus/commands/mineru.py` — `start` / `stop` / `status`; `status` reads
  the live pid file; `start` does not reconcile the config URL with the bound port.

## Research Findings

### Investigation

Captured config vs pid-file disagreement; probed both ports with `curl`; read the
extractor's endpoint-selection and fallback code; confirmed the multiprocessing
signature from the failing run's stderr.

#### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `nexus.pdf_extractor` | Yes | Endpoint chosen from `get_mineru_server_url()` (config) at `:740`/`:774`; `read_pid_file` imported at `:845` but not used to pick the endpoint; subprocess `do_parse` per page-range with 1-page OOM-retry. |
| `nexus._mineru_pid` | Yes | `read_pid_file()` returns `{pid, port, started_at, output_root}`; `is_process_alive(pid)` available — sufficient to resolve and validate a live endpoint. |
| `nexus.config.get_mineru_server_url` | Yes | Returns static config; no pid-file awareness. |
| MinerU `do_parse` (macOS spawn) | Docs/Spike | Multiprocessing workers require a `__main__` guard under spawn; absence → exit 1 + leaked-semaphore warning. |

### Key Discoveries

- **Verified** — config `:62485` dead, pid-file/live server `:53947` healthy; the
  extractor used the dead config endpoint.
- **Verified** — endpoint selection (`:740`/`:774`) reads config, not the pid file.
- **Verified (corroborating)** — leaked-semaphore warning ⇒ multiprocessing guard
  missing in the subprocess fallback.
- **Verified (fix probe)** — after `nx config set pdf.mineru_server_url
  http://127.0.0.1:53947`, the configured endpoint returns HTTP 200 and Self-RAG
  re-extracts via the server (the immediate stopgap, applied 2026-06-03).

### Critical Assumptions

- [ ] The pid file is the authoritative live endpoint whenever the recorded pid is
  alive — **Status**: Verified — **Method**: Spike (`nx mineru status` + curl).
- [ ] Resolving the endpoint from the pid file in the extractor does not break the
  CI/server-managed deployments that legitimately set `pdf.mineru_server_url`
  (remote MinerU) — **Status**: Unverified — **Method**: Source Search.
- [ ] A `__main__`-guarded subprocess entry fixes the macOS spawn crash without
  regressing the OOM-isolation behavior — **Status**: Unverified — **Method**: Spike.
- [ ] The page-31 `-9` is a content-specific MFR-model OOM that reproduces on the
  current MinerU (server OR subprocess), not a stale artifact of the pre-`oa7r`
  endpoint split-brain — **Status**: Verified (2026-05-31, isolation re-run) —
  **Method**: Spike. Re-confirm on the current MinerU version at implementation.
- [ ] `RLIMIT_AS` is unsuitable as a hard memory ceiling on darwin (torch maps
  large virtual arenas; the cap either spuriously kills or is ignored), so the
  ceiling is Linux-gated and darwin retains the OS-OOM-killer + degrade path —
  **Status**: Unverified — **Method**: Spike.

## Proposed Solution

### Approach

Fixes layered so any one alone reduces harm. Items 1–3 are the discovery /
correctness axis; 4–5 are the resource-resilience axis (folded-in beads). They
share one code region and one new catchable memory-error type (see item 5).

1. **Single-source endpoint resolution (Gap 1).** Make the extractor resolve the
   MinerU endpoint with precedence: explicit non-default config override →
   live pid file (when its pid is alive) → default config. So a server that
   restarts on a new port is followed automatically; an operator who *deliberately*
   points at a remote MinerU still wins.

2. **Rediscover-then-fail-loud, no silent OOM fallback (Gap 2).** When the
   resolved endpoint's `/health` fails, attempt pid-file rediscovery once; if a
   live server is found, use it. Only if no live server exists do we consider the
   subprocess path — and that decision is logged at WARNING with the reason, not
   silent.

3. **Fix the macOS subprocess multiprocessing guard (Gap 3).** Ensure the
   per-page-range `do_parse` subprocess entry is invoked under a guarded
   entrypoint (explicit `multiprocessing` start-method handling / `__main__`
   guard / module-level worker function) so formula inference does not exit 1 on
   darwin spawn. Preserve the existing OOM-isolation + 1-page retry.

4. **Catchable OOM + optional per-page degrade-to-docling (Gap 5).** Convert a
   `-9`/SIGKILL-class subprocess exit into a distinguishable `MineruMemoryError`
   (subclass `RuntimeError`, so the existing `except RuntimeError` 1-page retry
   still catches it). Thread an `on_formula_oom={"fail"|"docling"}` option through
   `extract()` → `_extract_with_mineru`; when a *single-page* run raises
   `MineruMemoryError` and the mode is `docling`, degrade THAT page to docling
   (formula-stripped) and continue. Default `fail` re-raises the existing
   formula-aware error (no silent fallback). CLI flag `--on-formula-oom`.

5. **Memory ceiling + per-page timeout + adaptive batch reduction (Gap 6).** Set a
   configurable `RLIMIT_AS` ceiling via `Popen(preexec_fn=...)`, **Linux-gated**
   (darwin keeps the OS-OOM-killer; see Critical Assumption). New config knob
   `mineru_memory_ceiling_mb` (mirrors `get_mineru_page_batch`). Replace the bare
   batch-level `timeout=180` with a per-page budget. Insert a `batch//2` step
   between batch-failed and the 1-page drop. A ceiling breach surfaces as the same
   `MineruMemoryError` item 4 keys on. **Depends on item 4** (the shared error
   type), so author 4 first within the arc even though the bead graph records
   `yrlbd` (Gap 6) depending on `m26oq` (Gap 5).

### Technical Design

**Endpoint resolver** (illustrative — verify signatures):

```text
// resolve_mineru_endpoint() -> str | None
//   1. if config pdf.mineru_server_url is set AND != built-in default -> return it
//      (operator intent: remote/managed MinerU)
//   2. info = _mineru_pid.read_pid_file(); if info and is_process_alive(info.pid):
//        return f"http://127.0.0.1:{info['port']}"
//   3. return config default (may be stale) — caller health-checks before use
```

Replace the direct `get_mineru_server_url()` reads at `pdf_extractor.py:740`/`:774`
with `resolve_mineru_endpoint()`. The `/health` probe stays; on failure, one
rediscovery pass (re-read the pid file in case the server restarted mid-run)
before any fallback. Endpoint strings continue to flow through
`_redact_url_credentials()` (existing) for log safety.

**Fallback policy.** Today: configured-unreachable → subprocess. New: resolved-
and-rediscovered-unreachable → subprocess **only if** the subprocess path is known
to work on this platform; otherwise raise the existing formula-aware error with
the `--extractor docling` guidance immediately (no silent OOM-risk attempt). Gate
by a capability check or a config flag, decided in the spike.

**Subprocess guard.** Audit how `do_parse` is launched (`pdf_extractor.py:597+`).
If it relies on implicit multiprocessing under spawn, route the worker through a
guarded module entrypoint or set `multiprocessing.set_start_method`/`get_context`
explicitly. Keep one subprocess per page-range and the 1-page OOM-retry.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| `resolve_mineru_endpoint()` | `config.get_mineru_server_url` + `_mineru_pid.read_pid_file` | Add: a thin resolver composing both; callers switch to it. |
| Rediscover-then-fail-loud | `pdf_extractor.py:740-778` health/parse path | Extend: one rediscovery pass + explicit fallback logging. |
| Subprocess guard | `pdf_extractor.py:597-657` do_parse subprocess | Fix: guard the worker entry; keep OOM-isolation. |
| `nx mineru start` writes live port to config | `commands/mineru.py` | Alternative/complement: optionally reconcile config on start (decide vs resolver). |

### Decision Rationale

Resolving from the pid file makes the live server the source of truth (matching
what `nx mineru status` already trusts), so port drift self-heals. Failing loud
instead of silently degrading turns a misleading subprocess crash into an
actionable "endpoint unreachable" message. Fixing the guard makes the fallback
real, so formula PDFs extract even with no server. Each is independently valuable;
together they close the cascade.

## Alternatives Considered

### Alternative 1: Just reconcile config on `nx mineru start`

**Description**: Have `start` write its bound port into `pdf.mineru_server_url`.

**Pros**: Minimal; keeps config the single read site.

**Cons**: Races (mid-run restart), and any path that starts MinerU out-of-band
(auto-spawn) still rots the config. Does not fix the broken subprocess fallback.

**Reason for rejection**: Partial; the resolver subsumes it and is restart-safe.
(May still adopt as a complement.)

### Briefly Rejected

- **Pin a fixed MinerU port**: brittle across environments; collides with port-0 testing.
- **Always use the subprocess path**: it's the broken, OOM-risk path — the opposite of the fix.

## Trade-offs

### Consequences

- (+) Formula PDFs extract reliably via the live server regardless of port drift.
- (+) Failures become actionable (loud "endpoint unreachable") instead of a cryptic subprocess exit.
- (+) The subprocess fallback actually works on macOS.
- (+) A single OOM-prone page degrades to formula-stripped docling (opt-in) instead of failing the whole document — and the OOM cascade is bounded by an explicit ceiling + `batch//2` instead of relying on the OS OOM killer.
- (−) Endpoint resolution gains a branch (config-override vs pid-file precedence) — must not steal remote/managed deployments.
- (−) `--on-formula-oom=docling` silently strips formulas from the degraded page; default `fail` preserves the no-silent-fallback guarantee, so the degrade is always an explicit operator opt-in.

### Risks and Mitigations

- **Risk**: Resolver hijacks a legitimate remote `pdf.mineru_server_url`.
  **Mitigation**: explicit non-default config override wins (precedence rule 1);
  cover with a test.
- **Risk**: Guard change regresses OOM isolation. **Mitigation**: keep per-page
  subprocess + 1-page retry; spike on a formula-dense PDF before merge.

### Failure Modes

- **Visible**: no live server + working guard → formula PDF extracts in-process (slower) or raises the docling-guidance error; logged.
- **Silent (eliminated)**: the current silent degrade onto the broken path is replaced by WARNING-logged, reasoned fallback.
- **Diagnosis**: `nx mineru status` (live port) vs `nx config get pdf.mineru_server_url`; the new resolver logs which source it chose.

## Implementation Plan

### Prerequisites

- [ ] Assumptions verified (remote-config precedence; guard fixes spawn without OOM regression).
- [ ] A formula-dense PDF fixture (Self-RAG arXiv 2310.11511) for regression.

### Minimum Viable Validation

With the config URL stale (pointing at a dead port) but a live MinerU server
running on a different port, indexing the Self-RAG PDF with `--extractor auto`
extracts formulas via the live server (resolver follows the pid file) — no
subprocess fallback, no crash. Separately, with NO server running, the same index
either extracts in-process (guard fixed) or fails loud with docling guidance — not
a silent exit-1.

### Phase 1: Code Implementation

#### Step 1: `resolve_mineru_endpoint()` + caller switch
Add the resolver; replace `get_mineru_server_url()` reads at the health/parse sites.

#### Step 2: Rediscover-then-fail-loud
One pid re-read on health failure; explicit WARNING-logged fallback decision.

#### Step 3: Subprocess multiprocessing guard
Guard the `do_parse` worker entry for macOS spawn; preserve OOM isolation + retry.

#### Step 4: Catchable OOM + per-page degrade-to-docling (Gap 5, `nexus-m26oq`)
Introduce `MineruMemoryError(RuntimeError)`; map the `-9`/SIGKILL-class exit in
`_mineru_run_subprocess` onto it. Thread `on_formula_oom` through `extract()` →
`_extract_with_mineru`; single-page `MineruMemoryError` + `docling` mode degrades
that page (default `fail` re-raises). Add `--on-formula-oom` CLI flag.

#### Step 5: Memory ceiling + per-page timeout + batch//2 (Gap 6, `nexus-yrlbd`)
`preexec_fn` `RLIMIT_AS` ceiling (Linux-gated; `_child_rlimit_preexec(ceiling)`
helper) + `mineru_memory_ceiling_mb` config knob; per-page timeout budget;
adaptive `batch//2` step before the 1-page drop. Ceiling breach surfaces as
`MineruMemoryError`.

Tests for Steps 4–5 are pure-Python / no-API-keys: patch
`nexus.pdf_extractor.subprocess.Popen` with a fake proc whose `wait()` returns
`-9` (assert catchable `MineruMemoryError` + `batch//2`→1-page cascade with exact
call counts) or raises `TimeoutExpired` (assert `_killpg_safe` + per-page budget);
assert `preexec_fn` is non-None on Linux / None on darwin (patch `sys.platform`).
Do NOT fork-and-OOM.

### Phase 2: Operational Activation

No new persistent resources. Optionally have `nx mineru start` log/record its live
port. Document the resolution precedence in `nx mineru` help.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| MinerU pid file | `nx mineru status` | `nx mineru status` | `nx mineru stop` | resolver logs chosen source | N/A (ephemeral) |

### New Dependencies

None — composes existing `config` + `_mineru_pid` + `pdf_extractor`.
