# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ax49 (Sam's ruling, 2026-11-01): a re-index fills a document's catalog ``tags`` and ``category`` only
when they are empty. Absent, ``None``, ``''`` and ``[]`` get the indexer's derived value; a non-empty value
already on the document is kept, never overwritten.

Before the ruling every register-time stamp rewrote a differing value (the repo pass's ``stamp_stale`` compare,
the markdown hook, the PDF hook on its update and resolved legs). Real engine, the same seams
``test_6pfgb8_23_register_stamps.py`` drives. ``source_format`` is NOT part of the ruling: it is derived from the
document's own content type and path, never curated, and stays an overwrite (pinned below).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import indexer
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_file_path
from tests.integration.test_6pfgb8_23_register_stamps import _git_identity as _git_identity  # noqa: F401 - autouse fixture
from tests.integration.test_rdr223_pdf_journey import _COLLECTION

pytestmark = [pytest.mark.integration]

REPO_HASH = "3ax49f01"
CURATED = {"tags": ["curated-tag"], "category": "curated-category"}
#: the ways a key can be empty on a stored document; "absent" is the key never having been written
EMPTIES = [
    pytest.param("absent", id="absent"),
    pytest.param(None, id="none"),
    pytest.param("", id="empty-string"),
    pytest.param([], id="empty-list"),
]


def _held(entry) -> dict:
    return dict(entry.meta)


def _curate(tumbler, **meta) -> None:
    ActiveCatalog().update(tumbler, meta=meta)


def _set_empty(tumbler, empty) -> None:
    """Make tags and category empty in the stored meta, in the given way ("absent" leaves them unwritten)."""
    if empty != "absent":
        _curate(tumbler, tags=empty, category=empty)


# ── the repo catalog pass ─────────────────────────────────────────────────────────────────────────────


def _repo(tmp_path: Path) -> list[tuple[Path, str, str]]:
    files = []
    for rel, ctype, text, col in (
        ("src/app.py", "code", "def f():\n    return 1\n", "code__3ax49"),
        ("docs/guide.md", "prose", "# Guide\n\nA guide.\n", "docs__3ax49"),
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        files.append((path, ctype, col))
    return files


def _hook(root: Path, files, head: str) -> None:
    indexer._catalog_hook(repo=root, repo_name="r3ax49", repo_hash=REPO_HASH, head_hash=head, indexed_files=files)


def _entry(rel: str):
    cat = ActiveCatalog()
    entry = cat.by_file_path(cat.owner_for_repo(REPO_HASH), rel)
    assert entry is not None, rel
    return entry


def test_the_repo_pass_keeps_a_curated_tags_and_category_across_a_re_index(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    _hook(tmp_path, files, "aaa")
    for rel in ("src/app.py", "docs/guide.md"):
        _curate(_entry(rel).tumbler, **CURATED)

    _hook(tmp_path, files, "bbb")  # a new HEAD: every document is rewritten, so a stamp that overwrote would show

    for rel in ("src/app.py", "docs/guide.md"):
        meta = _held(_entry(rel))
        assert meta["tags"] == CURATED["tags"] and meta["category"] == CURATED["category"], (rel, meta)
        assert meta["content_hash"], "non-vacuity: the re-index did write this document's meta"
    assert _entry("src/app.py").head_hash == "bbb", "non-vacuity: the second run did update the document"


def test_the_repo_pass_does_not_rewrite_a_document_only_because_its_curated_value_differs(tmp_path: Path) -> None:
    from nexus.catalog import factory

    files = _repo(tmp_path)
    _hook(tmp_path, files, "aaa")
    for rel in ("src/app.py", "docs/guide.md"):
        _curate(_entry(rel).tumbler, **CURATED)

    writes: list[str] = []
    real = factory.make_catalog_writer

    class _Rec:
        def __init__(self, inner) -> None:
            self._inner = inner

        def update_many(self, updates):
            writes.append("update_many")
            return self._inner.update_many(updates)

        def update(self, *a, **k):
            writes.append("update")
            return self._inner.update(*a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(factory, "make_catalog_writer", lambda *a, **k: _Rec(real(*a, **k)))
        _hook(tmp_path, files, "aaa")  # nothing changed but the curated values differ from the derived ones

    assert writes == [], "a curated value is not a stale stamp: a warm run writes nothing"


@pytest.mark.parametrize("empty", EMPTIES)
def test_the_repo_pass_fills_an_empty_tags_and_category(tmp_path: Path, empty) -> None:
    files = _repo(tmp_path)
    _hook(tmp_path, files, "aaa")
    for rel in ("src/app.py", "docs/guide.md"):
        _set_empty(_entry(rel).tumbler, empty)

    _hook(tmp_path, files, "aaa")  # HEAD and content unchanged: only the empty stamp can move the document

    meta = _held(_entry("src/app.py"))
    assert meta["tags"] == ["py"] and meta["category"] == "code", meta
    meta = _held(_entry("docs/guide.md"))
    assert meta["tags"] == ["markdown"] and meta["category"] == "prose", meta


def test_the_repo_pass_fills_the_empty_key_and_keeps_the_held_one(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    _hook(tmp_path, files, "aaa")
    tumbler = _entry("src/app.py").tumbler
    _curate(tumbler, tags=["mine"], category="")

    _hook(tmp_path, files, "bbb")

    meta = _held(_entry("src/app.py"))
    assert meta["tags"] == ["mine"] and meta["category"] == "code", meta


def test_the_repo_pass_still_overwrites_source_format(tmp_path: Path) -> None:
    """source_format is derived from the document's own columns, never curated: outside the ruling."""
    files = _repo(tmp_path)
    _hook(tmp_path, files, "aaa")
    _curate(_entry("src/app.py").tumbler, source_format="bogus")

    _hook(tmp_path, files, "aaa")

    assert _held(_entry("src/app.py"))["source_format"] == "code"


# ── the markdown hook ─────────────────────────────────────────────────────────────────────────────────


def _register_row(corpus: str, path: Path, content_type: str):
    cat = ActiveCatalog()
    owner = cat.register_owner(corpus, "curator")
    return cat.register(owner, path.stem, content_type=content_type, file_path=str(path), physical_collection=_COLLECTION)


def _only_row(path: Path):
    (row,) = documents_by_file_path(str(path.resolve()))
    return row


@pytest.mark.parametrize("resolved", [False, True], ids=["update-branch", "resolved-onto-existing-branch"])
def test_the_markdown_hook_keeps_curated_tags_and_category(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved: bool) -> None:
    from nexus.doc_indexer import _catalog_markdown_hook

    corpus = f"3ax49-md-keep-{'resolved' if resolved else 'update'}"
    md = tmp_path / "keep.md"
    md.write_text("# keep\n\nbody\n")
    tumbler = _register_row(corpus, md.resolve(), "prose")
    _curate(tumbler, **CURATED)
    if resolved:
        monkeypatch.setattr("nexus.doc_indexer._find_curator_document", lambda *a, **k: None)
        monkeypatch.setattr("nexus.doc_indexer._repo_owner_document_for", lambda *a, **k: None)

    _catalog_markdown_hook(md, _COLLECTION, "prose", corpus, 3)

    meta = _held(_only_row(md))
    assert meta["tags"] == CURATED["tags"] and meta["category"] == CURATED["category"], meta
    assert meta["extraction_source"] == "file" and meta["source_format"] == "markdown", f"non-vacuity: the hook wrote {meta}"


def test_the_markdown_hook_keeps_curated_tags_and_category_when_the_resolved_row_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved-onto-existing leg reads the resolved row to know what it holds. When that read raises, the
    hook has no held meta to apply the fill-only rule to, so the update carries no tags and no category at all
    (the PDF hook's fallback does the same): a curated value must survive, and the rest of the stamp is written."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.doc_indexer import _catalog_markdown_hook

    corpus = "3ax49-md-keep-resolve-raises"
    md = tmp_path / "keep-unread.md"
    md.write_text("# keep\n\nbody\n")
    tumbler = _register_row(corpus, md.resolve(), "prose")
    _curate(tumbler, **CURATED)
    monkeypatch.setattr("nexus.doc_indexer._find_curator_document", lambda *a, **k: None)
    monkeypatch.setattr("nexus.doc_indexer._repo_owner_document_for", lambda *a, **k: None)
    reads: list[str] = []
    real_resolve = HttpCatalogClient.resolve

    def _resolve_raises(self, *a, **k):
        reads.append("resolve")
        raise RuntimeError("catalog read refused")

    monkeypatch.setattr(HttpCatalogClient, "resolve", _resolve_raises)

    _catalog_markdown_hook(md, _COLLECTION, "prose", corpus, 3)

    monkeypatch.setattr(HttpCatalogClient, "resolve", real_resolve)
    meta = _held(_only_row(md))
    assert reads, "non-vacuity: the resolved leg did try to read the resolved row"
    assert meta["tags"] == CURATED["tags"] and meta["category"] == CURATED["category"], meta
    assert meta["extraction_source"] == "file" and meta["source_format"] == "markdown", f"non-vacuity: the hook wrote {meta}"


@pytest.mark.parametrize("resolved", [False, True], ids=["update-branch", "resolved-onto-existing-branch"])
@pytest.mark.parametrize("empty", EMPTIES)
def test_the_markdown_hook_fills_empty_tags_and_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved: bool, empty,
) -> None:
    from nexus.doc_indexer import _catalog_markdown_hook

    corpus = f"3ax49-md-fill-{'resolved' if resolved else 'update'}"
    md = tmp_path / f"fill-{'none' if empty is None else repr(empty).strip(chr(39))}.md"
    md.write_text("# fill\n\nbody\n")
    tumbler = _register_row(corpus, md.resolve(), "prose")
    _set_empty(tumbler, empty)
    if resolved:
        monkeypatch.setattr("nexus.doc_indexer._find_curator_document", lambda *a, **k: None)
        monkeypatch.setattr("nexus.doc_indexer._repo_owner_document_for", lambda *a, **k: None)

    _catalog_markdown_hook(md, _COLLECTION, "prose", corpus, 3)

    meta = _held(_only_row(md))
    assert meta["tags"] == ["markdown"] and meta["category"] == "prose", meta


def test_index_markdown_re_index_keeps_curated_tags_and_category(tmp_path: Path) -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_markdown

    md = tmp_path / "3ax49-reindex.md"
    md.write_text("# H\n\nBody of 3ax49 reindex.\n")
    assert index_markdown(md, corpus="3ax49-reindex", t3=HttpVectorClient(), collection_name=_COLLECTION) > 0
    first = _only_row(md)
    assert _held(first)["tags"] == ["markdown"], "non-vacuity: the first index stamped the derived tags"
    _curate(first.tumbler, **CURATED)

    index_markdown(md, corpus="3ax49-reindex", t3=HttpVectorClient(), collection_name=_COLLECTION, force=True)

    meta = _held(_only_row(md))
    assert meta["tags"] == CURATED["tags"] and meta["category"] == CURATED["category"], meta


# ── the PDF hook ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("resolved", [False, True], ids=["update-branch", "resolved-onto-existing-branch"])
@pytest.mark.parametrize("content_type", ["paper", "pdf"])
def test_the_pdf_hook_keeps_curated_tags_and_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved: bool, content_type: str,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.pipeline_stages import _catalog_pdf_hook

    corpus = f"3ax49-pdf-keep-{content_type}-{'resolved' if resolved else 'update'}"
    pdf = tmp_path / "keep.pdf"
    pdf.write_bytes(b"%PDF-1.4 3ax49 keep\n")
    tumbler = _register_row(corpus, pdf.resolve(), content_type)
    _curate(tumbler, **CURATED)
    if resolved:
        monkeypatch.setattr(HttpCatalogClient, "by_file_path", lambda self, *a, **k: None)

    _catalog_pdf_hook(pdf, _COLLECTION, title="Keep", corpus=corpus, chunk_count=1)

    row = _only_row(pdf)
    meta = _held(row)
    assert meta["tags"] == CURATED["tags"] and meta["category"] == CURATED["category"], meta
    assert meta["source_format"] == "pdf", f"non-vacuity: the hook wrote this update ({meta})"


@pytest.mark.parametrize("resolved", [False, True], ids=["update-branch", "resolved-onto-existing-branch"])
@pytest.mark.parametrize("empty", EMPTIES)
def test_the_pdf_hook_fills_empty_tags_and_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resolved: bool, empty,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.pipeline_stages import _catalog_pdf_hook

    corpus = f"3ax49-pdf-fill-{'resolved' if resolved else 'update'}"
    pdf = tmp_path / f"fill-{'none' if empty is None else repr(empty).strip(chr(39))}.pdf"
    pdf.write_bytes(b"%PDF-1.4 3ax49 fill\n")
    tumbler = _register_row(corpus, pdf.resolve(), "paper")
    _set_empty(tumbler, empty)
    if resolved:
        monkeypatch.setattr(HttpCatalogClient, "by_file_path", lambda self, *a, **k: None)

    _catalog_pdf_hook(pdf, _COLLECTION, title="Fill", corpus=corpus, chunk_count=1)

    meta = _held(_only_row(pdf))
    assert meta["tags"] == ["pdf"] and meta["category"] == "paper", meta
