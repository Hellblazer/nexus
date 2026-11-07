# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 (nexus-rfvyy.1): the catalog MCP tools apply the output posture to document rows.

A catalog document row's title, path, source URI, author and bibliographic fields are content (RDR-243 census rows
7 and 23). Under the default posture a sensitive document's row is its stub: the tumbler and the non-content
columns, plus ``withheld``; the response counts the stubs, and a stub takes its row's slot so a page does not
thin or shift. ``sensitivity.mcp_output: flag`` returns the full row with a badge and the reader clause.

Against the real engine and catalog, three scenarios (each mints a tenant, and the tenant ceiling is a count of
tests: the per-field cases live in tests/test_sensitive_output.py, which needs none). Each names a canary that
lives only in the sensitive document's title, path, URI, author and link meta, and asserts it is absent from the
WHOLE serialized response.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.context_annotations import SENSITIVE_READER_CLAUSE
from nexus.mcp_server import _reset_singletons, catalog_links, catalog_list, catalog_search, catalog_show
from tests._catalog_fixture_ops import ActiveCatalog

pytestmark = pytest.mark.t2_tenant

CANARY = f"CANARY-rfvyy1-{uuid4().hex[:12]}"
SECRET_TITLE = f"{CANARY} board minutes"


@pytest.fixture(autouse=True)
def _no_document_is_sensitive():
    """Redefines tests/conftest.py's fixture of this name: the alias leg reads the real catalog."""
    yield


@pytest.fixture(autouse=True)
def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = tmp_path / "nexus-config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@test.invalid")
    _reset_singletons()
    yield cfg
    _reset_singletons()


def _set_mcp_output(cfg: Path, value: str | None) -> None:
    (cfg / "config.yml").write_text("" if value is None else f"sensitivity:\n  mcp_output: {value}\n")


class _World:
    """An owner with three documents: a clean one either side of a sensitive one (title, path, URI, author carry
    the canary), a link clean -> sensitive whose meta carries the canary, a link clean -> clean, and the flag set
    through the audited route."""

    def __init__(self, *, flag: bool = True) -> None:
        from nexus.catalog.factory import make_catalog_writer

        self.cat = ActiveCatalog()
        self.owner = str(self.cat.register_owner("rfvyy1-owner", "curator", tumbler_prefix="71.1"))
        self.clean_a = str(self.cat.register(
            self.owner, "Gardening notes", content_type="knowledge", source_uri="file:///rfvyy1/a.md"))
        self.secret = str(self.cat.register(
            self.owner, SECRET_TITLE, content_type="knowledge", author=f"{CANARY} author",
            file_path=f"{CANARY}/minutes.md", source_uri=f"file:///{CANARY}/minutes.md",
        ))
        self.clean_b = str(self.cat.register(
            self.owner, "Orchard planning", content_type="knowledge", source_uri="file:///rfvyy1/b.md"))
        self.cat.link(self.clean_a, self.secret, "relates", "test", note=f"about {CANARY}")
        self.cat.link(self.clean_a, self.clean_b, "relates", "test", note="plain")
        if flag:
            out = make_catalog_writer().set_sensitivity([self.secret], True, reason="rfvyy1 test")
            assert out["updated"] == [1], out


def _wire(result) -> str:
    return json.dumps(result, default=str)


def _row(rows: list[dict], tumbler: str) -> dict:
    found = [r for r in rows if r.get("tumbler") == tumbler]
    assert len(found) == 1, (tumbler, rows)
    return found[0]


NON_CONTENT_ONLY = {"title", "author", "file_path", "source_uri", "meta", "corpus", "bib_doi"}


def test_withhold_is_the_default_on_search_list_show_and_links(_world: Path) -> None:
    w = _World()
    stub_line = f"[sensitive] {w.secret}: content withheld"
    count = {"_withheld": {"count": 1, "note": "1 sensitive hit withheld"}}

    # search: a query that matches only the sensitive title is itself a leak through the match; the row is a stub.
    rows = catalog_search(query=CANARY)
    stub = _row(rows, w.secret)
    assert stub["withheld"] == stub_line and stub["sensitive"] is True, rows
    assert NON_CONTENT_ONLY.isdisjoint(stub), stub
    assert count in rows and CANARY not in _wire(rows), rows

    # search by owner: the stub keeps its slot, the clean rows are whole.
    docs = [r for r in catalog_search(owner=w.owner) if "tumbler" in r]
    assert [r["tumbler"] for r in docs] == [w.clean_a, w.secret, w.clean_b], docs
    assert _row(docs, w.clean_a)["title"] == "Gardening notes" and _row(docs, w.clean_b)["title"] == "Orchard planning"

    # list (owner page): next_offset is unchanged by the stub, and the count is stated.
    rows = catalog_list(owner=w.owner, limit=2)
    assert [r["tumbler"] for r in rows if "tumbler" in r] == [w.clean_a, w.secret], rows
    assert rows[-1] == {"_pagination": {"next_offset": 2, "limit": 2}} and count in rows, rows
    assert CANARY not in _wire(rows)

    # list (all documents of a type).
    rows = catalog_list(content_type="knowledge")
    assert "withheld" in _row(rows, w.secret) and CANARY not in _wire(rows), rows

    # show by tumbler: the stub, and the links keep tumblers and types and lose their meta.
    shown = catalog_show(tumbler=w.secret)
    assert shown["withheld"] == stub_line and NON_CONTENT_ONLY.isdisjoint(shown), shown
    (edge,) = shown["links_to"]
    assert (edge["from"], edge["to"], edge["type"], edge["meta"]) == (w.clean_a, w.secret, "relates", {}), edge
    assert shown["_withheld"]["count"] == 1 and CANARY not in _wire(shown), shown

    # show by title: the title is not echoed back.
    by_title = catalog_show(title=SECRET_TITLE)
    assert by_title.get("tumbler") == w.secret and "withheld" in by_title, by_title
    assert CANARY not in _wire(by_title)

    # links: the sensitive node is a stub, both edges stay, and only the edge that touches it loses its meta.
    graph = catalog_links(tumbler=w.clean_a, direction="out")
    nodes = {n["tumbler"]: n for n in graph["nodes"]}
    assert "withheld" in nodes[w.secret] and "title" not in nodes[w.secret], nodes
    assert nodes[w.clean_b]["title"] == "Orchard planning", nodes
    edges = {(e["from"], e["to"]): e for e in graph["edges"]}
    assert set(edges) == {(w.clean_a, w.secret), (w.clean_a, w.clean_b)}, edges
    assert edges[(w.clean_a, w.secret)]["meta"] == {} and edges[(w.clean_a, w.clean_b)]["meta"] == {"note": "plain"}
    assert graph["_withheld"]["count"] == 1 and CANARY not in _wire(graph), graph

    # a misspelled posture is withhold: only the exact value `flag` shows content.
    _set_mcp_output(_world, "falg")
    assert CANARY not in _wire(catalog_search(query=CANARY))


def test_flag_returns_the_full_row_with_a_badge_and_the_reader_clause(_world: Path) -> None:
    w = _World()
    _set_mcp_output(_world, "flag")
    rows = catalog_search(query=CANARY)
    row = _row(rows, w.secret)
    assert row["title"] == SECRET_TITLE and row["sensitive"] is True and row["badge"] == "[sensitive]", row
    assert {"_sensitive_notice": SENSITIVE_READER_CLAUSE} in rows and not any("_withheld" in r for r in rows), rows
    shown = catalog_show(tumbler=w.secret)
    assert shown["title"] == SECRET_TITLE and shown["_sensitive_notice"] == SENSITIVE_READER_CLAUSE, shown
    assert shown["links_to"][0]["meta"] == {"note": f"about {CANARY}"}, shown
    graph = catalog_links(tumbler=w.clean_a, direction="out")
    assert {n["tumbler"]: n for n in graph["nodes"]}[w.secret]["title"] == SECRET_TITLE


def test_a_page_with_no_sensitive_document_is_unchanged_and_an_alias_of_a_sensitive_one_is_withheld() -> None:
    w = _World(flag=False)
    rows = catalog_search(owner=w.owner)
    assert [r["title"] for r in rows if "tumbler" in r] == ["Gardening notes", SECRET_TITLE, "Orchard planning"]
    assert not any("_withheld" in r or "_sensitive_notice" in r for r in rows), rows
    assert all(r["sensitive"] is False for r in rows if "tumbler" in r)
    assert catalog_show(tumbler=w.clean_b)["links_to"][0]["meta"] == {"note": "plain"}

    # Flag the canonical AFTER a duplicate was merged into it: the alias row carries its own, unset, flag and
    # its own title, and is withheld because the canonical's flag counts.
    dup = str(w.cat.register(w.owner, f"{CANARY} copy", content_type="knowledge", source_uri="file:///rfvyy1/dup.md"))
    w.cat.merge_documents(dup, w.secret)
    from nexus.catalog.factory import make_catalog_writer

    make_catalog_writer().set_sensitivity([w.secret], True, reason="rfvyy1 alias leg")
    rows = catalog_search(owner=w.owner)
    assert "withheld" in _row(rows, dup), rows
    assert CANARY not in _wire(rows)
