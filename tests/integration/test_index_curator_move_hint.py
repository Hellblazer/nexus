# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P3-S2: the curator hint (Gap 1), through ``nx index pdf`` and ``nx index md``.

Real engine substrate (one tenant per test; engine-backed tests live in tests/integration, tests/AGENTS.md): a PDF or markdown file is indexed, moved, and indexed again from its
new path, which registers a second document (GH #1571). The registering run prints ONE line, the dry-run form of
``nx catalog owners --execute move --document <old tumbler> --new-path <new path>``, after its own output, and writes
nothing for it. Only the extractor and the chunker of the PDF path are faked (as in the RDR-223 journeys), so the file
hash, the catalog rows and the engine's by-meta route are the real ones.

The no-hint cases are each the one input that would print a line if the rule they pin were missing: a copy whose
original is still on disk, a moved and edited file (a different hash), an in-repo markdown file (a repo owner), an old
row with no ``meta.content_hash``, an alias row, an engine without the move flag, and the second run of an unchanged
file, which returns at the staleness check before any writer and so asks the metadata route nothing."""
from __future__ import annotations

import functools
import shlex
import shutil
import subprocess
from pathlib import Path

import httpx
import pymupdf
import pytest
from click.testing import CliRunner

from nexus import doc_indexer
from nexus.catalog.factory import make_catalog_reader
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.tumbler import Tumbler
from nexus.cli import main
from nexus.pipeline_stages import UploadRun
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_file_path
from tests.integration.test_rdr223_pdf_journey import _FakeChunker, _FakeExtractor

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_SEQ = [0]
_HINT = "nx catalog owners --execute move --document "


def _unique(prefix: str) -> str:
    _SEQ[0] += 1
    return f"{prefix}-{_SEQ[0]}"


@pytest.fixture(autouse=True)
def _fake_pdf_extraction(monkeypatch, t2_service_env):
    ex = type("Extractor", (_FakeExtractor,), {"lines": ["page one text of the hinted pdf", "page two text"], "calls": []})
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", ex)
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")


@functools.cache
def _openable_pdf(text: str = "hinted") -> bytes:
    """A one-page PDF pymupdf can open, as the same bytes every call: the default route (``--streaming auto``) streams
    only a PDF it can open, and falls back to the all-at-once route for one it cannot."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _pdf(root: Path, body: bytes | None = None, name: str = "x.pdf") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_openable_pdf() if body is None else body)
    return path.resolve()


def _md(root: Path, text: str = "# Heading\n\nBody of the hinted markdown, unique text one.\n\nSecond paragraph.\n",
        name: str = "n.md") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.resolve()


def _index(kind: str, path: Path, corpus: str, *flags: str, streaming: str | None = "never"):
    """``nx index pdf|md``. A PDF run forces the all-at-once route unless ``streaming`` names another mode, or is None
    (no flag: the command's default, ``auto``, which is the streaming pipeline)."""
    args = ["index", kind, str(path), "--corpus", corpus, *flags]
    if kind == "pdf" and streaming is not None:
        args += ["--streaming", streaming]
    return CliRunner().invoke(main, args)


def _hint_lines(result) -> list[str]:
    return [ln for ln in result.output.splitlines() if ln.startswith("nx catalog owners")]


def _tumbler(path: Path) -> str:
    (row,) = documents_by_file_path(str(path))
    return str(row.tumbler)


class _ByMetaSpy:
    """Counts the calls a run makes to ``HttpCatalogClient.documents_by_meta``."""

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        original = HttpCatalogClient.documents_by_meta

        def counted(client, *args, **kwargs):
            self.calls += 1
            return original(client, *args, **kwargs)

        monkeypatch.setattr(HttpCatalogClient, "documents_by_meta", counted)


def _moved(kind: str, tmp_path: Path, corpus: str, *, streaming: str | None = "never",
           between=None) -> tuple[Path, Path, str, object]:
    """Index a file under ``a/``, move it to ``b/``, index it again: ``(old, new, old tumbler, the second run)``.
    ``between`` is called after the first run and before the second (to start a spy that counts the second only)."""
    make = _pdf if kind == "pdf" else _md
    old = make(tmp_path / "a")
    first = _index(kind, old, corpus, streaming=streaming)
    assert first.exit_code == 0, first.output
    assert _hint_lines(first) == [], "the first registration of a file hints nothing"
    old_tumbler = _tumbler(old)
    new = tmp_path / "b" / old.name
    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    if between is not None:
        between()
    return old, new.resolve(), old_tumbler, _index(kind, new.resolve(), corpus, streaming=streaming)


# ── the hint ────────────────────────────────────────────────────────────────


def test_moved_pdf_prints_dry_run_hint_on_registering_run(tmp_path, monkeypatch):
    spies: list[_ByMetaSpy] = []
    _old, new, old_tumbler, second = _moved("pdf", tmp_path, _unique("hint-pdf"),
                                            between=lambda: spies.append(_ByMetaSpy(monkeypatch)))
    assert second.exit_code == 0, second.output
    assert spies[0].calls == 1, "the registering run asked the metadata route exactly once, for the one file it wrote"
    assert _hint_lines(second) == [f"{_HINT}{old_tumbler} --new-path {shlex.quote(str(new))}"]
    assert second.output.rstrip("\n").splitlines()[-1] == _hint_lines(second)[0], "the hint is the last line"
    assert "--no-dry-run" not in second.output and "--confirm" not in second.output

    # nothing was written for the hint: both rows are still live at their own paths, the new one a second document
    assert _tumbler(new) != old_tumbler

    # and the line is a command the CLI runs: its dry run reports the absorb and writes nothing
    argv = shlex.split(_hint_lines(second)[0])[1:]
    dry = CliRunner().invoke(main, argv)
    if "does not support catalog moves" in dry.output:
        pytest.skip("the substrate engine predates the move routes: rebuild the gate jar")
    assert dry.exit_code == 0, dry.output
    assert "(dry run)" in dry.output and "absorbed (trashed): 1" in dry.output
    assert _tumbler(new) != old_tumbler, "the dry run changed nothing"


@pytest.mark.parametrize("streaming", [None, "auto", "always"], ids=["default-flag", "auto", "always"])
def test_moved_pdf_on_the_streaming_pipeline_prints_the_hint_as_the_last_line(tmp_path, monkeypatch, streaming):
    """The command's DEFAULT route (``--streaming auto``) is the streaming pipeline, whose stamp site is
    ``UploadRun.open_writer`` on the uploader thread, not ``_write_chunks_with_owner_rows``. Every other PDF test in
    this file forces ``--streaming never``; this one runs the route a user gets, and shows the writer it stamps from."""
    opened: list[str] = []
    original = UploadRun.open_writer

    def spy(self, *args, **kwargs):
        opened.append(kwargs.get("doc_id", ""))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(UploadRun, "open_writer", spy)
    _old, new, old_tumbler, second = _moved("pdf", tmp_path, _unique("hint-stream"), streaming=streaming)
    assert opened, "non-vacuity: the run went through the streaming writer"
    assert second.exit_code == 0, second.output
    assert _hint_lines(second) == [f"{_HINT}{old_tumbler} --new-path {shlex.quote(str(new))}"]
    assert second.output.rstrip("\n").splitlines()[-1] == _hint_lines(second)[0], "the hint is the last line"


def test_moved_md_prints_dry_run_hint_on_registering_run(tmp_path):
    _old, new, old_tumbler, second = _moved("md", tmp_path, _unique("hint-md"))
    assert second.exit_code == 0, second.output
    assert _hint_lines(second) == [f"{_HINT}{old_tumbler} --new-path {shlex.quote(str(new))}"]
    assert second.output.rstrip("\n").splitlines()[-1] == _hint_lines(second)[0]


def test_moved_pdf_over_the_incremental_threshold_prints_the_hint(tmp_path, monkeypatch):
    """The residual the plan named (a large single-file writer that might not stamp): a PDF of more chunks than
    ``_INCREMENTAL_THRESHOLD`` takes the batched writer, which stamps through the same helper. The size-oversize writer
    (``oversize_write``) is reached only by the repo indexers, so no curator document skips the stamp."""
    lines =[f"page {i} of the large hinted pdf" for i in range(doc_indexer._INCREMENTAL_THRESHOLD + 12)]
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        extractor = type("Extractor", (_FakeExtractor,), {"lines": lines, "calls": []})
        monkeypatch.setattr(f"{mod}.PDFExtractor", extractor)
    _old, new, old_tumbler, second = _moved("pdf", tmp_path, _unique("hint-big"))
    assert second.exit_code == 0, second.output
    assert _hint_lines(second) == [f"{_HINT}{old_tumbler} --new-path {shlex.quote(str(new))}"]


def test_owner_documents_page_is_one_ordered_page_on_the_real_engine(tmp_path):
    """The one-page read the repository hints and the census flag share, against the real ``/list`` route: the first
    ``limit`` live documents of an owner in tumbler order, and all of them when the limit is larger."""
    corpus = _unique("hint-page")
    paths = [_pdf(tmp_path / "a", body=f"%PDF-1.4 page {i}\n%%EOF\n".encode(), name=f"p{i}.pdf") for i in range(3)]
    for path in paths:
        assert _index("pdf", path, corpus).exit_code == 0
    reader = make_catalog_reader()
    tumblers = [_tumbler(p) for p in paths]
    owner = str(Tumbler.parse(tumblers[0]).owner_address())
    assert [str(d.tumbler) for d in reader.owner_documents_page(owner, 2)] == sorted(tumblers)[:2]
    assert [str(d.tumbler) for d in reader.owner_documents_page(owner, 200)] == sorted(tumblers)


def test_second_run_of_unchanged_file_prints_none_and_makes_no_documents_by_meta_call(tmp_path, monkeypatch):
    corpus = _unique("hint-second")
    _old, new, _old_tumbler, second = _moved("pdf", tmp_path, corpus)
    assert len(_hint_lines(second)) == 1, "non-vacuity: the registering run did print the hint"

    spy = _ByMetaSpy(monkeypatch)
    third = _index("pdf", new, corpus)
    assert third.exit_code == 0, third.output
    assert _hint_lines(third) == []
    assert spy.calls == 0, "an unchanged file returns at the staleness check: no metadata query, not just no hint"


def test_byte_identical_copy_with_original_present_prints_none(tmp_path):
    corpus = _unique("hint-copy")
    original = _pdf(tmp_path / "a")
    assert _index("pdf", original, corpus).exit_code == 0
    copy = _pdf(tmp_path / "b")  # same bytes, the original is still there
    result = _index("pdf", copy, corpus)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == [], "a copy whose original exists is two documents, not a move"


def test_moved_and_edited_pdf_prints_none(tmp_path):
    corpus = _unique("hint-edited")
    old = _pdf(tmp_path / "a")
    assert _index("pdf", old, corpus).exit_code == 0
    old.unlink()
    edited = _pdf(tmp_path / "b", body=b"%PDF-1.4 hinted EDITED\n%%EOF\n")
    result = _index("pdf", edited, corpus)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == [], "a different hash is a different file"


def test_in_repo_markdown_prints_none(tmp_path):
    """The old row is a curator document whose file is gone; the same bytes indexed inside a git repository land under
    a repo owner, and the curator hint does not run for them."""
    corpus = _unique("hint-repo")
    old = _md(tmp_path / "a")
    assert _index("md", old, corpus).exit_code == 0
    old.unlink()
    outside = _md(tmp_path / "b")
    control = _index("md", outside, corpus)
    assert len(_hint_lines(control)) == 1, "non-vacuity: the same bytes at a curator path DO hint"
    outside.unlink()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    inside = _md(repo)
    result = _index("md", inside, corpus)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []


def test_old_row_without_content_hash_prints_none_and_no_error(tmp_path):
    corpus = _unique("hint-nohash")
    old = _pdf(tmp_path / "a")
    assert _index("pdf", old, corpus).exit_code == 0
    old.unlink()
    new = _pdf(tmp_path / "b")
    control = _index("pdf", new, corpus)
    assert len(_hint_lines(control)) == 1, "non-vacuity: with the old row's hash intact the registering run hints"

    ActiveCatalog().update(_tumbler(old), meta={"content_hash": "legacy-no-file-hash"})
    result = _index("pdf", new, corpus, "--force")
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == [], "the hint cannot see an old row that carries no matching meta.content_hash"


def test_alias_row_prints_none(tmp_path):
    """A merged duplicate stays live as an alias row with its meta; documents/move refuses an alias as the document to
    move, so the hint must not name it."""
    corpus = _unique("hint-alias")
    old = _pdf(tmp_path / "a")
    assert _index("pdf", old, corpus).exit_code == 0
    other = _pdf(tmp_path / "other", body=b"%PDF-1.4 the canonical one\n%%EOF\n", name="y.pdf")
    assert _index("pdf", other, corpus).exit_code == 0
    old.unlink()
    new = _pdf(tmp_path / "b")
    control = _index("pdf", new, corpus)
    assert len(_hint_lines(control)) == 1, "non-vacuity: before the old row is an alias the registering run hints"

    ActiveCatalog().update(_tumbler(old), alias_of=_tumbler(other))
    result = _index("pdf", new, corpus, "--force")
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []


def test_old_engine_prints_none(tmp_path, monkeypatch):
    corpus = _unique("hint-oldengine")
    old = _pdf(tmp_path / "a")
    assert _index("pdf", old, corpus).exit_code == 0
    old.unlink()
    new = _pdf(tmp_path / "b")
    monkeypatch.setattr(HttpCatalogClient, "supports_catalog_move", lambda self: False)
    spy = _ByMetaSpy(monkeypatch)
    result = _index("pdf", new, corpus)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == [] and spy.calls == 0, "a client never prints a command its engine refuses"


def test_failing_metadata_query_leaves_exit_code_unchanged(tmp_path, monkeypatch):
    corpus = _unique("hint-fail")
    old = _pdf(tmp_path / "a")
    assert _index("pdf", old, corpus).exit_code == 0
    old.unlink()
    new = _pdf(tmp_path / "b")

    reached: list[int] = []

    def timeout(self, **kwargs):
        reached.append(1)
        raise httpx.ReadTimeout("by-meta timed out")

    monkeypatch.setattr(HttpCatalogClient, "documents_by_meta", timeout)
    monkeypatch.setattr(HttpCatalogClient, "supports_catalog_move", lambda self: True)
    result = _index("pdf", new, corpus)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []
    assert len(reached) == 1, "non-vacuity: the patched metadata query was reached, once"
    assert _tumbler(new), "the run registered and indexed the file as it always does"
