# Post-Mortem — RDR-148: MinerU Endpoint Discovery + Subprocess-Fallback Resilience

**Closed:** 2026-06-24 · **Status:** closed (implemented) · **Epic:** nexus-vehin (folded `nexus-m26oq` + `nexus-yrlbd`)

## What shipped

Six gaps closing the "formula-PDF extraction silently degrades onto a broken in-process path" problem, shipped as one coherent arc (all edit the same `_mineru_run_subprocess` / `_extract_with_mineru` / `get_mineru_server_url` surface). Merged to `develop` in PR #1328.

- **Gap 1 (nexus-vehin.1)** — `get_mineru_server_url()` re-ordered: an explicit, non-default `pdf.mineru_server_url` now wins over live-pid auto-discovery (the CA-2 precedence inversion — pid-file-first was overriding operator intent). Heuristic limit documented (`:8010` pin still yields to a live pid).
- **Gap 2 (nexus-vehin.2)** — `_mineru_server_available()` rediscover-then-fail-loud: on a `/health` failure, exactly one rediscovery pass (re-resolve, which re-reads the pid file on the default-config path), then a single loud WARNING fallback decision rather than a silent degrade. Also catches `httpx.RemoteProtocolError` (a server dying mid-startup returns malformed HTTP).
- **Gap 3 (nexus-vehin.3)** — macOS spawn-guard: **closed-as-moot** by source analysis. The worker is a fresh-interpreter `subprocess.Popen([sys.executable, "-c", ...])`, not a multiprocessing-spawn child, so the `__main__`-guard hazard is categorically inapplicable. No code change; structural regression guard added. Residual MinerU-internal multiprocessing mode is CA-3/CA-4 deferred (needs model weights).
- **Gap 4 (nexus-vehin.4)** — the two long-lived `mineru-api` server spawns (`nx mineru start`, `_restart_mineru_server`) route through `open_child_log_or_devnull`; the short-lived per-batch worker keeps DEVNULL as a documented, judged carve-out.
- **Gap 5 (nexus-m26oq)** — `MineruMemoryError(RuntimeError)` + `_MINERU_OOM_EXIT=42` sentinel + 3-way OOM classification; `on_formula_oom={fail|docling}` threaded through `extract()` and the streaming pipeline plus the `--on-formula-oom` CLI flag. A single OOM page degrades to docling only when opted in; the default `fail` re-raises (no silent formula fallback).
- **Gap 6 (nexus-yrlbd)** — config `mineru_memory_ceiling_mb` (default 0) + `mineru_page_timeout_s` (default 180); Linux-gated `RLIMIT_AS` ceiling via `Popen(preexec_fn)`; per-page timeout scaled by range; `batch//2` recursive bisection ladder replacing the straight-to-1-page retry.

## What went well

- **The stacked-reviewer gate earned its keep on every gap.** Each gap passed code-review-expert + substantive-critic; both caught real defects green tests missed: the no-op patch target (Gap 1), the uncaught `RemoteProtocolError` (Gap 2), the `_extract_page_via_docling` UnboundLocalError-on-`insert_pdf`-failure (Gap 5), and the `end=None` whole-doc timeout under-budget + the macOS ceiling silent-no-op (Gap 6). The critic also flagged the carve-out comment overclaiming Gap-5 coverage before m26oq had landed.
- **The gate critic's cross-gap finding shaped the design.** It caught that an `RLIMIT_AS` breach exits the worker via an in-process `MemoryError` (a normal non-zero code), NOT `-9` — so the original `-9`-only mapping would have missed Gap 6's ceiling breaches entirely. That drove the 3-way classification (SIGKILL OR sentinel OR ceiling-applied) and the worker's `MemoryError -> os._exit(sentinel)` catch.
- **VERIFY-FIRST on Gap 3 avoided speculative hardening.** Source analysis showed the originally-diagnosed hazard was already moot under the refactored worker; per the no-preventive-scope-beyond-evidence discipline, no speculative multiprocessing guard was added (which would have been untested surface), and the residual mode was explicitly CA-deferred.
- **The recursive bisection ladder (Gap 6) subsumed the prior retry helper.** It delivered `batch//2` AND eliminated the duplicate degrade path the critic flagged, replacing `_run_page_or_degrade` with a single degrade site.

## What we learned

- **An uncommitted working-tree edit passed locally and failed CI.** A Gap 6 review-fix reworded a source comment and updated the matching test assertion, but that test file was never staged into the commit. The local full suite (11137 passed) ran against the working tree *with* the fix; CI ran the committed version *without* it and failed on `test_worker_subprocess_keeps_devnull_carveout`. Lesson: when a review-fix edits a test file, run `git status` and confirm the file is staged before declaring the suite green or pushing. The full CI matrix caught what the local run could not — because the local run was testing different bytes than the commit.
- **Instance state beat signature churn for threading the page count.** Gap 6's whole-doc timeout needs `total_pages`, which lives in `_extract_with_mineru` but is consumed in `_mineru_run_subprocess`. Threading it as a parameter broke every test that mocked `_mineru_run_isolated` with a 3-arg signature; an instance field (`_mineru_run_total_pages`, consistent with the existing `_mineru_ceiling_applied` pattern) set once per extraction was the least-disruptive correct fix, and avoided re-opening the PDF.
- **`RLIMIT_AS` is virtual address space, not RSS.** PyTorch/MinerU mmap weights aggressively, so a ceiling set near physical RAM produces spurious OOMs; the config knob documents the 3-5x caveat. And the cap must be Linux-gated: darwin raises `ValueError` on `setrlimit(RLIMIT_AS)` and does not enforce it (an ungated preexec crashes darwin), so a configured-but-unenforceable ceiling logs a WARNING rather than silently no-op'ing.

## Deferred / follow-on

- **Residual MinerU-internal multiprocessing mode (Gap 3)** — if `do_parse` itself spawns multiprocessing-spawn children, the unguarded `-c` `__main__` could re-trigger an analogous hazard. Reproduction needs model weights (CA-3/CA-4 deferred, not run casually on a dev host). Documented in the RDR (Spike Result 3) and bead notes, not silent.
- **Live Python→worker OOM-diagnostic capture** — the per-batch worker keeps DEVNULL (returncode-covered); capturing its stderr for richer OOM diagnostics was noted as a possible future revisit, not built.
- No deferred items within the six gaps themselves; the broader `nexus-cgu27` epic (async/queue hardening) continues independently.
