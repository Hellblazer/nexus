---
title: "RDR-152 FTS5 to tsvector Parity Contract"
relates: [RDR-152]
status: locked
locked_by: nexus-gmiaf.2
---

# RDR-152 FTS5 to tsvector Parity Contract

## Purpose

This document is the locked parity contract for the FTS5-to-tsvector migration
in RDR-152 (Postgres Java Storage Service). It defines: (1) what parity means
per store, (2) the per-store inventory, and (3) the parity harness specification
each store's migration gate must satisfy. It is referenced by beads nexus-gmiaf.5
(Liquibase baseline FTS migration), .11 (plans), .14 (taxonomy -- see Store 4
note), .18 (catalog), and all phase gates.

## Locked Parity Definition

Byte-identical result ORDER is NOT achievable across the SQLite FTS5 and
Postgres tsvector paths. The tokenization models, index structures (BM25
inverted index vs GIN posting list), and ranking functions (FTS5 BM25 vs
ts_rank / ts_rank_cd) differ in ways that cannot be reconciled by tuning alone.
Tests that assert exact position must be explicitly relaxed.

Parity is defined per store as both of the following:

- **Set equality**: the top-K result set (by document identity) is identical
  between the SQLite and Postgres paths for every query in the labeled fixture
  battery. This assertion must hold exactly (no tolerance).
- **Rank correlation**: where the store's callers rely on ordering, the
  Spearman rank correlation coefficient rho must be >= 0.90 between the SQLite
  BM25 order and the Postgres ts_rank order, measured over the labeled fixture
  battery. Spearman is computed ONLY when set equality holds first; mismatched
  sets fail before reaching the rho assertion.

A store that CANNOT meet the Spearman floor must ESCALATE to substantive-critic
sign-off at its migration gate. The threshold is NEVER silently lowered by the
migration author. The escalation path is: document the shortfall in the gate PR
body, flag the failing query, and obtain explicit written substantive-critic
sign-off before merging. If the floor cannot be met at all for a given store,
the migration author must propose an alternative ranking strategy (e.g. switch
to ts_rank_cd, add a column-weight config, or accept positional non-determinism
as declared in the gate PR).

Stores that currently assert EXACT FTS order in their tests must have those
assertions RELAXED to this parity definition as an EXPLICIT, REVIEWED migration
step. The relaxation must appear as a named diff in the store's gate PR (not
squashed into the migration commit) so that a reviewer can see precisely which
assertions changed and why.

## Tokenization Configuration Rule

FTS5 uses the `unicode61` tokenizer (no stemming for most token shapes;
underscore is word-internal). The Postgres tsvector configuration to use depends
on column content:

- **Prose columns** (title, content, match_text, author, corpus): use
  `to_tsvector('english', ...)`. English stemming is acceptable; the fixture
  queries in the parity battery are not expected to produce set-equality
  failures from stemming on prose terms.
- **Identifier / tag / path columns** (tags, project, file_path): use
  `to_tsvector('simple', ...)`. Simple config disables stemming and stopwords.
  This is REQUIRED because:
  - `tags` values are CSV identifiers like `rdr`, `review,critical`,
    `rdr-152,fts,contract`. FTS5 `unicode61` keeps underscores and hyphens
    word-internal; `'english'` would stem or drop them. `'simple'` tokenizes
    consistently with `unicode61` behavior on these inputs.
  - `project` values are identifiers like `nexus_active`, `nexus_rdr`.
    `'english'` splits on `_` and may stem tokens; `'simple'` preserves them.
  - `file_path` values contain path separators (`/`) and underscore-separated
    tokens like `memory_store`. Both FTS5 `unicode61` and Postgres `'simple'`
    split on `/` and keep underscore-separated tokens intact. Using `'english'`
    risks stemming `store` to `store` (harmless) but also treating `src` as
    a stopword or splitting `memory_store` unpredictably on some PG versions.
    Use `'simple'` unconditionally for `file_path`. This is a COMMITTED
    decision, not an empirical question.

The `plainto_tsquery` input sanitizer uses `'english'` for prose query strings
and `'simple'` for tag/identifier queries. The Postgres migration layer must
select the correct tsquery config to match the indexed column's config; mixing
configs between index and query produces zero matches.

## Per-Store Inventory

### Store 1: memory_fts (T2 MemoryStore)

| Field | Value |
|-------|-------|
| Virtual table | `memory_fts` |
| Owner module | `src/nexus/db/t2/memory_store.py` |
| Base table | `memory` (columns: id, project, title, session, agent, content, tags, timestamp, ttl, access_count, last_accessed) |
| Indexed columns | `title`, `content`, `tags` (content= external-content table, content_rowid='id') |
| Ranking expression | `ORDER BY rank` (implicit FTS5 BM25, descending relevance) |
| Search methods | `search(query, project)`, `search_glob(query, project_glob)`, `search_by_tag(query, tag)` |
| Callers assert order? | NO -- tests assert set membership and single-result identity (len==1 + field value), never multi-result position |

**Test files:** `tests/test_memory.py`, `tests/test_memory_consolidation.py`

**Order-asserting tests that must be relaxed:** None. All search tests assert
set equality via `{r["title"] for r in results}` or single-hit identity
after `len(results) == 1`. The `results[0]["project"] == "proj_a"` assertion
at line 118 of `tests/test_memory.py` is guarded by `len(results) == 1`.
No tests assert the relative order of two or more memory search results.

**Scope of FTS-parity contract for this store:** Only the `search(query,
project)` method is in scope. `search_glob` and `search_by_tag` append
SQL-level `GLOB` / `LIKE` constraints that are NOT FTS constructs; their
correctness is governed by schema parity, not by this FTS-parity contract.

**Fixture coverage note:** The existing fixture battery for memory_fts has
effectively one Spearman-eligible query: `test_memory_search_fts5` produces
K=2 results ("quick fox" matching two entries). `test_memory_search_scoped_to_project`
produces K=1 (Spearman undefined, skipped). This is an acknowledged coverage
gap. The gate PR body for this store's migration MUST note the near-vacuous
rho evidence. The LOW escalation risk classification stands because no caller
asserts ordering.

**Postgres tsvector mapping:**
```sql
-- tsvector computed, auto-updated by Postgres on every row write (STORED generated column)
ALTER TABLE memory ADD COLUMN fts_vec tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')),  'A') ||
        setweight(to_tsvector('simple',  coalesce(tags,'')),   'B') ||
        setweight(to_tsvector('english', coalesce(content,'')), 'C')
    ) STORED;
CREATE INDEX idx_memory_fts_vec ON memory USING GIN(fts_vec);
-- Query (prose): ORDER BY ts_rank(fts_vec, plainto_tsquery('english', ?)) DESC
```

**Escalation risk:** LOW. No caller asserts ordering; set-equality alone
suffices. Acknowledged coverage gap: near-vacuous Spearman evidence (K=2
max); must be noted in the gate PR body.

---

### Store 2: plans_fts (T2 PlanLibrary)

| Field | Value |
|-------|-------|
| Virtual table | `plans_fts` |
| Owner module | `src/nexus/db/t2/plan_library.py` |
| Base table | `plans` (columns: id, project, query, plan_json, outcome, tags, ..., match_text, disabled_at) |
| Indexed columns | `match_text`, `tags`, `project` (content= external-content, content_rowid='id') |
| Ranking expression | `ORDER BY rank` (FTS5 BM25) in `search_plans()` |
| Search methods | `search_plans(query, limit, project)` |
| Callers assert order? | YES -- see below |

**Test files:** `tests/test_plan_library.py`, `tests/test_plan_match.py`

**Order-asserting tests that must be relaxed at the store's gate:**

- `tests/test_plan_library.py::test_search_plans_match` (line 44): single
  result, `results[0]["query"]` -- guarded by `len == 1`, safe after
  set-equality.
- `tests/test_plan_library.py::test_search_plans_tags` (line 54): single
  result, `results[0]["tags"]` -- guarded by `len == 1`, safe.
- `tests/test_plan_library.py::test_search_plans_project_filter` (line 121):
  single result, `results[0]["project"]` -- guarded by `len == 1`, safe.
- `tests/test_plan_library.py::test_search_plans_includes_ttl` (line 164):
  single result, `results[0]["ttl"]` -- guarded by `len == 1`, safe.
- `tests/test_plan_library.py::test_search_plans_hits_on_dimensional_suffix`
  (line 886): single result, `results[0]["name"]` -- guarded implicitly, safe.
- `tests/test_plan_library.py::test_search_plans_still_matches_raw_description`
  (line 906): single result, `results[0]["query"]` -- guarded, safe.
- `tests/test_plan_match.py::TestMatchTextRankRegression::test_specific_probe_hits_matching_verb`
  (line 821): asserts `matches[0].plan_id == research_id`. This is an FTS5
  fallback path test (no cosine cache). The assertion relies on FTS BM25
  ranking placing the matching plan at rank 1 over a dissimilar plan. **This
  is a genuine rank-order assertion that must be relaxed.** The relaxation
  should assert that the matching plan appears in the result set AND (if
  Spearman >= 0.90 is verified empirically) appears in the top-2, or else
  convert to a set-membership assertion if rank-1 cannot be guaranteed.

**Postgres tsvector mapping:**
```sql
-- tsvector computed, auto-updated by Postgres on every row write (STORED generated column)
ALTER TABLE plans ADD COLUMN fts_vec tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(match_text,'')), 'A') ||
        setweight(to_tsvector('simple',  coalesce(tags,'')),       'B') ||
        setweight(to_tsvector('simple',  coalesce(project,'')),    'C')
    ) STORED;
CREATE INDEX idx_plans_fts_vec ON plans USING GIN(fts_vec);
-- Query: ORDER BY ts_rank(fts_vec, plainto_tsquery('english', ?)) DESC
```

**Escalation risk:** MEDIUM. One FTS-path rank-1 assertion exists
(`test_specific_probe_hits_matching_verb`). The test seeds two plans with
distinct vocabulary ("Walk from an RDR to implementing code modules" vs
"Critique a change set vs prior decisions") and probes with "research
find-by-author". The token overlap is strong and unambiguous. Spearman >= 0.90
is likely achievable, but the rank-1 pin must be verified empirically. Flag
for escalation if ts_rank consistently places the mismatching plan first.

---

### Store 3: documents_fts (T2 CatalogDB)

| Field | Value |
|-------|-------|
| Virtual table | `documents_fts` |
| Owner module | `src/nexus/db/t2/catalog.py` |
| Base table | `documents` (columns: rowid, tumbler, title, author, corpus, file_path, ...) |
| Indexed columns | `title`, `author`, `corpus`, `file_path` (content= external-content, content_rowid=rowid) |
| Ranking expression | NO ORDER BY -- implicit rowid insertion order from FTS5 join |
| Search methods | `CatalogDB.search(query, content_type)`, called via `Catalog.find(query, content_type)` and MCP `catalog_search` |
| Callers assert order? | NO -- all tests assert set membership or single-result identity |

**Test files:** `tests/test_catalog_db.py`, `tests/test_catalog.py`,
`tests/test_catalog_mcp.py`

**Order-asserting tests that must be relaxed:** None. `CatalogDB.search`
does NOT include `ORDER BY rank` in its SQL (see `src/nexus/db/t2/catalog.py`
line 947). All catalog search tests assert `{r["tumbler"] for r in result}` or
`len == 1` with field identity. The `results[0]["tumbler"]` assertions in
`tests/test_catalog_db.py` (lines 162, 175, 197, 265) are all single-hit tests.

**Postgres tsvector mapping:**
```sql
-- tsvector computed, auto-updated by Postgres on every row write (STORED generated column)
-- file_path and corpus use 'simple' (no stemming) per the tokenization config rule above.
-- author uses 'simple' (author names are identifiers, not prose).
ALTER TABLE documents ADD COLUMN fts_vec tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')),     'A') ||
        setweight(to_tsvector('simple',  coalesce(author,'')),    'B') ||
        setweight(to_tsvector('simple',  coalesce(corpus,'')),    'C') ||
        setweight(to_tsvector('simple',  coalesce(file_path,'')), 'D')
    ) STORED;
CREATE INDEX idx_documents_fts_vec ON documents USING GIN(fts_vec);
-- Query (no ORDER BY, preserving current behaviour):
-- WHERE fts_vec @@ plainto_tsquery('english', ?)
-- Note: if the query targets a path or identifier token, use plainto_tsquery('simple', ?)
```

**Escalation risk:** LOW. No caller relies on ordering. Set-equality alone
suffices. The `file_path` tokenization decision is COMMITTED to `'simple'`
(see Tokenization Configuration Rule above); no empirical verification gate
is needed for this choice.

---

### Store 4: CatalogTaxonomy (no FTS -- bead .14 clarification)

| Field | Value |
|-------|-------|
| Virtual table | NONE |
| Owner module | `src/nexus/db/t2/catalog_taxonomy.py` |
| FTS5 usage | Zero FTS5 virtual tables. Topics are queried by exact label/collection equality and `doc_count` sort order only. |

**There is no FTS parity work for the taxonomy store.** Bead nexus-gmiaf.14
appears in the reference list because the parent RDR's FTS note named taxonomy
at a higher abstraction level. At the implementation level, `catalog_taxonomy.py`
has no `USING fts5` table and no `MATCH` clause; no tsvector column or GIN
index is needed.

The bead .14 migration gate MUST NOT create an FTS column for taxonomy. Its
parity contract is: exact preservation of column-equality query semantics and
`doc_count` sort order. The sort is a plain `ORDER BY doc_count DESC` on a
numeric column -- deterministic across both backends.

---

## Summary Table

| Store | Virtual table | Owner | Indexed columns | Ranking | Order-asserting tests | Escalation risk |
|-------|--------------|-------|-----------------|---------|----------------------|-----------------|
| MemoryStore | `memory_fts` | `memory_store.py` | title (english), content (english), tags (simple) | BM25 `ORDER BY rank` | None | LOW (coverage gap: K=2 max Spearman) |
| PlanLibrary | `plans_fts` | `plan_library.py` | match_text (english), tags (simple), project (simple) | BM25 `ORDER BY rank` | `test_specific_probe_hits_matching_verb` (rank-1 pin, FTS5 path) | MEDIUM |
| CatalogDB | `documents_fts` | `catalog.py` | title (english), author (simple), corpus (simple), file_path (simple) | None (insertion order) | None | LOW |
| CatalogTaxonomy | NONE | `catalog_taxonomy.py` | N/A | N/A | N/A | N/A -- no FTS migration |

---

## Parity Harness Specification

### Overview

For each FTS store (Stores 1-3), the parity harness runs the store's existing
fixture queries against both the live SQLite FTS5 path and the Postgres tsvector
path, then asserts:

1. Top-K set equality (exact): the set of result identifiers is identical.
2. Spearman rho >= 0.90 where ordering is asserted or relied upon, and ONLY
   when set equality holds.

The harness is NOT the unit tests themselves. It is a separate
`tests/db/test_fts_parity_<store>.py` file that the migration gate CI step
runs against both backends simultaneously.

### K and Fixture Sources

| Store | K | Fixture source | Identity field |
|-------|---|---------------|----------------|
| memory_fts | all returned (uncapped) | Queries from `test_memory_search_fts5` and `test_memory_search_scoped_to_project` in `tests/test_memory.py`; overlap queries from `tests/test_memory_consolidation.py` | `title` |
| plans_fts | min(limit, 10) | Queries from `test_search_plans_match`, `test_search_plans_tags`, `test_search_plans_project_filter`, `test_search_plans_hits_on_dimensional_suffix`, `test_search_plans_still_matches_raw_description` in `tests/test_plan_library.py`; probe text from `test_specific_probe_hits_matching_verb` in `tests/test_plan_match.py` | `id` (plan_id) |
| documents_fts | all returned (uncapped) | Queries from `test_search_by_title`, `test_search_by_author`, `test_search_with_content_type_filter`, `test_rebuild_via_bulk_load_matches_search_results` in `tests/test_catalog_db.py`; queries from `test_find_by_title`, `test_find_with_content_type` in `tests/test_catalog.py` | `tumbler` |

K=10 for plans_fts is chosen because `search_plans` default limit is 5; K=10
gives headroom for the Spearman computation without over-constraining the
ranking comparison.

### Spearman Computation

Set equality MUST pass first. Do not compute Spearman on mismatched sets.

When set equality holds and K >= 2, compute Spearman as follows:

```python
from scipy.stats import spearmanr

# sqlite_results and pg_results are each ordered lists of result dicts.
# Both contain the same identity values (set equality verified above).

universe = [r[identity_field] for r in sqlite_results]   # canonical sqlite order
sqlite_ranks = list(range(1, len(universe) + 1))          # [1, 2, ..., K]

# Map each pg result to its position in the sqlite universe (1-based).
# Ties in either backend: break by identity_field stable sort BEFORE this
# computation, so each result has a unique deterministic position.
pg_ranks = [universe.index(r[identity_field]) + 1 for r in pg_results]

rho, _ = spearmanr(sqlite_ranks, pg_ranks)
assert rho >= 0.90, f"Spearman {rho:.3f} < 0.90 for query {query!r}"
```

Additional rules:

- Ties in either backend's output are broken by stable sort on the identity
  field (string or integer) BEFORE building the rank vectors. This ensures
  the rank vectors are unique permutations and Spearman is well-defined.
- K < 2: skip Spearman (rho is undefined). The test calls `pytest.skip`.
- The `universe.index(...)` lookup is safe because set equality is pre-verified;
  every `pg_results` identity value is present in `universe`.

### Test Structure (per store)

```python
# tests/db/test_fts_parity_<store>.py
from scipy.stats import spearmanr

@pytest.mark.parametrize("query,expected_ids", FIXTURE_BATTERY)
def test_set_equality(sqlite_store, pg_store, query, expected_ids):
    sqlite_ids = {r[identity_field] for r in sqlite_store.search(query)}
    pg_ids = {r[identity_field] for r in pg_store.search(query)}
    assert sqlite_ids == pg_ids == set(expected_ids)


@pytest.mark.parametrize("query,expected_ids", ORDERED_FIXTURE_BATTERY)
def test_spearman_floor(sqlite_store, pg_store, query, expected_ids):
    sqlite_results = sorted(sqlite_store.search(query), key=lambda r: r[identity_field])
    # Re-sort by FTS rank after tie-breaking; FTS5 returns in rank order already,
    # so stable sort on identity_field for ties only.
    sqlite_results = sqlite_store.search(query)   # rank order
    pg_results = pg_store.search(query)           # rank order

    # Set equality pre-check (must pass before Spearman).
    assert {r[identity_field] for r in sqlite_results} == {r[identity_field] for r in pg_results}

    if len(sqlite_results) < 2:
        pytest.skip("single result, Spearman undefined")

    universe = [r[identity_field] for r in sqlite_results]
    sqlite_ranks = list(range(1, len(universe) + 1))
    pg_ranks = [universe.index(r[identity_field]) + 1 for r in pg_results]

    rho, _ = spearmanr(sqlite_ranks, pg_ranks)
    assert rho >= 0.90, f"Spearman {rho:.3f} < 0.90 for query {query!r}"
```

Fixture battery construction: each test populates a fresh SQLite temp DB and
a fresh Postgres schema (using the same Liquibase changelog as production)
via pytest fixtures, inserts the canonical fixture corpus (copy of the store's
unit-test corpus), then queries both.

### Escalation Path

If a store's parity harness cannot achieve Spearman >= 0.90 for any query in
the labeled battery:

1. The migration author documents the failing query, both result orders, and the
   computed rho in the gate PR body.
2. The PR is NOT merged until substantive-critic provides written sign-off.
3. The substantive-critic evaluates whether the ordering difference is
   user-visible (i.e., callers depend on rank-1 being a specific document)
   or benign (i.e., all callers iterate the full set). Only callers that rely
   on position N specifically require re-ranking mitigation.
4. If rank-1 is caller-visible and cannot clear the floor, the migration author
   must either: (a) switch to ts_rank_cd, (b) add explicit tiebreak columns,
   or (c) propose dropping the rank-order contract in the affected method's
   docstring and updating call sites.

The threshold is NEVER silently lowered. The escalation must be a recorded
artifact in the gate PR.

---

## Notes on `_sanitize_fts5`

All three FTS stores use `_sanitize_fts5` (defined in `memory_store.py`,
re-exported from `nexus.db.t2`) to escape FTS5 special characters before
passing queries to `MATCH`. The Postgres migration must provide an equivalent
sanitizer. `plainto_tsquery` ignores most special characters by design and is
the safest drop-in. Any query that raises a `ValueError` on the SQLite path
(via the `OperationalError` catch in each search method) must also be handled
gracefully on the Postgres path.

Note: the query passed to `plainto_tsquery` must use the same `'english'` or
`'simple'` config as the indexed column being searched. Mixing configs (e.g.
`plainto_tsquery('english', ...)` against a `'simple'`-indexed column) produces
zero matches.

---

## AMENDMENT-OPTION-B: Superset Criterion (2026-06-07)

**Decision:** OPTION B - Intentional upgrade. Recorded in T2 memory `152-FTS-tokenizer-DECISION`.
**Applies to:** Store 2 (plans_fts) and Store 1 (memory_fts). Committed on branch
`feature/nexus-gmiaf.11-plans-migration`, bead `nexus-gmiaf.11`.

### Background: the tokenizer mismatch

The `.11` parity harness exposed a latent within-PG config mismatch:

- `plans.fts_vector` stores tags via `to_tsvector('simple', tags)` (no stemming).
- The original `PlanRepository.searchPlans` used `plainto_tsquery('english', query)`
  exclusively. English stemming converts "indexing" to lexeme "index", which does NOT
  match the simple-tokenized lexeme "indexing" stored in the B-weight tag sub-vector.
- Result: FTS5 unicode61 (which does NOT stem) matched "indexing" tags correctly;
  PG did NOT match them (sqlite_set ⊄ pg_set). The original locked contract's
  set-equality criterion correctly flagged this as a failure.

The same latent bug exists in `MemoryRepository.search`, `searchGlob`, `searchByTag`.

### Fix (applied in this bead)

Both `PlanRepository` and `MemoryRepository` now use an OR'd tsquery:

```sql
fts_vector @@ (plainto_tsquery('english', ?) || plainto_tsquery('simple', ?))
```

The same OR'd tsquery is used for `ts_rank` scoring. jOOQ `{0}` double-binding
expands the single bind parameter to two identical values.

**Effect:** PG now matches via EITHER English-stemmed prose OR simple exact-identifier
tokens. English-stemming gives PG BETTER recall than FTS5 unicode61 on prose columns
(e.g. "index" matches "indexing" AND "indexed" AND "indexes" via stemming). Tag columns
match exactly (simple), consistent with FTS5 unicode61 on the same tokens.

### Amended parity criterion for stores 1 and 2

The locked §Parity Definition above defined **set equality** (exact). This amendment
REPLACES that criterion for stores 1 and 2 ONLY with the following:

1. **SUPERSET criterion** (replaces exact set equality):
   `set(sqlite_ids) ⊆ set(pg_ids)` per probe.
   - Every FTS5 result MUST appear in PG results.
   - PG MAY return additional results (from English stemming on prose columns).
     These are an intentional search-quality upgrade over FTS5 unicode61 and are
     NOT a parity failure.

2. **Spearman rho >= 0.90 over the COMMON SUBSET** (replaces full-set Spearman):
   - Filter PG results to only those IDs present in the SQLite set (the FTS5 result
     set), preserving PG rank order.
   - Compute Spearman rho comparing this filtered PG ordering against the SQLite
     canonical ordering.
   - The common-subset Spearman measures whether the FTS5 results appear in the same
     relative order within PG results -- the relevant quality signal.
   - Skipped when common-subset K < 2.

3. **Vacuity guard** (unchanged): at least one probe returns non-empty on BOTH engines.

The §Escalation Path (Spearman floor never silently lowered, PR sign-off required) is
unchanged and applies to the amended criterion.

### Why superset not exact equality

FTS5 `unicode61` tokenizer: no stemming. An FTS5 probe of "indexing" matches stored
text "indexing" exactly. PG English-tsquery probe "indexing" produces lexeme "index"
(stemmed), which matches: "index", "indexing", "indexed", "indexes" in the A-weight
prose sub-vector, AND is now paired with a simple-tsquery probe that matches "indexing"
exactly in the B-weight tag sub-vector. Net result: PG ⊇ FTS5. The additional PG hits
are improvements, not regressions.

Store 3 (documents_fts) and Store 4 (taxonomy) are unaffected by this amendment.
Store 3 uses `plainto_tsquery('english', ?)` against only prose columns (title,
author, corpus, file_path use `'simple'` but that store has no standalone tag
sub-vector matching prose queries). The original exact set-equality criterion
remains correct for Store 3.
