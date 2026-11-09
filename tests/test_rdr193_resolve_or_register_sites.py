# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): the three sites that resolved a document identity with several requests now make one.

``store_hook.catalog_store_hook_tracked`` (a titled ``nx store put``), ``doc_indexer._register_or_lookup_doc_id`` (the
pre-flight of ``nx index md`` / ``nx index pdf``) and ``doc_indexer._catalog_markdown_hook`` resolve or register a
document through ``POST /v1/catalog/doc/resolve_or_register``: the engine looks the source URI up, falls back to a ghost
of the same title where asked, and otherwise registers the document (born flagged when sensitive), carrying the title,
year and source_mtime. The tests count the requests on the wire against the real engine (``t2_tenant``) and read the
rows back; the ``created`` signal that ``rollback_minted_catalog_entry`` depends on is pinned in both directions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.store_hook import catalog_store_hook_tracked
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

pytestmark = pytest.mark.t2_tenant

ROR = "/v1/catalog/doc/resolve_or_register"
REGISTER = "/v1/catalog/doc/register"


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []
    real = RefreshableHttpStoreMixin._send

    def spy(self, method, path, **kw):  # noqa: ANN001
        sent.append((method, path))
        return real(self, method, path, **kw)

    monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", spy)
    return sent


def _n(sent: list[tuple[str, str]], path: str) -> int:
    return sum(1 for _m, p in sent if p == path)


def _row(tumbler: str):
    cat = HttpCatalogClient()
    try:
        return cat.resolve(tumbler)
    finally:
        cat.close()


# ── store_hook ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_titled_put_registers_with_one_resolve_or_register_and_stamps_its_chash(wire) -> None:
    chash = "a" * 64
    tumbler, created = catalog_store_hook_tracked("Note One", chash, "knowledge__sites")
    assert created is True and tumbler
    assert _n(wire, ROR) == 1 and _n(wire, REGISTER) == 0
    row = _row(tumbler)
    assert row.title == "Note One" and row.content_type == "knowledge" and row.physical_collection == "knowledge__sites"
    assert row.meta.get("doc_id") == chash and row.source_uri == "nx-note://knowledge__sites/Note One"


def test_a_reput_of_the_same_title_resolves_onto_its_row_and_reports_the_prior_stamp(wire) -> None:
    first, _ = catalog_store_hook_tracked("Note Two", "b" * 64, "knowledge__sites")
    wire.clear()
    prior: dict = {}
    again, created = catalog_store_hook_tracked("Note Two", "c" * 64, "knowledge__sites", pre_call_doc_id_out=prior)
    assert (again, created) == (first, False), "a re-put never reports a mint, so no rollback ever deletes its row"
    assert prior["doc_id"] == "b" * 64, "the stamp as it stood before this put overwrote it"
    assert _n(wire, ROR) == 1 and _n(wire, REGISTER) == 0
    assert _row(first).meta["doc_id"] == "c" * 64


def test_a_ghost_of_the_same_title_is_reused_and_takes_the_source_uri(wire) -> None:
    cat = HttpCatalogClient()
    try:
        owner = cat.register_owner("knowledge", "curator")
        ghost = str(cat.register(owner, "Ghost Note", content_type="knowledge", physical_collection="knowledge__old"))
    finally:
        cat.close()
    tumbler, created = catalog_store_hook_tracked("Ghost Note", "d" * 64, "knowledge__sites")
    assert (tumbler, created) == (ghost, False)
    row = _row(ghost)
    assert row.source_uri == "nx-note://knowledge__sites/Ghost Note" and row.physical_collection == "knowledge__sites"


def test_a_sensitive_titled_put_is_born_flagged(wire) -> None:
    tumbler, created = catalog_store_hook_tracked("Sealed Note", "e" * 64, "knowledge__sites", sensitive=True)
    assert created is True
    assert _row(tumbler).sensitive is True


def test_an_untitled_put_is_still_minted_by_register_not_by_resolve_or_register(wire) -> None:
    tumbler, created = catalog_store_hook_tracked("", "f" * 64, "knowledge__sites")
    assert created is True and tumbler
    assert _n(wire, REGISTER) == 1 and _n(wire, ROR) == 0


def test_a_stamp_that_cannot_be_written_rolls_the_minted_row_back_and_reports_no_document() -> None:
    def refuse(self, tumbler, **fields):  # noqa: ANN001, ARG001
        raise RuntimeError("the stamp write failed")

    error: dict = {}
    with pytest.MonkeyPatch.context() as local:
        local.setattr(HttpCatalogClient, "update", refuse)
        tumbler, created = catalog_store_hook_tracked("Doomed Note", "9" * 64, "knowledge__sites", error_out=error)
    assert (tumbler, created) == ("", False) and "stamp write failed" in error["error"]
    cat = HttpCatalogClient()
    try:
        assert not cat.find_by_title_exact("Doomed Note"), "the row this call minted was rolled back"
    finally:
        cat.close()


# ── doc_indexer ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_preflight_registers_a_curator_document_with_one_request_and_a_second_call_finds_it(tmp_path: Path, wire) -> None:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    f = tmp_path / "paper.md"
    f.write_text("# Paper\nbody\n")
    first, created = _register_or_lookup_doc_id(
        f, "sites-corpus", content_type="prose", physical_collection="docs__sites", title="A Paper", year=2021,
        author="Ada", with_created=True,
    )
    assert created is True and first
    assert _n(wire, ROR) == 1 and _n(wire, REGISTER) == 0
    row = _row(first)
    assert (row.title, row.year, row.content_type, row.physical_collection) == ("A Paper", 2021, "prose", "docs__sites")
    assert row.source_mtime == pytest.approx(f.stat().st_mtime)
    assert row.source_uri == "file://" + str(f.resolve()) or row.source_uri.endswith(str(f.name))
    assert row.author == "Ada" and row.meta.get("source_format"), "the stamp and author follow in one update"
    wire.clear()
    second, created2 = _register_or_lookup_doc_id(
        f, "sites-corpus", content_type="prose", physical_collection="docs__sites", title="A Paper", with_created=True,
    )
    assert (second, created2) == (first, False), "an existing document is found by the lookup, not minted again"
    assert _n(wire, ROR) == 0


def test_the_preflight_resolves_onto_a_row_another_owner_holds_and_does_not_mint(tmp_path: Path, wire) -> None:
    from nexus.doc_indexer import _register_or_lookup_doc_id

    f = tmp_path / "held.md"
    f.write_text("# Held\n")
    cat = HttpCatalogClient()
    try:
        other = cat.register_owner("sites-other", "curator")
        held = str(cat.register(other, "Held", content_type="prose", file_path=str(f.resolve()),
                                physical_collection="docs__other", source_uri="file://" + str(f.resolve())))
    finally:
        cat.close()
    tumbler, created = _register_or_lookup_doc_id(
        f, "sites-corpus", content_type="prose", physical_collection="docs__sites", title="Held", with_created=True,
    )
    assert tumbler == held and created is False
    assert _row(held).physical_collection == "docs__other", "the other owner's row is left as it is"
