# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.8 (nexus-p2m1x.13), piece A: the four writers that call
``t3_collection_name(..., for_write=True)`` with no ``t3`` (``nx index md``,
``nx index pdf`` single and ``--dir``, ``nx dt index``) find a knowledge subject
by its catalog row too.

They pass ``subject_finder=catalog_subject_finder()``: a callable that reads the
registry row only, for the process-default tenant, and never probes T3 names, so
the nexus-hmxi rule (indexers do not grandfather onto legacy names) is untouched.
A ``t3=None`` call with no finder stays pure: it makes no catalog read.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import nexus.catalog.factory as factory
import nexus.corpus as corpus
from nexus.catalog.collection_name import CollectionAttributes
from nexus.cli import main
from nexus.commands.dt import _resolve_dt_collection
from nexus.corpus import CollectionRegistryUnreadableError, t3_collection_name

pytestmark = pytest.mark.usefixtures("cloud_mode")

_MODEL = "voyage-context-3"
_RENDERED = f"knowledge__field-notes__{_MODEL}__v1"
_MD_RESULT = {"chunks": 2, "sections": 0}
_PDF_RESULT = {"chunks": 3, "pages": [1], "title": "T", "author": "A"}


def _attrs(label: str, *, subject: str = "field-notes") -> CollectionAttributes:
    return CollectionAttributes(
        collection_id=None, label=label, content_type="knowledge", owner_id=subject,
        embedding_model=_MODEL, model_version=1, dimension=1024, lifecycle_state=None,
    )


def _finder_returning(row: CollectionAttributes | None) -> MagicMock:
    return MagicMock(name="finder", return_value=row)


def _install_finder(monkeypatch: pytest.MonkeyPatch, finder) -> None:
    """Replace the factory the writers call, so the real catalog reader is never built."""
    monkeypatch.setattr(corpus, "catalog_subject_finder", lambda: finder)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_md(home: Path) -> Path:
    p = home / "doc.md"
    p.write_text("# Hello\n\nWorld.\n")
    return p


@pytest.fixture
def fake_pdf(home: Path) -> Path:
    p = home / "doc.pdf"
    p.write_bytes(b"fake pdf")
    return p


# -- nx index md --------------------------------------------------------------

def test_index_md_writes_to_the_label_of_the_subjects_row(runner, fake_md, monkeypatch) -> None:
    finder = _finder_returning(_attrs("notes-from-2024"))
    _install_finder(monkeypatch, finder)
    with patch("nexus.doc_indexer.index_markdown", return_value=_MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--collection", "field-notes"])
    assert result.exit_code == 0, result.output
    assert m.call_args.kwargs["collection_name"] == "notes-from-2024"
    finder.assert_called_once_with("knowledge", "field-notes", _MODEL)


def test_index_md_with_no_row_renders_the_name_as_before(runner, fake_md, monkeypatch) -> None:
    finder = _finder_returning(None)
    _install_finder(monkeypatch, finder)
    with patch("nexus.doc_indexer.index_markdown", return_value=_MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--collection", "field-notes"])
    assert result.exit_code == 0, result.output
    assert m.call_args.kwargs["collection_name"] == _RENDERED
    finder.assert_called_once_with("knowledge", "field-notes", _MODEL)


def test_index_md_fails_loudly_when_the_registry_cannot_be_read(runner, fake_md, monkeypatch) -> None:
    finder = MagicMock(name="finder", side_effect=RuntimeError("engine down"))
    _install_finder(monkeypatch, finder)
    with patch("nexus.doc_indexer.index_markdown", return_value=_MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--collection", "field-notes"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), "a click error, not a traceback"
    assert "cannot read the catalog row for knowledge subject 'field-notes'" in result.output
    assert not m.called, "a write must not proceed under the rendered name"


def test_index_md_without_collection_reads_no_row(runner, fake_md, monkeypatch) -> None:
    finder = _finder_returning(_attrs("notes-from-2024"))
    _install_finder(monkeypatch, finder)
    with patch("nexus.doc_indexer.index_markdown", return_value=_MD_RESULT) as m:
        result = runner.invoke(main, ["index", "md", str(fake_md), "--corpus", "myproject"])
    assert result.exit_code == 0, result.output
    assert m.call_args.kwargs.get("collection_name") is None
    finder.assert_not_called()


# -- nx index pdf (single file and --dir) ------------------------------------

def test_index_pdf_writes_to_the_label_of_the_subjects_row(runner, fake_pdf, monkeypatch) -> None:
    _install_finder(monkeypatch, _finder_returning(_attrs("notes-from-2024")))
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_RESULT) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--collection", "field-notes"])
    assert result.exit_code == 0, result.output
    assert m.call_args.kwargs["collection_name"] == "notes-from-2024"


def test_index_pdf_with_no_row_renders_the_name_as_before(runner, fake_pdf, monkeypatch) -> None:
    _install_finder(monkeypatch, _finder_returning(None))
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_RESULT) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--collection", "field-notes"])
    assert result.exit_code == 0, result.output
    assert m.call_args.kwargs["collection_name"] == _RENDERED


def test_index_pdf_dry_run_reads_no_row(runner, fake_pdf, monkeypatch) -> None:
    """A dry run writes nothing, so it must not need a readable registry."""
    finder = MagicMock(side_effect=RuntimeError("engine down"))
    _install_finder(monkeypatch, finder)
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_RESULT):
        runner.invoke(main, ["index", "pdf", str(fake_pdf), "--collection", "field-notes", "--dry-run"])
    finder.assert_not_called()


def test_index_pdf_fails_loudly_when_the_registry_cannot_be_read(runner, fake_pdf, monkeypatch) -> None:
    _install_finder(monkeypatch, MagicMock(side_effect=RuntimeError("engine down")))
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_RESULT) as m:
        result = runner.invoke(main, ["index", "pdf", str(fake_pdf), "--collection", "field-notes"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), "a click error, not a traceback"
    assert "cannot read the catalog row for knowledge subject 'field-notes'" in result.output
    assert not m.called


def test_index_pdf_dir_writes_to_the_label_of_the_subjects_row(runner, home, monkeypatch) -> None:
    d = home / "pdfs"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"fake pdf")
    _install_finder(monkeypatch, _finder_returning(_attrs("notes-from-2024")))
    with patch("nexus.doc_indexer.index_pdf", return_value=2) as m:  # the batch path takes a chunk count
        result = runner.invoke(main, ["index", "pdf", "--dir", str(d), "--collection", "field-notes"])
    assert result.exit_code == 0, result.output
    assert m.call_args.kwargs["collection_name"] == "notes-from-2024"


def test_index_pdf_dir_fails_loudly_when_the_registry_cannot_be_read(runner, home, monkeypatch) -> None:
    d = home / "pdfs"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"fake pdf")
    _install_finder(monkeypatch, MagicMock(side_effect=RuntimeError("engine down")))
    with patch("nexus.doc_indexer.index_pdf", return_value=_PDF_RESULT) as m:
        result = runner.invoke(main, ["index", "pdf", "--dir", str(d), "--collection", "field-notes"])
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), "a click error, not a traceback"
    assert "cannot read the catalog row for knowledge subject 'field-notes'" in result.output
    assert not m.called


# -- nx dt index (the resolver the command calls) ----------------------------

def test_dt_collection_override_writes_to_the_label_of_the_subjects_row(monkeypatch) -> None:
    _install_finder(monkeypatch, _finder_returning(_attrs("notes-from-2024")))
    assert _resolve_dt_collection("field-notes", "dt", ".pdf") == "notes-from-2024"


def test_dt_collection_override_with_no_row_renders_the_name_as_before(monkeypatch) -> None:
    _install_finder(monkeypatch, _finder_returning(None))
    assert _resolve_dt_collection("field-notes", "dt", ".pdf") == _RENDERED


def test_dt_collection_override_refuses_an_unreadable_registry(monkeypatch) -> None:
    _install_finder(monkeypatch, MagicMock(side_effect=RuntimeError("engine down")))
    with pytest.raises(CollectionRegistryUnreadableError):
        _resolve_dt_collection("field-notes", "dt", ".pdf")


# -- t3_collection_name's subject_finder -------------------------------------

def test_a_t3_less_call_with_a_finder_answers_the_row_label() -> None:
    finder = _finder_returning(_attrs("notes-from-2024"))
    assert t3_collection_name("field-notes", for_write=True, subject_finder=finder) == "notes-from-2024"
    finder.assert_called_once_with("knowledge", "field-notes", _MODEL)


def test_the_finder_serves_a_read_too_and_a_failed_read_falls_back() -> None:
    assert t3_collection_name(
        "field-notes", subject_finder=_finder_returning(_attrs("notes-from-2024")),
    ) == "notes-from-2024"
    assert t3_collection_name(
        "field-notes", subject_finder=MagicMock(side_effect=RuntimeError("down")),
    ) == _RENDERED


def test_the_finder_underscored_subject_is_looked_up_by_its_owner_segment() -> None:
    finder = _finder_returning(None)
    t3_collection_name("field_notes", for_write=True, subject_finder=finder)
    finder.assert_called_once_with("knowledge", "field-notes", _MODEL)


def test_a_code_or_docs_target_is_not_looked_up_by_the_finder() -> None:
    finder = _finder_returning(_attrs("x"))
    t3_collection_name("code__field-notes", for_write=True, subject_finder=finder)
    t3_collection_name("docs__field-notes", for_write=True, subject_finder=finder)
    finder.assert_not_called()


def test_a_given_t3_uses_its_own_find_collection_not_the_finder() -> None:
    class _T3:
        def find_collection(self, ct, owner, model):
            return _attrs("from-the-store")

        def collection_exists(self, name: str) -> bool:
            return False

    finder = _finder_returning(_attrs("from-the-finder"))
    assert t3_collection_name("field-notes", t3=_T3(), subject_finder=finder) == "from-the-store"
    finder.assert_not_called()


def test_a_t3_less_call_with_no_finder_makes_no_catalog_read(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("a t3-less call with no finder must not read the catalog")

    monkeypatch.setattr(factory, "make_catalog_reader", boom)
    assert t3_collection_name("field-notes", for_write=True) == _RENDERED
    assert t3_collection_name("field-notes") == _RENDERED
    assert t3_collection_name("knowledge__field-notes", for_write=True) == _RENDERED


# -- catalog_subject_finder --------------------------------------------------

def test_the_factory_builds_no_reader_until_the_finder_is_called(monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("constructing the finder must not read the catalog")

    monkeypatch.setattr(factory, "make_catalog_reader", boom)
    assert callable(corpus.catalog_subject_finder())


def test_the_finder_reads_the_row_through_the_catalog_reader_and_closes_it(monkeypatch) -> None:
    row = _attrs("notes-from-2024")
    reader = MagicMock()
    reader.find_collection.return_value = row
    monkeypatch.setattr(factory, "make_catalog_reader", lambda: reader)
    assert corpus.catalog_subject_finder()("knowledge", "field-notes", _MODEL) is row
    reader.find_collection.assert_called_once_with("knowledge", "field-notes", _MODEL)
    reader.close.assert_called_once_with()


def test_the_finder_closes_the_reader_when_the_read_raises(monkeypatch) -> None:
    reader = MagicMock()
    reader.find_collection.side_effect = RuntimeError("engine down")
    monkeypatch.setattr(factory, "make_catalog_reader", lambda: reader)
    with pytest.raises(RuntimeError, match="engine down"):
        corpus.catalog_subject_finder()("knowledge", "field-notes", _MODEL)
    reader.close.assert_called_once_with()


def test_the_finder_calls_only_find_collection_and_close_on_the_reader(monkeypatch) -> None:
    """The finder touches the catalog reader only through find_collection and close; with
    no t3 there is nothing for it to probe (nexus-hmxi's no-grandfathering rule stands)."""
    reader = MagicMock(spec=["find_collection", "close"])
    reader.find_collection.return_value = None
    monkeypatch.setattr(factory, "make_catalog_reader", lambda: reader)
    assert t3_collection_name(
        "field-notes", for_write=True, subject_finder=corpus.catalog_subject_finder(),
    ) == _RENDERED
    assert [c[0] for c in reader.method_calls] == ["find_collection", "close"]
