# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rfvyy.9 item 1: the engine routes that serve flagged rows under ``sensitivity=exclude`` have only callers
that need every row or that withhold before a model reads the result.

``/get-all-metadata``, ``/get-embeddings`` and ``/reapable`` take no ``sensitivity`` and return ids, metadata
(titles among them) and vectors for every chunk, flagged or not; ``/store-list`` has no client at all. RDR-243's
Technical Design leaves them unfiltered and puts the withhold on the output side. Filtering them in the engine would
break their callers: the indexer's staleness cache and the orphan sweep must see a flagged document's rows or they
re-embed it every run and miss its orphans, an export or a re-embed must copy flagged chunks or lose them, and the
reaper lists chunks with no live owner, which therefore have no sensitive owner.

So the guard is on the callers. Every call of the client methods for these routes is listed here by file and
enclosing function, with the reason it may see flagged rows. A new caller fails this test until it is listed with a
reason, and a listed caller that is gone fails too. The route literals live in ``db/http_vector_client.py`` only.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

#: client methods whose engine route serves flagged rows unfiltered
METHODS = frozenset({"get_all_metadata", "get_embeddings", "get_embeddings_by_id", "reapable"})

_MAINT = "index maintenance: it must see a flagged document's rows, or it re-embeds or orphans them"
_VEC = "reads vectors, not text, and hands no title or text from them to a model"

ALLOWED: dict[tuple[str, str], str] = {
    ("indexer_utils.py", "build_staleness_cache"): _MAINT + " (the staleness cache)",
    ("pipeline_stages.py", "_force_t3_orphan_cleanup"): _MAINT + " (--force resolves a file's chunk ids to delete them)",
    ("db/http_vector_client.py", "HttpVectorClient.reapable_chunks"): (
        "the reaper pages reapable(c): chunks with no live owner, so none has a sensitive owner"
    ),
    ("commands/catalog_cmds/doctor.py", "_run_name_vs_embed_dim._probe"): _VEC + " (the dimension of one vector)",
    ("doctor_assignments.py", "probe_collection"): _VEC + " (topic assignment distances)",
    ("commands/taxonomy_cmd.py", "_fetch_service_vectors"): (
        _VEC + "; the labels a model writes skip flagged documents (llm-surfaces taxonomy-label, behaviour skip)"
    ),
    ("db/t2/http_taxonomy_store.py", "HttpTaxonomyStore._svc_fetch_all_embeddings"): _VEC + " (topic projection)",
    ("db/t2/http_taxonomy_store.py", "HttpTaxonomyStore._svc_fetch_by_ids"): _VEC + " (topic assignment)",
    ("exporter.py", "export_collection"): "an operator's export: a flagged chunk is copied with its row, or lost",
    ("search_engine.py", "_fetch_embeddings_for_results._fetch"): (
        "vectors of hits a search already returned, so an exclude search's hits only"
    ),
}

ROUTES = ("/v1/vectors/get-all-metadata", "/v1/vectors/get-embeddings", "/v1/vectors/reapable", "/v1/vectors/store-list")


class _Calls(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.found: set[str] = set()

    def _scoped(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        hit = isinstance(f, ast.Attribute) and f.attr in METHODS
        hit = hit or (
            isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant) and node.args[1].value in METHODS
        )
        if hit:
            self.found.add(".".join(self.scope) or "<module>")
        self.generic_visit(node)


def _callers() -> dict[tuple[str, str], None]:
    out: dict[tuple[str, str], None] = {}
    for path in sorted(SRC.rglob("*.py")):
        v = _Calls()
        v.visit(ast.parse(path.read_text(encoding="utf-8")))
        rel = path.relative_to(SRC).as_posix()
        for fn in v.found:
            out[(rel, fn)] = None
    return out


def test_every_caller_of_an_unfiltered_route_is_listed_with_a_reason() -> None:
    callers = set(_callers())
    assert callers, "the scan found no caller at all: the scan is broken, not the code clean"
    assert sorted(callers - ALLOWED.keys()) == [], "a new caller of an unfiltered route: list it with a reason"
    assert sorted(ALLOWED.keys() - callers) == [], "a listed caller is gone: drop its row"


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
