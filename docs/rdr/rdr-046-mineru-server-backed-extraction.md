---
title: "MinerU Server-Backed PDF Extraction"
id: RDR-046
type: Architecture
status: closed
closed_date: 2026-04-02
close_reason: implemented
priority: high
author: Hal Hildebrand
reviewed-by: self
accepted_date: 2026-04-02
created: 2026-04-02
related_issues:
  - "RDR-044 - Math-Aware PDF Extraction (closed)"
  - "nexus#122 - uv tool install drops [mineru] extra"
  - "nexus#123 - Noisy PostToolUse hook"
---

# RDR-046: MinerU Server-Backed PDF Extraction

> Revise during planning; lock at implementation.
> If wrong, abandon code and iterate RDR.

## Problem Statement

MinerU's subprocess-per-page extraction is functionally correct but operationally intractable. Processing 100+ equation-heavy Grossberg papers at 1 page per subprocess takes ~30s/page due to model initialization overhead repeated for every page. A 100-page paper takes 50 minutes. A corpus of 100 papers at ~30 pages average would take **40+ hours**.

The root cause is that MinerU loads 6 ML models (layout YOLO, formula YOLO, UniMERNet MFR, OCR det, OCR rec, table det) on every subprocess invocation. These models take ~5s to load. The actual per-page inference is 2-5s, meaning **33-70% of wall time is model init overhead** (5s out of 8-15s total per page).

We can't avoid subprocess isolation because:
1. MinerU leaks memory across in-process `do_parse` calls (models cached by `AtomModelSingleton` never unload)
2. MinerU's internal `ProcessPoolExecutor` workers keep pipes open, causing `subprocess.run` deadlocks
3. Even with `os._exit(0)`, MinerU's worker processes don't always terminate cleanly
4. On Apple Silicon unified memory, formula-dense pages (100+ regions) OOM at batch sizes > 1 page

## Context

### What RDR-044 delivered

RDR-044 shipped auto-detect math routing: Docling enriched pass (with `do_formula_enrichment=True`) detects formulas via FormulaItem count, then MinerU re-extracts when formulas are found. Note: the Docling pass uses full enrichment, not a "fast" unenriched scan — formula detection requires the enrichment pipeline. This works for individual papers but the subprocess-per-page MinerU approach doesn't scale to corpus-level indexing.

### Current architecture (post-RDR-044)

```
nx index pdf paper.pdf
  → Docling enriched pass → formula count
  → If formulas: spawn subprocess per page → do_parse(start=N, end=N+1)
  → Each subprocess: load 6 models (~5s) → extract 1 page (~3s) → os._exit(0)
  → Parent: read output files, merge markdown, count formulas
  → Upload chunks to T3
```

### OOM history (empirical, 2026-04-02)

| Batch size | Result |
|---|---|
| Full PDF (108 pages) | OOM at MFR 90% (batch 2, pages 65-108) |
| 40 pages | OOM at MFR batch |
| 10 pages | OOM at MFR (pages 80-90, 241 formula regions) |
| 5 pages | OOM on Self-Supervised ARTMAP pages 15-20 |
| 1 page | **No OOM** — 0.3% memory, all papers succeed |

### Performance at 1-page batches

| Paper | Pages | Time | Rate |
|---|---|---|---|
| Grossberg 2020 (31 pages) | 31 | ~8 min | ~15s/page |
| Self-Supervised ARTMAP (95 pages) | 95 | ~30 min | ~19s/page |
| Estimated 100-paper corpus (avg 30 pages) | 3000 | ~40 hrs | - |

## Proposed Solution

### Recommended: `mineru-api` persistent server

MinerU ships a built-in FastAPI server (`mineru-api`) that loads models once on startup and serves extraction requests via HTTP. Models remain resident in memory across requests — eliminating the 5s/page init overhead.

```
mineru-api (persistent process, models loaded once)
  ↕ HTTP
nx index pdf paper.pdf
  → POST /file_parse per page range
  → ~2-5s/page (inference only, no model init)
```

**Expected improvement**: 1.5-4x per-page throughput from eliminating model init (5s saved per page). Additional gains from increased batch sizes when serving multiple pages per HTTP request (fewer round-trips, amortized overhead). A 100-paper corpus drops from 40 hours to ~10-20 hours on per-page improvement alone, potentially further with batch-size tuning on non-formula-dense papers.

### Server configuration

```bash
MINERU_TABLE_ENABLE=false \
MINERU_PROCESSING_WINDOW_SIZE=8 \
MINERU_VIRTUAL_VRAM_SIZE=8192 \
MINERU_API_OUTPUT_ROOT=/tmp/mineru-output \
MINERU_API_TASK_RETENTION_SECONDS=300 \
mineru-api --host 127.0.0.1 --port 8010
```

Key environment variables:
- `MINERU_TABLE_ENABLE=false` — drops 2 table models, reduces memory from ~16GB to ~8GB peak. **Behavior change from current subprocess code** which uses `table_enable=True`. MinerU's table markdown rendering differs with tables disabled — tables may appear as plain text instead of pipe tables. This is an intentional trade-off: the Grossberg corpus is equation-dense, not table-dense, and Docling (the primary extractor) handles tables via TableFormer anyway. Papers routed to MinerU are routed because of formulas, not tables. For corpora where MinerU table extraction matters, this should be configurable via `nx config set pdf.mineru_table_enable=true`.
- `MINERU_PROCESSING_WINDOW_SIZE=8` — smaller sliding window for streaming
- `MINERU_API_MAX_CONCURRENT_REQUESTS` — **ignored on Mac** (hardcoded to 1); default 3 on Linux
- `MINERU_VIRTUAL_VRAM_SIZE=8192` — explicit VRAM limit in MB
- `MINERU_API_OUTPUT_ROOT` — temp dir for extraction artifacts (default `./output`)
- `MINERU_API_TASK_RETENTION_SECONDS=300` — clean up completed tasks after 5 min (default 24h)

### Client integration in `pdf_extractor.py`

Replace `_mineru_run_isolated` subprocess call with HTTP POST. **The server path reads exclusively from the JSON response body** — the filesystem output path documented in RF-11 is irrelevant for the client. Do not add fallback reads of `MINERU_API_OUTPUT_ROOT`; those files are managed by the server's retention/cleanup loop and may be stale or absent.

**Key behavioral differences from subprocess path:**
- `lang_list` must be explicitly `["en"]` — server defaults to `["ch"]` (Chinese OCR), which silently degrades quality on English scans
- `parse_method` must be explicitly `"auto"` — verified identical to `do_parse()` default, but passed explicitly for auditability
- `end_page_id` uses `99999` as sentinel for "all remaining pages" — the subprocess path uses `None`, converted here
- `content_list` and `middle_json` are returned as **JSON-encoded strings**, not parsed objects — `json.loads()` required

```python
def _mineru_run_via_server(self, pdf_path, start, end):
    resp = requests.post(
        f"{self._mineru_url}/file_parse",
        files=[("files", (pdf_path.name, pdf_path.open("rb"), "application/pdf"))],
        data={
            "backend": "pipeline",
            "start_page_id": start,
            "end_page_id": end if end is not None else 99999,
            "formula_enable": "true",
            "table_enable": str(self._mineru_table_enable).lower(),
            "return_md": "true",
            "return_middle_json": "true",
            "return_content_list": "true",
            "parse_method": "auto",
            "lang_list": '["en"]',
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    # Server normalizes filenames via normalize_upload_filename → normalize_task_stem.
    # Match by stem first; if miss, fall back to first (and usually only) result key.
    all_results = data.get("results", {})
    stem = pdf_path.stem
    results = all_results.get(stem)
    if results is None:
        if len(all_results) == 1:
            results = next(iter(all_results.values()))
        else:
            raise RuntimeError(
                f"Server results missing key {stem!r}; "
                f"available keys: {list(all_results.keys())}"
            )
    md = results.get("md_content", "")
    if not md:
        raise RuntimeError(
            f"Server returned empty md_content for {pdf_path.name}"
        )
    # content_list=None is acceptable (no structured blocks); middle_json=None
    # means no pdf_info, which breaks inline formula counting — log at warning.
    raw_cl = results.get("content_list")
    raw_mj = results.get("middle_json")
    if raw_mj is None:
        _log.warning("mineru_server_no_middle_json", path=str(pdf_path))
    content_list = json.loads(raw_cl) if raw_cl else []
    middle = json.loads(raw_mj) if raw_mj else {}
    return md, content_list, middle.get("pdf_info", [])
```

### Fallback: subprocess isolation (current)

When the server is not running, fall back to current subprocess-per-page approach. Server availability is checked **before each batch**, not just once per document — a server OOM-killed mid-extraction surfaces as `ConnectionRefusedError`, not HTTP 409.

```python
def _mineru_run_isolated(self, pdf_path, start, end):
    if self._mineru_server_available():
        try:
            return self._mineru_run_via_server(pdf_path, start, end)
        except (ConnectionError, requests.ConnectionError):
            # Server crashed (OOM kill) — fall back to subprocess for remaining pages.
            # Unlike HTTP 409 (task failed, server alive), ConnectionError means
            # the server process is gone. Log and degrade gracefully.
            _log.warning("mineru_server_lost", path=str(pdf_path),
                         pages=f"{start}–{end}")
            return self._mineru_run_subprocess(pdf_path, start, end)
    return self._mineru_run_subprocess(pdf_path, start, end)
```

### Server lifecycle management

Options (in order of preference):
1. **User-managed**: `nx mineru start` / `nx mineru stop` commands
2. **Auto-start on first use**: `pdf_extractor.py` launches server if not running, reuses across papers
3. **Always-on via launchd**: `nx mineru install` creates a LaunchAgent

Option 1 is simplest and most transparent. Option 2 adds complexity but better UX for batch indexing.

### Page-range strategy with server

**Caution**: The OOM root cause (RF-5) is MFR batching all formula regions in a page range as a single inference batch. This is a function of formula density, not model-loading overhead. Pre-loading models does **not** change MFR's memory behavior — formula-dense pages will still OOM at larger batch sizes.

Strategy:
- **Default to 1-page ranges** for formula-dense papers (same as current subprocess mode)
- For papers with low formula density (< 10 formulas/page), try 5-page ranges
- If OOM (server returns 409 with error), fall back to 1-page ranges
- The throughput gain comes from eliminating 5s model-init per request, not from larger batches
- `MINERU_PROCESSING_WINDOW_SIZE=8` may allow the server to internally subdivide large page ranges — needs empirical validation in Phase 3

## Research Findings

### RF-1: MinerU built-in API server (2026-04-02)

**Classification**: Verified — documentation + source inspection
**Confidence**: HIGH

`mineru-api` is a FastAPI server shipped with MinerU 3.x. It keeps models loaded via `ModelSingleton` (RLock-protected, cache-keyed by backend + model path + device). Models load once on first request; all subsequent requests reuse them.

Endpoints:
- `POST /file_parse` — synchronous single-file extraction
- `POST /tasks` — async task submission (returns 202)
- `GET /tasks/{id}` — poll task status
- `GET /tasks/{id}/result` — fetch completed result
- `GET /health` — health check with task stats

Source: MinerU documentation, DeepWiki analysis, source inspection (`mineru/cli/fast_api.py`)

### RF-2: MinerU environment variables for memory control (2026-04-02)

**Classification**: Verified — documentation + GitHub issues
**Confidence**: HIGH

| Variable | Purpose |
|---|---|
| `MINERU_TABLE_ENABLE=false` | Drops 2 models, 16GB → 8GB requirement |
| `MINERU_PROCESSING_WINDOW_SIZE=N` | Sliding window batch size (default 64) |
| `MINERU_VIRTUAL_VRAM_SIZE=N` | Virtual VRAM limit in MB |
| `MINERU_API_MAX_CONCURRENT_REQUESTS=N` | Server concurrency (default 3, **hardcoded 1 on Mac**) |

Source: DeepWiki configuration docs, GitHub issue #4397, source inspection

### RF-3: MLX memory management APIs — background context (2026-04-02)

**Classification**: Verified — MLX documentation
**Confidence**: HIGH

**Note**: These APIs exist and work as documented, but they are **insufficient to solve the in-process OOM** because MinerU's `AtomModelSingleton` holds model references that prevent memory reclamation. See RF-5 for why process isolation remains necessary. These APIs are documented here for completeness — they may be useful inside the server process for inter-request cleanup but cannot replace subprocess isolation.

```python
import mlx.core as mx
mx.set_cache_limit(2 * 1024**3)  # 2GB cap on buffer cache
mx.clear_cache()                  # explicit flush
```

For PyTorch models (MFR uses PyTorch):
```python
import torch; torch.mps.empty_cache()
```

MinerU also exposes: `from mineru.utils.model_utils import clean_memory; clean_memory('mps')`

Source: MLX unified memory docs, MinerU GitHub issue #3399

### RF-4: Nougat comparison — MinerU is decisively better (2026-04-02)

**Classification**: Verified — OmniDocBench CVPR 2025
**Confidence**: HIGH

| Criterion | MinerU | Nougat |
|---|---|---|
| Formula CDM (English) | **57.3%** | 15.1% |
| UniMERNet complex ODE BLEU | **0.916** | no benchmark |
| Scanned PDF handling | explicit OCR | poor (arXiv-only training) |
| Apple Silicon | supported | no MPS code paths; CPU-only fallback (~15-30s/page) |
| Maintenance | active (2026) | abandoned (last release Oct 2023) |
| License | Apache 2.0 | CC-BY-NC |
| Hallucination rate | rare | ~1.5%+ in-domain, higher OOD |

MinerU is 3.8x more accurate on formula extraction. Nougat is unsuitable for pre-digital scanned papers and is effectively unmaintained.

Source: OmniDocBench (arXiv 2412.07626), UniMERNet (arXiv 2404.15254), Nougat GitHub issues

### RF-5: Subprocess OOM root cause (2026-04-02)

**Classification**: Verified — empirical testing
**Confidence**: HIGH

MinerU's MFR (Math Formula Recognition) step processes all formula regions in a page/batch as a single inference batch. On formula-dense pages (100+ regions), the accumulated activations exceed Apple Silicon unified memory limits. The MFR model has no internal batching for formula regions.

GitHub issue #2379 documents memory "rising at 45-degree angle to 45GB" on symbol-heavy content. Issue #2771 patched excessive memory consumption but only for certain batch-processing paths.

**Important**: pre-loading models in a persistent server does **not** change MFR's per-batch memory behavior. The OOM is caused by formula region count in the inference batch, not by model-loading competing with inference. A persistent server with resident models may actually have *less* available headroom (models permanently occupy memory). The throughput benefit of the server comes from eliminating the 5s model-init cost per request, not from enabling larger batch sizes. Formula-dense pages should continue to use 1-page ranges.

Source: Empirical testing (this session), MinerU GitHub issues #2379, #2771

### RF-6: mineru-api server — full source-verified API contract (2026-04-02)

**Classification**: Verified — source code inspection (`mineru/cli/fast_api.py`)
**Confidence**: HIGH

**Endpoints** (from source, not docs):

| Endpoint | Method | Behavior |
|---|---|---|
| `POST /file_parse` | sync | Submits task, blocks until complete, returns result |
| `POST /tasks` | async | Returns 202 with `task_id`, poll for result |
| `GET /tasks/{task_id}` | status | Returns task status payload |
| `GET /tasks/{task_id}/result` | result | Returns extraction result (200) or 202 if not ready |
| `GET /health` | health | Returns server health + task stats |

**`POST /file_parse` form parameters** (all Form fields, not JSON):

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `files` | `UploadFile[]` | required | PDF/image upload |
| `backend` | `str` | `"hybrid-auto-engine"` | Options: `pipeline`, `vlm-auto-engine`, `vlm-http-client`, `hybrid-auto-engine`, `hybrid-http-client` |
| `parse_method` | `str` | `"auto"` | Options: `auto`, `txt`, `ocr` |
| `formula_enable` | `bool` | `True` | |
| `table_enable` | `bool` | `True` | |
| `lang_list` | `list[str]` | `["ch"]` | |
| `return_md` | `bool` | `True` | Return markdown in response |
| `return_middle_json` | `bool` | `False` | Return `_middle.json` (contains `pdf_info` with formula spans) |
| `return_content_list` | `bool` | `False` | Return `_content_list.json` (equation/text blocks) |
| `start_page_id` | `int` | `0` | **0-indexed** |
| `end_page_id` | `int` | `99999` | **0-indexed**, inclusive upper bound |
| `response_format_zip` | `bool` | `False` | Return ZIP instead of JSON |

**Success response (JSON, non-ZIP)**:
```json
{
  "task_id": "uuid",
  "status": "completed",
  "backend": "pipeline",
  "version": "3.x.y",
  "results": {
    "<filename_stem>": {
      "md_content": "# Title\n...",
      "middle_json": "{\"pdf_info\": [...]}",
      "content_list": "[{\"type\": \"equation\", ...}]"
    }
  }
}
```

**Critical**: `md_content`, `middle_json`, and `content_list` are **string** values (not parsed JSON). The caller must `json.loads()` the `middle_json` and `content_list` fields.

**Error responses**:
- `409` — task failed (extraction error). Body: `{..., "status": "failed", "error": "...", "message": "Task execution failed"}`
- `503` — task manager unavailable (shutdown race). Body: `{..., "message": "Task manager became unavailable..."}`
- `400` — invalid parse_method or unsupported file type
- `404` — task not found (async endpoints only)

**Concurrency**: On macOS, `max_concurrent_requests` is hardcoded to `1` (line 238). The `MINERU_API_MAX_CONCURRENT_REQUESTS` env var is **ignored on Mac**. Only one extraction runs at a time.

Source: `mineru/cli/fast_api.py` lines 130-849, 1323-1368

### RF-7: mineru-api server CLI and lifecycle (2026-04-02)

**Classification**: Verified — source code inspection
**Confidence**: HIGH

**Entry point**: `mineru-api` (console_script → `mineru.cli.fast_api:main`)

**CLI flags**:
```
--host      (default: 127.0.0.1)
--port      (default: 8000)
--reload    (flag, dev mode)
--enable-vlm-preload  (bool, default: false — preload VLM on startup)
```

**Environment variables** (server-specific, beyond RF-2):

| Variable | Default | Purpose |
|---|---|---|
| `MINERU_API_OUTPUT_ROOT` | `./output` | Where extraction results are written |
| `MINERU_API_TASK_RETENTION_SECONDS` | `86400` (24h) | How long completed tasks persist |
| `MINERU_API_TASK_CLEANUP_INTERVAL_SECONDS` | `300` (5m) | Cleanup sweep interval |
| `MINERU_API_DISABLE_ACCESS_LOG` | off | Suppress uvicorn access log |
| `MINERU_API_SHUTDOWN_ON_STDIN_EOF` | off | Shutdown when stdin closes (useful for subprocess management) |
| `MINERU_API_ENABLE_FASTAPI_DOCS` | on | Enable `/docs` and `/redoc` |
| `MINERU_LOG_LEVEL` | `INFO` | Loguru log level |

**Startup flow**: uvicorn starts → `lifespan()` creates `AsyncTaskManager` → optional VLM preload → server ready. Models load lazily on first extraction request (not at startup), unless `--enable-vlm-preload` is set.

**Shutdown**: SIGTERM → uvicorn graceful shutdown → `lifespan()` finally block → `AsyncTaskManager.shutdown()` cancels dispatcher, cleanup loop, and all active tasks. Also supports `MINERU_API_SHUTDOWN_ON_STDIN_EOF` for subprocess management (daemon thread watches stdin, sets `server.should_exit`).

**Health check**: `GET /health` returns `{"status": "healthy", ...}` with task stats, or `503` with `{"status": "unhealthy", "error": "..."}` if dispatcher or cleanup loop crashed.

Source: `mineru/cli/fast_api.py` lines 1490-1542, 216-292, 1447-1487

### RF-8: Current pdf_extractor.py architecture — integration points (2026-04-02)

**Classification**: Verified — source code inspection
**Confidence**: HIGH

**Current extraction flow** (`_extract_with_mineru`):
1. Open PDF with pymupdf to get `total_pages`
2. Build batches of `MINERU_PAGE_BATCH=1` page each
3. For each batch: `_mineru_run_isolated(pdf_path, start, end)` → subprocess
4. Subprocess writes to `result_dir/{pdf_name}/auto/`:
   - `{pdf_name}.md` — markdown output
   - `{pdf_name}_content_list.json` — structured blocks with equation types
   - `{pdf_name}_middle.json` — contains `pdf_info` list with `para_blocks/lines/spans`
5. Parent reads files, merges all batches
6. `_mineru_build_result()` assembles `ExtractionResult`

**Key data consumed by `_mineru_build_result`**:
- `md_text`: concatenated markdown from all batches
- `content_list`: display equation count (`type == "equation"`)
- `pdf_info`: inline equation count (spans with `type == "inline_equation"`)
- `page_boundaries`: synthetic (evenly divided by page count — imprecise but functional)

**Subprocess worker script** (line 34-51) calls `do_parse()` with:
```python
do_parse(result_dir, pdf_file_names=[name], pdf_bytes_list=[bytes],
         p_lang_list=["en"], formula_enable=True, table_enable=True,
         start_page_id=start, end_page_id=end)
```
Then `os._exit(0)` to force-terminate.

**Server integration changes needed**:
1. Add `_mineru_server_available()` — `GET /health` check with connection timeout
2. Add `_mineru_run_via_server()` — `POST /file_parse` with `return_md=True, return_middle_json=True, return_content_list=True`
3. Parse response: `results[stem]["md_content"]` (string), `json.loads(results[stem]["middle_json"])`, `json.loads(results[stem]["content_list"])`
4. Modify `_mineru_run_isolated()` to try server first, fall back to subprocess
5. Server uses `backend="pipeline"` (not hybrid) for consistency with current subprocess `do_parse` call

**No changes needed to**: `_mineru_build_result()`, `ExtractionResult`, `pdf_chunker.py`, or the auto-detect routing logic.

Source: `src/nexus/pdf_extractor.py` lines 241-363

### RF-9: Server backend selection — pipeline vs hybrid (2026-04-02)

**Classification**: Verified — source code inspection
**Confidence**: HIGH

The server default backend is `"hybrid-auto-engine"` but the current subprocess code calls `do_parse()` directly (which uses the `pipeline` backend). The `pipeline` backend:
- Runs `do_parse` synchronously in a thread (`asyncio.to_thread`)
- Supports `formula_enable`, `table_enable`, `start_page_id`, `end_page_id`
- Is hallucination-free (rule-based layout + neural models, no LLM)

The `hybrid-auto-engine` backend uses `aio_do_parse` (async) and requires a local VLM. For our use case, **`backend="pipeline"` is correct** — it matches current behavior and doesn't require VLM infrastructure.

Key difference in `run_parse_job` (line 961):
```python
if request_options.backend == "pipeline":
    await asyncio.to_thread(do_parse, **parse_kwargs)
else:
    await aio_do_parse(**parse_kwargs)
```

Source: `mineru/cli/fast_api.py` lines 927-965

### RF-10: Mac-specific server constraints (2026-04-02)

**Classification**: Verified — source code inspection
**Confidence**: HIGH

`create_app()` at line 237 checks `is_mac_environment()`:
```python
if is_mac_environment():
    max_concurrent_requests = 1
```

This **overrides** `MINERU_API_MAX_CONCURRENT_REQUESTS` on macOS. The semaphore is hardcoded to 1. This means:
1. Only one extraction task runs at a time (serialized)
2. `/file_parse` (sync) blocks until the current extraction finishes
3. Multiple concurrent `/file_parse` requests queue behind the semaphore

This is actually desirable for Apple Silicon unified memory — prevents two extractions from competing for GPU memory. Our client should use `/file_parse` (sync) and send page ranges sequentially.

The `MINERU_API_DEFAULT_MAX_CONCURRENT_REQUESTS` constant is 3 (for Linux/CUDA), but Mac always gets 1.

Source: `mineru/cli/fast_api.py` lines 237-246, `mineru/cli/api_protocol.py`

### RF-11: Server output file management (2026-04-02)

**Classification**: Verified — source code inspection
**Confidence**: MEDIUM

The server writes extraction outputs to `MINERU_API_OUTPUT_ROOT` (default `./output`), organized as:
```
output/
  {task_id}/
    uploads/
      {filename}
    {filename_stem}/
      {backend}/            # e.g. "pipeline" or "auto"
        {filename_stem}.md
        {filename_stem}_content_list.json
        {filename_stem}_middle.json
        {filename_stem}_model.json
        images/
```

Completed tasks are cleaned up after `MINERU_API_TASK_RETENTION_SECONDS` (default 24h). For our use case, we should:
1. Set `MINERU_API_OUTPUT_ROOT` to a temp directory
2. Set `MINERU_API_TASK_RETENTION_SECONDS=300` (5 min) to avoid disk bloat
3. Use inline JSON response (`return_md=True`, etc.) instead of reading output files

Source: `mineru/cli/fast_api.py` lines 968-973, 432-476

## Rejected Alternatives

### Nougat replacement
Formula accuracy 3.8x worse, no Apple Silicon support, abandoned project, CC-BY-NC license. See RF-4.

### Docling formula enrichment only
2.9x slower than MinerU, misses most inline math (29 vs 457 on same paper). Evaluated in RDR-044 RF-7.

### In-process batching with explicit memory cleanup
Tried `mx.clear_cache()`, `torch.mps.empty_cache()`, `gc.collect()`, `clean_memory('mps')`. MinerU's `AtomModelSingleton` holds references that prevent cleanup. Memory accumulates monotonically across `do_parse` calls within the same process.

### multiprocessing.spawn per batch
Deadlocks on macOS due to MinerU's internal `ProcessPoolExecutor` workers keeping pipes open. Tested and abandoned.

## Success Criteria

- [ ] `mineru-api` server starts with `nx mineru start` and reports healthy within 30s
- [ ] `nx index pdf` uses HTTP when server is available, subprocess when not
- [ ] 100-page paper indexes in < 20 minutes (vs 50 min current, ~2-3x improvement from eliminating model-init)
- [ ] No OOM on any paper in the Grossberg corpus (100+ papers) at 1-page ranges
- [ ] Batch indexing of 80+ papers completes in < 20 hours (vs 40+ current, ~2x minimum from model-init elimination; further gains from batch-size tuning on low-formula papers)
- [ ] Server shutdown cleans up cleanly (`nx mineru stop`)
- [ ] Existing `--extractor` CLI flag still works
- [ ] Non-math papers still use Docling fast path (no regression)
- [ ] Filename normalization: extraction succeeds for PDFs with spaces, parens, Unicode in name

## Implementation Notes

### Phase 1: Server management commands

Tasks:
- `nx mineru start` — launch server with recommended env vars, wait for health check
- `nx mineru stop` — graceful shutdown via SIGTERM (use `MINERU_API_SHUTDOWN_ON_STDIN_EOF` for subprocess-managed server)
- `nx mineru status` — check if running, report health endpoint response

**Acceptance criteria**:
- `nx mineru start` exits 0 and `nx mineru status` reports healthy within 30s (on subsequent starts; first-ever start may take several minutes for ~2-3 GB model download)
- `nx mineru stop` terminates the process; subsequent `nx mineru status` reports not running
- `nx mineru start` on an already-running server detects the existing process and exits cleanly
- `nx mineru start` when port 8010 is occupied by a non-managed process surfaces the bind error clearly (not a silent failure)
- Server PID and port stored in `~/.config/nexus/mineru.pid` for lifecycle management

**Decision: user-managed (Option 1)**. Auto-start (Option 2) adds PID management, port-conflict detection, and log capture complexity that isn't justified until batch UX (Phase 4) proves it's needed. Users run `nx mineru start` before a batch session and `nx mineru stop` after.

### Phase 2: HTTP client in pdf_extractor.py

Tasks:
- Add `_mineru_server_available()` — `GET /health` with 2s connection timeout
- Add `_mineru_run_via_server()` — `POST /file_parse` with key-miss guard and empty-content guard
- Modify `_mineru_run_isolated()` to try server first, fall back to subprocess
- Add `httpx` to optional `[mineru]` extra (already async-capable, lighter than `requests`)
- Send warmup extraction request on first server contact to trigger lazy model loading. **`GET /health` is NOT a warmup** — it returns immediately without loading models. Warmup must be `POST /file_parse` on a minimal 1-page test PDF, or use `--enable-vlm-preload` in server startup. Without warmup, the first paper in a batch pays full model-load time (~5-10s)

**Acceptance criteria**:
- `nx index pdf paper.pdf` with server running uses HTTP path (verify via structlog)
- `nx index pdf paper.pdf` with server not running falls back to subprocess (no user-visible change)
- Filename normalization: test with spaces, parentheses, Unicode in PDF filenames
- Empty-content guard fires on simulated server failure (409 response)
- Existing `--extractor` flag continues to work unchanged

### Phase 3: Adaptive page ranges

Tasks:
- Default 1-page ranges for all papers (consistent with current behavior)
- For papers with low formula density, allow configurable page-range size
- Empirically validate `MINERU_PROCESSING_WINDOW_SIZE` interaction with page ranges
- Track OOM retries per paper via structlog (not persisted — session-only)

**Acceptance criteria**:
- Formula-dense papers (100+ regions/page) complete without OOM at 1-page range
- Low-formula papers (< 10 formulas/page average) complete at 5-page range without OOM
- OOM on a batch triggers automatic 1-page fallback for remaining pages
- Throughput improvement measurable: < 5s/page for non-formula-dense content (vs 15-19s current)

### Phase 4: Batch indexing UX

Tasks:
- `nx index pdf --dir docs/papers/` — batch mode
- Progress reporting: `[N/M] paper.pdf — K chunks, Xs`
- Summary: total chunks, failures, OOMs
- Warmup request before first paper in batch to exclude model-load from timing
- Evaluate async `/tasks` endpoint for progress reporting on long extractions

**Acceptance criteria**:
- 10-paper batch completes with per-paper progress output
- Failed papers are reported in summary, don't abort the batch
- First-paper model-load time is excluded from per-paper timing statistics
- If server is not running during batch mode, print warning: "MinerU server not running. Batch indexing will use subprocess mode (~30s/page). Run `nx mineru start` for faster extraction (~3-5s/page)."
