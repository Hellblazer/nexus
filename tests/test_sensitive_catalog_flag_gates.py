# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 fix round 1 (nexus-rfvyy.6.19 / .6.20 ship-blocker): the index paths key the external
legs on the CATALOG flag, not on the verb's ``--sensitive`` option.

A document flagged earlier (``nx catalog sensitive``, or an earlier ``--sensitive`` run) and re-indexed
WITHOUT ``--sensitive`` must still: skip the MinerU server leg (local only, loudly), refuse ``--enrich``
(no title to Semantic Scholar), and refuse a write into a Voyage collection without
``--ack-external-embedder``. A catalog read that cannot answer refuses the external legs the same way.

Covered paths: ``nx index pdf`` (single file and ``--dir``), ``nx index md``, ``nx dt index`` (which calls the
same two indexers) and the ``nx index repo`` PDF path. The Voyage tokens below are collection-name labels.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
from click.testing import CliRunner

from nexus.cli import main
from tests.test_sensitive_external_rules import (
    BGE, Stop, _VOYAGE, _entry, _md, _pdf, _reader, _register_in, server_watch,  # noqa: F401 — server_watch is a fixture
)

CORPUS = "rfvyy-flag"
_BGE_DOCS = f"docs__{CORPUS}__{BGE}__v1"
_VOYAGE_DOCS = f"docs__{CORPUS}__voyage-context-3__v1"


@pytest.fixture(autouse=True)
def _no_document_is_sensitive():
    """Redefines tests/conftest.py's fixture of this name: the subject here is the gate, so it reads the real catalog."""
    yield


def _flag(tumbler: str) -> None:
    from nexus.catalog.factory import make_catalog_writer

    out = make_catalog_writer().set_sensitivity([tumbler], True, reason="test: flagged before the re-index")
    assert out["updated"] == [1], out


def _tumbler_of(path: Path) -> str:
    (entry,) = _reader().find_all_by_file_path(str(path.resolve()))
    return str(entry.tumbler)


def _index_pdf(pdf: Path, *extra: str):
    return CliRunner().invoke(
        main, ["index", "pdf", str(pdf), "--corpus", CORPUS, "--extractor", "pymupdf", "--force", *extra])


@pytest.fixture
def extractor_flags(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """The ``sensitive`` argument of every PDFExtractor built from here on."""
    from nexus.pdf_extractor import PDFExtractor

    built: list[bool] = []
    real_init = PDFExtractor.__init__

    def spy(self, *a, **k):
        built.append(bool(k.get("sensitive", False)))
        real_init(self, *a, **k)

    monkeypatch.setattr(PDFExtractor, "__init__", spy)
    return built


@pytest.fixture
def mineru_worker_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """A stand-in for the local MinerU worker, counted (labrun-pex). These tests are about server contact and
    the skip warning; the sensitive path runs in full up to the parse, and a real parse held about 1 GB on a CI
    shard. The worker raises at once, and a test asserts it was reached, so the seam cannot drift away silently."""
    from nexus.pdf_extractor import PDFExtractor

    calls: list[tuple] = []

    def _fake_worker(self, pdf_path, start, end, formula_enable=True):
        calls.append((start, end))
        raise RuntimeError("the local MinerU worker is not exercised by this test")

    monkeypatch.setattr(PDFExtractor, "_mineru_run_subprocess", _fake_worker)
    return calls


@pytest.fixture
def bib_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every title handed to the Semantic Scholar enricher."""
    calls: list[str] = []
    monkeypatch.setattr("nexus.bib_enricher.enrich", lambda title, *a, **k: calls.append(title) or {})
    return calls


# --------------------------------------------------------------------------- #
# MinerU: a flagged document is local only.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("streaming", ["never", "always"])
def test_a_catalog_flagged_pdf_reindexed_without_the_flag_builds_only_sensitive_extractors(
    t2_service_env, tmp_path: Path, extractor_flags: list[bool], streaming: str,
) -> None:
    pdf = _pdf(tmp_path, "flagged.pdf")
    assert _index_pdf(pdf, "--streaming", streaming).exit_code == 0
    assert extractor_flags and not any(extractor_flags), "control: an unflagged run builds plain extractors"
    _flag(_tumbler_of(pdf))
    extractor_flags.clear()

    out = _index_pdf(pdf, "--streaming", streaming)

    assert out.exit_code == 0, out.output
    assert extractor_flags and all(extractor_flags), f"a flagged document got a plain extractor: {extractor_flags}"


def test_a_catalog_flagged_pdf_never_reaches_a_mineru_server_without_the_flag(
    t2_service_env, tmp_path: Path, server_watch: dict[str, list], mineru_worker_calls: list[tuple],
) -> None:
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "flagged-mineru.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _flag(_tumbler_of(pdf))

    try:
        index_pdf(pdf, corpus=CORPUS, extractor="mineru", force=True, streaming="never")
    except Exception:  # noqa: BLE001 — the stand-in worker raises; only server contact matters
        pass

    assert mineru_worker_calls, "extraction was never reached, so no server contact proves nothing"
    assert server_watch == {"resolve": [], "probe": [], "http": [], "autostart": []}


def test_the_mineru_skip_for_a_flagged_document_is_logged_loudly(
    t2_service_env, tmp_path: Path, capsys: pytest.CaptureFixture[str], mineru_worker_calls: list[tuple],
) -> None:
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "flagged-loud.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _flag(_tumbler_of(pdf))
    capsys.readouterr()

    with structlog.testing.capture_logs() as logs:
        try:
            index_pdf(pdf, corpus=CORPUS, extractor="mineru", force=True, streaming="never")
        except Exception:  # noqa: BLE001 — the stand-in worker raises; the skip is announced first
            pass

    skips = [e for e in logs if e.get("event") == "sensitive_mineru_server_skipped"]
    assert skips and skips[0]["log_level"] == "warning" and skips[0]["reason"] == "catalog", logs
    assert mineru_worker_calls, "extraction was never reached"
    assert "no MinerU server is contacted" in capsys.readouterr().err


def test_the_verb_flag_logs_the_mineru_skip_in_the_library_and_says_it_on_the_single_file_cli(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mineru_worker_calls: list[tuple],
) -> None:
    """``--sensitive`` on the single-file path: a structlog warning from the indexer and a CLI line."""
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "verb-flag-loud.pdf")

    with structlog.testing.capture_logs() as logs:
        try:
            index_pdf(pdf, corpus=CORPUS, extractor="mineru", force=True, streaming="never", sensitive=True)
        except Exception:  # noqa: BLE001 — the stand-in worker raises after the warning is logged
            pass
    assert any(
        e.get("event") == "sensitive_mineru_server_skipped" and e.get("log_level") == "warning" for e in logs), logs
    assert mineru_worker_calls, "extraction was never reached"

    from nexus.commands import index as index_cmds

    monkeypatch.setattr(index_cmds.pdf_stack, "ensure_or_exit", lambda *a, **k: None)
    monkeypatch.setattr(index_cmds.pdf_stack, "refuse_bad_mineru_bounds", lambda *a, **k: None)
    monkeypatch.setattr(
        "nexus.doc_indexer.index_pdf", lambda *a, **k: {"chunks": 1, "pages": [1], "title": "", "author": ""})
    out = CliRunner().invoke(
        main, ["index", "pdf", str(pdf), "--corpus", CORPUS, "--extractor", "mineru", "--sensitive"])
    assert out.exit_code == 0, out.output
    assert "no MinerU server is contacted" in out.output, out.output


# --------------------------------------------------------------------------- #
# Fix round 3 (nexus-rfvyy.6.20): a DRY RUN reaches the same external legs, so it reads the flag too.
# --------------------------------------------------------------------------- #

def _dry_run(pdf: Path, *extra: str):
    return CliRunner().invoke(
        main, ["index", "pdf", str(pdf), "--corpus", CORPUS, "--dry-run", "--streaming", "never", *extra])


def _catalog_rows_for(path: Path) -> list[str]:
    return sorted(str(e.tumbler) for e in _reader().find_all_by_file_path(str(path.resolve())))


def test_a_dry_run_of_a_catalog_flagged_pdf_builds_only_sensitive_extractors_and_registers_nothing(
    t2_service_env, tmp_path: Path, extractor_flags: list[bool],
) -> None:
    pdf = _pdf(tmp_path, "dry-flagged.pdf")
    assert _index_pdf(pdf).exit_code == 0
    tumbler = _tumbler_of(pdf)
    extractor_flags.clear()
    out = _dry_run(pdf, "--extractor", "pymupdf")
    assert out.exit_code == 0, out.output
    assert extractor_flags and not any(extractor_flags), "control: an unflagged dry run builds plain extractors"
    _flag(tumbler)
    extractor_flags.clear()
    rows_before = _catalog_rows_for(pdf)

    out = _dry_run(pdf, "--extractor", "pymupdf")

    assert out.exit_code == 0, out.output
    assert extractor_flags and all(extractor_flags), f"a dry run of a flagged PDF got a plain extractor: {extractor_flags}"
    assert _catalog_rows_for(pdf) == rows_before, "a dry run must write nothing to the catalog"


def test_a_dry_run_of_a_catalog_flagged_pdf_never_reaches_a_mineru_server(
    t2_service_env, tmp_path: Path, server_watch: dict[str, list], monkeypatch: pytest.MonkeyPatch,
    mineru_worker_calls: list[tuple],
) -> None:
    from nexus.commands import index as index_cmds

    monkeypatch.setattr(index_cmds.pdf_stack, "ensure_or_exit", lambda *a, **k: None)
    monkeypatch.setattr(index_cmds.pdf_stack, "refuse_bad_mineru_bounds", lambda *a, **k: None)
    pdf = _pdf(tmp_path, "dry-flagged-mineru.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _flag(_tumbler_of(pdf))

    _dry_run(pdf, "--extractor", "mineru")  # the stand-in worker raises; only server contact matters

    assert mineru_worker_calls, "extraction was never reached, so no server contact proves nothing"
    assert server_watch == {"resolve": [], "probe": [], "http": [], "autostart": []}


def test_a_dry_run_with_enrich_of_a_catalog_flagged_pdf_refuses_and_sends_no_title(
    t2_service_env, tmp_path: Path, bib_calls: list[str],
) -> None:
    pdf = _pdf(tmp_path, "dry-flagged-enrich.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _dry_run(pdf, "--extractor", "pymupdf", "--enrich")
    assert len(bib_calls) == 1, "control: an unflagged dry run with --enrich looks the title up"
    bib_calls.clear()
    _flag(_tumbler_of(pdf))

    out = _dry_run(pdf, "--extractor", "pymupdf", "--enrich")

    assert out.exit_code != 0, out.output
    assert "sensitive" in out.output and "--enrich" in out.output, out.output
    assert bib_calls == []


def test_a_dry_run_of_a_pdf_the_catalog_never_saw_is_plain_and_registers_nothing(
    t2_service_env, tmp_path: Path, extractor_flags: list[bool],
) -> None:
    pdf = _pdf(tmp_path, "dry-new.pdf")
    out = _dry_run(pdf, "--extractor", "pymupdf")
    assert out.exit_code == 0, out.output
    assert extractor_flags and not any(extractor_flags)
    assert _catalog_rows_for(pdf) == []


def test_a_dry_run_whose_catalog_cannot_answer_reads_the_pdf_as_sensitive(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extractor_flags: list[bool], bib_calls: list[str],
) -> None:
    pdf = _pdf(tmp_path, "dry-unknown.pdf")
    extractor_flags.clear()

    def unreadable() -> Any:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", unreadable)

    out = _dry_run(pdf, "--extractor", "pymupdf")
    assert out.exit_code == 0, out.output
    assert extractor_flags and all(extractor_flags), "an unknown flag must read as sensitive on a dry run"

    out = _dry_run(pdf, "--extractor", "pymupdf", "--enrich")
    assert out.exit_code != 0 and bib_calls == [], out.output


# --------------------------------------------------------------------------- #
# --enrich: no title leaves for a flagged document.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("streaming", ["never", "always"])
def test_enrich_refuses_a_catalog_flagged_pdf_and_sends_no_title(
    t2_service_env, tmp_path: Path, bib_calls: list[str], streaming: str,
) -> None:
    pdf = _pdf(tmp_path, "flagged-enrich.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _flag(_tumbler_of(pdf))

    out = _index_pdf(pdf, "--enrich", "--streaming", streaming)

    assert out.exit_code != 0, out.output
    assert "sensitive" in out.output and "--enrich" in out.output, out.output
    assert bib_calls == []


def test_the_inline_bib_lookups_are_gated_on_the_sensitive_argument_too(
    t2_service_env, tmp_path: Path, bib_calls: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: with the front gate disabled, ``_pdf_chunks`` and the streaming pipeline's tail
    still make no lookup when told the document is sensitive."""
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "inline.pdf")
    with monkeypatch.context() as m:
        m.setattr("nexus.sensitive_write.external_legs_gate", lambda *a, **k: True)
        for streaming in ("never", "always"):
            index_pdf(
                pdf, corpus=CORPUS, extractor="pymupdf", force=True, streaming=streaming, enrich=True,
                sensitive=True)
    assert bib_calls == []
    # Control: the same two runs for a plain document do look the title up.
    plain = _pdf(tmp_path, "inline-plain.pdf")
    for streaming in ("never", "always"):
        index_pdf(plain, corpus=CORPUS, extractor="pymupdf", force=True, streaming=streaming, enrich=True)
    assert len(bib_calls) == 2, bib_calls


# --------------------------------------------------------------------------- #
# Voyage: a flagged document is not written to a Voyage collection without the ack.
# --------------------------------------------------------------------------- #

def test_a_catalog_flagged_pdf_in_a_voyage_collection_refuses_without_the_ack(t2_service_env, tmp_path: Path) -> None:
    pdf = _pdf(tmp_path, "flagged-voyage.pdf")
    tumbler = _register_in(pdf, _VOYAGE_DOCS)
    _flag(tumbler)

    out = _index_pdf(pdf, "--collection", _VOYAGE_DOCS)

    assert out.exit_code != 0, out.output
    assert "--ack-external-embedder" in out.output and _VOYAGE_DOCS in out.output, out.output
    assert _entry(tumbler).physical_collection == _VOYAGE_DOCS


def test_a_catalog_flagged_markdown_in_a_voyage_collection_refuses_without_the_ack(t2_service_env, tmp_path: Path) -> None:
    md = _md(tmp_path, "flagged-voyage.md")
    tumbler = _register_in(md, _VOYAGE_DOCS)
    _flag(tumbler)

    out = CliRunner().invoke(main, ["index", "md", str(md), "--corpus", CORPUS, "--collection", _VOYAGE_DOCS, "--force"])

    assert out.exit_code != 0, out.output
    assert "--ack-external-embedder" in out.output and _VOYAGE_DOCS in out.output, out.output


def test_a_catalog_flagged_markdown_in_the_local_collection_is_indexed(t2_service_env, tmp_path: Path) -> None:
    """Negative control: only an external embedder refuses; the local collection proceeds."""
    md = _md(tmp_path, "flagged-local.md")
    out = CliRunner().invoke(main, ["index", "md", str(md), "--corpus", CORPUS, "--force"])
    assert out.exit_code == 0, out.output
    _flag(_tumbler_of(md))
    out = CliRunner().invoke(main, ["index", "md", str(md), "--corpus", CORPUS, "--force"])
    assert out.exit_code == 0, out.output


def test_a_flagged_batch_pdf_refuses_alone_and_the_rest_of_the_batch_runs(t2_service_env, tmp_path: Path) -> None:
    d = tmp_path / "batch"
    d.mkdir()
    flagged, other = _pdf(d, "a-flagged.pdf"), _pdf(d, "b-plain.pdf")
    tumbler = _register_in(flagged, _VOYAGE_DOCS)
    _flag(tumbler)

    out = CliRunner().invoke(
        main, ["index", "pdf", "--dir", str(d), "--corpus", CORPUS, "--extractor", "pymupdf", "--collection", _VOYAGE_DOCS])

    assert "--ack-external-embedder" in out.output, out.output
    assert _entry(tumbler).physical_collection == _VOYAGE_DOCS


# --------------------------------------------------------------------------- #
# A catalog that cannot answer refuses the external legs.
# --------------------------------------------------------------------------- #

def test_an_unreadable_catalog_fails_closed_on_the_external_legs(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extractor_flags: list[bool], bib_calls: list[str],
) -> None:
    pdf = _pdf(tmp_path, "unknown.pdf")
    assert _index_pdf(pdf).exit_code == 0
    extractor_flags.clear()

    def unreadable() -> Any:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", unreadable)

    out = _index_pdf(pdf)
    assert out.exit_code == 0, out.output
    assert extractor_flags and all(extractor_flags), "an unknown flag must read as sensitive for the MinerU leg"

    out = _index_pdf(pdf, "--enrich")
    assert out.exit_code != 0 and bib_calls == [], out.output

    voyage_pdf = _pdf(tmp_path, "unknown-voyage.pdf")
    _register_in(voyage_pdf, _VOYAGE_DOCS)
    out = _index_pdf(voyage_pdf, "--collection", _VOYAGE_DOCS)
    assert out.exit_code != 0 and "--ack-external-embedder" in out.output, out.output


# --------------------------------------------------------------------------- #
# The gate itself, and the paths that do not go through index_pdf.
# --------------------------------------------------------------------------- #

def test_the_gate_reads_the_catalog_row_not_the_verb_option(t2_service_env, tmp_path: Path) -> None:
    from nexus.sensitive_write import ExternalEmbedderRefusedError, external_legs_gate

    pdf = _pdf(tmp_path, "gate.pdf")
    tumbler = _register_in(pdf, _BGE_DOCS)
    kw = dict(sensitive=False, enrich=False, ack=False, what="nx index pdf", pdf=True)
    assert external_legs_gate(tumbler, _VOYAGE_DOCS, **kw) is False
    assert external_legs_gate("", _VOYAGE_DOCS, **kw) is False
    _flag(tumbler)
    assert external_legs_gate(tumbler, _BGE_DOCS, **kw) is True
    with pytest.raises(ExternalEmbedderRefusedError):
        external_legs_gate(tumbler, _VOYAGE_DOCS, **kw)
    assert external_legs_gate(tumbler, _VOYAGE_DOCS, **{**kw, "ack": True}) is True


def test_the_gate_says_the_mineru_line_for_a_pdf_only(
    t2_service_env, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Fix round 3 (nexus-rfvyy.6.20): markdown has no MinerU leg, so its line does not mention one."""
    from nexus.sensitive_write import external_legs_gate

    tumbler = _register_in(_md(tmp_path, "line.md"), _VOYAGE_DOCS)
    _flag(tumbler)
    kw = dict(sensitive=False, enrich=False, ack=True, what="nx index md")
    capsys.readouterr()

    assert external_legs_gate(tumbler, _VOYAGE_DOCS, **kw) is True
    md_line = capsys.readouterr().err
    assert "flagged in the catalog" in md_line and "MinerU" not in md_line, md_line

    assert external_legs_gate(tumbler, _VOYAGE_DOCS, **{**kw, "pdf": True}) is True
    assert "MinerU" in capsys.readouterr().err


def test_the_gate_reads_the_catalog_for_markdown_only_when_the_target_is_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 3: a batch of N markdown files into a local collection used to pay N catalog reads for a gate that
    could change nothing (no MinerU, no --enrich, no Voyage). The collection is checked first."""
    from nexus.sensitive_write import external_legs_gate

    reads: list[str] = []
    monkeypatch.setattr("nexus.sensitive_write.catalog_sensitive", lambda doc_id: reads.append(doc_id) or False)
    kw = dict(sensitive=False, enrich=False, ack=False, what="nx index md")

    assert external_legs_gate("1.2.3", _BGE_DOCS, **kw) is False
    assert reads == [], "a local-embedder markdown target asked the catalog"

    assert external_legs_gate("1.2.3", _VOYAGE_DOCS, **kw) is False
    assert reads == ["1.2.3"], "control: a Voyage target reads the flag"

    reads.clear()
    external_legs_gate("1.2.3", _BGE_DOCS, **{**kw, "pdf": True})
    external_legs_gate("1.2.3", _BGE_DOCS, **{**kw, "enrich": True})
    assert reads == ["1.2.3", "1.2.3"], "a PDF (MinerU) and an --enrich run read the flag whatever the target"


def test_the_repo_pdf_path_indexes_a_flagged_pdf_locally_and_skips_it_for_voyage(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.indexer import _index_pdf_file

    pdf = _pdf(tmp_path, "repo.pdf")
    tumbler = _register_in(pdf, _BGE_DOCS)
    _flag(tumbler)
    seen: list[dict[str, Any]] = []

    def fake_chunks(*a, **k):
        seen.append(k)
        raise Stop

    monkeypatch.setattr("nexus.doc_indexer._pdf_chunks", fake_chunks)

    def run(collection: str) -> int:
        return _index_pdf_file(
            pdf, tmp_path, collection, "m", object(), object(), "", "2026-01-01T00:00:00+00:00", 0.0,
            force=True, doc_id_resolver=lambda f: tumbler)

    with pytest.raises(Stop):
        run(_BGE_DOCS)
    assert seen and seen[0].get("sensitive") is True

    seen.clear()
    with structlog.testing.capture_logs() as logs:
        assert run(_VOYAGE_DOCS) == 0
    assert seen == [], "a flagged PDF must not be chunked for a Voyage collection from a repo run"
    assert any(e.get("event") == "sensitive_pdf_skipped_external_embedder" for e in logs), logs


def test_nx_dt_index_refuses_a_catalog_flagged_record_for_a_voyage_collection(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx dt index`` of a record that is flagged in the catalog but run without ``--sensitive``."""
    from nexus.commands import dt as dtmod
    from nexus.sensitive_write import ExternalEmbedderRefusedError

    md = _md(tmp_path, "dt-flagged.md")
    tumbler = _register_in(md, _VOYAGE_DOCS)
    _flag(tumbler)
    monkeypatch.setattr(dtmod, "_dt_record_facts", lambda uuid: {"name": "dt-flagged"})
    monkeypatch.setattr(dtmod, "_dt_uri_is_catalogued", lambda uuid: False)

    with pytest.raises(ExternalEmbedderRefusedError, match="--ack-external-embedder"):
        dtmod._index_record("U-FLAGGED", str(md), collection=_VOYAGE_DOCS, corpus=CORPUS, dry_run=False)


# --------------------------------------------------------------------------- #
# Fix round 2 (nexus-rfvyy.6.20): the markdown, prose and code paths of a repo or batch run.
# A repo run and a batch run have no --ack-external-embedder: a flagged document going to a Voyage
# collection is SKIPPED with a warning; one going to a bge collection is indexed normally.
# --------------------------------------------------------------------------- #

def _prose_ctx(file: Path, collection: str, tumbler: str, embedded: list[int], **extra: Any):
    """An IndexContext whose embed function is the seam under watch: it records the call and stops the file."""
    from nexus.index_context import IndexContext

    def embed(texts: list[str]) -> list[list[float]]:
        embedded.append(len(texts))
        raise Stop

    return IndexContext(
        col=object(), db=object(), voyage_key="", voyage_client=None, repo_path=file.parent, corpus=collection,
        embedding_model="m", now_iso="2026-01-01T00:00:00+00:00", force=True, embed_fn=embed,
        doc_id_resolver=lambda f: tumbler, **extra)


def _skips(logs: list[dict], event: str) -> list[dict]:
    return [e for e in logs if e.get("event") == event]


@pytest.mark.parametrize("suffix,event", [(".md", "sensitive_md_skipped_external_embedder"),
                                          (".txt", "sensitive_prose_skipped_external_embedder")])
def test_the_repo_prose_path_skips_a_flagged_file_for_voyage_and_indexes_it_for_bge(
    t2_service_env, tmp_path: Path, suffix: str, event: str,
) -> None:
    from nexus.prose_indexer import index_prose_file

    f = tmp_path / f"flagged{suffix}"
    f.write_text(f"# flagged\n\n{'the harbour lease memo, second draft. ' * 12}\n", encoding="utf-8")
    tumbler = _register_in(f, _BGE_DOCS)
    control_embedded: list[int] = []
    with pytest.raises(Stop):  # control: an unflagged document reaches the embedder in the Voyage collection
        index_prose_file(_prose_ctx(f, _VOYAGE_DOCS, tumbler, control_embedded), f)
    assert control_embedded
    _flag(tumbler)

    embedded: list[int] = []
    with structlog.testing.capture_logs() as logs:
        assert index_prose_file(_prose_ctx(f, _VOYAGE_DOCS, tumbler, embedded), f) == 0
    assert embedded == [], "a flagged document reached the embedder of a Voyage collection"
    (skip,) = _skips(logs, event)
    assert skip["log_level"] == "warning" and skip["doc_id"] == tumbler and skip["collection"] == _VOYAGE_DOCS

    with pytest.raises(Stop):  # a flagged document in a local collection is indexed
        index_prose_file(_prose_ctx(f, _BGE_DOCS, tumbler, embedded), f)
    assert embedded


def test_the_repo_code_path_skips_a_flagged_file_for_voyage_code_3_and_indexes_it_for_bge(
    t2_service_env, tmp_path: Path, cloud_mode,
) -> None:
    from nexus.code_indexer import index_code_file

    f = tmp_path / "flagged.py"
    f.write_text("def lease_memo():\n    return 'harbour'\n\n\ndef second_draft():\n    return lease_memo()\n", encoding="utf-8")
    tumbler = _register_in(f, f"code__{CORPUS}__{BGE}__v1")
    voyage_code = f"code__{CORPUS}__voyage-code-3__v1"
    control: list[int] = []
    with pytest.raises(Stop):
        index_code_file(_prose_ctx(f, voyage_code, tumbler, control), f)
    assert control
    _flag(tumbler)

    embedded: list[int] = []
    with structlog.testing.capture_logs() as logs:
        assert index_code_file(_prose_ctx(f, voyage_code, tumbler, embedded), f) == 0
    assert embedded == []
    (skip,) = _skips(logs, "sensitive_code_skipped_external_embedder")
    assert skip["log_level"] == "warning" and skip["collection"] == voyage_code

    with pytest.raises(Stop):
        index_code_file(_prose_ctx(f, f"code__{CORPUS}__{BGE}__v1", tumbler, embedded), f)
    assert embedded


def test_an_unreadable_catalog_skips_a_voyage_prose_file_and_a_run_read_is_used_when_given(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.prose_indexer import index_prose_file
    from nexus.sensitive_write import SensitiveDocs

    f = tmp_path / "unknown.md"
    f.write_text("# unknown\n\n" + "the harbour lease memo. " * 20, encoding="utf-8")
    tumbler = _register_in(f, _BGE_DOCS)
    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    embedded: list[int] = []
    assert index_prose_file(_prose_ctx(f, _VOYAGE_DOCS, tumbler, embedded), f) == 0
    assert embedded == []
    # The run's read wins over a per-file read: an empty run read lets the file through with no catalog call.
    with pytest.raises(Stop):
        index_prose_file(_prose_ctx(f, _VOYAGE_DOCS, tumbler, embedded, sensitive_docs=SensitiveDocs()), f)
    assert embedded


def test_the_batch_markdown_path_skips_a_flagged_file_for_voyage_with_one_batched_read(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus import doc_sensitivity
    from nexus.doc_indexer import batch_index_markdowns

    files = [_md(tmp_path, f"b{i}.md") for i in range(3)]
    tumblers = [_register_in(f, _VOYAGE_DOCS) for f in files]
    _flag(tumblers[1])
    indexed: list[str] = []
    monkeypatch.setattr(
        "nexus.doc_indexer.index_markdown", lambda path, *a, **k: indexed.append(path.name) or 1)
    reads: list[int] = []
    real_flagged = doc_sensitivity.flagged
    monkeypatch.setattr(doc_sensitivity, "flagged", lambda ts, **k: reads.append(1) or real_flagged(ts, **k))

    with structlog.testing.capture_logs() as logs:
        result = batch_index_markdowns(files, CORPUS, collection_name=_VOYAGE_DOCS)

    assert result == {str(files[0]): "indexed", str(files[1]): "skipped", str(files[2]): "indexed"}, result
    assert indexed == ["b0.md", "b2.md"], "the flagged file reached index_markdown"
    assert len(reads) == 1, f"one batched flag read per batch, got {len(reads)}"
    (skip,) = _skips(logs, "sensitive_md_skipped_external_embedder")
    assert skip["log_level"] == "warning" and skip["path"] == str(files[1])


def test_the_batch_markdown_path_indexes_a_flagged_file_for_bge_and_reads_nothing(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus import doc_sensitivity
    from nexus.doc_indexer import batch_index_markdowns

    files = [_md(tmp_path, f"l{i}.md") for i in range(2)]
    _flag(_register_in(files[0], _BGE_DOCS))
    indexed: list[str] = []
    monkeypatch.setattr("nexus.doc_indexer.index_markdown", lambda path, *a, **k: indexed.append(path.name) or 1)
    reads: list[int] = []
    monkeypatch.setattr(doc_sensitivity, "flagged", lambda ts, **k: reads.append(1) or set())

    result = batch_index_markdowns(files, CORPUS, collection_name=_BGE_DOCS)

    assert set(result.values()) == {"indexed"} and indexed == ["l0.md", "l1.md"]
    assert reads == [], "a local collection costs no flag read"


def test_the_batch_markdown_path_with_an_unreadable_catalog_skips_every_file_for_voyage(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.doc_indexer import batch_index_markdowns

    files = [_md(tmp_path, f"u{i}.md") for i in range(2)]
    for f in files:
        _register_in(f, _VOYAGE_DOCS)
    indexed: list[str] = []
    monkeypatch.setattr("nexus.doc_indexer.index_markdown", lambda path, *a, **k: indexed.append(path.name) or 1)
    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: (_ for _ in ()).throw(RuntimeError("down")))

    result = batch_index_markdowns(files, CORPUS, collection_name=_VOYAGE_DOCS)

    assert indexed == [] and set(result.values()) == {"skipped"}, result


def test_the_batch_backstop_turns_the_per_file_refusal_into_a_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No collection_name: the batch cannot tell the collection up front, so index_markdown's own gate may refuse
    a flagged file for Voyage; the batch reports a skip (not a failure, no on_error) and keeps going."""
    from nexus.doc_indexer import batch_index_markdowns
    from nexus.sensitive_write import ExternalEmbedderRefusedError

    a, b = _md(tmp_path, "refused.md"), _md(tmp_path, "fine.md")

    def fake_index(path, *args, **kwargs):
        if path.name == "refused.md":
            raise ExternalEmbedderRefusedError("nx index md refused: --ack-external-embedder")
        return 1

    monkeypatch.setattr("nexus.doc_indexer.index_markdown", fake_index)
    failures: list[BaseException] = []
    with structlog.testing.capture_logs() as logs:
        result = batch_index_markdowns([a, b], CORPUS, on_error=lambda p, e: failures.append(e))

    assert result == {str(a): "skipped", str(b): "indexed"}, result
    assert failures == []
    (skip,) = _skips(logs, "sensitive_md_skipped_external_embedder")
    assert skip["log_level"] == "warning" and skip["path"] == str(a)


def test_a_repo_run_reads_the_flags_once_and_skips_only_the_flagged_files_for_voyage(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``index_repository`` end to end: three markdown files and a code file, one markdown file and the
    code file flagged after a first run; a second run whose collections read as Voyage skips those two alone, with one batched
    flag read for the run."""
    import subprocess

    from nexus import doc_sensitivity
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("a.md", "secret.md", "c.md"):
        (repo / name).write_text(f"# {name}\n\n{'the harbour lease memo, second draft. ' * 12}\n", encoding="utf-8")
    (repo / "m.py").write_text("def lease_memo():\n    return 'harbour'\n", encoding="utf-8")
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True)
    first = CliRunner().invoke(main, ["index", "repo", str(repo), "--no-taxonomy"])
    assert first.exit_code == 0, first.output
    (entry,) = _reader().find_all_by_file_path("secret.md")  # a repo owner registers the path relative to the repo
    _flag(str(entry.tumbler))
    (code_entry,) = _reader().find_all_by_file_path("m.py")
    _flag(str(code_entry.tumbler))

    monkeypatch.setattr("nexus.sensitive_write.is_external_embedder", lambda name: True)
    reads: list[int] = []
    real_flagged = doc_sensitivity.flagged
    monkeypatch.setattr(doc_sensitivity, "flagged", lambda ts, **k: reads.append(1) or real_flagged(ts, **k))
    second = CliRunner().invoke(main, ["index", "repo", str(repo), "--no-taxonomy", "--force"])
    assert second.exit_code == 0, second.output

    # The CLI configures its own logging, so the warnings are read from its output.
    lines = second.output.splitlines()
    md_skips = [ln for ln in lines if "event='sensitive_md_skipped_external_embedder'" in ln]
    assert len(md_skips) == 1 and "secret.md" in md_skips[0], md_skips
    code_skips = [ln for ln in lines if "event='sensitive_code_skipped_external_embedder'" in ln]
    assert len(code_skips) == 1 and "m.py" in code_skips[0], code_skips
    assert "a.md — 1 chunks" in second.output and "c.md — 1 chunks" in second.output, second.output
    assert len(reads) == 1, f"one batched flag read for the run, got {len(reads)}"


# --------------------------------------------------------------------------- #
# nx collection reembed: it re-sends STORED chunk text to Voyage, so a chunk of a flagged document is held back.
# --------------------------------------------------------------------------- #

class _ReembedDb:
    """A one-page collection handle that records every chunk id handed to ``upsert_chunks``."""

    def __init__(self, ids: list[str]) -> None:
        self.ids, self.written = ids, []

    def get_collection(self, name: str):
        outer = self

        class Col:
            def count(self) -> int:
                return len(outer.ids)

            def get(self, *, limit, offset, include, sensitivity="include"):
                ids = outer.ids[offset:offset + limit]
                return {"ids": ids, "documents": [f"text {i}" for i in ids], "metadatas": [{} for _ in ids]}

        return Col()

    def upsert_chunks(self, name, ids, docs, metadatas=None, force_re_embed=False):
        self.written += list(ids)


class _FlaggedManifests:
    """A catalog reader with one flagged document in the collection, whose manifest names ``chashes``."""

    def __init__(self, chashes: list[str]) -> None:
        self.chashes = chashes

    def list_by_collection(self, collection, *, limit=None):
        return [type("E", (), {"tumbler": "1.1.9", "sensitive": True})()]

    def get_manifests(self, doc_ids):
        return {"1.1.9": [type("M", (), {"chash": c, "collection": None})() for c in self.chashes]}

    def close(self) -> None:
        return None


def test_reembed_holds_back_the_chunks_of_a_flagged_document_in_a_voyage_collection(
    monkeypatch: pytest.MonkeyPatch, cloud_mode,
) -> None:
    from nexus.commands.collection import _reembed_collection

    flagged_id, plain_id = "a" * 64, "b" * 64
    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: _FlaggedManifests([flagged_id]))
    db = _ReembedDb([flagged_id, plain_id])
    with structlog.testing.capture_logs() as logs:
        processed, skipped = _reembed_collection(
            db, f"docs__{CORPUS}__voyage-context-3__v1", "voyage-context-3", dry_run=False, hooks=_NullHooks())
    assert db.written == [plain_id], "a chunk of a flagged document was re-sent to Voyage"
    assert (processed, skipped) == (1, 1)
    (held,) = _skips(logs, "sensitive_reembed_held_back")
    assert held["log_level"] == "warning" and held["held_back"] == 1


def test_reembed_of_a_bge_collection_reads_no_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.commands.collection import _reembed_collection

    def never() -> Any:
        raise AssertionError("a local collection must not read the catalog")

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", never)
    db = _ReembedDb(["a" * 64])
    _reembed_collection(db, _BGE_DOCS, "bge-base-en-v15-768", dry_run=False, hooks=_NullHooks())
    assert db.written == ["a" * 64]


def test_reembed_of_a_voyage_collection_refuses_when_the_catalog_cannot_answer(monkeypatch: pytest.MonkeyPatch, cloud_mode) -> None:
    import click

    from nexus.commands.collection import _reembed_collection

    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    db = _ReembedDb(["a" * 64])
    with pytest.raises(click.ClickException, match="sensitiv"):
        _reembed_collection(
            db, f"docs__{CORPUS}__voyage-context-3__v1", "voyage-context-3", dry_run=False, hooks=_NullHooks())
    assert db.written == []


class _NullHooks:
    def fire_store_chains(self, *a, **k) -> None:
        return None

    def without_batch(self, fn):
        return self  # a re-embed drops the manifest hook from the registry it fires (RDR-236 Phase 4 Step 1)


# --------------------------------------------------------------------------- #
# Notes: nx store put, MCP store_put, nx memory promote and the recovery import all write through put_note.
# --------------------------------------------------------------------------- #

def _note_put(monkeypatch: pytest.MonkeyPatch, tumbler: str, collection: str, *, minted: bool = False):
    """``put_note`` of a re-put onto the registered document *tumbler*; ``write_note`` records instead of writing."""
    from nexus.catalog import note_write

    written: list[str] = []
    monkeypatch.setattr(
        "nexus.catalog.store_hook.catalog_store_hook_tracked", lambda **k: (tumbler, minted))
    monkeypatch.setattr(note_write, "write_note", lambda **k: written.append(k["collection"]) or (_ for _ in ()).throw(Stop()))
    return written, lambda **kw: note_write.put_note(
        content="the harbour lease memo", collection=collection, title="flagged note", tags="", category="",
        source_agent="test", ttl_days=None, entity_key="", **kw)


def test_a_reput_of_a_flagged_note_into_a_voyage_collection_is_refused_before_any_write(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.sensitive_write import ExternalEmbedderRefusedError

    tumbler = _register_in(_md(tmp_path, "note.md"), _VOYAGE_DOCS)
    _flag(tumbler)
    written, put = _note_put(monkeypatch, tumbler, _VOYAGE_DOCS)

    with pytest.raises(ExternalEmbedderRefusedError, match="--ack-external-embedder"):
        put()
    assert written == [], "a flagged note was written to a Voyage collection"


def test_the_reput_refusal_is_worded_for_every_caller_of_the_note_writer(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix round 3 (nexus-rfvyy.6.20): the refusal reaches ``nx store put``, ``nx memory promote``, MCP ``store_put`` and
    ``nx catalog import``. Only two of them can accept Voyage, so the text names those two and says the others cannot,
    rather than telling a caller to pass flags its verb does not have."""
    from nexus.sensitive_write import ExternalEmbedderRefusedError

    tumbler = _register_in(_md(tmp_path, "worded.md"), _VOYAGE_DOCS)
    _flag(tumbler)
    _, put = _note_put(monkeypatch, tumbler, _VOYAGE_DOCS)

    with pytest.raises(ExternalEmbedderRefusedError) as exc:
        put()
    text = exc.value.message
    assert "Nothing was written" in text and tumbler in text
    assert "nx store put --sensitive --ack-external-embedder" in text and "nx catalog import --ack-external-embedder" in text
    assert "nx memory promote" in text and "store_put" in text, "names the callers that cannot accept Voyage"
    assert not text.startswith("store put refused"), "the verb is not always store put"
    assert "bge" in text, "and the way out every caller has"


def test_the_refused_reput_puts_the_identity_stamp_back(t2_service_env, tmp_path: Path, cloud_mode) -> None:
    """Fix round 3 (nexus-rfvyy.6.20): a REAL reconcile branch (``by_source_uri``) stamps ``meta.doc_id`` with the new
    text's first chash before the write; the refusal must put the old value back, or the stamp names a chunk that was
    never written. ``catalog_store_hook_tracked`` is not patched, so ``pre_call`` is filled by the real branch."""
    import hashlib

    from nexus.aspect_readers import uri_for
    from nexus.catalog.factory import make_catalog_writer
    from nexus.catalog.note_write import put_note
    from nexus.sensitive_write import ExternalEmbedderRefusedError

    collection = f"knowledge__{CORPUS}__voyage-context-3__v1"
    title = "stamp restore note"
    old_chash = hashlib.sha256(b"the old text of the note").hexdigest()
    writer = make_catalog_writer()
    try:
        owner = writer.register_owner("knowledge", "curator")
        tumbler = str(writer.register(
            owner=owner, title=title, content_type="knowledge", physical_collection=collection,
            source_uri=uri_for(collection, title), meta={"doc_id": old_chash}))
    finally:
        writer.close()
    _flag(tumbler)

    with pytest.raises(ExternalEmbedderRefusedError, match="--ack-external-embedder"):
        put_note(
            content="the new text of the note, which must never be embedded", collection=collection, title=title,
            tags="", category="", source_agent="test", ttl_days=None, entity_key="")

    entry = _reader().by_doc_id(tumbler)
    assert (entry.meta or {}).get("doc_id") == old_chash, \
        f"the refused re-put left its own stamp on the note: {(entry.meta or {}).get('doc_id')}"


def test_a_reput_of_a_flagged_note_into_a_bge_collection_and_an_unflagged_one_into_voyage_proceed(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    flagged = _register_in(_md(tmp_path, "n1.md"), _BGE_DOCS)
    _flag(flagged)
    written, put = _note_put(monkeypatch, flagged, _BGE_DOCS)
    with pytest.raises(Stop):
        put()
    assert written == [_BGE_DOCS]

    plain = _register_in(_md(tmp_path, "n2.md"), _VOYAGE_DOCS)
    written, put = _note_put(monkeypatch, plain, _VOYAGE_DOCS)
    with pytest.raises(Stop):
        put()
    assert written == [_VOYAGE_DOCS]
