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

import shlex
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from nexus import doc_indexer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
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


def _pdf(root: Path, body: bytes = b"%PDF-1.4 hinted\n%%EOF\n", name: str = "x.pdf") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path.resolve()


def _md(root: Path, text: str = "# Heading\n\nBody of the hinted markdown, unique text one.\n\nSecond paragraph.\n",
        name: str = "n.md") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.resolve()


def _index(kind: str, path: Path, corpus: str, *flags: str):
    args = ["index", kind, str(path), "--corpus", corpus, *flags]
    if kind == "pdf":
        args += ["--streaming", "never"]
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


def _moved(kind: str, tmp_path: Path, corpus: str) -> tuple[Path, Path, str, object]:
    """Index a file under ``a/``, move it to ``b/``, index it again: ``(old, new, old tumbler, the second run)``."""
    make = _pdf if kind == "pdf" else _md
    old = make(tmp_path / "a")
    first = _index(kind, old, corpus)
    assert first.exit_code == 0, first.output
    assert _hint_lines(first) == [], "the first registration of a file hints nothing"
    old_tumbler = _tumbler(old)
    new = tmp_path / "b" / old.name
    new.parent.mkdir(parents=True)
    shutil.move(str(old), str(new))
    return old, new.resolve(), old_tumbler, _index(kind, new.resolve(), corpus)


# ── the hint ────────────────────────────────────────────────────────────────


def test_moved_pdf_prints_dry_run_hint_on_registering_run(tmp_path):
    _old, new, old_tumbler, second = _moved("pdf", tmp_path, _unique("hint-pdf"))
    assert second.exit_code == 0, second.output
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

    def timeout(self, **kwargs):
        raise httpx.ReadTimeout("by-meta timed out")

    monkeypatch.setattr(HttpCatalogClient, "documents_by_meta", timeout)
    monkeypatch.setattr(HttpCatalogClient, "supports_catalog_move", lambda self: True)
    result = _index("pdf", new, corpus)
    assert result.exit_code == 0, result.output
    assert _hint_lines(result) == []
    assert _tumbler(new), "the run registered and indexed the file as it always does"
