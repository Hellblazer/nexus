---
title: "RDR-095: Post-Store Hook Framework: Batch Contract"
id: RDR-095
type: Architecture
status: accepted
priority: medium
author: Hal Hildebrand
reviewed-by: self
created: 2026-04-25
accepted_date: 2026-04-25
related_issues: []
related_tests: [test_mcp_infra.py, test_taxonomy_assign.py, test_chash_index_store.py]
related: [RDR-060, RDR-070, RDR-075, RDR-077, RDR-086, RDR-089]
---

# RDR-095: Post-Store Hook Framework: Batch Contract

RDR-070 introduced `register_post_store_hook` in `src/nexus/mcp_infra.py`
as the canonical extensibility point for per-document post-store
enrichment. The framework currently fires from one site only:
`src/nexus/mcp/core.py:887` inside the MCP `store_put` tool. Every
CLI ingest path (`indexer.py`, `code_indexer.py`, `prose_indexer.py`,
`pipeline_stages.py`, `doc_indexer.py`) bypasses the chain and
calls **two** hardcoded enrichment functions directly:
`taxonomy_assign_batch` (RDR-070) and `chash_dual_write_batch`
(RDR-086). Both have identical call-site shape: hardcoded into the
same seven sites across the same five files, fired one immediately
after the other in each. New per-document enrichments (RDR-089
aspect extraction is the immediate pressure) face a forced choice:
register a hook that only covers the MCP path, or copy-paste
fourteen more hardcoded calls. Both options are wrong. This RDR
adds a batch contract to the hook framework and migrates both
hardcoded callers so the framework covers every ingest path
through registered hooks.

## Problem Statement

### Enumerated gaps to close

#### Gap 1: Hook chain only fires from one of six ingest entry points

`fire_post_store_hooks` is invoked from exactly one site:
`src/nexus/mcp/core.py:887` inside the MCP `store_put` tool body.
The CLI ingest paths call two hardcoded enrichment functions
directly across seven sites in five files:

| Site | Calls |
| --- | --- |
| `src/nexus/indexer.py:807-815` | `chash_dual_write_batch`, `taxonomy_assign_batch` |
| `src/nexus/code_indexer.py:435-443` | `chash_dual_write_batch`, `taxonomy_assign_batch` |
| `src/nexus/prose_indexer.py:200-208` | `chash_dual_write_batch`, `taxonomy_assign_batch` |
| `src/nexus/pipeline_stages.py:408-416` | `chash_dual_write_batch`, `taxonomy_assign_batch` |
| `src/nexus/doc_indexer.py:373-381` | `chash_dual_write_batch`, `taxonomy_assign_batch` |
| `src/nexus/doc_indexer.py:499-507` | `chash_dual_write_batch`, `taxonomy_assign_batch` |
| `src/nexus/doc_indexer.py:902-910` | `chash_dual_write_batch`, `taxonomy_assign_batch` |

None of these sites invoke `fire_post_store_hooks`. They hardcode
both currently known consumers.

This contradicts RDR-070 RF-070-6 which designed the hook chain as
the per-document post-store mounting point. The
`taxonomy_assign_batch` calls landed after RDR-070 as a perf
shortcut (see Gap 2). `chash_dual_write_batch` (RDR-086) followed
the same shape, copying the hardcoding into the same seven sites.
The pattern is now the precedent that any future per-document
enrichment will inherit unless this RDR fixes it.

#### Gap 2: Framework has no batch contract; the perf shortcut is genuine

`taxonomy.assign_batch` (`src/nexus/db/t2/catalog_taxonomy.py:2031`)
issues a single ChromaDB Cloud `query()` call for N documents,
returning N nearest-centroid assignments in one network round-trip.
The single-document hook (`taxonomy_assign_hook` in
`mcp_infra.py:365`) issues one `query()` call per document.

For a 1000-document bulk ingest, the difference is approximately
50ms (one batched query) versus 50 seconds (1000 sequential
queries). The 1000x cliff is real, ChromaDB Cloud's network round-
trip dominates. Forcing the hardcoded calls to flow through the
existing single-document chain would cause an unacceptable bulk-
ingest regression.

`chash_dual_write_batch` (`mcp_infra.py:507`, RDR-086) has the
same shape: one batched T2 SQL upsert covering N rows is materially
cheaper than N separate single-row upserts (transaction overhead
plus per-row prepared-statement re-binding). The cost asymmetry
is smaller than taxonomy's 1000x ChromaDB cliff but still
meaningful at corpus scale.

The framework therefore needs a batch contract, not a removal of
the existing single-document contract. Both shapes are real and
both currently-hardcoded enrichments justify batch-shape hooks.

#### Gap 3: Every future per-document enrichment compounds the problem

RDR-089 (aspect extraction) is the next consumer. Aspects do not
batch through `claude_dispatch` (one Haiku call per document by
construction), so single-document hook semantics are correct for
aspects. But there is no clean place for aspects to register today
because the chain misses the dominant CLI ingest path. Without a
fix, RDR-089 is forced to either accept partial coverage or add a
third hardcoded enrichment to all seven CLI sites, joining
`taxonomy_assign_batch` and `chash_dual_write_batch`.

Future enrichments (entity extraction, glossary capture, license
classification) will face the same choice. Each one paid in
duplication is a permanent tax. Fix once, all future enrichments
register cleanly.

## Context

### Background

RDR-070 (taxonomy clustering, 2026-04-12) introduced
`register_post_store_hook` as the trigger architecture for per-
document topic assignment. RF-070-6 explicitly designed the hook
chain as the canonical extensibility point and noted that batch
indexing was unaffected because batch ingest could trigger
`nx taxonomy discover` at the end of the pipeline (corpus-level
HDBSCAN, a separate concern).

`taxonomy_assign_batch` was added later as a per-document fast
path for CLI ingest, alongside the existing `assign_hook` for MCP
`store_put`. It was a pragmatic choice when taxonomy was the only
consumer: hardcoding into a handful of ingest files cost the same
as plumbing the hook chain, and the ChromaDB-batched query
genuinely won. RDR-086 followed the same template for chash
dual-write, copying the hardcoding pattern into the same files.
The set is now seven sites in five files for each function (so
fourteen call sites total) and the framework's single-shape
limitation has become visible because RDR-089 needs to add a
third consumer.

The investigation that led to this RDR also looked at three other
post-store actions: `_catalog_store_hook` / `_catalog_pdf_hook` /
`indexer.py`'s ad-hoc registration (three catalog-registration
mechanisms) and `_catalog_auto_link` (the agent-driven link
creator). None of these belong in this RDR. The catalog
mechanisms have genuinely different per-domain semantics (see
Decision Rationale). The auto-linker reads T1 scratch entries
seeded by agents before MCP `store_put`; CLI bulk ingest has no
equivalent pre-declaration semantics and uses entirely separate
post-hoc linkers (`generate_citation_links`, `generate_code_rdr_links`,
`generate_rdr_filepath_links` in `link_generator.py`). MCP-only
auto-linking is intentional path-shape coupling, not coverage gap.

### Technical Environment

- `src/nexus/mcp_infra.py:296`. `register_post_store_hook` and
  `fire_post_store_hooks`. The current single-document hook chain.
  Failure capture via `_record_hook_failure` writes to T2
  `hook_failures` (RDR-070, GH #251).
- `src/nexus/mcp_infra.py:365`. `taxonomy_assign_hook`. Existing
  single-document hook, currently registered.
- `src/nexus/mcp_infra.py:453`. `taxonomy_assign_batch`. Hardcoded
  fast path for taxonomy assignment, called from seven CLI ingest
  sites. RDR-095 migrates this into the chain as a registered
  batch hook.
- `src/nexus/mcp_infra.py:507`. `chash_dual_write_batch` (RDR-086).
  Hardcoded fast path for content-hash dual-write, called from the
  same seven sites in the same five files, fired immediately
  before `taxonomy_assign_batch` at each site. RDR-095 migrates
  this alongside taxonomy as a second registered batch hook.
- `src/nexus/db/t2/catalog_taxonomy.py:2031`. `assign_batch`.
  Single ChromaDB query for N documents, plus per-document
  `assign_topic` SQL upsert. The ChromaDB call is the load-bearing
  optimization, the SQL writes are not.
- `src/nexus/db/t2/chash_index.py`. Chash dual-write target. Single
  T2 SQL upsert covering N rows. Cheaper than N single-row upserts
  for transaction overhead reasons but not as load-bearing as the
  taxonomy ChromaDB cliff.
- `src/nexus/mcp/core.py:887`. The only call site of
  `fire_post_store_hooks`.
- `src/nexus/indexer.py:807-815`, `src/nexus/code_indexer.py:435-443`,
  `src/nexus/prose_indexer.py:200-208`,
  `src/nexus/pipeline_stages.py:408-416`,
  `src/nexus/doc_indexer.py:373-381`,
  `src/nexus/doc_indexer.py:499-507`,
  `src/nexus/doc_indexer.py:902-910`. The seven CLI ingest sites
  where both hardcoded enrichments fire (chash first, then
  taxonomy at each site).

## Research Findings

### Investigation

Ran the call-site survey on 2026-04-25 (RDR-089 verify pass):

```
grep -rn "fire_post_store_hooks\|register_post_store_hook" src/nexus/
# Definitions: mcp_infra.py:296, :301
# Call sites: mcp/core.py:887 (single)

grep -rn "taxonomy_assign_batch\|chash_dual_write_batch" src/nexus/
# Definitions: mcp_infra.py:453 (taxonomy), mcp_infra.py:507 (chash)
# Call sites: indexer.py:807-815, code_indexer.py:435-443,
#             prose_indexer.py:200-208, pipeline_stages.py:408-416,
#             doc_indexer.py:373-381, doc_indexer.py:499-507,
#             doc_indexer.py:902-910
# Both functions called at every site (chash first, taxonomy second).
```

A third sweep checked the rest of the post-store landscape:
`_catalog_store_hook`, `_catalog_pdf_hook`, `indexer.py:250`'s
ad-hoc registration, `_catalog_auto_link`. None match the
`taxonomy_assign_batch` shape; see Decision Rationale for why
they belong outside RDR-095's scope.

Inspected `taxonomy.assign_batch` (`catalog_taxonomy.py:2031`) to
confirm the ChromaDB-Cloud round-trip count: one `query()` call
per `assign_batch` invocation, regardless of N. The per-document
`assign_topic` SQL writes are independent of the batch shape and
fire either way.

Inspected `chash_dual_write_batch` (`mcp_infra.py:507`): single
batched T2 SQL upsert into `chash_index`. The batch optimization
here is transaction overhead and prepared-statement re-binding,
not a network cliff. Smaller perf win than taxonomy but consistent
in shape: one batched call cheaper than N single calls. Same
batch contract serves both.

### Dependency Source Verification

| Dependency | Source Searched? | Key Findings |
| --- | --- | --- |
| `fire_post_store_hooks` | Yes | Catches per-hook exceptions, persists to T2 `hook_failures`, never blocks the calling store_put. Signature `(doc_id, collection, content)`. |
| `taxonomy.assign_batch` | Yes | One ChromaDB `query()` for N docs, then N SQL writes via `assign_topic`. The ChromaDB call is the perf-critical part. |
| `register_post_store_hook` | Yes | Append-only registration into a module-level list. Module-import-time hook registration in `mcp/core.py:367` is the only current registration site. |

### Key Discoveries

- **Verified** (`mcp_infra.py:296-326`): the existing hook chain
  has a clean failure-isolation pattern (per-hook try/except, T2
  persistence, structured log). Reuse for the batch contract.
- **Verified** (`catalog_taxonomy.py:2078-2086`): the ChromaDB
  batched query is the entire perf optimization. The SQL writes
  are linear either way.
- **Verified** (no test mocks of `taxonomy_assign_batch`): a single
  docstring reference in `tests/test_chash_index_store.py:454`.
  Migration does not break test fixtures.
- **Verified** (`mcp/core.py:887`): the single fire site is the
  only place the chain runs today. Any new fire site is additive.

### Critical Assumptions

- [x] ChromaDB-batched query is the load-bearing perf optimization
  for `assign_batch`, not the SQL writes. **Status**: Verified by
  source search. `assign_batch` issues one `query()` call (single
  network round-trip to ChromaDB Cloud), then N
  `taxonomy.assign_topic` SQL upserts in a tight loop. The SQL
  upserts are linear either way. **Method**: Source Search.
- [ ] Per-document hook overhead in batch fire path is negligible
  next to extractor work. **Status**: Unverified. Hook framework
  overhead is a Python function call plus try/except (microseconds);
  hooks themselves do real work (Haiku call ~1-3s for aspects, ANN
  lookup for taxonomy). **Method**: confirm by smoke-test against a
  100-document fixture corpus once Phase 2 lands.
- [ ] Test fixtures that index documents under `nx index repo` do
  not break when batch hooks fire on previously-untracked paths.
  **Status**: Partially verified by source search (095-research-2
  confirmed only one docstring reference to `taxonomy_assign_batch`
  in `tests/test_chash_index_store.py:454`, not a live mock).
  Final verification requires full suite run post-migration.
  **Method**: confirm via full unit test run after Phase 2.

## Proposed Solution

### Approach

Four pieces, in order:

1. **Add the batch contract.** Extend `mcp_infra.py` with
   `register_post_store_batch_hook(fn)` and
   `fire_post_store_batch_hooks(doc_ids, collection, contents,
   embeddings, metadatas)`. Same per-hook failure-capture pattern
   as the existing single-document chain.
2. **Migrate taxonomy.** Convert `taxonomy_assign_batch` from a
   directly-called function into a registered batch hook
   (`taxonomy_assign_batch_hook`). Register at MCP server startup
   alongside the existing `taxonomy_assign_hook`.
3. **Migrate chash dual-write.** Convert `chash_dual_write_batch`
   from a directly-called function into a registered batch hook
   (`chash_dual_write_batch_hook`). Register at MCP server startup.
   `chash_dual_write_batch_hook` reads `metadatas` from the fire
   payload; `taxonomy_assign_batch_hook` reads `embeddings`. Both
   ignore parameters they don't need. The per-hook failure capture
   ensures one consumer's failure doesn't affect the other.
4. **Replace the fourteen hardcoded sites.** In
   `indexer.py:807-815`, `code_indexer.py:435-443`,
   `prose_indexer.py:200-208`, `pipeline_stages.py:408-416`, and
   `doc_indexer.py` (three sites), delete BOTH the direct
   `chash_dual_write_batch` call and the direct
   `taxonomy_assign_batch` call (currently fired in pairs) and
   replace each pair with a single
   `fire_post_store_batch_hooks(doc_ids, collection, contents,
   embeddings, metadatas)` invocation. Both registered hooks fire
   in registration order.

After this RDR lands, future per-document enrichments register
once via `register_post_store_hook` (single-shape) or
`register_post_store_batch_hook` (batch-shape) and are
automatically called from every ingest path.

### Technical Design

**New signatures** (`mcp_infra.py`):

```python
_post_store_batch_hooks: list = []


def register_post_store_batch_hook(fn) -> None:
    """Register a callable(doc_ids, collection, contents, embeddings,
    metadatas) to fire after batch CLI ingest."""
    _post_store_batch_hooks.append(fn)


def fire_post_store_batch_hooks(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None = None,
    metadatas: list[dict] | None = None,
) -> None:
    """Invoke all registered batch hooks. Per-hook failures are
    captured and persisted to T2 hook_failures, never raised."""
    import structlog
    log = structlog.get_logger()
    for hook in _post_store_batch_hooks:
        try:
            hook(doc_ids, collection, contents, embeddings, metadatas)
        except Exception as exc:
            hook_name = getattr(hook, "__name__", "?")
            log.warning(
                "post_store_batch_hook_failed",
                hook=hook_name,
                exc_info=True,
            )
            _record_batch_hook_failure(
                doc_ids=doc_ids,
                collection=collection,
                hook_name=hook_name,
                error=str(exc),
            )
```

`embeddings` and `metadatas` are both optional. Different consumers
need different inputs:

- `taxonomy_assign_batch_hook` reads `embeddings` (ignores
  `metadatas`).
- `chash_dual_write_batch_hook` reads `metadatas` (ignores
  `embeddings`).
- Future text-only consumers (aspect extraction is single-shape
  and registers via the single-doc hook chain, but text-only batch
  consumers could exist) need only `contents`.

When the caller has any of these already (all seven CLI ingest
sites have all three: doc_ids + contents + embeddings + metadatas),
passing preserves the existing perf optimisation. The hook reads
what it needs and ignores the rest.

`contents` is required because the canonical use case is per-
document text-keyed enrichment. Callers that have content in a
different shape (chunks, summaries) join into a single string at
the call site.

**Failure persistence**: `_record_batch_hook_failure` mirrors the
existing `_record_hook_failure` but persists the full doc_id list
for batch failures. The existing `hook_failures.doc_id` column is
TEXT and stores a scalar id today (`_record_hook_failure` writes
a single string; `nx taxonomy status` and any future readers parse
it as scalar). Repurposing the column to hold either a scalar or
a JSON array would silently corrupt those readers on first batch-
hook failure. RDR-095 therefore adds a small additive migration
via the RDR-076 registry:

- New column `hook_failures.batch_doc_ids` (TEXT, nullable). When
  the failure originates in a batch hook, store the JSON-encoded
  doc_id list here; leave `doc_id` set to a representative single
  id (the first in the batch) so existing scalar readers continue
  to display something meaningful.
- New column `hook_failures.is_batch` (INTEGER, default 0). Boolean
  flag for fast filtering. Readers that care about batch-vs-single
  distinction (a future `nx doctor` surface, or `nx taxonomy
  status`) check this flag before parsing `batch_doc_ids`.
- `nx taxonomy status` updated in Phase 3 to display batch-shaped
  failures with the doc_id list rendered, rather than treating
  them as scalar.

The migration entry lands in `src/nexus/db/migrations.py` alongside
the framework primitives in Phase 1; the reader update is deferred
to Phase 3 alongside the drift guard.

**Migrated batch hooks** (`mcp_infra.py`, renamed):

```python
def taxonomy_assign_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
) -> None:
    """Registered batch hook. Wraps the existing
    taxonomy.assign_batch perf path. Reads embeddings; ignores
    metadatas."""
    if not doc_ids or not embeddings:
        return
    # ... existing exclusion + assign_batch logic ...


def chash_dual_write_batch_hook(
    doc_ids: list[str],
    collection: str,
    contents: list[str],
    embeddings: list[list[float]] | None,
    metadatas: list[dict] | None,
) -> None:
    """Registered batch hook. Wraps the existing chash dual-write
    perf path. Reads metadatas; ignores embeddings."""
    if not doc_ids or not metadatas:
        return
    # ... existing chash_dual_write logic ...
```

Each body is the existing function body verbatim, with the return
type narrowed to `None` (return values were never read by callers,
confirmed by grep) and the unused parameters present-but-ignored.

Registration (in `mcp/core.py` near the existing
`register_post_store_hook` call):

```python
from nexus.mcp_infra import (
    register_post_store_hook,
    register_post_store_batch_hook,
    taxonomy_assign_hook,
    taxonomy_assign_batch_hook,
    chash_dual_write_batch_hook,
)

register_post_store_hook(taxonomy_assign_hook)
register_post_store_batch_hook(chash_dual_write_batch_hook)
register_post_store_batch_hook(taxonomy_assign_batch_hook)
```

Registration order matters: `chash_dual_write_batch_hook` runs
before `taxonomy_assign_batch_hook` because that mirrors the
current call-site ordering (chash precedes taxonomy at every site)
and preserves the dual-write-first invariant for any reader that
expects chash rows present before topic assignment runs.

**CLI ingest call-site replacement** (one example, the rest are
identical in shape):

Before (`indexer.py:807-815`):

```python
from nexus.mcp_infra import chash_dual_write_batch
chash_dual_write_batch(ids, collection_name, metadatas)
# ... a few lines of intervening logic ...
from nexus.mcp_infra import taxonomy_assign_batch
taxonomy_assign_batch(ids, collection_name, embeddings)
```

After:

```python
from nexus.mcp_infra import fire_post_store_batch_hooks
fire_post_store_batch_hooks(
    ids,
    collection_name,
    <document_texts>,   # local variable name varies by site
    embeddings,
    metadatas,
)
```

The `<document_texts>` placeholder above is the per-document text
already in scope at each call site. The actual local variable
name varies and must be looked up per file:

- `indexer.py:807-815`: `documents`
- `code_indexer.py:435-443`: `documents`
- `prose_indexer.py:200-208`: `documents`
- `pipeline_stages.py:408-416`: `documents`
- `doc_indexer.py:373-381`: `documents`
- `doc_indexer.py:499-507`: `batch_docs`
- `doc_indexer.py:902-910`: `documents`

(Confirmed by source survey 2026-04-25; the prerequisite checklist
re-verifies these names at implementation time.) Each pair of
hardcoded calls collapses into one hook-chain fire.

### Existing Infrastructure Audit

| Proposed Component | Existing Module | Decision |
| --- | --- | --- |
| Batch hook list, register, fire | `mcp_infra.py:_post_store_hooks`, `register_post_store_hook`, `fire_post_store_hooks` | **Mirror**: parallel single-shape contract, same conventions, same failure isolation. |
| `_record_batch_hook_failure` | `mcp_infra.py:_record_hook_failure` (RDR-070) | **Mirror**: same T2 `hook_failures` table, same error truncation. JSON-serialize the doc_id list into the existing `doc_id` column. |
| Migrated taxonomy hook body | `mcp_infra.py:453` (`taxonomy_assign_batch`) | **Move + rename**: function body unchanged, called via the chain instead of directly. |
| Migrated chash hook body | `mcp_infra.py:507` (`chash_dual_write_batch`, RDR-086) | **Move + rename**: function body unchanged. Same migration shape as taxonomy; same registration call. |
| Caller-side replacements | `indexer.py:807-815`, `code_indexer.py:435-443`, `prose_indexer.py:200-208`, `pipeline_stages.py:408-416`, `doc_indexer.py:373-381`, `doc_indexer.py:499-507`, `doc_indexer.py:902-910` | **Collapse**: each pair of hardcoded calls (chash + taxonomy) collapses into one `fire_post_store_batch_hooks` invocation. Seven sites across five files; ~5 lines removed and ~3 added per site. |
| Catalog-registration mechanisms | `_catalog_store_hook` (`commands/store.py:116`), `_catalog_pdf_hook` (`pipeline_stages.py:458`), `indexer.py:250` ad-hoc | **Out of scope, intentionally**: each captures different per-domain metadata (knowledge-curator + doc_id; corpus-curator + file_path + author + year + chunk_count; repo-owner + rel_path + source_mtime + file_hash). Consolidating would lose information or branch internally based on origin. Three legitimate per-domain registrations, not three copies of the same hook. |
| `_catalog_auto_link` | `mcp_infra.py:catalog_auto_link` | **Out of scope, intentionally**: reads T1 scratch entries tagged `link-context`, agent-seeded before MCP `store_put`. CLI bulk ingest has no per-file pre-declaration semantics; uses entirely separate post-hoc linkers in `link_generator.py`. MCP-only is path-shape coupled. |
| Single-document chain | `mcp_infra.py:296`, `mcp/core.py:887` | **Untouched**: the MCP `store_put` path keeps firing single-shape hooks (taxonomy_assign_hook, future single-shape consumers like RDR-089 aspect_assign_hook). |

### Decision Rationale

The framework has two real workload shapes (single-document MCP,
batch CLI) and one pretend-extensibility mechanism (single-shape
hooks, hardcoded batch calls). Adding a batch contract makes both
shapes first-class. The cost is small: roughly 30 lines of new
framework code (register + fire + failure capture), two function
renames (`taxonomy_assign_batch` and `chash_dual_write_batch` to
their `_hook` variants), and the seven call-site replacements.

The alternative is to keep the duplication: register single-shape
hooks for the MCP path, hardcode batch calls into the five CLI
files for any consumer that benefits from batching. This is a
permanent tax on every future per-document enrichment, paid in
multi-file edits and easy-to-miss coverage. The set has already
grown from four sites at first sighting to seven once the survey
broadened, then to fourteen call sites once `chash_dual_write_batch`
was counted. The pattern is the precedent: every new per-document
batch enrichment will copy itself into the same five files unless
the framework absorbs the work. RDR-089's verify pass caught the
tax explicitly. There will be more.

**Why catalog-registration and auto-linker are NOT in scope.**
The investigation that broadened the call-site count from seven
to fourteen also examined three other post-store mechanisms.
None match the same shape:

- The three catalog-registration paths (`_catalog_store_hook`,
  `_catalog_pdf_hook`, `indexer.py:250`'s ad-hoc registration)
  capture different per-domain metadata: knowledge-curator owner +
  doc_id for ad-hoc store; corpus-curator owner + file_path +
  author + year + chunk_count for PDFs; repo-owner +
  rel_path + source_mtime + file_hash for repo files. Consolidating
  would either lose information or branch internally based on
  origin. They are three legitimate per-domain registrations, not
  three copies of the same hook.
- `_catalog_auto_link` reads T1 scratch entries tagged
  `link-context` that agents seed *before* calling `store_put`.
  CLI bulk ingest has no equivalent pre-declaration semantics;
  it uses entirely separate post-hoc linkers
  (`generate_citation_links`, `generate_code_rdr_links`,
  `generate_rdr_filepath_links` in `link_generator.py`).
  MCP-only auto-linking is intentional path-shape coupling.

These were genuine deferrals (not scope-reduction debt) once the
shape comparison showed they are different mechanisms doing
different work. The two functions this RDR migrates
(`taxonomy_assign_batch` and `chash_dual_write_batch`) are the
real twins: identical call-site shape, identical seven sites,
identical batch-perf-optimization motivation.

The single-document chain stays. The MCP `store_put` tool still
uses it; future single-shape consumers (small, cheap, MCP-only
hooks) still register through it. We are not removing a contract,
we are adding the missing one.

The narrow batch-perf optimizations are preserved because each
migrated hook wraps its existing function body verbatim:
`taxonomy_assign_batch_hook` keeps the one-`query()`-per-batch
ChromaDB call, `chash_dual_write_batch_hook` keeps the
single-batched T2 SQL upsert. No regression on bulk ingest for
either consumer.

## Alternatives Considered

### Alternative 1: Force everything through single-document hooks

**Description**: Plumb `fire_post_store_hooks` into the seven CLI
ingest sites, delete `taxonomy_assign_batch`, accept the perf cost.

**Pros**: Smallest framework surface, single shape only.

**Cons**: 1000x bulk-ingest regression on taxonomy assignment for
medium-and-large corpora. The ChromaDB Cloud round-trip is the
load-bearing cost; replacing one batched query with N sequential
queries turns a 50ms operation into ~50s for 1000 documents. Not
acceptable.

**Reason for rejection**: Eats a real perf optimization for no
gain. The two-shape reality is intrinsic, not avoidable.

### Alternative 2: Pass embeddings through a single-document context dict

**Description**: Change `fire_post_store_hooks` signature to
`(doc_id, collection, content, **ctx)` where `ctx` carries optional
fields like `embedding`. Hooks consume what they need. Plumb the
chain into CLI paths, pass embeddings via `ctx`.

**Pros**: One contract, hooks pick what they consume, no batch
machinery.

**Cons**: Still N ChromaDB round-trips for taxonomy. The
embedding-reuse fixes the local re-embed cost (~1ms per document
becomes 0) but the dominant cost is the ChromaDB Cloud query call,
which happens regardless. Pre-fetching the embedding does not
collapse N queries into one.

**Reason for rejection**: Looks like a fix, is not. The shape
mismatch is at the ChromaDB API boundary, not at the embedding-
fetch boundary.

### Alternative 3: Leave it alone, RDR-089 absorbs the duplication

**Description**: RDR-089 plumbs `fire_post_store_hooks` into the
CLI sites for non-taxonomy enrichment only. Taxonomy keeps its
hardcoded path. The two paths become a documented perf split.

**Pros**: No framework changes, RDR-089 lands sooner.

**Cons**: Documents the duplication as a feature. Every future
enrichment author must understand the split: which workloads
batch (and need their own hardcoded fast path), which do not (and
register through the chain). The decision is fragile, mostly
unwritten, and grows in cost with each new consumer.

**Reason for rejection**: Defers cost without reducing it.
Acceptable as a tactical fallback if RDR-095 cannot land in time
for RDR-089, not as a long-term shape.

### Briefly Rejected

- **Make taxonomy register both single and batch under one hook
  object**: conflates two contracts at the registration call site;
  callers either fire single or fire batch, never both per document.
- **Replace taxonomy's CLI fast path with a discover-only
  rebuild**: corpus-level rebuild is not a substitute for per-
  document assignment; freshness lag would be hours instead of
  milliseconds.

## Trade-offs

### Consequences

- Framework gains roughly 30 lines: `register_post_store_batch_hook`,
  `fire_post_store_batch_hooks`, `_record_batch_hook_failure`.
- `taxonomy_assign_batch` becomes `taxonomy_assign_batch_hook`,
  body unchanged, called via the chain.
- `chash_dual_write_batch` becomes `chash_dual_write_batch_hook`,
  body unchanged, called via the chain.
- Five CLI ingest files (seven sites) each lose two hardcoded
  function calls and gain one `fire_post_store_batch_hooks` call.
  Net per site: roughly 5 lines removed, 3 lines added.
- T2 `hook_failures` rows from batch hooks store the doc_id list
  as a JSON array in the existing `doc_id` column. Callers that
  parse the column must tolerate either shape.
- New per-document enrichments (RDR-089 aspects, future RDRs) get
  a clean registration path that covers every ingest.
- The three catalog-registration mechanisms and the auto-linker
  remain as-is. RDR-095 does not move them; their per-domain
  semantics make them legitimate non-twins of the batch-hook
  pattern.

### Risks and Mitigations

- **Risk**: A registered batch hook raises mid-way through a
  bulk-ingest fire, partial work commits, partial does not.
  **Mitigation**: per-hook failure capture wraps the entire batch
  invocation; failures are atomic at hook granularity. The
  registered batch hook owns its own atomicity (current
  `taxonomy.assign_batch` already handles partial failures by
  returning a smaller `assigned` count). Consumer hooks document
  their atomicity model.

- **Risk**: Hook-registration ordering matters when one hook reads
  state another wrote.
  **Mitigation**: registration order is deterministic (append to
  list), MCP server startup imports are deterministic. The current
  call-site ordering (chash before taxonomy) is preserved by
  registering `chash_dual_write_batch_hook` first. If a richer
  cross-hook dependency emerges later, the framework can grow a
  priority argument; today it does not need one.

- **Risk**: The seven CLI call-site replacements drift apart over
  time as authors add similar code without using the chain.
  **Mitigation**: a static check (grep-based, in `tests/` or
  `scripts/checks/`) asserts that no source file outside
  `mcp_infra.py` calls `taxonomy_assign_batch_hook` or
  `chash_dual_write_batch_hook` directly. Wire into `nx doctor`
  if desirable.

- **Risk**: A future contributor adding a new per-document batch
  enrichment copies the hardcoded pattern (mirroring what
  `chash_dual_write_batch` did to `taxonomy_assign_batch`) instead
  of registering a hook.
  **Mitigation**: documentation in CLAUDE.md and the post-store
  hook docstring explicitly directs new consumers to register a
  hook; the drift guard catches direct-call regressions; the
  three already-migrated examples (taxonomy, chash, future RDR-089)
  serve as worked precedents.

### Failure Modes

- **Visible**: a registered batch hook raises, structured log
  entry plus T2 `hook_failures` row (with the full doc_id list in
  `batch_doc_ids`), ingest completes successfully.
- **Visible**: callers pass empty `doc_ids`/`embeddings`, fire-
  function returns early, no hooks called.
- **Partial-commit, partial-diagnostic** (new failure-isolation
  shape): a batch hook makes multiple internal dependency calls
  (e.g. `taxonomy_assign_batch_hook` issues two ChromaDB queries:
  same-collection then cross-collection projection). If the second
  call raises after the first committed, up to N same-collection
  assignments land but zero cross-collection projections. The
  `hook_failures` row captures the doc_id list and the exception,
  but does not capture which sub-step succeeded. This is a
  legitimate weakening relative to the per-document granularity of
  the single-doc chain (which fires once per `store_put` and so
  cannot have partial-commit at the framework level). Mitigation
  options:
  - **Document the contract**: registered batch hooks are atomic
    at the hook-invocation level, not the per-document level
    within the hook. Hook authors are responsible for their own
    sub-step atomicity.
  - **Sub-step recovery is hook-internal**: `taxonomy.assign_batch`
    already returns a smaller `assigned` count on partial query
    failures within a single ChromaDB call. Authors of new batch
    hooks should follow this pattern (catch transient errors at
    the dependency boundary, return partial-success counts).
  - The framework does not introduce per-sub-step failure capture
    in this RDR; doing so would couple the framework to specific
    consumer shapes. Future RDR can add a `record_partial_progress`
    helper if a consumer needs it.
- **Silent**: a registered batch hook silently does the wrong
  thing (mis-assigns to wrong topic, writes to wrong table). Same
  failure mode as today; the chain does not introduce new silent
  failures.

## Implementation Plan

### Prerequisites

- [ ] Confirm test fixtures: `tests/test_chash_index_store.py:454`
  is a docstring reference, not a mock. Search broader test suite
  for any direct mocks of `taxonomy_assign_batch` or
  `chash_dual_write_batch`; migrate test fixtures to mock the
  hooks instead if any are found.
- [ ] Identify per-document content sources and metadata sources at
  each of the seven CLI call sites (local variable names confirmed
  in the Technical Design section: `documents` at six sites,
  `batch_docs` at `doc_indexer.py:499-507`). Re-verify at
  implementation time in case unrelated edits have shifted names.
- [ ] **Drift guard wired before migration lands**, not after. The
  Phase 3 static check that asserts no production source file
  outside `mcp_infra.py` calls `taxonomy_assign_batch_hook` or
  `chash_dual_write_batch_hook` directly belongs in the test suite
  the moment the hooks are renamed (Phase 2 Step 1), so any
  call-site replacement that drifts will fail CI. Promoting from
  Phase 3 prevents the very debt-accretion this RDR exists to
  reverse.

### Minimum Viable Validation

- [ ] One probe batch hook registered in a unit test, fires on a
  10-document fixture indexed via `nx index repo`, asserts the
  hook saw all 10 documents in the expected collection.
- [ ] Existing taxonomy integration test passes after migration
  (no behavior regression on bulk ingest).
- [ ] Manual smoke: `nx index pdf` on a small corpus, verify topic
  assignments still land in T2 `topic_assignments` exactly as
  before.

### Phase 1: Add the batch contract

#### Step 1: New framework primitives in `mcp_infra.py`

- Add `_post_store_batch_hooks` list.
- Add `register_post_store_batch_hook(fn)`.
- Add `fire_post_store_batch_hooks(doc_ids, collection, contents,
  embeddings, metadatas)` with both optional payloads.
- Add `_record_batch_hook_failure(doc_ids, collection, hook_name,
  error)`.

#### Step 2: Unit tests for the new primitives

`tests/test_mcp_infra.py`: register a probe hook, fire, assert
arguments. Failure-isolation test: probe hook raises, fire still
returns, second probe hook still fires, T2 `hook_failures` row
written.

### Phase 2: Migrate taxonomy and chash dual-write

#### Step 1: Rename and convert both hooks

- `taxonomy_assign_batch(doc_ids, collection, embeddings)` becomes
  `taxonomy_assign_batch_hook(doc_ids, collection, contents,
  embeddings, metadatas)`. Body unchanged; reads `embeddings`,
  ignores `contents` and `metadatas`.
- `chash_dual_write_batch(doc_ids, collection, metadatas)` becomes
  `chash_dual_write_batch_hook(doc_ids, collection, contents,
  embeddings, metadatas)`. Body unchanged; reads `metadatas`,
  ignores `contents` and `embeddings`.
- Register at MCP server startup, chash first then taxonomy:
  ```python
  register_post_store_batch_hook(chash_dual_write_batch_hook)
  register_post_store_batch_hook(taxonomy_assign_batch_hook)
  ```
  Order preserves the existing chash-before-taxonomy invariant
  from the call sites.

#### Step 2: Replace all seven CLI call sites

In each of `indexer.py:807-815`, `code_indexer.py:435-443`,
`prose_indexer.py:200-208`, `pipeline_stages.py:408-416`,
`doc_indexer.py:373-381`, `doc_indexer.py:499-507`,
`doc_indexer.py:902-910`, replace BOTH the direct
`chash_dual_write_batch` call AND the direct
`taxonomy_assign_batch` call (currently fired in pairs) with a
single
`fire_post_store_batch_hooks(doc_ids, collection, contents,
embeddings, metadatas)` invocation.

Each call site already has `contents`, `embeddings`, and
`metadatas` in scope (the same sources currently feeding the
paired hardcoded calls); confirm during the prerequisite survey.
The three `doc_indexer.py` sites correspond to three distinct
ingest entry points within that file (initial bulk write,
batch-loop within bulk indexing, and per-document path); each
gets its own caller-side replacement.

### Phase 3: `nx taxonomy status` reader update and docs

#### Step 1: Update `nx taxonomy status` to render batch failures

The `hook_failures` reader (today displays the scalar `doc_id`
column) gains a branch for `is_batch=1` rows that parses
`batch_doc_ids` and renders the doc_id list. Without this, batch
failures show up as opaque rows whose `doc_id` field is the first
id in the batch with no indication that more docs were affected.

#### Step 2: Update CLAUDE.md and `docs/architecture.md`

Brief note on the dual-shape contract: single-document hooks for
ad-hoc/MCP enrichment, batch hooks for bulk-ingest enrichment with
batched dependency calls. Cross-reference RDR-070 and RDR-095.
Also document the explicit out-of-scope decisions: catalog
registration mechanisms are per-domain by design; the auto-linker
is MCP-only by design.

### Day 2 Operations

| Resource | List | Info | Verify |
| --- | --- | --- | --- |
| Registered hooks | `nx doctor --check=hooks` | enumerate registered hook names | confirm taxonomy_assign_batch_hook, chash_dual_write_batch_hook, and future hooks are present |
| Hook failures | T2 `hook_failures` table via `nx memory list`-style probe | full row | spot-check error column for parseable JSON when batch-shaped |

### New Dependencies

None.

## Test Plan

- **Scenario**: register a no-op probe batch hook, run
  `fire_post_store_batch_hooks([], "x", [], None, None)`, verify
  return is None and no hooks fired (early return on empty
  doc_ids).
- **Scenario**: register two batch hooks where the first is a
  **synthetic raising probe** (the real `taxonomy_assign_batch_hook`
  body wraps everything in its own try/except and returns 0 on any
  exception, so it cannot exercise `fire_post_store_batch_hooks`'s
  failure capture). Verify the second probe still fires after the
  first raises, and that a `hook_failures` row is written with
  `is_batch=1`, the full doc_id list in `batch_doc_ids`, and the
  exception text in `error`.
- **Scenario**: a batch hook commits its first sub-step (e.g. a
  same-collection ChromaDB query succeeds) then raises in its
  second sub-step (cross-collection projection). Verify the
  partial-commit failure mode documented in Failure Modes:
  `hook_failures` row captures the doc_id list and exception, the
  same-collection assignments persisted, and structured logs
  identify the sub-step granularity.
- **Scenario**: run the full unit suite with both migrated batch
  hooks (`taxonomy_assign_batch_hook`, `chash_dual_write_batch_hook`)
  registered, verify behavioral parity with pre-migration
  baseline.
- **Scenario**: `nx index repo .` against a fixture, assert that
  topic_assignments rows AND chash_index rows both match the
  pre-migration count.
- **Scenario**: registration order: register chash hook first,
  taxonomy second; assert chash row exists before taxonomy
  assignment runs (probe via inserted hook between them).
- **Scenario**: drift guard regex assertion fails when a new file
  imports `taxonomy_assign_batch_hook` or
  `chash_dual_write_batch_hook` outside `mcp_infra.py`. The
  assertion is wired in Phase 2 (alongside the renames) so any
  call-site replacement that drifts fails CI immediately.
- **Scenario**: `hook_failures` schema migration. After
  `nx upgrade` runs the additive migration, verify (a) existing
  scalar-`doc_id` rows are unchanged, (b) new `batch_doc_ids` and
  `is_batch` columns exist with appropriate defaults, (c) writing
  a batch-shape failure populates the new columns and the scalar
  `doc_id` field carries a representative id, (d) `nx taxonomy
  status` renders both shapes correctly.

## Validation

### Testing Strategy

Unit tests for the new framework primitives cover the
register/fire/failure-isolation triad. Existing taxonomy
integration tests cover behavioral parity (no regression). A small
manual smoke covers the cross-cutting wiring (full ingest pipeline
fires hooks; topic assignments land).

### Performance Expectations

Per-batch fire overhead: a single Python list iteration over
registered hooks (microseconds). The hook bodies do the real work.
Bulk-ingest taxonomy assignment and chash dual-write are both
unchanged from pre-migration because each migrated hook wraps the
existing batched implementation verbatim. The two hooks fire
sequentially per batch, in registration order; total wall-clock
time per batch is the sum of the existing two hardcoded calls
(no change from today).

## Finalization Gate

_To be completed during /nx:rdr-gate._

## References

- `src/nexus/mcp_infra.py:296`. Current single-document hook
  framework
- `src/nexus/mcp_infra.py:365`. `taxonomy_assign_hook`
- `src/nexus/mcp_infra.py:453`. `taxonomy_assign_batch` (one of
  the two hardcoded fast paths being migrated)
- `src/nexus/mcp_infra.py:507`. `chash_dual_write_batch` (RDR-086;
  the second hardcoded fast path being migrated alongside taxonomy)
- `src/nexus/db/t2/catalog_taxonomy.py:2031`. `assign_batch`,
  the perf-critical ChromaDB-batched query
- `src/nexus/db/t2/chash_index.py`. Chash dual-write target
- `src/nexus/mcp/core.py:887`. Current single fire site
- `src/nexus/indexer.py:807-815`, `src/nexus/code_indexer.py:435-443`,
  `src/nexus/prose_indexer.py:200-208`,
  `src/nexus/pipeline_stages.py:408-416`,
  `src/nexus/doc_indexer.py:373-381`,
  `src/nexus/doc_indexer.py:499-507`,
  `src/nexus/doc_indexer.py:902-910`. The seven CLI ingest sites
  where each pair of hardcoded calls is being collapsed into one
  `fire_post_store_batch_hooks` invocation
- `src/nexus/commands/store.py:116`, `src/nexus/pipeline_stages.py:458`,
  `src/nexus/indexer.py:250`. The three catalog-registration
  mechanisms that were investigated and explicitly excluded from
  scope (per-domain semantics; not twins of the batch-hook pattern)
- `src/nexus/catalog/auto_linker.py`, `src/nexus/mcp_infra.py:catalog_auto_link`.
  The auto-linker, investigated and explicitly excluded from scope
  (T1-link-context driven; agent-only by design)
- RDR-060: rename detection that motivates `file_hash` capture in
  the indexer.py catalog registration
- RDR-070: taxonomy infrastructure that introduced the
  single-document hook chain (RF-070-6 designed it as the
  canonical extensibility point)
- RDR-076: T2 migration framework (T2 `hook_failures` table)
- RDR-086: chash dual-write (the second hardcoded enrichment, now
  migrated alongside taxonomy)
- RDR-089: aspect extraction (the consumer that surfaced the
  framework gap)
