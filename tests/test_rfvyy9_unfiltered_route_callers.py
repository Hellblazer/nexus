# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rfvyy.9 item 1: the engine routes that serve flagged rows under ``sensitivity=exclude`` have only callers
that need every row, or that see no flagged title or text.

``/get-all-metadata`` (ids and metadata, titles among them) and ``/get-embeddings`` (ids and vectors) take no
``sensitivity`` and serve every chunk, flagged or not; ``/store-list`` has no client. RDR-243's Technical Design leaves
them unfiltered, and filtering them would break their callers: the indexer's staleness cache and ``--force`` orphan
cleanup must see a flagged document's rows or they re-embed it every run and leave its orphans, and an export must copy
flagged chunks or lose them. ``/reapable`` lists chunks with no manifest row in their own collection, but its label
names a live owner in ANOTHER collection and shows the first 80 characters of the shared text, so the engine withholds
that label when a live flagged document anywhere holds the text (``ChunkOwnerLabel``); its callers see no flagged
title or text.

So the guard is on the callers. Every reference to the client methods for these routes, a call, an uncalled
attribute (``_call(t3.get_embeddings_by_id, ...)``) or a ``getattr`` by name, is listed here by file and enclosing
function with the reason it may see flagged rows. A new one fails this test until it is listed, and a listed one that
is gone fails too. The route literals live in ``db/http_vector_client.py`` only.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

#: client methods whose engine route serves flagged rows unfiltered (``reapable_chunks`` pages ``reapable``)
METHODS = frozenset({"get_all_metadata", "get_embeddings", "get_embeddings_by_id", "reapable", "reapable_chunks"})

_MAINT = "index maintenance: it must see a flagged document's rows, or it re-embeds or orphans them"
_VEC = "reads vectors, not text, and hands no title or text from them to a model"
_REAP = "the engine withholds a reapable chunk's label when a live flagged document anywhere holds its text"
_NOT_CLIENT = "not the client method: a count field of the same name"

ALLOWED: dict[tuple[str, str], str] = {
    ("indexer_utils.py", "build_staleness_cache"): _MAINT + " (the staleness cache)",
    ("pipeline_stages.py", "_force_t3_orphan_cleanup"): _MAINT + " (--force resolves a file's chunk ids to delete them)",
    ("db/http_vector_client.py", "HttpVectorClient.reapable_chunks"): _REAP + " (the pager)",
    ("commands/store.py", "_list_reapable"): _REAP + " (nx store list --reapable prints title and prefix)",
    ("commands/t3.py", "gc_cmd"): _REAP + " (the gc listing prints titles)",
    ("commands/catalog_cmds/doctor.py", "_run_name_vs_embed_dim._probe"): _VEC + " (the dimension of one vector)",
    ("doctor_embeddings.py", "probe_collection"): (
        "compares stored vectors with a re-embed of the same chunk text through the collection's own embedder: the "
        "local model, or Voyage for a collection Voyage already embedded (llm-surfaces voyage row), so the text goes "
        "nowhere it has not gone; no title or text goes to a model"
    ),
    ("doctor_assignments.py", "probe_collection"): _VEC + " (topic assignment distances)",
    ("commands/taxonomy_cmd.py", "_fetch_service_vectors"): (
        _VEC + "; its ids come from an exclude read, and topic labels skip flagged documents (llm-surfaces taxonomy-label)"
    ),
    ("db/t2/http_taxonomy_store.py", "HttpTaxonomyStore._svc_fetch_all_embeddings"): _VEC + " (topic projection)",
    ("db/t2/http_taxonomy_store.py", "HttpTaxonomyStore._svc_fetch_by_ids"): _VEC + " (topic assignment)",
    ("exporter.py", "export_collection"): "an operator's export: a flagged chunk is copied with its row, or lost",
    ("search_engine.py", "_fetch_embeddings_for_results._fetch"): (
        "vectors of hits a search already returned, so an exclude search's hits only"
    ),
}

#: references that are not the client methods, found by the attribute scan and kept out by name
NOT_CLIENT: dict[tuple[str, str], str] = {
    ("indexer.py", "_prune_collection_serverside"): _NOT_CLIENT + " (GcFloorRefused.reapable, logged)",
}


class _Refs(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: set[str] = set()

    def _scoped(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _hit(self) -> None:
        self.found.add(".".join(self.scope) or "<module>")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in METHODS and isinstance(node.ctx, ast.Load):
            self._hit()
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            isinstance(f, ast.Name) and f.id in ("getattr", "hasattr") and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant) and node.args[1].value in METHODS
        ):
            self._hit()
        self.generic_visit(node)


def _callers() -> dict[tuple[str, str], None]:
    out: dict[tuple[str, str], None] = {}
    for path in sorted(SRC.rglob("*.py")):
        v = _Refs()
        v.visit(ast.parse(path.read_text(encoding="utf-8")))
        rel = path.relative_to(SRC).as_posix()
        for fn in v.found:
            if (rel, fn) not in NOT_CLIENT:
                out[(rel, fn)] = None
    return out


def test_every_caller_of_an_unfiltered_route_is_listed_with_a_reason() -> None:
    callers = set(_callers())
    assert callers, "the scan found no caller at all: the scan is broken, not the code clean"
    assert sorted(callers - ALLOWED.keys()) == [], "a new caller of an unfiltered route: list it with a reason"
    assert sorted(ALLOWED.keys() - callers) == [], "a listed caller is gone: drop its row"


def test_every_not_client_entry_is_still_found() -> None:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        v = _Refs()
        v.visit(ast.parse(path.read_text(encoding="utf-8")))
        found |= {(path.relative_to(SRC).as_posix(), fn) for fn in v.found}
    assert sorted(NOT_CLIENT.keys() - found) == [], "a NOT_CLIENT entry is gone: drop its row"


ROUTES = ("/v1/vectors/get-all-metadata", "/v1/vectors/get-embeddings", "/v1/vectors/reapable", "/v1/vectors/store-list")


def test_the_route_literals_live_in_the_vector_client_only() -> None:
    seen: dict[str, list[str]] = {r: [] for r in ROUTES}
    for path in SRC.rglob("*.py"):
        consts = {
            n.value for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        for r in ROUTES:
            if r in consts:  # a string that IS the route path: a request, not prose naming it
                seen[r].append(path.relative_to(SRC).as_posix())
    client = ["db/http_vector_client.py"]
    assert seen["/v1/vectors/get-all-metadata"] == client
    assert seen["/v1/vectors/get-embeddings"] == client
    assert seen["/v1/vectors/reapable"] == client
    assert seen["/v1/vectors/store-list"] == [], "/store-list had no client; a new one needs the withhold review"
