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
(Liquibase baseline FTS5 to tsvector), .11/.14/.18 (plans/taxonomy/catalog migrations),
and all phase gates.

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
  battery.

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

**Postgres tsvector mapping:**
```sql
-- Column: tsvector pre-computed, updated by trigger
ALTER TABLE memory ADD COLUMN fts_vec tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(tags,'')), 'B')  ||
        setweight(to_tsvector('english', coalesce(content,'')), 'C')
    ) STORED;
CREATE INDEX idx_memory_fts_vec ON memory USING GIN(fts_vec);
-- Query: ORDER BY ts_rank(fts_vec, plainto_tsquery('english', ?)) DESC
```

**Escalation risk:** LOW. No caller asserts ordering; set-equality alone suffices.

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
  (line 886): single result, `results[0]["name"]` -- guarded implicitly,
  safe.
- `tests/test_plan_library.py::test_search_plans_still_matches_raw_description`
  (line 906): single result, `results[0]["query"]` -- guarded, safe.
- `tests/test_plan_match.py::TestMatchTextRankRegression::test_specific_probe_hits_matching_verb`
  (line 821): asserts `matches[0].plan_id == research_id`. This is an FTS5
  fallback path test (no cosine cache). The assertion relies on FTS BM25
  ranking placing the matching plan at rank 1 over a dissimilar plan. **This
  is a genuine rank-order assertion that must be relaxed.** The relaxation
  should assert that the matching plan appears in the result set AND (if
  Spearman >= 0.90) appears in the top-2, or else convert to a set-membership
  assertion if rank-1 cannot be guaranteed after Spearman verification.

**Postgres tsvector mapping:**
```sql
ALTER TABLE plans ADD COLUMN fts_vec tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(match_text,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(tags,'')), 'B')       ||
        setweight(to_tsvector('english', coalesce(project,'')), 'C')
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
ALTER TABLE documents ADD COLUMN fts_vec tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(author,'')), 'B') ||
        setweight(to_tsvector('english', coalesce(corpus,'')), 'C') ||
        setweight(to_tsvector('english', coalesce(file_path,'')), 'D')
    ) STORED;
CREATE INDEX idx_documents_fts_vec ON documents USING GIN(fts_vec);
-- Query (no ORDER BY, preserving current behaviour):
-- WHERE fts_vec @@ plainto_tsquery('english', ?)
```

**Escalation risk:** LOW. No caller relies on ordering. Set-equality alone
suffices. Note that `file_path` contains path separators (`/`) which the
English dictionary tokenizer does not split on; paths must be indexed with
`simple` config or a custom parser to match the current FTS5 whitespace
tokenization. Flag as a correctness risk at the implementation gate -- not a
ranking risk.

---

## Summary Table

| Store | Virtual table | Owner | Indexed columns | Ranking | Order-asserting tests | Escalation risk |
|-------|--------------|-------|-----------------|---------|----------------------|-----------------|
| MemoryStore | `memory_fts` | `memory_store.py` | title, content, tags | BM25 `ORDER BY rank` | None | LOW |
| PlanLibrary | `plans_fts` | `plan_library.py` | match_text, tags, project | BM25 `ORDER BY rank` | `test_specific_probe_hits_matching_verb` (rank-1 pin, FTS5 path) | MEDIUM |
| CatalogDB | `documents_fts` | `catalog.py` | title, author, corpus, file_path | None (insertion order) | None | LOW (correctness: file_path tokenization) |

---

## Parity Harness Specification

### Overview

For each store, the parity harness runs the store's existing fixture queries
against both the live SQLite FTS5 path and the Postgres tsvector path, then
asserts:

1. Top-K set equality (exact): the set of result identifiers is identical.
2. Spearman rho >= 0.90 where ordering is asserted or relied upon.

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

- Rank ties in either backend are broken by identity field (stable sort by id
  or tumbler string) before computing Spearman. This ensures deterministic
  rho computation even when multiple results have equal rank scores.
- Spearman is computed only when two or more results are returned. Single-result
  queries contribute to set-equality but not to rho (rho is undefined for K=1).
- If the Postgres result set contains results not present in the SQLite set (or
  vice versa), set equality FAILS before Spearman is computed. Do not attempt
  Spearman on mismatched sets.

### Test Structure (per store)

```python
# tests/db/test_fts_parity_<store>.py
@pytest.mark.parametrize("query,expected_ids", FIXTURE_BATTERY)
def test_set_equality(sqlite_store, pg_store, query, expected_ids):
    sqlite_ids = {r[identity_field] for r in sqlite_store.search(query)}
    pg_ids = {r[identity_field] for r in pg_store.search(query)}
    assert sqlite_ids == pg_ids == set(expected_ids)

@pytest.mark.parametrize("query,expected_ids", ORDERED_FIXTURE_BATTERY)
def test_spearman_floor(sqlite_store, pg_store, query, expected_ids):
    sqlite_results = sqlite_store.search(query)
    pg_results = pg_store.search(query)
    if len(sqlite_results) < 2:
        pytest.skip("single result, Spearman undefined")
    sqlite_order = [r[identity_field] for r in sqlite_results]
    pg_order = [r[identity_field] for r in pg_results]
    # Align pg_order to sqlite universe (identical by set equality).
    rho = spearman_rho(sqlite_order, pg_order)
    assert rho >= 0.90, f"Spearman {rho:.3f} < 0.90 for query {query!r}"
```

The `spearman_rho` helper uses the standard formula over positional ranks; scipy
is available in the test environment. Fixture battery construction: each test
populates a fresh SQLite temp DB and a fresh Postgres schema (using the same
Liquibase changelog as production) via pytest fixtures, inserts the canonical
fixture corpus (copy of the store's unit-test corpus), then queries both.

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

All three stores use `_sanitize_fts5` (defined in `memory_store.py`, re-exported
from `nexus.db.t2`) to escape FTS5 special characters before passing queries to
`MATCH`. The Postgres migration must provide an equivalent sanitizer that escapes
`plainto_tsquery` / `phraseto_tsquery` / `websearch_to_tsquery` input. The
`plainto_tsquery` function ignores most special characters by design and is the
safest drop-in. Any query that raises a `ValueError` on the SQLite path
(via the `OperationalError` catch in each search method) must also be handled
gracefully on the Postgres path.

---

## File Path Tokenization Note (CatalogDB)

The `documents_fts` table indexes `file_path` (e.g.
`src/nexus/db/t2/memory_store.py`). SQLite FTS5 with its default `unicode61`
tokenizer treats `/` as a separator, producing tokens `src`, `nexus`, `db`,
`t2`, `memory_store`. Postgres `to_tsvector('english', ...)` also splits on
`/` (it is not an alphabetic character). Behavior should be equivalent, but
must be verified empirically in the harness against path-substring queries.
If a discrepancy is found, replace `'english'` config with `'simple'` for the
`file_path` component to avoid stemming artifacts on path tokens.
