"""nexus-bc7ps: routing consumers ask the engine for live collections only.

A quarantine collection is registered as a first-class ``catalog_collections``
row under ``quarantine-<name>``, a name no client parser accepts. The engine's
two list routes keep serving every state by default (doctor, gc, backfill and
export must not go blind: 35 sites, two of which would invert into a wrong
delete under a hidden default), and every ROUTING consumer opts into
``lifecycle_state=live`` through one client surface. These pins hold the
client half of that contract:

- the filter reaches the wire on both clients, and an unfiltered listing is
  the only one that primes the process collection-row cache;
- ``live_collection_rows`` uses the engine filter on a real client and the
  equivalent row predicate on anything else;
- the fan-out projection ``get_live_collection_names`` hides non-live rows
  while the cache behind it stays complete;
- (lint) no routing module calls the bare listing.
"""
from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import MagicMock

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.commands import command_context
from nexus.mcp import core as mcp_core
from nexus.db.http_vector_client import (
    LIFECYCLE_STATES,
    HttpVectorClient,
    is_live_collection_row,
    live_collection_rows,
)

_ROWS = [
    {"name": "code__own__bge-base-en-v15-768__v1", "count": 10, "lifecycle_state": "live",
     "content_type": "code", "owner_id": "own", "embedding_model": "bge-base-en-v15-768"},
    {"name": "quarantine-code__own__bge-base-en-v15-768__v1", "count": 4, "lifecycle_state": "quarantine",
     "content_type": "code", "owner_id": "own", "embedding_model": "bge-base-en-v15-768"},
    {"name": "docs__own__bge-base-en-v15-768__v1", "count": 7, "lifecycle_state": "dormant",
     "content_type": "docs", "owner_id": "own", "embedding_model": "bge-base-en-v15-768"},
    {"name": "unregistered-coll", "count": 2},
]


# ── the row predicate ──────────────────────────────────────────────────────

def test_lifecycle_vocabulary_matches_the_engine_check():
    assert LIFECYCLE_STATES == {"live", "quarantine", "dormant", "disputed"}


@pytest.mark.parametrize(
    ("row", "live"),
    [
        ({"name": "x", "lifecycle_state": "live"}, True),
        ({"name": "x", "lifecycle_state": "quarantine"}, False),
        ({"name": "x", "lifecycle_state": "dormant"}, False),
        ({"name": "x", "lifecycle_state": "disputed"}, False),
        ({"name": "x"}, True),          # unregistered: absent state is not non-live
        ("bare-name", True),            # a name-only substrate carries no state
    ],
)
def test_is_live_collection_row(row: Any, live: bool):
    assert is_live_collection_row(row) is live


# ── the vector client threads the filter and guards the cache prime ────────

def _capture_vector_get(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        seen.append(path)
        if "lifecycle_state=live" in path:
            return [r for r in _ROWS if r.get("lifecycle_state") == "live"]
        return list(_ROWS)

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    return seen


def test_collection_stats_passes_the_filter_only_when_asked(monkeypatch):
    seen = _capture_vector_get(monkeypatch)
    c = HttpVectorClient()
    c.collection_stats()
    c.collection_stats("live")
    c.collection_stats("quarantine")
    assert seen == [
        "/v1/vectors/stats",
        "/v1/vectors/stats?lifecycle_state=live",
        "/v1/vectors/stats?lifecycle_state=quarantine",
    ]


def test_list_live_collections_is_the_live_filter_and_never_primes_the_cache(monkeypatch):
    seen = _capture_vector_get(monkeypatch)
    primed: list[list[dict]] = []
    monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: primed.append(rows))
    c = HttpVectorClient()

    live = c.list_live_collections()
    assert [r["name"] for r in live] == ["code__own__bge-base-en-v15-768__v1"]
    assert seen[-1].endswith("?lifecycle_state=live")
    assert primed == [], "a filtered listing must not overwrite the identity cache"

    full = c.list_collections()
    assert len(full) == len(_ROWS)
    assert len(primed) == 1 and len(primed[0]) == len(_ROWS), (
        "only the unfiltered listing primes the cache, with every row"
    )


def test_list_live_collections_filters_client_side_when_the_engine_ignores_the_param(monkeypatch):
    """An engine older than the filter returns the full inventory for
    ``?lifecycle_state=live``; the routing view must still be live-only, or the
    new-client / old-engine window silently regresses to the pre-fix behaviour."""
    def old_engine_get(path: str, *, tenant: str = "default") -> Any:
        return list(_ROWS)   # param ignored, quarantine row included

    monkeypatch.setattr("nexus.db.http_vector_client._get", old_engine_get)
    monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: None)
    assert [r["name"] for r in HttpVectorClient().list_live_collections()] == [
        "code__own__bge-base-en-v15-768__v1", "unregistered-coll",
    ]


def test_live_collection_rows_uses_the_engine_filter_on_a_real_client(monkeypatch):
    seen = _capture_vector_get(monkeypatch)
    rows = live_collection_rows(HttpVectorClient())
    assert [r["name"] for r in rows] == ["code__own__bge-base-en-v15-768__v1"]
    assert seen == ["/v1/vectors/stats?lifecycle_state=live"]


def test_live_collection_rows_filters_client_side_on_a_double():
    class _Fake:
        def list_collections(self):
            return list(_ROWS)

    assert [r["name"] for r in live_collection_rows(_Fake())] == [
        "code__own__bge-base-en-v15-768__v1", "unregistered-coll",
    ]
    # MagicMock doubles (repo_identity's tests) go the same way, never through
    # an auto-created list_live_collections attribute.
    m = MagicMock()
    m.list_collections.return_value = list(_ROWS)
    assert [r["name"] for r in live_collection_rows(m)] == [
        "code__own__bge-base-en-v15-768__v1", "unregistered-coll",
    ]
    m.list_live_collections.assert_not_called()


# ── the catalog client threads the filter; collections_by_owner is routing ─

def test_catalog_list_collections_threads_the_filter(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def fake_get(self, path: str, **params: Any) -> Any:
        seen.append((path, {k: v for k, v in params.items() if v}))
        state = params.get("lifecycle_state")
        rows = [r for r in _ROWS if "lifecycle_state" in r and (state is None or r["lifecycle_state"] == state)]
        return {"collections": rows}

    monkeypatch.setattr(HttpCatalogClient, "_get", fake_get)
    cat = HttpCatalogClient(base_url="http://127.0.0.1:1", tenant="t", _token="x")
    assert len(cat.list_collections()) == 3
    assert [r["name"] for r in cat.list_collections("quarantine")] == ["quarantine-code__own__bge-base-en-v15-768__v1"]
    # collections_by_owner is the repo -> collection router: live only, so the
    # quarantine sibling hygiene-002-1 gave the same owner_id is not a candidate.
    assert [r["name"] for r in cat.collections_by_owner("own")] == ["code__own__bge-base-en-v15-768__v1"]
    assert seen == [
        ("/collections/list", {}),
        ("/collections/list", {"lifecycle_state": "quarantine"}),
        ("/collections/list", {"lifecycle_state": "live"}),
    ]


# ── the fan-out projection over a complete cache ───────────────────────────

def test_get_live_collection_names_hides_non_live_rows_but_keeps_the_cache_complete():
    mcp_infra.prime_collections_cache(list(_ROWS))
    assert mcp_infra.get_collection_names() == [r["name"] for r in _ROWS]
    assert mcp_infra.get_live_collection_names() == ["code__own__bge-base-en-v15-768__v1", "unregistered-coll"]
    # The identity read still resolves the quarantine row: gc tooling needs it.
    row = mcp_infra.get_collection_row("quarantine-code__own__bge-base-en-v15-768__v1")
    assert row is not None and row["lifecycle_state"] == "quarantine"
    mcp_infra.invalidate_collections_cache()


# ── an explicit non-live corpus name is excluded from the MCP fan-out ─────

def test_mcp_fanout_excludes_an_explicit_non_live_name(monkeypatch):
    """Sam, 2026-09-16: an explicit corpus name in an MCP call is the model's
    navigation, not a person's choice, so a registered quarantine (or dormant
    / disputed) collection named explicitly is excluded like a fan-out member,
    and reported through ``excluded_out``. An unregistered name (no cached
    row) is not non-live and still resolves."""
    mcp_infra.prime_collections_cache(list(_ROWS))
    try:
        excluded: list[str] = []
        target = mcp_core._resolve_corpus_target(
            "quarantine-code__own__bge-base-en-v15-768__v1,"
            "docs__own__bge-base-en-v15-768__v1,"
            "code__own__bge-base-en-v15-768__v1,"
            "unregistered-coll__x",
            t3=None, excluded_out=excluded,
        )
    finally:
        mcp_infra.invalidate_collections_cache()
    assert target == ["code__own__bge-base-en-v15-768__v1", "unregistered-coll__x"]
    assert excluded == [
        "quarantine-code__own__bge-base-en-v15-768__v1",
        "docs__own__bge-base-en-v15-768__v1",
    ]


# ── the preamble's knowledge-collection probe reads dict rows ──────────────

def test_knowledge_collections_probe_reads_dict_rows_and_skips_non_live(monkeypatch):
    """Production ``list_collections()`` returns dicts; before nexus-bc7ps the
    probe stringified the row, so it never matched a name and always returned
    nothing. Now it reads the name, and it reads the live view."""
    rows = [
        {"name": "knowledge__a__bge-base-en-v15-768__v1", "count": 3, "lifecycle_state": "live", "content_type": "knowledge"},
        {"name": "quarantine-knowledge__a__bge-base-en-v15-768__v1", "count": 3, "lifecycle_state": "quarantine", "content_type": "knowledge"},
        {"name": "code__a__bge-base-en-v15-768__v1", "count": 3, "lifecycle_state": "live", "content_type": "code"},
    ]

    class _T3:
        def list_collections(self):
            return list(rows)

    monkeypatch.setattr("nexus.db.make_t3", lambda: _T3())
    mcp_infra.prime_collections_cache(rows)
    try:
        assert command_context._knowledge_collections() == ["knowledge__a__bge-base-en-v15-768__v1"]
    finally:
        mcp_infra.invalidate_collections_cache()


# ── lint: routing modules never call the bare listing ──────────────────────

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "nexus"

#: Modules whose enumeration of collections is ROUTING (they parse names or
#: pick search targets). A bare ``.list_collections(`` / ``get_collection_names(``
#: in one of them is the defect class this bead closed. The lint sees listing
#: call sites; the explicit-name branch of the MCP fan-out is pinned by
#: test_mcp_fanout_excludes_an_explicit_non_live_name instead.
_ROUTING_MODULES = [
    "corpus.py",
    "repos.py",
    "repo_identity.py",
    "commands/taxonomy_cmd.py",
    "commands/search_cmd.py",
    "commands/command_context.py",
    "mcp/core.py",
]

#: (module, line) pairs that are bare on purpose: search_cmd's FULL listing
#: primes the identity cache, with the routing candidates derived from it
#: through is_live_collection_row on the next line; taxonomy_cmd's raw
#: ``_client`` branch is the retired in-process Chroma substrate, which has
#: no lifecycle column and is not on either engine route.
_ALLOWED = {
    ("commands/search_cmd.py", "collection_rows = db.list_collections()"),
    ("commands/taxonomy_cmd.py", "for c in t3._client.list_collections():"),
    # the MCP collection_list TOOL is the inventory surface (twin of
    # `nx collection list`), not a routing read; the fan-out in the same
    # module goes through the live projection.
    ("mcp/core.py", "cols = _get_t3().list_collections()"),
}


def _bare_listing_calls(module: str, src: str) -> list[str]:
    """Every bare ``list_collections()`` / ``get_collection_names()`` call in
    *src*, as ``module:line: text``, minus the ``_ALLOWED`` exemptions and
    minus a ``list_collections(lifecycle_state="live")`` call."""
    tree = ast.parse(src)
    lines = src.splitlines()
    bare: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        # collections_by_owner is itself the live view by contract (pinned in
        # test_catalog_list_collections_threads_the_filter), so a call to it
        # is not a bare listing.
        if name not in {"list_collections", "get_collection_names"}:
            continue
        line = lines[node.lineno - 1].strip()
        if (module, line) in _ALLOWED:
            continue
        if name == "list_collections" and any(
            isinstance(kw.value, ast.Constant) and kw.value.value == "live"
            for kw in node.keywords if kw.arg == "lifecycle_state"
        ):
            continue
        bare.append(f"{module}:{node.lineno}: {line}")
    return bare


@pytest.mark.parametrize("module", _ROUTING_MODULES)
def test_routing_modules_never_call_the_bare_listing(module: str):
    bare = _bare_listing_calls(module, (_SRC / module).read_text())
    assert not bare, (
        "routing code must enumerate through live_collection_rows / "
        "list_live_collections / get_live_collection_names, never the bare "
        "listing:\n  " + "\n  ".join(bare)
    )


def test_lint_flags_a_bare_listing_and_admits_the_live_forms():
    """The lint's failure path, so the parametrized pass above is not vacuous:
    a synthetic routing module with one bare call is flagged by line, and the
    two sanctioned shapes are not."""
    src = (
        "def routing(t3, cat):\n"
        "    a = t3.list_collections()\n"
        "    b = t3.list_collections(lifecycle_state='live')\n"
        "    c = get_collection_names()\n"
        "    d = live_collection_rows(t3)\n"
        "    e = cat.collections_by_owner('o')\n"
    )
    assert _bare_listing_calls("synthetic.py", src) == [
        "synthetic.py:2: a = t3.list_collections()",
        "synthetic.py:4: c = get_collection_names()",
    ]


def test_lint_exemptions_still_exist_in_the_real_files():
    """Each ``_ALLOWED`` line is present verbatim, so an exemption cannot
    outlive the code it exempts and hide a new bare call on the same line."""
    for module, line in _ALLOWED:
        assert line in (_SRC / module).read_text(), (module, line)
