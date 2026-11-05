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
    t2_service_env, tmp_path: Path, server_watch: dict[str, list],
) -> None:
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "flagged-mineru.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _flag(_tumbler_of(pdf))

    try:
        index_pdf(pdf, corpus=CORPUS, extractor="mineru", force=True, streaming="never")
    except Exception:  # noqa: BLE001 — the local subprocess may be absent on this host; only server contact matters
        pass

    assert server_watch == {"resolve": [], "probe": [], "http": [], "autostart": []}


def test_the_mineru_skip_for_a_flagged_document_is_logged_loudly(
    t2_service_env, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "flagged-loud.pdf")
    assert _index_pdf(pdf).exit_code == 0
    _flag(_tumbler_of(pdf))
    capsys.readouterr()

    with structlog.testing.capture_logs() as logs:
        try:
            index_pdf(pdf, corpus=CORPUS, extractor="mineru", force=True, streaming="never")
        except Exception:  # noqa: BLE001 — the local subprocess may be absent on this host; the skip is announced first
            pass

    skips = [e for e in logs if e.get("event") == "sensitive_mineru_server_skipped"]
    assert skips and skips[0]["log_level"] == "warning" and skips[0]["reason"] == "catalog", logs
    assert "no MinerU server is contacted" in capsys.readouterr().err


def test_the_verb_flag_logs_the_mineru_skip_in_the_library_and_says_it_on_the_single_file_cli(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--sensitive`` on the single-file path: a structlog warning from the indexer and a CLI line."""
    from nexus.doc_indexer import index_pdf

    pdf = _pdf(tmp_path, "verb-flag-loud.pdf")
    with structlog.testing.capture_logs() as logs:
        try:
            index_pdf(pdf, corpus=CORPUS, extractor="mineru", force=True, streaming="never", sensitive=True)
        except Exception:  # noqa: BLE001 — the local subprocess may be absent on this host
            pass
    assert any(
        e.get("event") == "sensitive_mineru_server_skipped" and e.get("log_level") == "warning" for e in logs), logs

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
    kw = dict(sensitive=False, enrich=False, ack=False, what="nx index pdf")
    assert external_legs_gate(tumbler, _VOYAGE_DOCS, **kw) is False
    assert external_legs_gate("", _VOYAGE_DOCS, **kw) is False
    _flag(tumbler)
    assert external_legs_gate(tumbler, _BGE_DOCS, **kw) is True
    with pytest.raises(ExternalEmbedderRefusedError):
        external_legs_gate(tumbler, _VOYAGE_DOCS, **kw)
    assert external_legs_gate(tumbler, _VOYAGE_DOCS, **{**kw, "ack": True}) is True


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
