---
title: "RDR-071: Query Sanitizer"
status: draft
type: feature
priority: P1
created: 2026-04-13
---

# RDR-071: Query Sanitizer

Port MemPalace's 4-step query sanitizer to defend against system prompt contamination in search queries. Permanence mode split to RDR-074 (pending tier resolution).

## Problem

AI agents sometimes prepend system prompts to search queries. The embedding model represents the concatenated string as a single vector where the system prompt (2000+ chars) overwhelms the actual question (10-50 chars).

MiniLM (local mode) is severely impacted: 5x distance inflation, irrelevant results intrude, distance thresholds filter everything as noise, returning 0 results. Voyage (cloud) is resilient (80% overlap at 75x inflation) but not immune.

This is a silent catastrophic failure. No error, no warning. The search just returns wrong or empty results.

## Research

### RF-071-1: MemPalace query sanitizer effectiveness

Source: `mempalace/query_sanitizer.py` + `tests/test_query_sanitizer.py`

4-step cascade, ~130 lines of pure Python:

1. **Passthrough** (query <= 200 chars): no degradation, ~89% recall
2. **Question extraction** (find sentences ending with ?): near-full recovery, ~85-89%
3. **Tail sentence extraction** (last meaningful sentence): moderate recovery, ~80-89%
4. **Tail truncation** (last 500 chars, fallback): minimum viable, ~70-80%

Without sanitizer: **1% recall** (catastrophic silent failure).

No dependencies, no LLM calls, runs in <1ms.

### RF-071-2: Contamination vectors in nexus

Nexus search queries arrive via three paths:

1. **MCP `search()` tool**: agents pass the query string. Contamination possible when agents concatenate context + question.
2. **MCP `query()` tool**: same risk, plus catalog routing parameters may carry extra context.
3. **CLI `nx search`**: human-typed, contamination unlikely.

The MCP paths are the vulnerable surface.

### RF-071-3: Voyage AI resilience (measured 2026-04-13)

Tested on `knowledge__delos` with "byzantine fault tolerant consensus":
- Clean (34 chars): avg distance 0.478
- Moderate contamination (302 chars): avg distance 0.413, **80% top-5 overlap**
- Extreme contamination (2646 chars): avg distance 0.443, **80% top-5 overlap**

Voyage AI (1024d) maintains 80% result overlap even with 75x query inflation. The sanitizer is defense-in-depth for cloud, critical for local.

### RF-071-4: MiniLM contamination confirmed (measured 2026-04-13)

Local MiniLM (384d) on 5-doc test corpus:
- Clean: top result d=0.151, all 3 results BFT-relevant
- Contaminated (1274 chars): top result d=0.775 (5x worse), **database indexing intrudes as #2**

With nexus distance thresholds (knowledge=0.65), contaminated results would be filtered as noise, returning 0 results. **Local mode users are fully exposed to silent search failure.**

## Design

Add `sanitize_query(raw: str) -> str` as a preprocessing step in the MCP `search` and `query` tools, before the query reaches `search_cross_corpus`.

The function ports MemPalace's 4-step cascade:
1. Short query passthrough (<= 200 chars)
2. Question extraction (last sentence ending with ?)
3. Tail sentence extraction (last meaningful sentence)
4. Tail truncation (last 500 chars)

### Integration point

The sanitizer runs in the MCP tool functions (`mcp/core.py`), not inside `search_cross_corpus`. This keeps the search engine pure (it receives clean queries) and makes the sanitizer testable independently.

```python
# In mcp/core.py search() and query() tools:
from nexus.filters import sanitize_query
query = sanitize_query(query)
```

### Configuration

Add `search.query_sanitizer: true` to `_DEFAULTS` in `config.py`. Read it in the MCP tool functions where `load_config()` is already called. Pass a `sanitize: bool` parameter to control behavior.

### Logging

Log at debug level when sanitization activates, including the method used and length reduction. This aids debugging without noise.

## Success Criteria

- SC-1 (local): sanitizer recovers top-3 result overlap to >= 60% on MiniLM with 1000+ char contamination (baseline: ~40% overlap without sanitizer)
- SC-2 (cloud): sanitizer does not degrade clean-query results on Voyage (overlap stays >= 95%)
- SC-3: sanitizer adds < 1ms latency (no LLM, no network)
- SC-4: test fixture with 5 contamination patterns (system prompt, chain-of-thought, tool preamble, multi-turn context, empty)

## Open Questions

1. Should the sanitizer log when it activates? (Proposed: yes, structured log at debug level)
2. Should the sanitizer be disabled for CLI queries? (Proposed: yes, only MCP paths are vulnerable)
