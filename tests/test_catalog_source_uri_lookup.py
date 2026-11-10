# SPDX-License-Identifier: AGPL-3.0-or-later
"""A document's source URI finds it in every catalog lookup.

Measured 2026-11-09 on the live catalog: tumbler 1.12.241 carries
``source_uri = x-devonthink-item://B60D6C67-...``, yet ``nx catalog show
<uri>``, MCP ``show(title=<uri>)`` and MCP ``search(file_path=<uri>)`` all
reported Not found. The resolvers tried a tumbler, then a title or path, and
never the URI the document is keyed by. A DEVONthink record's ``file_path`` is
the database package path, so the URI is the only handle a caller holds.

Five scenarios against the real engine (each mints a tenant, and the tenant
ceiling is a count of tests, so the surfaces share a test rather than one each).
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog import resolve_tumbler
from nexus.catalog.factory import make_catalog_writer
from nexus.cli import main
from nexus.mcp_server import _reset_singletons, catalog_resolve, catalog_search, catalog_show
from tests._catalog_fixture_ops import ActiveCatalog

pytestmark = pytest.mark.t2_tenant

UUID = "B60D6C67-9FB2-4418-8299-C655336F189F"
DT_URI = f"x-devonthink-item://{UUID}"
UNINDEXED_URI = "x-devonthink-item://FFFFFFFF-0000-0000-0000-000000000000"
DT_PATH = "/Users/someone/Library/Application Support/DEVONthink/Inbox.dtBase2/Files.noindex/pdf/26/paper.pdf"
COLLECTION = "knowledge__writing-craft__voyage-context-3__v1"


@pytest.fixture(autouse=True)
def _no_document_is_sensitive():
    """Redefines tests/conftest.py's fixture of this name: the withhold leg reads the real catalog."""
    yield


@pytest.fixture
def dt_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    cfg = tmp_path / "nexus-config"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))
    _reset_singletons()
    cat = ActiveCatalog()
    cat.register_owner("dt", "curator")
    # The decoy's title is the target's URI: a title search must never win over the URI.
    cat.register("1.1", DT_URI, content_type="paper", file_path="/x/decoy.pdf",
                 source_uri="x-devonthink-item://00000000-0000-0000-0000-000000000000")
    t = cat.register("1.1", "Why is AI-generated language so hard to understand?",
                     content_type="paper", file_path=DT_PATH, source_uri=DT_URI,
                     physical_collection=COLLECTION)
    yield str(t)
    _reset_singletons()


def _tumblers(rows: list[dict]) -> list[str]:
    assert not any("error" in r for r in rows), rows
    return [r["tumbler"] for r in rows if "tumbler" in r]


def _cli(*args: str):
    return CliRunner().invoke(main, ["catalog", *args])


def test_every_surface_finds_the_document_by_its_source_uri(dt_doc) -> None:
    t, err = resolve_tumbler(ActiveCatalog(), DT_URI)
    assert err is None and str(t) == dt_doc

    shown = _cli("show", DT_URI, "--json")
    assert shown.exit_code == 0, shown.output
    assert f'"tumbler": "{dt_doc}"' in shown.stdout
    found = _cli("search", DT_URI)
    assert found.exit_code == 0 and dt_doc in found.stdout, found.output

    for kwargs in ({"title": DT_URI}, {"tumbler": DT_URI}):
        d = catalog_show(**kwargs)
        assert "error" not in d and d["tumbler"] == dt_doc, (kwargs, d)
    assert _tumblers(catalog_search(file_path=DT_URI)) == [dt_doc]
    assert _tumblers(catalog_search(query=DT_URI)) == [dt_doc]
    assert catalog_resolve(tumbler=DT_URI) == [COLLECTION]
    # The package path still finds it, as before.
    assert _tumblers(catalog_search(file_path=DT_PATH)) == [dt_doc]


@pytest.mark.parametrize("variant", [
    DT_URI.lower(),                 # a lowercased link
    UUID,                           # the bare UUID `nx dt` verbs take
    UUID.lower(),
    f"{DT_URI}?page=3",             # an item link to a page
    f"{DT_URI}/",
])
def test_devonthink_handle_variants_find_the_stored_uri(dt_doc, variant: str) -> None:
    t, err = resolve_tumbler(ActiveCatalog(), variant)
    assert err is None and str(t) == dt_doc, (variant, err)
    d = catalog_show(tumbler=variant)
    assert d.get("tumbler") == dt_doc, (variant, d)
    assert _tumblers(catalog_search(file_path=variant)) == [dt_doc], variant


def test_an_unindexed_uri_matches_nothing_on_any_surface(dt_doc) -> None:
    t, err = resolve_tumbler(ActiveCatalog(), UNINDEXED_URI)
    assert t is None and err and err.startswith("Not found"), err
    assert _cli("show", UNINDEXED_URI).exit_code != 0
    assert dt_doc not in _cli("search", UNINDEXED_URI).stdout
    for kwargs in ({"title": UNINDEXED_URI}, {"tumbler": UNINDEXED_URI}):
        assert "error" in catalog_show(**kwargs), kwargs
    assert _tumblers(catalog_search(file_path=UNINDEXED_URI)) == []
    assert _tumblers(catalog_search(query=UNINDEXED_URI)) == []
    assert catalog_resolve(tumbler=UNINDEXED_URI) == []


def test_filters_apply_to_a_uri_hit(dt_doc) -> None:
    for uri_arg in ({"query": DT_URI}, {"file_path": DT_URI}):
        assert _tumblers(catalog_search(**uri_arg, content_type="paper")) == [dt_doc], uri_arg
        assert _tumblers(catalog_search(**uri_arg, content_type="code")) == [], uri_arg
        assert _tumblers(catalog_search(**uri_arg, owner="1.1")) == [dt_doc], uri_arg
        assert _tumblers(catalog_search(**uri_arg, owner="1.2")) == [], uri_arg
        assert _tumblers(catalog_search(**uri_arg, corpus="no-such-corpus")) == [], uri_arg


def test_a_sensitive_document_found_by_uri_is_withheld(dt_doc) -> None:
    out = make_catalog_writer().set_sensitivity([dt_doc], True, reason="source-uri lookup test")
    assert out["updated"] == [1], out
    for rows in (catalog_search(query=DT_URI), catalog_search(file_path=DT_URI)):
        row = next(r for r in rows if r.get("tumbler") == dt_doc)
        assert "withheld" in row and "title" not in row and "source_uri" not in row, row
    shown = catalog_show(tumbler=DT_URI)
    assert shown["tumbler"] == dt_doc and "withheld" in shown and "title" not in shown, shown
