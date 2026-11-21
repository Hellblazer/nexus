# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 Phase 1 Step 3 (nexus-uwggq.1.3): the two backfill commands read one prefix, not the catalog.

``nx catalog backfill-source-uri`` wants the ``chroma://`` rows, and ``nx catalog backfill-etags`` with no
``--owner`` wants the ``https://`` rows. Both used to walk ``all_documents()``, which an HTTP client pages
1000 documents at a time. They now read ``list_scoped(source_uri_prefix=...)``: one ``GET /list`` with the
prefix.

The tests drive the real ``HttpCatalogClient`` with ``_get`` stubbed over a fake of 50,000 documents. One
test per command records the requests. The equality tests run each command twice over the same fixture, once
on the new read and once with ``list_scoped`` replaced by the old walk (``all_documents()`` filtered with
``str.startswith``, run through the same real client), and require identical output.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import HTTPS_ETAG_META_KEY
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands import catalog as _cat_cmd

pytestmark = pytest.mark.usefixtures("named_collection_rows")  # RDR-229 P2.9: prefix-named fixtures get a catalog row

DOCUMENTS = 50_000


def _wire_doc(i: int) -> dict[str, Any]:
    """Ten shapes in rotation: two chroma:// rows, five https:// rows (one with an ETag, one alias), and
    an uppercase CHROMA:// and HTTPS:// row that a literal, case-sensitive prefix excludes."""
    t = f"1.2.{i + 1}"
    base: dict[str, Any] = {"tumbler": t, "title": f"d{i}", "physical_collection": "rdr__x", "source_uri": ""}
    match i % 10:
        case 0:
            base["source_uri"] = f"chroma://rdr__x//abs/{i}.md"
        case 1:
            base.update(physical_collection="knowledge__x", source_uri=f"chroma://knowledge__x/T{i}")
        case 2:
            base["source_uri"] = f"CHROMA://rdr__x//abs/{i}.md"
        case 3 | 9:
            base["source_uri"] = f"https://example.com/{i}"
        case 4:
            base.update(source_uri=f"https://example.com/{i}", metadata={HTTPS_ETAG_META_KEY: '"have"'})
        case 5:
            base["source_uri"] = f"HTTPS://example.com/{i}"
        case 6:
            base.update(source_uri=f"https://example.com/{i}", alias_of="1.2.1")
        case 7:
            base["source_uri"] = f"file:///abs/{i}.md"
    return base


class _FakeEngine:
    """The ``/list`` route the catalog client reads: a literal prefix filter, an owner filter, and the
    unfiltered pages. Every request is recorded as ``(path, params)``."""

    def __init__(self, *, ignore_case: bool = False) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.docs = [_wire_doc(i) for i in range(DOCUMENTS)]
        self.ignore_case = ignore_case  # an engine whose prefix match ignores case, for the client guard

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.requests.append((path, dict(params)))
        assert path == "/list", path
        if prefix := params.get("source_uri_prefix"):
            if self.ignore_case:
                return {"documents": [d for d in self.docs if d["source_uri"].lower().startswith(prefix.lower())]}
            return {"documents": [d for d in self.docs if d["source_uri"].startswith(prefix)]}
        if owner := params.get("owner"):
            return {"documents": [d for d in self.docs if d["tumbler"].startswith(f"{owner}.")]}
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 200))
        return {"documents": self.docs[off:off + lim]}


@pytest.fixture
def engine() -> _FakeEngine:
    return _FakeEngine()


def _client(engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
    cat = HttpCatalogClient(base_url="http://backfill.fake", _token="t")
    monkeypatch.setattr(
        cat, "_get", lambda path, **kw: engine.get(path, **{k: v for k, v in kw.items() if k != "idempotent"}),
    )
    return cat


def _use(monkeypatch: pytest.MonkeyPatch, cat: HttpCatalogClient, *, walk: bool) -> None:
    """Serve *cat* to the commands. With *walk*, ``list_scoped`` is the OLD behaviour: every document through
    the real client's ``all_documents()``, filtered with ``str.startswith``."""
    if walk:
        def _old_walk(*, source_uri_prefix: str = "", **_kw: Any) -> list:
            return [e for e in cat.all_documents() if (e.source_uri or "").startswith(source_uri_prefix)]
        monkeypatch.setattr(cat, "list_scoped", _old_walk)
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: cat)


def _run(*args: str):
    result = CliRunner().invoke(main, ["catalog", *args])
    assert result.exit_code == 0, result.output
    return result


class TestBackfillSourceUri:
    def test_one_prefix_request_and_no_unfiltered_list(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        report = json.loads(_run("backfill-source-uri", "--json").stdout)
        assert engine.requests == [("/list", {"source_uri_prefix": "chroma://"})], engine.requests
        assert report["total_chroma_rows"] == DOCUMENTS // 5  # CHROMA:// rows are not chroma:// rows

    def test_output_equals_the_walks(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        cat = _client(engine, monkeypatch)
        _use(monkeypatch, cat, walk=True)
        walked = _run("backfill-source-uri", "--json").stdout
        assert len(engine.requests) > DOCUMENTS // 1000  # the old read really did page the catalog
        engine.requests.clear()
        _use(monkeypatch, cat, walk=False)
        scoped = _run("backfill-source-uri", "--json").stdout
        assert scoped == walked
        report = json.loads(scoped)
        assert report["by_collection"] == {
            # candidate_rows (RDR-229 P2.10): the collection's rows the verb counts as residue to re-derive
            "knowledge__x": {"count": DOCUMENTS // 10, "candidate": False, "candidate_rows": 0},
            "rdr__x": {"count": DOCUMENTS // 10, "candidate": True, "candidate_rows": DOCUMENTS // 10},
        }
        assert report["candidates"] == report["rederivable"] == DOCUMENTS // 10
        assert all(u["was"].startswith("chroma://") for u in report["updates"])  # none of the CHROMA:// rows

    def test_text_report_equals_the_walks(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        cat = _client(engine, monkeypatch)
        _use(monkeypatch, cat, walk=True)
        walked = _run("backfill-source-uri").output
        _use(monkeypatch, cat, walk=False)
        assert _run("backfill-source-uri").output == walked


class TestBackfillEtags:
    def test_one_prefix_request_and_no_unfiltered_list(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        out = _run("backfill-etags", "--dry-run").output
        assert engine.requests == [("/list", {"source_uri_prefix": "https://"})], engine.requests
        # i % 10 in {3, 9} are the candidates: https://, no ETag, not an alias
        assert f"missing an ETag: {DOCUMENTS // 5}" in out

    def test_output_equals_the_walks(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        cat = _client(engine, monkeypatch)
        _use(monkeypatch, cat, walk=True)
        walked = _run("backfill-etags", "--dry-run", "--limit", "2000").output
        assert len(engine.requests) > DOCUMENTS // 1000
        _use(monkeypatch, cat, walk=False)
        scoped = _run("backfill-etags", "--dry-run", "--limit", "2000").output
        assert scoped == walked
        assert f"missing an ETag: {DOCUMENTS // 5}" in scoped
        assert "HTTPS://" not in scoped  # the uppercase scheme is no candidate

    def test_an_owner_still_reads_that_owner_and_sends_no_prefix(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        out = _run("backfill-etags", "--dry-run", "--owner", "1.2").output
        assert engine.requests == [("/list", {"owner": "1.2"})], engine.requests
        assert f"missing an ETag: {DOCUMENTS // 5}" in out
        assert "under owner 1.2" in out


@pytest.mark.parametrize("command", ["backfill-source-uri", "backfill-etags"])
def test_an_engine_that_ignores_case_does_not_change_either_command(
    command: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's prefix match is case-sensitive SQL LIKE today; nothing in this repo runs it against an
    upper-case scheme. Each command checks the scheme itself, so its output is the same when the engine
    returns CHROMA:// and HTTPS:// rows too."""
    args = [command, "--json"] if command == "backfill-source-uri" else [command, "--dry-run", "--limit", "2000"]
    exact = _FakeEngine()
    _use(monkeypatch, _client(exact, monkeypatch), walk=False)
    expected = _run(*args).stdout
    loose = _FakeEngine(ignore_case=True)
    _use(monkeypatch, _client(loose, monkeypatch), walk=False)
    assert _run(*args).stdout == expected
