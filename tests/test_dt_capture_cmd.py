# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer G — nx dt capture command.

The one DT-bound verb: DT absent -> non-zero exit + DT-required message (NOT a
silent no-op). DT present -> capture (web/doi/file) then index end to end.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _force_darwin(monkeypatch):
    # capture_cmd platform-gates on _is_darwin; pin True so CI (linux) exercises
    # the real logic instead of the macOS-only early exit.
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)


def _patch_index(monkeypatch):
    """Stub the index path so capture's ctx.invoke(index_cmd) runs without DT
    selectors / real indexing. Returns (dt_content_calls, file_calls)."""
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/captured.webarchive")])
    dt_calls: list[str] = []
    file_calls: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._index_dt_content_record",
                        lambda uuid, **kw: dt_calls.append(uuid) or True)
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: (file_calls.append(uuid) or True, 1))
    return dt_calls, file_calls


def test_dt_absent_exits_nonzero_with_required_message(runner, monkeypatch) -> None:
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: False)
    result = runner.invoke(__import__("nexus.cli", fromlist=["main"]).main,
                           ["dt", "capture", "https://example.com"])
    assert result.exit_code != 0
    assert "requires DEVONthink" in result.output


def test_webarchive_capture_routes_through_dt_content(runner, monkeypatch) -> None:
    # Neutral model token on purpose (RDR-109 mode lint): a collection-NAME
    # CLI flag value -- _patch_index below stubs out the actual indexing
    # call, so no embedder or credential path is exercised.
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "NEW-UUID")
    dt_calls, file_calls = _patch_index(monkeypatch)
    result = runner.invoke(main, ["dt", "capture", "https://example.com",
                                  "--collection", "docs__t__model-ctx__v1"])
    assert result.exit_code == 0, result.output
    assert "Captured https://example.com -> DEVONthink record NEW-UUID" in result.output
    assert dt_calls == ["NEW-UUID"]   # non-file-backed -> Layer D path
    assert file_calls == []


def test_pdf_capture_is_file_backed(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "PDF-UUID")
    # a pdf-typed capture is file-backed -> normal index path
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/captured.pdf")])
    file_calls: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: (file_calls.append(uuid) or True, 1))
    monkeypatch.setattr("nexus.commands.dt._index_dt_content_record",
                        lambda uuid, **kw: (_ for _ in ()).throw(AssertionError("should not route to dt_content")))
    result = runner.invoke(main, ["dt", "capture", "https://e.com", "--type", "pdf"])
    assert result.exit_code == 0, result.output
    assert file_calls == ["PDF-UUID"]


def test_markdown_capture_is_file_backed(runner, monkeypatch) -> None:
    """SIG-1: a markdown capture is stored by DT as a .md file -> index from the
    file (file-backed), NOT the AI re-extract (dt_content) path."""
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "MD-UUID")
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/captured.md")])
    file_calls: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: (file_calls.append(uuid) or True, 1))
    monkeypatch.setattr("nexus.commands.dt._index_dt_content_record",
                        lambda uuid, **kw: (_ for _ in ()).throw(AssertionError("markdown must not route to dt_content")))
    result = runner.invoke(main, ["dt", "capture", "https://e.com", "--type", "markdown"])
    assert result.exit_code == 0, result.output
    assert file_calls == ["MD-UUID"]


def test_doi_capture_downloads_pdf_and_indexes(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    seen = {}
    def _dl(doi, **kw):
        seen["doi"] = doi; seen["email"] = kw.get("contact_email"); return "DOI-UUID"
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_download_pdf_from_doi", _dl)
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/p.pdf")])
    indexed: list[str] = []
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: (indexed.append(uuid) or True, 1))
    result = runner.invoke(main, ["dt", "capture", "--doi", "10.1/x",
                                  "--contact-email", "me@x.co"])
    assert result.exit_code == 0, result.output
    assert seen == {"doi": "10.1/x", "email": "me@x.co"}
    assert indexed == ["DOI-UUID"]  # the captured record was actually indexed


def test_capture_forwards_extractor_to_index(runner, monkeypatch) -> None:
    """nexus-pxxyn: nx dt capture --extractor reaches the PDF indexer (so a captured PDF
    can use the text-only extractor)."""
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "PDF-UUID")
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/captured.pdf")])
    seen = {}
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: (seen.update(kw) or True, 1))
    result = runner.invoke(main, ["dt", "capture", "https://e.com",
                                  "--type", "pdf", "--extractor", "pymupdf"])
    assert result.exit_code == 0, result.output
    assert seen.get("extractor") == "pymupdf"


def test_capture_reads_the_pdf_extractor_setting_when_no_flag_is_given(runner, monkeypatch) -> None:
    """nexus-tr1rj.14: no --extractor means `pdf.extractor`, like `nx index pdf`; a host set to
    pymupdf neither asks for the PDF stack nor indexes with MinerU."""
    from nexus import config as nx_config
    from nexus.cli import main

    real = nx_config.load_config
    monkeypatch.setattr(nx_config, "load_config", lambda **kw: {**real(**kw), "pdf": {"extractor": "pymupdf"}})
    gated: list[str] = []
    monkeypatch.setattr("nexus.pdf_stack.ensure_or_exit", lambda *a, **kw: gated.append(kw["extractor"]))
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page", lambda url, **kw: "PDF-UUID")
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [(kw["uuids"][0], "/captured.pdf")])
    seen = {}
    monkeypatch.setattr("nexus.commands.dt._index_record", lambda uuid, path, **kw: (seen.update(kw) or True, 1))
    result = runner.invoke(main, ["dt", "capture", "https://e.com", "--type", "pdf"])
    assert result.exit_code == 0, result.output
    assert seen.get("extractor") == "pymupdf"
    assert gated and set(gated) == {"pymupdf"}, "both stack gates (capture's, then index's) key on the setting"


def test_capture_stops_on_a_mineru_breaker_trip_without_calling_it_a_defect(runner, monkeypatch) -> None:
    """nexus-tr1rj.13: capture hands its one record to `nx dt index`, which now classifies a trip
    as a breaker trip, not an unexpected defect."""
    from nexus.cli import main
    from nexus.pdf_extractor import MineruSystemicFailure

    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page", lambda url, **kw: "PDF-UUID")
    monkeypatch.setattr("nexus.commands.dt._gather_records", lambda **kw: [(kw["uuids"][0], "/captured.pdf")])

    def _trip(uuid, path, **kw):
        raise MineruSystemicFailure("timeout", "", [1, 2, 3], "captured.pdf")

    monkeypatch.setattr("nexus.commands.dt._index_record", _trip)
    result = runner.invoke(main, ["dt", "capture", "https://e.com", "--type", "pdf", "--extractor", "mineru"])
    assert result.exit_code != 0, result.output
    assert "MinerU failed systemically on captured.pdf (failure class timeout)" in result.output
    assert "unexpected error" not in result.output
    assert "Recover with: nx dt index --uuid PDF-UUID" in result.output


@pytest.mark.parametrize("name", ["auto", "docling"])
def test_capture_runs_an_older_extractor_name_as_mineru(runner, monkeypatch, name) -> None:
    """RDR-237: the older names are aliases for mineru, resolved before the capture starts."""
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "PDF-UUID")
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/captured.pdf")])
    seen = {}
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: (seen.update(kw) or True, 1))
    result = runner.invoke(main, ["dt", "capture", "https://e.com",
                                  "--type", "pdf", "--extractor", name])
    assert result.exit_code == 0, result.output
    assert seen.get("extractor") == "mineru"


def test_capture_prints_the_older_name_notice_before_it_touches_devonthink(runner, monkeypatch) -> None:
    """The alias is resolved at the top of the verb, so a capture that stops at once (DEVONthink not
    running) still told the user once that their older name now runs as mineru."""
    from nexus import config as nexus_config
    from nexus.cli import main

    nexus_config._NOTICES_SHOWN.clear()
    monkeypatch.setattr("nexus.commands.dt._is_darwin", lambda: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: False)
    try:
        result = runner.invoke(main, ["dt", "capture", "https://e.com", "--extractor", "docling"])
    finally:
        notices = [ln for ln in result.output.splitlines() if ln.startswith("notice:")]
        nexus_config._NOTICES_SHOWN.clear()
    assert result.exit_code != 0 and "requires DEVONthink" in result.output
    assert len(notices) == 1 and "--extractor says 'docling'" in notices[0]


def test_doi_without_email_warns(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_download_pdf_from_doi",
                        lambda doi, **kw: "U")
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/p.pdf")])
    monkeypatch.setattr("nexus.commands.dt._index_record", lambda uuid, path, **kw: (True, 1))
    result = runner.invoke(main, ["dt", "capture", "--doi", "10.1/x"])
    assert result.exit_code == 0, result.output
    assert "Unpaywall open-access PDF discovery is disabled" in result.output


def test_empty_file_source_errors(runner, monkeypatch) -> None:
    from nexus.cli import main
    result = runner.invoke(main, ["dt", "capture", "--file", ""])
    assert result.exit_code != 0
    assert "exactly one capture source" in result.output


def test_partial_failure_surfaces_recovery_hint(runner, monkeypatch) -> None:
    """MEDIUM-4: capture succeeds but indexing fails -> recovery hint + non-zero."""
    import click as _click
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "ORPHAN-UUID")

    def _boom(**kw):
        raise _click.ClickException("indexing blew up")
    monkeypatch.setattr("nexus.commands.dt._gather_records", _boom)
    result = runner.invoke(main, ["dt", "capture", "https://e.com"])
    assert result.exit_code != 0
    assert "ORPHAN-UUID was captured but indexing failed" in result.output
    assert "nx dt index --uuid ORPHAN-UUID" in result.output


def test_capture_failure_is_clean_error(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: None)
    result = runner.invoke(main, ["dt", "capture", "https://e.com"])
    assert result.exit_code != 0
    assert "capture failed" in result.output


def test_doi_failure_hints_no_oa_pdf(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_download_pdf_from_doi",
                        lambda doi, **kw: None)
    result = runner.invoke(main, ["dt", "capture", "--doi", "10.1/x"])
    assert result.exit_code != 0
    assert "no open-access PDF" in result.output


def test_no_source_errors(runner, monkeypatch) -> None:
    from nexus.cli import main
    result = runner.invoke(main, ["dt", "capture"])
    assert result.exit_code != 0
    assert "exactly one capture source" in result.output


def test_two_sources_error(runner, monkeypatch) -> None:
    from nexus.cli import main
    result = runner.invoke(main, ["dt", "capture", "https://e.com", "--doi", "10.1/x"])
    assert result.exit_code != 0
    assert "exactly one capture source" in result.output
