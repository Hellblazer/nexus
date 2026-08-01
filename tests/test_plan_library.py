# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for plan library (save_plan, search_plans, list_plans) in T2Database."""
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from tests._t2_fixture_ops import (
    seed_plan,
)


@pytest.fixture
def plan_db(tmp_path: Path) -> T2Database:
    """Fresh T2Database for plan library tests."""
    database = T2Database(tmp_path / "plans.db")
    yield database
    database.close()


def test_save_plan(plan_db: T2Database) -> None:
    """save_plan() inserts a row and returns an integer row ID."""
    row_id = plan_db.save_plan(
        query="how to index code",
        plan_json='{"steps": ["classify", "chunk", "embed"]}',
    )
    assert isinstance(row_id, int)
    assert row_id > 0

    row = plan_db.plans.get_plan(row_id)
    assert row is not None
    assert row["query"] == "how to index code"
    assert row["outcome"] == "success"
    assert row["tags"] == ""


def test_save_plan_json_stored(plan_db: T2Database) -> None:
    """save_plan() stores plan_json verbatim and it is retrievable as-is."""
    json_payload = '{"steps": ["step1", "step2"], "meta": {"version": 2}}'
    row_id = plan_db.save_plan(query="complex query", plan_json=json_payload)

    row = plan_db.plans.get_plan(row_id)
    assert row is not None
    assert row["plan_json"] == json_payload


def test_search_plans_match(plan_db: T2Database) -> None:
    """search_plans() returns plans whose query text matches the FTS5 query."""
    plan_db.save_plan(query="semantic search over code repositories", plan_json='{"steps":[]}')
    plan_db.save_plan(query="memory management in Python", plan_json='{"steps":[]}')

    results = plan_db.search_plans("semantic")
    assert len(results) == 1
    assert "semantic" in results[0]["query"]


def test_search_plans_tags(plan_db: T2Database) -> None:
    """search_plans() matches on the tags field via FTS5."""
    plan_db.save_plan(query="generic query", plan_json='{}', tags="indexing,code")
    plan_db.save_plan(query="another query", plan_json='{}', tags="memory,retrieval")

    results = plan_db.search_plans("indexing")
    assert len(results) == 1
    assert results[0]["tags"] == "indexing,code"


def test_search_plans_no_match(plan_db: T2Database) -> None:
    """search_plans() returns an empty list when no plans match the query."""
    plan_db.save_plan(query="index repository", plan_json='{}')

    results = plan_db.search_plans("xyzzy_nonexistent_term")
    assert results == []


def test_list_plans_ordered(plan_db: T2Database) -> None:
    """list_plans() returns plans ordered by created_at DESC (most recent first)."""
    # Timestamps are placed deliberately: save_plan stamps now() on both
    # substrates at second granularity, so three saves in a fast test tie and
    # the ordering assertion below would be reading insertion order, not
    # created_at (nexus-aqbrk).
    seed_plan(plan_db, query="first plan", created_at="2020-01-01T00:00:00Z")
    seed_plan(plan_db, query="second plan", created_at="2020-01-02T00:00:00Z")
    seed_plan(plan_db, query="third plan", created_at="2020-01-03T00:00:00Z")

    results = plan_db.list_plans()
    assert len(results) == 3
    assert results[0]["query"] == "third plan"
    assert results[1]["query"] == "second plan"
    assert results[2]["query"] == "first plan"


def test_list_plans_empty(plan_db: T2Database) -> None:
    """list_plans() returns an empty list when the plans table is empty."""
    results = plan_db.list_plans()
    assert results == []


def test_list_plans_limit(plan_db: T2Database) -> None:
    """list_plans() respects the limit parameter."""
    for i in range(5):
        plan_db.save_plan(query=f"plan number {i}", plan_json='{}')

    results = plan_db.list_plans(limit=3)
    assert len(results) == 3


def test_save_plan_with_project(plan_db: T2Database) -> None:
    """save_plan() stores the project field correctly."""
    row_id = plan_db.save_plan(
        query="find error patterns",
        plan_json='{"steps":[]}',
        project="nexus",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["project"] == "nexus"


def test_search_plans_project_filter(plan_db: T2Database) -> None:
    """search_plans() with project filter returns only matching project plans."""
    plan_db.save_plan(query="search code patterns", plan_json='{}', project="nexus")
    plan_db.save_plan(query="search code patterns", plan_json='{}', project="other")

    results = plan_db.search_plans("search", project="nexus")
    assert len(results) == 1
    assert results[0]["project"] == "nexus"

    # Without project filter, both returned
    all_results = plan_db.search_plans("search")
    assert len(all_results) == 2


def test_list_plans_project_filter(plan_db: T2Database) -> None:
    """list_plans() with project filter returns only matching project plans."""
    plan_db.save_plan(query="plan a", plan_json='{}', project="nexus")
    plan_db.save_plan(query="plan b", plan_json='{}', project="other")
    plan_db.save_plan(query="plan c", plan_json='{}', project="nexus")

    results = plan_db.list_plans(project="nexus")
    assert len(results) == 2
    assert all(r["project"] == "nexus" for r in results)


def test_save_plan_with_ttl(plan_db: T2Database) -> None:
    """save_plan() stores the ttl field correctly."""
    row_id = plan_db.save_plan(
        query="cached author search",
        plan_json='{"steps":[]}',
        ttl=30,
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["ttl"] == 30


def test_save_plan_ttl_none_by_default(plan_db: T2Database) -> None:
    """save_plan() without ttl stores NULL (permanent)."""
    row_id = plan_db.save_plan(query="permanent plan", plan_json='{}')
    row = plan_db.plans.get_plan(row_id)
    assert row["ttl"] is None


def test_search_plans_includes_ttl(plan_db: T2Database) -> None:
    """search_plans() results include the ttl field."""
    plan_db.save_plan(query="search with ttl", plan_json='{}', ttl=7)
    results = plan_db.search_plans("search")
    assert len(results) == 1
    assert results[0]["ttl"] == 7


def test_list_plans_includes_ttl(plan_db: T2Database) -> None:
    """list_plans() results include the ttl field."""
    plan_db.save_plan(query="plan with ttl", plan_json='{}', ttl=14)
    results = plan_db.list_plans()
    assert len(results) == 1
    assert results[0]["ttl"] == 14


# ── plan_exists (RDR-063 Landmine 1 fix) ────────────────────────────────────


def test_plan_exists_returns_false_on_empty_db(plan_db: T2Database) -> None:
    """plan_exists() returns False when the plans table is empty."""
    assert plan_db.plan_exists("any query", "any-tag") is False


def test_plan_exists_matches_query_and_tag(plan_db: T2Database) -> None:
    """plan_exists() returns True when a plan has both the query and tag."""
    plan_db.save_plan(
        query="seed query",
        plan_json='{}',
        tags="builtin-template,catalog,author",
    )
    assert plan_db.plan_exists("seed query", "builtin-template") is True
    assert plan_db.plan_exists("seed query", "catalog") is True
    assert plan_db.plan_exists("seed query", "author") is True


def test_plan_exists_false_on_query_mismatch(plan_db: T2Database) -> None:
    """plan_exists() returns False when the query does not match."""
    plan_db.save_plan(
        query="exists",
        plan_json='{}',
        tags="builtin-template",
    )
    assert plan_db.plan_exists("different query", "builtin-template") is False


def test_plan_exists_false_on_tag_mismatch(plan_db: T2Database) -> None:
    """plan_exists() returns False when the tag is not among the plan's tags."""
    plan_db.save_plan(
        query="q",
        plan_json='{}',
        tags="builtin-template,catalog",
    )
    assert plan_db.plan_exists("q", "nonexistent-tag") is False


def test_plan_exists_uses_comma_boundary_match(plan_db: T2Database) -> None:
    """plan_exists() matches whole tokens, not substrings.

    Regression guard for the review finding: the pre-fix substring LIKE would
    return True for ``builtin-template`` when a plan's tags contained
    ``builtin-template-v2`` or ``not-builtin-template``. The comma-boundary
    pattern ``(',' || tags || ',') LIKE '%,<tag>,%'`` prevents that.
    """
    # Plan tagged with a SUPERSTRING of the search tag — must NOT match.
    plan_db.save_plan(
        query="superstring",
        plan_json='{}',
        tags="builtin-template-v2,other",
    )
    assert plan_db.plan_exists("superstring", "builtin-template") is False

    # Plan tagged with a PREFIXED variant — must NOT match.
    plan_db.save_plan(
        query="prefixed",
        plan_json='{}',
        tags="not-builtin-template,other",
    )
    assert plan_db.plan_exists("prefixed", "builtin-template") is False

    # Plan tagged with the exact token in the MIDDLE of the comma list — must match.
    plan_db.save_plan(
        query="middle",
        plan_json='{}',
        tags="other,builtin-template,more",
    )
    assert plan_db.plan_exists("middle", "builtin-template") is True

    # Plan tagged with the exact token at the END — must match.
    plan_db.save_plan(
        query="end",
        plan_json='{}',
        tags="other,builtin-template",
    )
    assert plan_db.plan_exists("end", "builtin-template") is True


def test_plan_exists_isolated_per_query(plan_db: T2Database) -> None:
    """plan_exists() scopes the match to a single query string."""
    plan_db.save_plan(query="query-a", plan_json='{}', tags="builtin-template")
    plan_db.save_plan(query="query-b", plan_json='{}', tags="other")

    assert plan_db.plan_exists("query-a", "builtin-template") is True
    assert plan_db.plan_exists("query-b", "builtin-template") is False


# ── Scope tags (RDR-091 Phase 2a) ───────────────────────────────────────────
#
# The ``scope_tags`` column captures which corpora / collections a plan
# actually touched at save time. Phase 2a stores and infers; Phase 2b
# consumes during match-time re-ranking.


def test_normalize_scope_string_strips_hash_suffix() -> None:
    """_normalize_scope_string strips an 8-char hex suffix like '-2ad2825c'."""
    from nexus.plans.scope import _normalize_scope_string

    assert _normalize_scope_string("rdr__arcaneum-2ad2825c") == "rdr__arcaneum"
    assert _normalize_scope_string("knowledge__delos-deadbeef") == "knowledge__delos"


def test_normalize_scope_string_strips_trailing_glob() -> None:
    """_normalize_scope_string strips ``*`` and ``-*`` glob suffixes."""
    from nexus.plans.scope import _normalize_scope_string

    assert _normalize_scope_string("rdr__arcaneum-*") == "rdr__arcaneum"
    assert _normalize_scope_string("rdr__arcaneum*") == "rdr__arcaneum"


def test_normalize_scope_string_preserves_bare_family() -> None:
    """_normalize_scope_string leaves a bare family prefix alone."""
    from nexus.plans.scope import _normalize_scope_string

    assert _normalize_scope_string("rdr__") == "rdr__"
    assert _normalize_scope_string("code__nexus") == "code__nexus"


def test_normalize_scope_string_preserves_tumbler_form() -> None:
    """_normalize_scope_string passes tumbler addresses through untouched."""
    from nexus.plans.scope import _normalize_scope_string

    assert _normalize_scope_string("1.16") == "1.16"
    assert _normalize_scope_string("2.5.3") == "2.5.3"


def test_normalize_scope_string_empty_passthrough() -> None:
    """_normalize_scope_string('') returns ''."""
    from nexus.plans.scope import _normalize_scope_string

    assert _normalize_scope_string("") == ""


def test_normalize_scope_string_strips_uppercase_hex_suffix() -> None:
    """Real collection names carry mixed-case hex (code__Delos-5AF9BFE0).
    Normalization must strip those just like lowercase variants
    (nexus-yi7m)."""
    from nexus.plans.scope import _normalize_scope_string

    assert _normalize_scope_string("code__Delos-5AF9BFE0") == "code__Delos"
    assert _normalize_scope_string("knowledge__Delos-5af9bfe0") == "knowledge__Delos"
    # Mixed-case hex also stripped.
    assert _normalize_scope_string("rdr__arcaneum-2Ad2825C") == "rdr__arcaneum"


def test_normalize_scope_string_does_not_strip_short_or_nonhex() -> None:
    """Only 8-char lowercase hex suffixes are stripped; other trailing
    segments survive (collection-name hash convention is strict)."""
    from nexus.plans.scope import _normalize_scope_string

    # 7 hex chars — not stripped.
    assert _normalize_scope_string("rdr__x-1234567") == "rdr__x-1234567"
    # 9 hex chars — not stripped.
    assert _normalize_scope_string("rdr__x-123456789") == "rdr__x-123456789"
    # 8 chars but one non-hex — not stripped.
    assert _normalize_scope_string("rdr__x-1234567z") == "rdr__x-1234567z"


def test_infer_scope_tags_single_step_corpus() -> None:
    """_infer_scope_tags pulls ``corpus`` out of a single retrieval step."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = '{"steps":[{"tool":"search","args":{"corpus":"rdr__arcaneum"}}]}'
    assert _infer_scope_tags(plan_json) == "rdr__arcaneum"


def test_infer_scope_tags_skips_var_placeholders() -> None:
    """_infer_scope_tags skips ``$var`` bindings (not yet resolved)."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":[{"tool":"search","args":{"corpus":"$corpus"}},'
        '{"tool":"search","args":{"corpus":"rdr__arcaneum"}}]}'
    )
    assert _infer_scope_tags(plan_json) == "rdr__arcaneum"


def test_infer_scope_tags_union_across_steps() -> None:
    """_infer_scope_tags unions corpus/collection across all retrieval steps."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":['
        '{"tool":"search","args":{"corpus":"knowledge__delos"}},'
        '{"tool":"search","args":{"corpus":"rdr__arcaneum"}}'
        ']}'
    )
    result = _infer_scope_tags(plan_json)
    assert result == "knowledge__delos,rdr__arcaneum"


def test_infer_scope_tags_collection_arg() -> None:
    """_infer_scope_tags reads a ``collection`` arg as well as ``corpus``."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":[{"tool":"search","args":{"collection":"rdr__arcaneum"}}]}'
    )
    assert _infer_scope_tags(plan_json) == "rdr__arcaneum"


def test_infer_scope_tags_hash_suffix_normalized() -> None:
    """Inferred tags are normalized at save time."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":[{"tool":"search","args":{"corpus":"rdr__arcaneum-2ad2825c"}}]}'
    )
    assert _infer_scope_tags(plan_json) == "rdr__arcaneum"


def test_infer_scope_tags_traverse_only_agnostic() -> None:
    """A plan that only traverses is scope-agnostic — empty tags."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":[{"tool":"traverse","args":{"start":"$doc_id","depth":2}}]}'
    )
    assert _infer_scope_tags(plan_json) == ""


def test_infer_scope_tags_empty_steps() -> None:
    """An empty steps list yields an empty scope string."""
    from nexus.plans.scope import _infer_scope_tags

    assert _infer_scope_tags('{"steps":[]}') == ""


def test_infer_scope_tags_malformed_json_safe() -> None:
    """_infer_scope_tags returns empty string when plan_json is not JSON."""
    from nexus.plans.scope import _infer_scope_tags

    assert _infer_scope_tags("not valid json{{{") == ""


def test_infer_scope_tags_dedup_and_sort() -> None:
    """Same corpus cited multiple times appears once, sorted."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":['
        '{"tool":"search","args":{"corpus":"rdr__arcaneum"}},'
        '{"tool":"search","args":{"corpus":"rdr__arcaneum"}},'
        '{"tool":"search","args":{"corpus":"code__nexus"}}'
        ']}'
    )
    assert _infer_scope_tags(plan_json) == "code__nexus,rdr__arcaneum"


def test_save_plan_explicit_scope_tags_round_trip(plan_db: T2Database) -> None:
    """save_plan(scope_tags=...) stores the value verbatim (after normalization)."""
    row_id = plan_db.save_plan(
        query="q",
        plan_json='{"steps":[]}',
        scope_tags="rdr__arcaneum",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "rdr__arcaneum"


def test_save_plan_explicit_scope_tags_normalized(plan_db: T2Database) -> None:
    """Explicit scope_tags with a hash suffix is normalized at save time."""
    row_id = plan_db.save_plan(
        query="q",
        plan_json='{"steps":[]}',
        scope_tags="rdr__arcaneum-2ad2825c",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "rdr__arcaneum"


def test_save_plan_omitted_scope_tags_infers(plan_db: T2Database) -> None:
    """save_plan() without scope_tags infers from plan_json."""
    row_id = plan_db.save_plan(
        query="q",
        plan_json='{"steps":[{"tool":"search","args":{"corpus":"rdr__arcaneum"}}]}',
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "rdr__arcaneum"


def test_save_plan_omitted_scope_tags_traverse_only(plan_db: T2Database) -> None:
    """Traverse-only plans save with empty scope_tags (agnostic)."""
    row_id = plan_db.save_plan(
        query="q",
        plan_json='{"steps":[{"tool":"traverse","args":{"start":"$d"}}]}',
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == ""


# test_migration_no_op_on_missing_plans_table and
# test_migration_adds_column_to_empty_plans_table DELETED (RDR-158 P4
# Stage 4, nexus-i711w): their subject was the 4.8.0 _add_plan_scope_tags
# migration itself, which died with nexus/db/migrations.py.


def test_infer_scope_tags_skips_all_sentinel() -> None:
    """_infer_scope_tags treats ``corpus='all'`` as agnostic, not a concrete tag.

    Regression guard for the nexus-dfok bug: without this rule, builtin
    plans using ``corpus: all`` were filtered out of any scoped
    ``nx_answer`` call, inverting RDR-091's entire purpose.
    """
    from nexus.plans.scope import _infer_scope_tags

    assert _infer_scope_tags(
        '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}'
    ) == ""


def test_infer_scope_tags_mixes_all_with_specific_drops_all() -> None:
    """Multi-step plans that mix ``corpus: all`` with a specific corpus keep
    only the specific tag; ``all`` never contributes."""
    from nexus.plans.scope import _infer_scope_tags

    plan_json = (
        '{"steps":['
        '{"tool":"search","args":{"corpus":"all"}},'
        '{"tool":"search","args":{"corpus":"rdr__arcaneum"}}'
        ']}'
    )
    assert _infer_scope_tags(plan_json) == "rdr__arcaneum"


# test_save_plan_explicit_scope_tags_survive_backfill DELETED (RDR-158 P4
# Stage 4, nexus-i711w): its subject was the 4.8.0 _add_plan_scope_tags
# backfill's preserve-explicit-rows behaviour (nexus-dfok), which died with
# nexus/db/migrations.py. The live explicit-tags round-trip survives in
# test_save_plan_explicit_scope_tags_round_trip above.


# test_rewash_migration_fixes_all_sentinel_rows and
# test_rewash_migration_no_op_when_no_all_rows were DELETED (nexus-i711w
# Stage 2 sub-stage A3): their subject was ``repair_scope_tags``'s rewash of
# 'all'-contaminated LEGACY rows in the retired SQLite snapshot — the repair
# verb and its module (``nexus.plans.repair``) are gone. The live contract
# ("the 'all' sentinel never reaches storage") survives in
# ``nexus.plans.scope.normalize_scope_tags`` and is pinned on the engine
# substrate by the two save_plan tests immediately below plus
# test_set_scope_tags_drops_all_sentinel.


def test_save_plan_explicit_scope_tags_filters_all_sentinel(plan_db: T2Database) -> None:
    """save_plan(scope_tags='all') must drop the sentinel — same contract as
    the inference path. Without this filter the sentinel would reach storage
    via the explicit path (RDR-091 code-review finding C-3)."""
    row_id = plan_db.save_plan(
        query="q",
        plan_json='{"steps":[]}',
        scope_tags="all",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == ""


def test_save_plan_explicit_scope_tags_drops_all_mixed_with_specific(
    plan_db: T2Database,
) -> None:
    """Mixed 'all' + specific tags drops the 'all' and keeps the rest."""
    row_id = plan_db.save_plan(
        query="q",
        plan_json='{"steps":[]}',
        scope_tags="all,rdr__arcaneum",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "rdr__arcaneum"


# test_rewash_migration_fixes_trailing_all_variant and
# test_rewash_migration_no_op_without_plans_table were DELETED (nexus-i711w
# Stage 2 sub-stage A3) with ``nexus.plans.repair`` — both probed
# ``repair_scope_tags``'s SQL over the retired SQLite snapshot (the LIKE
# '%,all' branch and the missing-table guard). The trailing-'all' input shape
# is handled substrate-independently by ``normalize_scope_tags`` (order-blind
# split/drop/sort), pinned via set_scope_tags/save_plan tests in this file.


# ── RDR-092 Phase 3.2: match_text wiring into save_plan + search_plans ──────


def test_save_plan_computes_match_text_on_dimensional_insert(
    plan_db: T2Database,
) -> None:
    """save_plan populates match_text using the hybrid synthesiser
    whenever verb/name/scope are provided.
    """
    row_id = plan_db.save_plan(
        query="Find documents attributed to a specific author.",
        plan_json='{"steps":[]}',
        verb="research",
        scope="global",
        name="find-by-author",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row is not None
    assert "research find-by-author scope global" in row["match_text"]


def test_save_plan_match_text_falls_back_to_query_on_legacy_row(
    plan_db: T2Database,
) -> None:
    """When verb/name are absent, match_text mirrors the raw query."""
    row_id = plan_db.save_plan(
        query="legacy plan about chunking",
        plan_json='{"steps":[]}',
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["match_text"] == "legacy plan about chunking"


def test_search_plans_hits_on_dimensional_suffix(plan_db: T2Database) -> None:
    """RDR-092 Phase 3.2: FTS now indexes match_text, so a probe
    against the dimensional suffix hits the row even when the raw
    query column doesn't mention the suffix tokens.
    """
    plan_db.save_plan(
        query="Find documents attributed to a specific author.",
        plan_json='{"steps":[]}',
        verb="research",
        scope="global",
        name="find-by-author",
        tags="builtin-template",
    )
    # Probe that only hits via the dimensional suffix, not the raw
    # description.
    results = plan_db.search_plans("find-by-author")
    assert results, "match_text must expose the dimensional name to FTS"
    assert results[0]["name"] == "find-by-author"


def test_search_plans_still_matches_raw_description(
    plan_db: T2Database,
) -> None:
    """Description text stays at the front of match_text so
    existing callers that FTS-search on description tokens keep
    working byte-for-byte.
    """
    plan_db.save_plan(
        query="semantic search over code repositories",
        plan_json='{"steps":[]}',
        verb="research",
        scope="global",
        name="research-default",
    )
    results = plan_db.search_plans("semantic")
    assert results, "description tokens still FTS-hit via match_text"
    assert "semantic" in results[0]["query"]


# ── nexus-89uc4: the #1069 project-column fallback is GONE ──────────────────
#
# #1069 made save_plan derive scope_tags from the `project` column when
# plan_json had no inferable retrieval scope (the corpus:all case). The derived
# value is in a DIFFERENT NAMESPACE from what the matcher compares against:
# `_scope_fit` prefix-matches collection-name shape `<content_type>__<owner>`,
# and a bare project token satisfies neither of its two directions. Measured:
#
#     _scope_fit('nexus', 'rdr__nexus') = None   <- conflict, plan DROPPED
#     _scope_fit('',      'rdr__nexus') = 0.0    <- agnostic, plan KEPT
#
# So the fallback converted "kept as neutral" into "dropped as conflicting",
# for exactly the corpus:all population it was written to serve. It made those
# plans strictly LESS reachable than doing nothing. Removed in both save paths
# (PlanLibrary and HttpPlanLibrary — the twin mirrored it verbatim).


def test_save_plan_corpus_all_with_project_stays_agnostic(
    plan_db: T2Database,
) -> None:
    """A corpus:all plan with a populated project stays scope-AGNOSTIC.

    Inverted from test_save_plan_corpus_all_with_project_derives_scope_from_
    project, which pinned the #1069 behaviour and therefore pinned the defect
    (nexus-89uc4).
    """
    corpus_all_json = (
        '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}'
    )
    row_id = plan_db.save_plan(
        query="What does the canon-chat headless test verify",
        plan_json=corpus_all_json,
        project="canon-chat",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "", (
        "a corpus:all plan must stay agnostic; deriving 'canon-chat' from the "
        "project column produces a tag the matcher can only read as a CONFLICT"
    )


def test_corpus_all_plan_survives_a_scoped_caller(
    plan_db: T2Database,
) -> None:
    """The property the fix exists for: such a plan is KEPT, not dropped.

    Asserting the stored value is '' is necessary but not sufficient — what
    broke was reachability. This pins the matcher outcome directly, so the test
    fails if a future change reintroduces any project-shaped tag, whatever it
    is spelled.
    """
    from nexus.plans.matcher import _scope_fit

    corpus_all_json = (
        '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}'
    )
    row_id = plan_db.save_plan(
        query="cross-corpus question with a project attached",
        plan_json=corpus_all_json,
        project="nexus",
    )
    stored = plan_db.plans.get_plan(row_id)["scope_tags"]

    for scope_pref in ("rdr__nexus", "knowledge__conexus", "code__nexus-1-1"):
        assert _scope_fit(stored, scope_pref) == 0.0, (
            f"stored scope_tags {stored!r} must be NEUTRAL against "
            f"{scope_pref!r} (0.0 = kept). A project-derived tag yields None "
            f"= conflict = dropped, which is the nexus-89uc4 defect."
        )


def test_save_plan_corpus_all_without_project_stays_empty(
    plan_db: T2Database,
) -> None:
    """corpus:all with NO project stores '' — unchanged by nexus-89uc4, now
    true for the simpler reason that nothing derives from project at all.
    """
    corpus_all_json = (
        '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}'
    )
    row_id = plan_db.save_plan(
        query="search across all corpora",
        plan_json=corpus_all_json,
        project="",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == ""


# test_save_plan_corpus_all_project_normalized was REMOVED with the fallback
# (nexus-89uc4). Its subject was the sentinel-drop INSIDE the fallback —
# project='all' must not leak the 'all' sentinel into scope_tags. With no
# project ever contributing, that assertion is subsumed by
# test_save_plan_corpus_all_with_project_stays_agnostic above, which covers
# every project value rather than just the sentinel.


def test_save_plan_specific_corpus_unaffected_by_project(
    plan_db: T2Database,
) -> None:
    """Plans with specific corpus retrieval steps still infer from those steps.

    Kept through nexus-89uc4: inference is the surviving mechanism, and this is
    the only test that guards it against the project argument.
    """
    row_id = plan_db.save_plan(
        query="search rdr corpus",
        plan_json='{"steps":[{"tool":"search","args":{"corpus":"rdr__arcaneum"}}]}',
        project="some-project",
    )
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "rdr__arcaneum", (
        "specific-corpus plan must infer from retrieval steps, not from project"
    )


# ── #1073: set_scope_tags() method in PlanLibrary ──────────────────────────


def test_set_scope_tags_writes_normalized_value(plan_db: T2Database) -> None:
    """set_scope_tags writes normalized, sentinel-dropped scope_tags (#1073)."""
    row_id = plan_db.save_plan(
        query="corpus:all grown plan",
        plan_json='{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
        project="",
    )
    before = plan_db.plans.get_plan(row_id)
    assert before["scope_tags"] == ""

    updated = plan_db.plans.set_scope_tags(row_id, "canon-chat")
    assert updated is True

    after = plan_db.plans.get_plan(row_id)
    assert after["scope_tags"] == "canon-chat"


def test_set_scope_tags_normalizes_hash_suffix(plan_db: T2Database) -> None:
    """set_scope_tags applies _normalize_scope_string to each entry (#1073)."""
    row_id = plan_db.save_plan(
        query="q", plan_json='{"steps":[]}', project="",
    )
    plan_db.plans.set_scope_tags(row_id, "rdr__arcaneum-2ad2825c,knowledge__delos-deadbeef")
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "knowledge__delos,rdr__arcaneum"


def test_set_scope_tags_drops_all_sentinel(plan_db: T2Database) -> None:
    """set_scope_tags drops the 'all' sentinel (#1073)."""
    row_id = plan_db.save_plan(query="q", plan_json='{"steps":[]}', project="")
    updated = plan_db.plans.set_scope_tags(row_id, "all,rdr__arcaneum")
    assert updated is True
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "rdr__arcaneum"


def test_set_scope_tags_idempotent(plan_db: T2Database) -> None:
    """Calling set_scope_tags twice with the same value is a no-op (#1073)."""
    row_id = plan_db.save_plan(query="q", plan_json='{"steps":[]}', project="")
    plan_db.plans.set_scope_tags(row_id, "canon-chat")
    plan_db.plans.set_scope_tags(row_id, "canon-chat")
    row = plan_db.plans.get_plan(row_id)
    assert row["scope_tags"] == "canon-chat"


def test_set_scope_tags_missing_plan_returns_false(plan_db: T2Database) -> None:
    """set_scope_tags returns False for a non-existent plan_id (#1073)."""
    result = plan_db.plans.set_scope_tags(99999, "canon-chat")
    assert result is False
