# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer G — DT capture helpers (capture_web_page / download_pdf_from_doi
/ import_file). Each returns the new record's UUID or None, fail-soft."""
from __future__ import annotations

from unittest.mock import patch

from nexus.mcp_client.devonthink import (
    dt_capture_web_page,
    dt_download_pdf_from_doi,
    dt_import_file,
)


def test_capture_web_page_reads_top_level_uuid() -> None:
    # CA6 live shape: a flat dict with uuid at top level.
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"uuid": "ABC-123", "type": "webarchive"}):
        assert dt_capture_web_page("https://example.com") == "ABC-123"


def test_capture_web_page_unavailable_is_none() -> None:
    with patch("nexus.mcp_client.devonthink.dt_call", return_value=None):
        assert dt_capture_web_page("https://example.com") is None


def test_capture_web_page_no_uuid_is_none() -> None:
    with patch("nexus.mcp_client.devonthink.dt_call", return_value={"error": "boom"}):
        assert dt_capture_web_page("https://example.com") is None


def test_download_pdf_reads_nested_record_uuid() -> None:
    # download_pdf_from_doi returns "metadata and the imported record".
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"record": {"uuid": "PDF-9"}, "title": "X"}):
        assert dt_download_pdf_from_doi("10.1/x", contact_email="a@b.co") == "PDF-9"


def test_download_pdf_metadata_only_no_pdf_is_none() -> None:
    # metadata-only (no open-access PDF) -> no imported record -> None
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"title": "X", "doi": "10.1/x"}):
        assert dt_download_pdf_from_doi("10.1/x", contact_email="a@b.co") is None


def test_import_file_reads_uuid() -> None:
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"uuid": "FILE-7"}):
        assert dt_import_file("/tmp/x.pdf") == "FILE-7"


def test_capture_passes_type_and_name() -> None:
    with patch("nexus.mcp_client.devonthink.dt_call",
               return_value={"uuid": "U"}) as m:
        dt_capture_web_page("https://e.com", capture_type="pdf", name="My Page")
    args = m.call_args.args
    assert args[0] == "capture_web_page"
    assert args[1]["url"] == "https://e.com"
    assert args[1]["type"] == "pdf"
    assert args[1]["name"] == "My Page"
