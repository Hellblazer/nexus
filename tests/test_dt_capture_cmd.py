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
                        lambda uuid, path, **kw: file_calls.append(uuid) or True)
    return dt_calls, file_calls


def test_dt_absent_exits_nonzero_with_required_message(runner, monkeypatch) -> None:
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: False)
    result = runner.invoke(__import__("nexus.cli", fromlist=["main"]).main,
                           ["dt", "capture", "https://example.com"])
    assert result.exit_code != 0
    assert "requires DEVONthink" in result.output


def test_webarchive_capture_routes_through_dt_content(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_capture_web_page",
                        lambda url, **kw: "NEW-UUID")
    dt_calls, file_calls = _patch_index(monkeypatch)
    result = runner.invoke(main, ["dt", "capture", "https://example.com",
                                  "--collection", "docs__t__voyage-context-3__v1"])
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
                        lambda uuid, path, **kw: file_calls.append(uuid) or True)
    monkeypatch.setattr("nexus.commands.dt._index_dt_content_record",
                        lambda uuid, **kw: (_ for _ in ()).throw(AssertionError("should not route to dt_content")))
    result = runner.invoke(main, ["dt", "capture", "https://e.com", "--type", "pdf"])
    assert result.exit_code == 0, result.output
    assert file_calls == ["PDF-UUID"]


def test_doi_capture_downloads_pdf(runner, monkeypatch) -> None:
    from nexus.cli import main
    monkeypatch.setattr("nexus.mcp_client.devonthink.available", lambda **k: True)
    seen = {}
    def _dl(doi, **kw):
        seen["doi"] = doi; seen["email"] = kw.get("contact_email"); return "DOI-UUID"
    monkeypatch.setattr("nexus.mcp_client.devonthink.dt_download_pdf_from_doi", _dl)
    monkeypatch.setattr("nexus.commands.dt._gather_records",
                        lambda **kw: [(kw["uuids"][0], "/p.pdf")])
    monkeypatch.setattr("nexus.commands.dt._index_record",
                        lambda uuid, path, **kw: True)
    result = runner.invoke(main, ["dt", "capture", "--doi", "10.1/x",
                                  "--contact-email", "me@x.co"])
    assert result.exit_code == 0, result.output
    assert seen == {"doi": "10.1/x", "email": "me@x.co"}


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
