# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.8 (RDR-236 Phase 1): an indexed document's ``tags`` and ``category`` live in its catalog ``meta``.

Every indexer writes them as constants of the file's kind: ``tags`` the file extension (``markdown`` for a
markdown file, ``pdf`` for a PDF), ``category`` ``code`` / ``prose`` / ``paper``. On a chunk row they are
the last writer's when documents share a chunk (a ``.py`` file and a ``.md`` file holding the same text),
so the row cannot say which document is which. The document's meta can. ``tags`` is a JSON ARRAY, the
shape the notes' tags take (nexus-6pfgb.38), so one display reads both; ``category`` is a string.

Only what the row writer wrote is stamped. A file with no extension has no tag (the row writer wrote an
empty string, and an empty value is never sent: the engine merges ``meta`` and an explicit empty
overwrites). The repo PDF path says category ``prose`` where ``nx index pdf`` says ``paper``; that is
RECORDED here, not fixed.

Real engine: the repo catalog pass (``_catalog_hook``) for code, prose, rdr and repo PDFs, and the
single-file entry points (``index_markdown``, ``index_pdf``).
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


EXPECTED = {
    "src/app.py": (["py"], "code"),
    "src/UPPER.PY": (["py"], "code"),            # the row writer lower-cases the suffix
    "docs/guide.md": (["markdown"], "prose"),
    "docs/notes.txt": (["txt"], "prose"),
    "docs/rdr/rdr-9001-x.md": (["markdown"], "prose"),   # rdr markdown goes through the prose indexer
    "papers/p.pdf": (["pdf"], "prose"),          # the repo PDF path: "prose", where nx index pdf says "paper"
}


def test_the_repo_catalog_pass_stamps_each_document_with_its_own_tags_and_category(tmp_path: Path) -> None:
    files = _write_repo(tmp_path)
    _hook(tmp_path, files)
    cat = ActiveCatalog()
    for rel, (tags, category) in EXPECTED.items():
        meta = _meta(cat, rel)
        assert meta.get("tags") == tags, (rel, meta)
        assert meta.get("category") == category, (rel, meta)
    make = _meta(cat, "Makefile")
    assert make.get("category") == "code"
    assert "tags" not in make, "no extension, no tag: an empty value is never sent"


def test_two_files_sharing_a_chunk_each_keep_their_own_value(tmp_path: Path) -> None:
    """The failing-first pin: a ``.py`` and a ``.md`` with the SAME text share a chunk (one content hash),
    so a row can hold only one of ``py`` / ``markdown``. Each document's meta holds its own."""
    shared = "# shared 6pfgb8 body\n"
    (tmp_path / "a.py").write_text(shared)
    (tmp_path / "b.md").write_text(shared)
    _hook(tmp_path, [(tmp_path / "a.py", "code", COLLECTIONS["code"]), (tmp_path / "b.md", "prose", COLLECTIONS["prose"])])
    cat = ActiveCatalog()
    assert _meta(cat, "a.py")["tags"] == ["py"] and _meta(cat, "a.py")["category"] == "code"
    assert _meta(cat, "b.md")["tags"] == ["markdown"] and _meta(cat, "b.md")["category"] == "prose"


class _RecordingWriter:
    """The real catalog writer, with the document updates it is asked for recorded."""

    def __init__(self, inner, log: list[tuple[str, int]]) -> None:
        self._inner, self._log = inner, log

    def update_many(self, updates):
        self._log.append(("update_many", len(updates)))
        return self._inner.update_many(updates)

    def update(self, tumbler, **kw):
        self._log.append(("update", 1))
        return self._inner.update(tumbler, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_registration_that_predates_the_stamp_is_filled_once_and_a_warm_run_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _write_repo(tmp_path)
    with monkeypatch.context() as m:
        m.setattr(indexer, "document_tags_category", lambda *a, **k: {})  # the registration as the previous client made it
        _hook(tmp_path, files)
    cat = ActiveCatalog()
    assert "tags" not in _meta(cat, "src/app.py") and "category" not in _meta(cat, "src/app.py")

    _hook(tmp_path, files)  # content and HEAD unchanged: only the missing stamp can move the document
    for rel, (tags, category) in EXPECTED.items():
        assert _meta(cat, rel)["tags"] == tags and _meta(cat, rel)["category"] == category

    writes: list[tuple[str, int]] = []
    real_make = factory.make_catalog_writer
    with monkeypatch.context() as m:
        m.setattr(factory, "make_catalog_writer", lambda *a, **k: _RecordingWriter(real_make(*a, **k), writes))
        _hook(tmp_path, files)
    assert writes == [], "a stamped, unchanged document is not rewritten on a warm run"


# ── the single-file entry points ──────────────────────────────────────────────────────────────


def test_index_markdown_stamps_markdown_and_prose(tmp_path: Path) -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    md = tmp_path / "6pfgb8-single.md"
    md.write_text("---\ntitle: T\ntags: [a, b]\ncategory: spec\n---\n\n# H\n\nBody of 6pfgb8 single.\n")

    assert index_markdown(md, corpus="6pfgb8-md", t3=HttpVectorClient(), collection_name=_COLLECTION) > 0

    (row,) = documents_by_file_path(str(md.resolve()))
    # What the row writer wrote: the constants. Its frontmatter ``tags`` / ``category`` are not carried
    # by the chunk rows, so they are not invented here.
    assert row.meta["tags"] == ["markdown"] and row.meta["category"] == "prose"


@pytest.fixture()
def fake_pdf(monkeypatch):
    ex = type("Extractor", (_FakeExtractor,), {"lines": [], "calls": []})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    return ex


@pytest.mark.parametrize("streaming", ["always", "never"], ids=["streaming", "all-at-once"])
def test_index_pdf_stamps_pdf_and_paper(tmp_path: Path, fake_pdf, streaming) -> None:
    marker = f"6pfgb8-pdf-{streaming}"
    fake_pdf.lines = _lines(marker, 3)
    path = _write_pdf(tmp_path, marker)

    assert _index(path, marker, streaming=streaming) == 3

    (row,) = documents_by_file_path(str(path))
    assert row.meta["tags"] == ["pdf"] and row.meta["category"] == "paper"
