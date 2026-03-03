---
rdr: RDR-015
title: "Post-Mortem: Indexing Pipeline Rethink"
date: "2026-03-02"
author: Hal Hildebrand
commits: ["dc443f3", "cfbab97"]
---

# Post-Mortem: RDR-015 — Indexing Pipeline Rethink

## Summary

RDR-015 was implemented across multiple sessions and multiple PRs. The core pipeline
gaps (Fix A: context prefix injection, Fix B: AST language expansion, Fix E: test
coverage) were completed as planned. A significant unplanned discovery emerged during
real-corpus validation of the Luciferase indexing: the `docs__` collections were
substantially polluted by known-noise file types (GPU shaders, build configs, markup
files, lock files) because the classifier had no concept of "skip this file entirely."
This led to an additional bead (nexus-txwo: Classifier SKIP Extension), a design
session with several rejected approaches, and a clean minimal fix that should have
been caught at design time.

---

## Unplanned Discovery: Prose Collection Noise

### How It Was Found

During manual validation of the Luciferase corpus indexing (`~/git/Luciferase`), the
`docs__Luciferase-f2d57dbc` collection was inspected by paginating its contents (ChromaDB
limits `get()` to 300 per call — always paginate in batches). Of 4,183 total chunks:

- 4,057 chunks: `.md` files via `SemanticMarkdownChunker` ✓ (correct)
- 29 chunks: `.html` files via `_line_chunk` (noise — Maven site output)
- 26 chunks: `.xml` files via `_line_chunk` (noise — pom.xml files)
- 28 chunks: GPU shader files (`.cl`, `.comp`, `.frag`, `.vert`, `.metal`) — code, not prose
- 12 chunks: extensionless files (LICENSE, mvnw, git hooks) via `_line_chunk` (noise)
- 7 chunks: `.json` files (noise)
- 4 chunks: `.proto` files — Protobuf schemas should be CODE, not PROSE
- rest: `.yml`, `.css`, `.cmd`, `.properties`, etc. (noise)

The metadata field for markdown section headers is `section_title` (stored in ChromaDB
metadata), NOT `header_path`. This was a source of confusion during validation — querying
`meta.get("header_path")` returned `None` for all markdown chunks, causing false concern
about chunking quality. The field name is correct; the query was wrong.

### Root Cause

`src/nexus/classifier.py` had three enum values: `CODE`, `PROSE`, `PDF`. Every file
that was not code or PDF became PROSE. There was no mechanism to say "don't index this
at all." The consequence:

1. GPU shader files (`.cl`, `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`) —
   code-like, should be in `code__` — went to `docs__` as prose.
2. Protobuf schemas (`.proto`) — same: code-like, wrong collection.
3. Build/config detritus (pom.xml, package.json, pyproject.toml, .yml, .html, .css,
   LICENSE, mvnw) — semantically useless for search, should not be indexed at all.

### Why It Wasn't Caught in RDR Design

RDR-015 audited the chunking and extraction pipeline gaps vs. arcaneum. It did not audit
the classifier's coverage of edge-case file types. The classifier was documented in
the RDR as a nexus innovation ("file classification + dual-collection routing") but
was not reviewed for extension completeness. The Luciferase corpus was the first large,
mixed-type repo indexed after the classifier was written, exposing the gap.

---

## Design Session: Rejected Approaches

The design for the SKIP fix took longer than expected because several plausible-sounding
approaches were considered and rejected. These rejections are the hard-won knowledge.

### Rejected: AI Classification Per File (Haiku with Extension Caching)

**Proposed:** For file extensions not in the known-CODE or known-PROSE set, call
`claude-haiku-4-5` with the filename and a content snippet, cache the result in
`~/.config/nexus/extension_cache.json` keyed by extension.

**Why rejected:** The problem is not "ambiguous extensions." It is "known-noise extensions
being treated as prose." `.xml`, `.json`, `.yml`, `.html` are not ambiguous — they have
no semantic value for code search and should always be skipped. AI classification adds
latency, API cost, and non-determinism for a problem that is entirely solvable with a
static list. The "unknown extension" tail (`.abc`, `.xyz`) is not a real problem in practice.

**Rule:** Use AI classification only when the classification is genuinely ambiguous from
structure/extension alone. Extension-based noise is not ambiguous.

### Rejected: Fine-Grained `doc_type` Metadata (with query-time filtering)

**Proposed:** Tag every chunk at index time with `file_category` (markdown, java,
protobuf, gpu_shader, build_config, etc.) and `doc_type` (readme, architecture, rdr,
api_reference, changelog, guide, etc.), then use ChromaDB's `where=` clause at query
time to filter by category.

**Why rejected (YAGNI + usability):** The metadata tagging is cheap (extension + path
patterns, no AI). But the query-time filtering creates a usability problem: users must
know the taxonomy to filter by it, and fine-grained categories (rdr vs. architecture vs.
guide) are harder to use correctly in practice than expected. The user's exact words:
"I suspect this fine-grained is likely more confusing and harder to use than we suspect
in practice." YAGNI applies. The `code__` / `docs__` collection split already IS the
coarse filter. If the noise is fixed at ingest, no filtering is needed.

**Rule:** Metadata categories that users cannot naturally name in a query are likely
not useful. Query-time filtering is a power-user feature; solve the ingest problem first.

### Rejected: Embedding-Based Chunk Filtering

**Proposed:** Use embeddings to filter or categorize chunks — compute cosine similarity
between each chunk and a set of "reference" embeddings (e.g., centroid of known-good
architecture docs), exclude chunks below a threshold.

**Why rejected (real capability, wrong problem):** This is a real technique — it is
called semantic reranking and is used in production RAG systems. It IS possible with
ChromaDB via post-retrieval Python computation (ChromaDB's `where=` clause is
metadata-only, not embedding-based). But it solves the wrong problem: the docs__
collection noise is a file classification problem, not a semantic similarity problem.
Correctly classified files (all markdown) do not need embedding-based filtering.
Incorrectly classified files (XML, shaders, configs) are not filtered by semantic
similarity — they would need to be semantically close to "noise" for a threshold to
exclude them, but they're not necessarily more similar to noise than to real docs.
The right fix is to not index the noise in the first place.

**Rule:** Semantic reranking improves retrieval quality within a well-classified
collection. It does not compensate for ingest classification errors.

---

## What Actually Shipped (Classifier SKIP Extension)

Epic: nexus-txwo (`cfbab97`)

**`ContentClass.SKIP`** — fourth enum value, silently ignored by indexer:
```python
case ContentClass.SKIP:
    pass  # known-noise file; silently ignore
```

**`_SKIP_EXTENSIONS`** (18 extensions):
- Build/config: `.xml`, `.json`, `.yml`, `.yaml`, `.toml`, `.properties`, `.ini`,
  `.cfg`, `.conf`, `.gradle`
- Web/markup: `.html`, `.htm`, `.css`, `.svg`
- Windows scripts: `.cmd`, `.bat`, `.ps1`
- Lock files: `.lock`

Note: `.lock` was already in `indexer.py:DEFAULT_IGNORE` — the SKIP class is
defense-in-depth for callers who override `DEFAULT_IGNORE`.

**`_CODE_EXTENSIONS` expansion** (+9 extensions):
`.proto`, `.cl`, `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl`

**Shebang detection for extensionless files:** `_has_shebang(path)` reads first 2 bytes;
`b'#!'` → `ContentClass.CODE`, else → `ContentClass.SKIP`. Catches `OSError` (returns
False for non-existent paths). Priority order in `classify_file()`:

```
PDF → prose_overrides (config wins) → effective_code → _SKIP_EXTENSIONS →
extensionless shebang check → PROSE (everything else)
```

**Test coverage:** 42 classifier tests including parametrized coverage of all 18 SKIP
extensions and all 9 new CODE extensions. Full suite: 1787 passing.

---

## Plan Audit Findings (Worth Recording)

The plan-auditor caught several issues before implementation that would have caused
confusing test failures:

1. **Function name mismatch:** Strategic planner wrote `classify()` throughout, but the
   actual function is `classify_file()`. If uncaught, Phase 2 would have created a new
   function alongside the existing one, leaving the old behavior intact.

2. **Shebang tests require real files:** The existing test suite used bare `Path("Makefile")`
   (non-existent path) for the `test_no_extension_classified_as_prose` test. Shebang
   detection reads actual bytes; `_has_shebang(Path("Makefile"))` catches `OSError` and
   returns `False` → `SKIP`. A test asserting `CODE` with a non-existent path would fail
   for the right reason, but a test asserting `SKIP` with a non-existent path would pass
   vacuously even without the shebang check. Always use `tmp_path` for tests that exercise
   real file I/O.

3. **`test_no_extension_classified_as_prose`** — this test existed and would have become
   a false pass: `Makefile` (non-existent path) would return `SKIP` after the change for
   the wrong reason (OSError fallback), not for the right reason (no shebang). Updating
   it to use `tmp_path` with a file containing real Makefile content (no shebang) makes
   the test meaningful.

4. **`test_code_extensions_set_matches_design`** — strict equality check on the frozenset.
   Had to be updated in the test (red phase) before the implementation change, or Phase 2
   would have produced a passing implementation with a failing unrelated test — confusing
   TDD signal.

---

## Timeline

| Event | Commit / PR |
|-------|-------------|
| RDR-015 created, researched, gated, accepted | docs commits |
| Fix A (context prefix, DEFINITION_TYPES), Fix B (AST expansion), Fix E (tests) | PRs #57, #58 |
| Luciferase corpus indexed with `nx -v index repo .` | in-session |
| Prose collection noise discovered during manual validation | in-session |
| `--chunk-size`/`--no-chunk-warning` removed from `nx index repo` (unrelated cleanup) | `dc443f3` |
| Classifier SKIP design session (3 approaches rejected) | in-session |
| nexus-txwo: Classifier SKIP Extension implemented | `cfbab97` |

---

## What Went Well

- The three-phase TDD structure (red → green → integration) was clean.
- The plan-auditor caught real issues before any code was written.
- The SKIP fix is minimal (~60 LOC in classifier.py, 1 line in indexer.py) and handles
  the real problem without overengineering.
- The design rejection process is documented — future sessions won't re-explore the
  same dead ends.
- ChromaDB pagination pattern discovered: `get()` returns at most 300 entries per call
  regardless of collection size; always paginate in batches of 200.

## What Could Be Improved

- **The classifier should have been audited during RDR-015 research.** The classifier
  is listed as a nexus innovation in the RDR but its extension coverage was never
  reviewed against the actual file types in a real mixed-language repo.
- **Real-corpus validation should gate implementation closure.** Indexing a synthetic
  test repo (all `.py` and `.md`) hides classification gaps. Add "indexed at least one
  real mixed-language repo and inspected the docs__ collection contents" to the
  post-implementation checklist.

---

## Artifacts

- **Commits:** `dc443f3` (--chunk-size removal), `cfbab97` (Classifier SKIP Extension)
- **Design doc:** `docs/plans/2026-03-02-classifier-skip-design.md`
- **Implementation:** `src/nexus/classifier.py`
- **Indexer integration:** `src/nexus/indexer.py:1026` (`case ContentClass.SKIP: pass`)
- **Tests:** `tests/test_classifier.py` (42 tests), `tests/test_indexer_e2e.py` (updated)
