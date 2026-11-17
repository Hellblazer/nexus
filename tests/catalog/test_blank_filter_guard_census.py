# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 (nexus-uwggq.1.9): closure census of the client's scoped-read and scoped-write routes.

The engine reads a blank filter as absent, so every ``HttpCatalogClient`` method that sends a scoped request
must refuse a blank value before it sends (``nexus.errors.require_nonblank_filter`` and its siblings). The guards
are per-method; nothing else stops a new method, or a guard deleted in a refactor, from reopening the hole. This
test parses ``http_catalog_client.py`` and fails when a method that calls ``self._get`` / ``self._post`` with one
of the scoped routes does not call a guard and is not named in ``EXEMPT`` with a reason.

Falsified by hand on 2026-11-17: deleting the ``_tumbler_prefix`` call from ``descendants`` (and, separately,
the ``_nonblank`` call from ``by_corpus``) turns ``test_every_scoped_route_caller_is_guarded_or_exempt`` red.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import nexus.catalog.http_catalog_client as client_module

#: Routes whose engine handler reads a blank query/body value as "no filter" (or, for /descendants, as a LIKE
#: pattern with live wildcards). A method that sends to one of these is a scoped call.
SCOPED_ROUTES = frozenset({
    "/list", "/links", "/unlink", "/collections/list", "/gc_audit/list", "/coverage", "/resolve",
    "/descendants", "/link_query", "/search", "/by_title",
})

#: The names the client imports the guards under (see the ``from nexus.errors import`` line), plus the originals.
GUARDS = frozenset({
    "_nonblank", "_refuse_ws", "_link_types", "_tumbler_prefix", "_unlink_args",
    "require_nonblank_filter", "refuse_whitespace_filters", "require_link_types", "require_tumbler_prefix",
    "require_unlink_args",
})

#: ``method name -> why a scoped route is reachable without a guard``. Empty: every caller is guarded today.
EXEMPT: dict[str, str] = {}

#: Methods that call ``_get`` / ``_post`` with a route this census cannot read (a variable, an f-string),
#: reviewed by hand. A new one fails the census until someone reads it and adds it here.
DYNAMIC_ROUTE_REVIEWED: dict[str, str] = {
    "collection_doc_counts": "picks one of two constant count routes, neither takes a filter",
    "_post_embedding_write": "the combined-write choke point; its path is the caller's write route, no read filter",
}

#: Non-vacuity floor: methods the census must find (a parser that matched nothing would pass everything).
MUST_FIND = frozenset({
    "by_source_uri", "by_corpus", "by_content_type", "by_owner", "list_by_collection", "list_scoped", "descendants",
    "unlink", "bulk_unlink", "links_from", "links_to", "link_query", "gc_audit_list", "list_collections",
    "coverage_by_content_type", "lookup_doc_id_by_collection_and_path", "find", "find_by_title_exact",
})


def _scan() -> tuple[dict[str, tuple[set[str], set[str]]], set[str]]:
    """``{method: (scoped routes it sends to, guards it calls)}`` and the methods with an unreadable route."""
    tree = ast.parse(Path(client_module.__file__).read_text(encoding="utf-8"))
    scoped: dict[str, tuple[set[str], set[str]]] = {}
    dynamic: set[str] = set()
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HttpCatalogClient"):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            routes: set[str] = set()
            guards: set[str] = set()
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr in ("_get", "_post") and isinstance(f.value, ast.Name) and f.value.id == "self":
                    first = node.args[0] if node.args else None
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        if first.value in SCOPED_ROUTES:
                            routes.add(first.value)
                    else:
                        dynamic.add(fn.name)
                elif isinstance(f, ast.Name) and f.id in GUARDS:
                    guards.add(f.id)
            if routes:
                scoped[fn.name] = (routes, guards)
    return scoped, dynamic


def test_every_scoped_route_caller_is_guarded_or_exempt() -> None:
    scoped, _ = _scan()
    unguarded = {m: sorted(r) for m, (r, g) in scoped.items() if not g and m not in EXEMPT}
    assert not unguarded, (
        f"HttpCatalogClient methods send to a scoped route with no blank-filter guard: {unguarded}. "
        "Call require_nonblank_filter / refuse_whitespace_filters (nexus.errors), or name the method in EXEMPT with a reason."
    )


def test_the_census_is_not_vacuous() -> None:
    scoped, _ = _scan()
    missing = MUST_FIND - set(scoped)
    assert not missing, f"the census no longer finds these scoped callers (parser or rename drift): {sorted(missing)}"
    assert len(scoped) >= len(MUST_FIND)


def test_every_exemption_is_still_a_scoped_unguarded_caller() -> None:
    scoped, _ = _scan()
    stale = [m for m in EXEMPT if m not in scoped or scoped[m][1]]
    assert not stale, f"EXEMPT names methods that are gone or now guarded; remove them: {stale}"


def test_a_method_with_a_route_the_census_cannot_read_is_reviewed() -> None:
    _, dynamic = _scan()
    unreviewed = dynamic - set(DYNAMIC_ROUTE_REVIEWED)
    assert not unreviewed, f"read these methods' routes by hand and add them to DYNAMIC_ROUTE_REVIEWED: {sorted(unreviewed)}"
    stale = set(DYNAMIC_ROUTE_REVIEWED) - dynamic
    assert not stale, f"DYNAMIC_ROUTE_REVIEWED names methods that no longer use a dynamic route: {sorted(stale)}"


@pytest.mark.parametrize("src,expected", [
    ("class HttpCatalogClient:\n    def a(self, x):\n        return self._get('/list', corpus=x)\n", {"a": ({"/list"}, set())}),
    ("class HttpCatalogClient:\n    def a(self, x):\n        _nonblank('a', 'x', x)\n        return self._get('/list', corpus=x)\n", {"a": ({"/list"}, {"_nonblank"})}),
    ("class HttpCatalogClient:\n    def a(self):\n        return self._get('/other')\n", {}),
])
def test_the_scanner_reads_a_synthetic_class(src: str, expected: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scanner itself: an unguarded caller is seen, a guarded one lists its guard, an unscoped route is ignored."""
    f = tmp_path / "fake_client.py"
    f.write_text(src)
    monkeypatch.setattr(client_module, "__file__", str(f))
    assert _scan()[0] == expected
