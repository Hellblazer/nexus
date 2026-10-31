# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.8 and nexus-6pfgb.23 (RDR-236 Phase 1): an indexed document's ``source_format``, ``tags`` and
``category`` live in its own catalog ``meta``, stamped at register.

Every indexer writes ``tags`` and ``category`` on each chunk row as constants of the file's kind (the file
extension, ``markdown`` or ``pdf``; ``code``, ``prose`` or ``paper``), and ``content_type`` there is the
chunk's format. Two documents that share a chunk (a ``.py`` and a ``.md`` with the same text, a PDF page and a
markdown file with the same line) share one row, which holds the last writer's value. The document's meta
holds its own. ``tags`` is a JSON ARRAY, the shape the notes' tags take (nexus-6pfgb.38); ``category`` is a
string; ``source_format`` is what the engine's ``nexus.catalog_source_format`` derives for the document's
content type and file path (pdf / markdown / code / prose).

Only what the row writer wrote is stamped. A file with no extension has no tag (the row writer wrote an empty
string, and an empty value is never sent: the engine merges ``meta`` and an explicit empty overwrites). The
repo PDF path says category ``prose`` where ``nx index pdf`` says ``paper``; that is RECORDED here, not fixed.

Real engine: the repo catalog pass (``_catalog_hook``) for code, prose, rdr and repo PDFs, and the single-file
entry points (``index_markdown``, ``index_pdf``, both routes). The stamp rides the register / update the seam
already sends, one ``meta`` per document: the warm-run pin counts the writes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import indexer
from nexus.catalog import factory
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_file_path
from tests.integration.test_rdr223_pdf_journey import _COLLECTION, _FakeChunker, _FakeExtractor, _index, _lines, _write_pdf

pytestmark = [pytest.mark.integration]

REPO_HASH = "6bf8e4a1"
FILES = {  # rel path -> (content_type, text)
    "src/app.py": ("code", "def f():\n    return 1\n"),
    "src/UPPER.PY": ("code", "def g():\n    return 2\n"),
    "Makefile": ("code", "all:\n\techo hi\n"),
    "docs/guide.md": ("prose", "# Guide\n\nA guide.\n"),
    "docs/notes.txt": ("prose", "plain notes\n"),
    "docs/rdr/rdr-9001-x.md": ("rdr", "---\ntitle: x\nid: RDR-9001\n---\n# x\n"),
    "papers/p.pdf": ("pdf", "%PDF-1.4 6pfgb8\n"),
}
COLLECTIONS = {"code": "code__6pfgb8", "prose": "docs__6pfgb8", "rdr": "rdr__6pfgb8", "pdf": "docs__6pfgb8"}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _write_repo(root: Path) -> list[tuple[Path, str, str]]:
    out = []
    for rel, (ctype, text) in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        out.append((path, ctype, COLLECTIONS[ctype]))
    return out


def _hook(root: Path, files: list[tuple[Path, str, str]], head: str = "aaa") -> None:
    indexer._catalog_hook(repo=root, repo_name="r6pfgb8", repo_hash=REPO_HASH, head_hash=head, indexed_files=files)


def _meta(cat: ActiveCatalog, rel: str) -> dict:
    owner = cat.owner_for_repo(REPO_HASH)
    entry = cat.by_file_path(owner, rel)
    assert entry is not None, rel
    return dict(entry.meta)


#: rel path -> (tags, category, source_format)
EXPECTED = {
    "src/app.py": (["py"], "code", "code"),
    "src/UPPER.PY": (["py"], "code", "code"),            # the row writer lower-cases the suffix
    "docs/guide.md": (["markdown"], "prose", "markdown"),
    "docs/notes.txt": (["txt"], "prose", "prose"),
    "docs/rdr/rdr-9001-x.md": (["markdown"], "prose", "markdown"),   # rdr markdown goes through the prose indexer
    "papers/p.pdf": (["pdf"], "prose", "pdf"),           # the repo PDF path: "prose", where nx index pdf says "paper"
}


def test_the_repo_catalog_pass_stamps_each_document_with_its_own_values(tmp_path: Path) -> None:
    files = _write_repo(tmp_path)
    _hook(tmp_path, files)
    cat = ActiveCatalog()
    for rel, (tags, category, fmt) in EXPECTED.items():
        meta = _meta(cat, rel)
        assert meta.get("tags") == tags, (rel, meta)
        assert meta.get("category") == category, (rel, meta)
        assert meta.get("source_format") == fmt, (rel, meta)
    make = _meta(cat, "Makefile")
    assert make.get("category") == "code" and make.get("source_format") == "code"
    assert "tags" not in make, "no extension, no tag: an empty value is never sent"


def test_two_files_sharing_a_chunk_each_keep_their_own_value(tmp_path: Path) -> None:
    """A ``.py`` and a ``.md`` with the SAME text share a chunk (one content hash), so a row can hold only one
    of ``py`` / ``markdown``. Each document's meta holds its own."""
    shared = "# shared 6pfgb8 body\n"
    (tmp_path / "a.py").write_text(shared)
    (tmp_path / "b.md").write_text(shared)
    _hook(tmp_path, [(tmp_path / "a.py", "code", COLLECTIONS["code"]), (tmp_path / "b.md", "prose", COLLECTIONS["prose"])])
    cat = ActiveCatalog()
    a, b = _meta(cat, "a.py"), _meta(cat, "b.md")
    assert (a["tags"], a["category"], a["source_format"]) == (["py"], "code", "code")
    assert (b["tags"], b["category"], b["source_format"]) == (["markdown"], "prose", "markdown")


class _RecordingWriter:
    """The real catalog writer, with the document writes it is asked for recorded."""

    def __init__(self, inner, log: list[tuple[str, int]]) -> None:
        self._inner, self._log = inner, log

    def update_many(self, updates):
        self._log.append(("update_many", len(updates)))
        return self._inner.update_many(updates)

    def update(self, tumbler, **kw):
        self._log.append(("update", 1))
        return self._inner.update(tumbler, **kw)

    def register_many(self, owner, docs, **kw):
        self._log.append(("register_many", len(docs)))
        return self._inner.register_many(owner, docs, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _record(monkeypatch: pytest.MonkeyPatch, log: list[tuple[str, int]]):
    real_make = factory.make_catalog_writer
    monkeypatch.setattr(factory, "make_catalog_writer", lambda *a, **k: _RecordingWriter(real_make(*a, **k), log))


def test_the_stamp_costs_no_request_of_its_own_and_a_warm_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _write_repo(tmp_path)
    cold: list[tuple[str, int]] = []
    with monkeypatch.context() as m:
        _record(m, cold)
        _hook(tmp_path, files)
    # one register_many page carries every document and its meta: no update rides behind it
    assert cold == [("register_many", len(files))], cold

    warm: list[tuple[str, int]] = []
    with monkeypatch.context() as m:
        _record(m, warm)
        _hook(tmp_path, files)
    assert warm == [], "a stamped, unchanged document is not rewritten on a warm run"


def test_a_registration_that_predates_the_stamp_is_filled_once_in_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _write_repo(tmp_path)
    with monkeypatch.context() as m:
        # the registration as the previous client made it (raising=False: the red run is on a tree without the name)
        m.setattr(indexer, "document_register_meta", lambda *a, **k: {}, raising=False)
        _hook(tmp_path, files)
    cat = ActiveCatalog()
    held = _meta(cat, "src/app.py")
    assert not {"tags", "category", "source_format"} & set(held)

    filled: list[tuple[str, int]] = []
    with monkeypatch.context() as m:
        _record(m, filled)
        _hook(tmp_path, files)  # content and HEAD unchanged: only the missing stamp can move the document
    assert filled == [("update_many", len(files))], "every document is filled by ONE batched update, none by a per-file one"
    for rel, (tags, category, fmt) in EXPECTED.items():
        meta = _meta(cat, rel)
        assert (meta["tags"], meta["category"], meta["source_format"]) == (tags, category, fmt)

    again: list[tuple[str, int]] = []
    with monkeypatch.context() as m:
        _record(m, again)
        _hook(tmp_path, files)
    assert again == []


# ── the single-file entry points ──────────────────────────────────────────────────────────────


def test_index_markdown_stamps_markdown_prose_and_the_derived_format(tmp_path: Path) -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    md = tmp_path / "6pfgb8-single.md"
    md.write_text("---\ntitle: T\ntags: [a, b]\ncategory: spec\n---\n\n# H\n\nBody of 6pfgb8 single.\n")

    assert index_markdown(md, corpus="6pfgb8-md", t3=HttpVectorClient(), collection_name=_COLLECTION) > 0

    (row,) = documents_by_file_path(str(md.resolve()))
    # What the row writer wrote: the constants. Its frontmatter ``tags`` / ``category`` are not carried
    # by the chunk rows, so they are not invented here.
    assert row.meta["tags"] == ["markdown"] and row.meta["category"] == "prose"
    assert row.meta["source_format"] == "markdown"


@pytest.fixture()
def fake_pdf(monkeypatch):
    ex = type("Extractor", (_FakeExtractor,), {"lines": [], "calls": []})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    return ex


@pytest.mark.parametrize("streaming", ["always", "never"], ids=["streaming", "all-at-once"])
def test_index_pdf_stamps_pdf_paper_and_the_derived_format(tmp_path: Path, fake_pdf, streaming) -> None:
    marker = f"6pfgb8-pdf-{streaming}"
    fake_pdf.lines = _lines(marker, 3)
    path = _write_pdf(tmp_path, marker)

    assert _index(path, marker, streaming=streaming) == 3

    (row,) = documents_by_file_path(str(path))
    assert row.meta["tags"] == ["pdf"] and row.meta["category"] == "paper"
    assert row.meta["source_format"] == "pdf"


def test_a_pdf_and_a_markdown_file_sharing_a_chunk_each_keep_their_own_source_format(tmp_path: Path, fake_pdf) -> None:
    """The failing-first pin of nexus-6pfgb.23: one docs collection, one chunk both documents hold. The row says
    one format; each catalog entry says its own."""
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    line = "6pfgb23 the one line a pdf page and a markdown file share"
    fake_pdf.lines = [line]
    pdf = _write_pdf(tmp_path, "6pfgb23-shared")
    md = tmp_path / "6pfgb23-shared.md"
    md.write_text(line + "\n")

    assert _index(pdf, "6pfgb23-shared", streaming="never") == 1
    assert index_markdown(md, corpus="z0o2p11-6pfgb23-shared", t3=HttpVectorClient(), collection_name=_COLLECTION) >= 1

    (pdf_row,) = documents_by_file_path(str(pdf))
    (md_row,) = documents_by_file_path(str(md.resolve()))
    reader = make_catalog_reader()
    held_by = {t: {m.chash for m in reader.get_manifest(t)} for t in (str(pdf_row.tumbler), str(md_row.tumbler))}
    assert set.intersection(*held_by.values()), f"the two documents do not share a chunk: {held_by}"
    assert pdf_row.meta["source_format"] == "pdf" and md_row.meta["source_format"] == "markdown"
    assert pdf_row.meta["tags"] == ["pdf"] and md_row.meta["tags"] == ["markdown"]
    assert pdf_row.meta["category"] == "paper" and md_row.meta["category"] == "prose"


def test_the_pre_flight_registration_carries_source_format_before_any_chunk_is_written(tmp_path: Path) -> None:
    """A document whose index run dies after the pre-flight still has its format on the catalog entry."""
    from nexus.doc_indexer import _register_or_lookup_doc_id

    pdf = _write_pdf(tmp_path, "6pfgb23-preflight")
    md = tmp_path / "6pfgb23-preflight.md"
    md.write_text("preflight\n")
    for path, ctype in ((pdf, "paper"), (md, "prose")):
        doc_id = _register_or_lookup_doc_id(path, "6pfgb23-preflight", content_type=ctype, physical_collection=_COLLECTION)
        assert doc_id
    (pdf_row,) = documents_by_file_path(str(pdf))
    (md_row,) = documents_by_file_path(str(md.resolve()))
    assert pdf_row.meta["source_format"] == "pdf" and md_row.meta["source_format"] == "markdown"
