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
related_issues: []
---

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

## Proposed Solution

### Approach

Three fixes, one per gap, layered so any one alone reduces harm:

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
- (−) Endpoint resolution gains a branch (config-override vs pid-file precedence) — must not steal remote/managed deployments.

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

### Phase 2: Operational Activation

No new persistent resources. Optionally have `nx mineru start` log/record its live
port. Document the resolution precedence in `nx mineru` help.

### Day 2 Operations

| Resource | List | Info | Delete | Verify | Backup |
| --- | --- | --- | --- | --- | --- |
| MinerU pid file | `nx mineru status` | `nx mineru status` | `nx mineru stop` | resolver logs chosen source | N/A (ephemeral) |

### New Dependencies

None — composes existing `config` + `_mineru_pid` + `pdf_extractor`.
