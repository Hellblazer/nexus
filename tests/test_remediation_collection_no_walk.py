# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-241 Phase 1 Step 6 (nexus-uwggq.1.6): ``--collection`` reads one collection, not the catalog.

``nx catalog prune-stale --collection X`` and ``nx catalog remediate-paths --collection X`` used to walk
``all_documents()`` (1000 documents a page over HTTP) and keep the rows whose ``physical_collection == X``. They
now read ``list_by_collection(X)``: one ``GET /list?collection=X``.

The tests drive the real ``HttpCatalogClient`` with ``_get`` stubbed over a fake engine of 50,000 documents. The
request tests record the requests. The equality tests run each command twice over the same fixture, once on the new
read and once with ``list_by_collection`` replaced by the old walk (``all_documents()`` filtered with ``==``, run
through the same real client), and require identical output. The fake engine answers a ``collection`` read in
tumbler order and an unfiltered page in tumbler order, as the real engine does (both ``ORDER BY tumbler``,
both hide deleted rows), but it holds its rows in a shuffled list so an answer in insertion order would fail.

One input is not an equality case: a ``--collection`` that is whitespace only is a usage error. The engine treats a blank
``collection`` param as absent and lists the first 200 documents of the tenant, so the commands answer it with
no entries and send no ``/list`` request. The old walk matched nothing for it, so those tests take the old
walk's output for a name nothing matches as the expected output.
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from nexus.commands import catalog as _cat_cmd

DOCUMENTS = 50_000
TARGET = "rdr__target"
SHAPES = 10


def _tkey(d: dict[str, Any]) -> int:
    return int(d["tumbler"].rsplit(".", 1)[1])


def _wire_doc(i: int, tmp: Path) -> dict[str, Any]:
    """Ten shapes in rotation. Six are in TARGET (stale, stale, a basename, a live path, no path, a flagged
    stale row). The other four are in a differently cased name, a longer name that starts with TARGET, the empty
    collection and an unrelated collection: a ``== TARGET`` walk and an engine ``eq`` both exclude them."""
    t = f"1.2.{i + 1}"
    base: dict[str, Any] = {
        "tumbler": t, "title": f"d{i}", "physical_collection": TARGET,
        "file_path": f"/gone/{i}.md", "content_type": "rdr",
    }
    match i % SHAPES:
        case 0 | 1:
            pass  # stale absolute path in TARGET
        case 2:
            base["file_path"] = f"basename-{i}.pdf"  # remediable basename in TARGET
        case 3:
            base["file_path"] = str(tmp)  # exists on disk, so neither stale nor in need of remediation
        case 4:
            base["file_path"] = ""  # no source file
        case 5:
            base["sensitive"] = True  # a flagged row in TARGET: both reads carry it through unchanged
        case 6:
            base["physical_collection"] = TARGET.upper()  # differently cased: not TARGET
        case 7:
            base["physical_collection"] = f"{TARGET}-2"  # a longer name that starts with TARGET
        case 8:
            base["physical_collection"] = ""
        case 9:
            base["physical_collection"] = "knowledge__other"
    return base


class _FakeEngine:
    """The routes the two commands read: ``/list`` (a ``collection`` filter, ``limit``/``offset`` pages) and
    ``/owners/all-with-roots``. Rows come back sorted by tumbler, deleted rows never. Every request is recorded as
    ``(path, params)``."""

    def __init__(self, tmp: Path) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        docs = [_wire_doc(i, tmp) for i in range(DOCUMENTS)]
        deleted = {"tumbler": f"1.2.{DOCUMENTS + 1}", "title": "gone", "physical_collection": TARGET,
                   "file_path": "/gone/deleted.md", "deleted_at": "2026-01-01T00:00:00Z"}
        rows = docs + [deleted]
        random.Random(7).shuffle(rows)  # insertion order is not tumbler order
        self.rows = rows

    def _live(self) -> list[dict[str, Any]]:
        return sorted((d for d in self.rows if not d.get("deleted_at")), key=_tkey)

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.requests.append((path, dict(params)))
        if path == "/owners/all-with-roots":
            return {"owners": []}
        assert path == "/list", path
        live = self._live()
        if (c := params.get("collection")) and c.strip():
            return {"documents": [d for d in live if d["physical_collection"] == c]}
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 200))
        return {"documents": live[off:off + lim]}


@pytest.fixture
def engine(tmp_path: Path) -> _FakeEngine:
    return _FakeEngine(tmp_path)


def _client(engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
    cat = HttpCatalogClient(base_url="http://remediation.fake", _token="t")
    monkeypatch.setattr(
        cat, "_get", lambda path, **kw: engine.get(path, **{k: v for k, v in kw.items() if k != "idempotent"}),
    )
    return cat


def _use(monkeypatch: pytest.MonkeyPatch, cat: HttpCatalogClient, *, walk: bool) -> None:
    """Serve *cat* to the commands. With *walk*, ``list_by_collection`` is the OLD behaviour: every document
    through the real client's ``all_documents()``, filtered with ``==``. Without it, the client's own method."""
    cat.__dict__.pop("list_by_collection", None)  # drop a walk stub a previous call put on this client
    if walk:
        def _old_walk(collection: str, **_kw: Any) -> list:
            return [e for e in cat.all_documents() if e.physical_collection == collection]
        monkeypatch.setattr(cat, "list_by_collection", _old_walk)
    monkeypatch.setattr(_cat_cmd, "_get_catalog", lambda: cat)
    monkeypatch.setattr(_cat_cmd, "_get_catalog_writer", lambda: cat)  # the dry runs below never write


def _run(*args: str):
    result = CliRunner().invoke(main, ["catalog", *args])
    assert result.exit_code == 0, result.output
    return result


def _prune(*extra: str) -> list[str]:
    return ["prune-stale", "--dry-run", *extra]


def _remediate(src: Path, *extra: str) -> list[str]:
    return ["remediate-paths", str(src), "--dry-run", *extra]


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    d = tmp_path / "src"
    d.mkdir()
    (d / "basename-2.pdf").write_text("x")  # one basename resolves, so the report is not all "none"
    return d


# ── the request: one filtered /list, no unfiltered page ─────────────────────


class TestOneCollectionRequest:
    def test_prune_stale_sends_one_collection_list(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        out = _run(*_prune("--collection", TARGET)).stdout
        assert engine.requests == [
            ("/list", {"collection": TARGET}),
            ("/owners/all-with-roots", {}),
        ], engine.requests
        assert f"{DOCUMENTS // SHAPES * 3} stale entries" in out  # shapes 0, 1 and the flagged 5

    def test_remediate_paths_sends_one_collection_list(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, source_dir: Path,
    ) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        out = _run(*_remediate(source_dir, "--collection", TARGET)).stdout
        assert engine.requests == [("/list", {"collection": TARGET})], engine.requests
        # shapes 0, 1 and 5 (missing paths) and 2 (a basename) need remediation
        assert f"{DOCUMENTS // SHAPES * 4} entries need remediation" in out

    def test_without_collection_the_unfiltered_walk_is_unchanged(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, source_dir: Path,
    ) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        _run(*_prune())
        pages = [r for r in engine.requests if r[0] == "/list"]
        assert len(pages) == DOCUMENTS // 1000 + 1  # 50 full pages and the empty page that ends the walk
        assert all("collection" not in p for _, p in pages)
        engine.requests.clear()
        _run(*_remediate(source_dir))
        pages = [r for r in engine.requests if r[0] == "/list"]
        assert len(pages) == DOCUMENTS // 1000 + 1
        assert all("collection" not in p for _, p in pages)


# ── the rows: the same output as the old walk ───────────────────────────────


class TestOutputEqualsTheWalks:
    @pytest.mark.parametrize("collection", [TARGET, TARGET.upper(), f"{TARGET}-2", "knowledge__other", "absent__x"])
    def test_prune_stale(self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, collection: str) -> None:
        cat = _client(engine, monkeypatch)
        _use(monkeypatch, cat, walk=True)
        walked = _run(*_prune("--collection", collection)).stdout
        assert len([r for r in engine.requests if r[0] == "/list"]) > DOCUMENTS // 1000  # the old read paged
        engine.requests.clear()
        _use(monkeypatch, cat, walk=False)
        scoped = _run(*_prune("--collection", collection)).stdout
        assert scoped == walked
        assert [r for r in engine.requests if r[0] == "/list"] == [("/list", {"collection": collection})]

    @pytest.mark.parametrize("collection", [TARGET, TARGET.upper(), f"{TARGET}-2", "knowledge__other", "absent__x"])
    def test_remediate_paths(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, source_dir: Path, collection: str,
    ) -> None:
        cat = _client(engine, monkeypatch)
        _use(monkeypatch, cat, walk=True)
        walked = _run(*_remediate(source_dir, "--collection", collection)).stdout
        assert len(engine.requests) > DOCUMENTS // 1000
        engine.requests.clear()
        _use(monkeypatch, cat, walk=False)
        scoped = _run(*_remediate(source_dir, "--collection", collection)).stdout
        assert scoped == walked
        assert engine.requests == [("/list", {"collection": collection})]

    def test_the_target_report_is_not_vacuous(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, source_dir: Path,
    ) -> None:
        """The fixture really holds stale rows, a flagged row, and rows of other collections that must not be
        counted, so an equal pair of empty reports cannot pass."""
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        out = _run(*_prune("--collection", TARGET)).stdout
        assert "Sample (first 20):" in out
        assert "[rdr__target]" in out
        assert "[rdr__TARGET]" not in out and "rdr__target-2" not in out and "knowledge__other" not in out


# ── a whitespace-only name: a usage error, no /list request, nothing written ─


def _refused(*args: str):
    result = CliRunner().invoke(main, ["catalog", *args])
    assert result.exit_code == 2, result.output
    assert "--collection is blank" in result.output
    return result


# ASCII spaces and controls, and the Unicode spaces Java's isBlank also reads as blank (U+3000, U+2003,
# U+2028, U+001C): Python's str.strip covers every one, so none reaches the engine's blank branch.
@pytest.mark.parametrize("blank", [" ", "   ", "\t", " \n ", "\u3000", "\u2003", "\u2028", "\u001c"])
class TestWhitespaceOnlyCollection:
    def test_prune_stale_refuses_and_sends_no_list(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, blank: str,
    ) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        _refused(*_prune("--collection", blank))
        assert [r for r in engine.requests if r[0] == "/list"] == []

    def test_remediate_paths_refuses_and_sends_no_list(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, source_dir: Path, blank: str,
    ) -> None:
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        _refused(*_remediate(source_dir, "--collection", blank))
        assert engine.requests == []

    def test_prune_stale_confirmed_deletes_nothing(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, blank: str,
    ) -> None:
        """The destructive run, not only the dry run: --no-dry-run --confirm with a blank name deletes nothing."""
        cat = _client(engine, monkeypatch)
        _use(monkeypatch, cat, walk=False)
        deleted: list = []

        def _delete_many(tumblers, *_a, **_k):
            deleted.extend(tumblers)
            return list(tumblers)

        monkeypatch.setattr(cat, "delete_many", _delete_many, raising=False)
        _refused("prune-stale", "--no-dry-run", "--confirm", "--collection", blank)
        assert deleted == []  # no tumbler reached the writer
        assert [r for r in engine.requests if r[0] == "/list"] == []

    def test_remediate_paths_applied_rewrites_nothing(
        self, engine: _FakeEngine, monkeypatch: pytest.MonkeyPatch, source_dir: Path, blank: str,
    ) -> None:
        """The rewriting run, without --dry-run: a blank name sends no request and rewrites nothing."""
        _use(monkeypatch, _client(engine, monkeypatch), walk=False)
        _refused("remediate-paths", str(source_dir), "--collection", blank)
        assert engine.requests == []
