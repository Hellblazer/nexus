# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer D — content extraction for non-file-backed DT records.

``_index_dt_content_record`` sources a record's text from DEVONthink
(``extract_record_content``) when no on-disk file backs it, feeds it through
the Markdown chunking pipeline, and stamps every chunk
``extraction_source=dt_content``. The ``nx dt index --dt-content`` flag routes
previously-skipped (unsupported-extension / no-file) records here; without the
flag, or with DT unavailable, those records are skipped exactly as before
(Gap 0).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ── _index_dt_content_record ────────────────────────────────────────────────

@patch("nexus.commands.dt._stamp_dt_uri_on_entry", return_value=True)
@patch("nexus.doc_indexer.index_markdown", return_value=3)
@patch("nexus.mcp_client.devonthink.dt_record_name", return_value="A Web Clip")
@patch("nexus.mcp_client.devonthink.dt_extract_content", return_value="body text")
def test_index_dt_content_happy_path(
    mock_extract: MagicMock,
    mock_name: MagicMock,
    mock_index: MagicMock,
    mock_stamp: MagicMock,
    tmp_path,
) -> None:
    from nexus.commands.dt import _index_dt_content_record

    with patch("nexus.config.catalog_path", return_value=tmp_path):
        ok = _index_dt_content_record(
            "U1", collection="docs__dt__voyage-context-3__v1", corpus="dt",
        )
    assert ok is True
    # index_markdown stamped extraction_source=dt_content on the write path
    assert mock_index.call_args.kwargs["extraction_source"] == "dt_content"
    assert mock_index.call_args.kwargs["collection_name"] == "docs__dt__voyage-context-3__v1"
    mock_stamp.assert_called_once()
    # text cached at a STABLE per-uuid path (re-index idempotency, HIGH-1)
    cached = mock_index.call_args.args[0]
    assert cached == tmp_path / ".dt-content" / "U1.md"
    assert cached.exists()


@patch("nexus.doc_indexer.index_markdown")
@patch("nexus.mcp_client.devonthink.dt_extract_content", return_value=None)
def test_index_dt_content_empty_text_skips(
    mock_extract: MagicMock, mock_index: MagicMock,
) -> None:
    from nexus.commands.dt import _index_dt_content_record

    ok = _index_dt_content_record("U1", collection="docs__dt__m__v1", corpus="dt")
    assert ok is False
    mock_index.assert_not_called()  # never reached the pipeline


@patch("nexus.doc_indexer.index_markdown", return_value=0)
@patch("nexus.mcp_client.devonthink.dt_record_name", return_value="X")
@patch("nexus.mcp_client.devonthink.dt_extract_content", return_value="body")
def test_index_dt_content_zero_chunks_is_false(
    mock_extract: MagicMock, mock_name: MagicMock, mock_index: MagicMock,
    tmp_path,
) -> None:
    from nexus.commands.dt import _index_dt_content_record

    with patch("nexus.config.catalog_path", return_value=tmp_path):
        assert _index_dt_content_record("U1", collection="c", corpus="dt") is False


@patch("nexus.commands.dt._stamp_dt_uri_on_entry", return_value=True)
@patch("nexus.doc_indexer.index_markdown", return_value=2)
@patch("nexus.mcp_client.devonthink.dt_record_name", return_value="Clip")
@patch("nexus.mcp_client.devonthink.dt_extract_content", return_value="body")
def test_reindex_uses_same_stable_path(
    mock_extract: MagicMock,
    mock_name: MagicMock,
    mock_index: MagicMock,
    mock_stamp: MagicMock,
    tmp_path,
) -> None:
    """Two indexes of the same UUID resolve to the SAME path so the catalog
    by_file_path lookup dedups on re-index (HIGH-1 idempotency)."""
    from nexus.commands.dt import _index_dt_content_record

    with patch("nexus.config.catalog_path", return_value=tmp_path):
        _index_dt_content_record("U9", collection="c", corpus="dt")
        _index_dt_content_record("U9", collection="c", corpus="dt")
    p1 = mock_index.call_args_list[0].args[0]
    p2 = mock_index.call_args_list[1].args[0]
    assert p1 == p2 == tmp_path / ".dt-content" / "U9.md"


def test_index_dt_content_rejects_non_dt_source() -> None:
    from nexus.commands.dt import _index_dt_content_record

    with pytest.raises(ValueError, match="not a DT source"):
        _index_dt_content_record(
            "U1", collection="c", corpus="dt", extraction_source="file",
        )


# ── nx dt index --dt-content routing ────────────────────────────────────────

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_gather(monkeypatch):
    """Stub ``_gather_records`` so routing tests don't touch DT selectors."""
    records: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "nexus.commands.dt._gather_records", lambda **kw: records,
    )
    return records


class TestDtContentRouting:
    def test_flag_routes_unsupported_record_to_dt_content(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/clip.webarchive"))
        monkeypatch.setattr(
            "nexus.mcp_client.devonthink.available", lambda **k: True,
        )
        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record",
            lambda uuid, **kw: calls.append(uuid) or True,
        )
        result = runner.invoke(
            main, ["dt", "index", "--selection", "--dt-content"],
        )
        assert result.exit_code == 0, result.output
        assert calls == ["U1"]
        assert "1 from DT content" in result.output

    def test_no_flag_skips_unsupported_record(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/clip.webarchive"))
        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record",
            lambda uuid, **kw: calls.append(uuid) or True,
        )
        result = runner.invoke(main, ["dt", "index", "--selection"])
        assert result.exit_code == 0, result.output
        assert calls == []
        assert "1 skipped" in result.output
        assert "from DT content" not in result.output

    def test_flag_with_dt_unavailable_skips(
        self, runner, fake_gather, monkeypatch,
    ):
        from nexus.cli import main

        fake_gather.append(("U1", "/clip.webarchive"))
        monkeypatch.setattr(
            "nexus.mcp_client.devonthink.available", lambda **k: False,
        )
        calls: list[str] = []
        monkeypatch.setattr(
            "nexus.commands.dt._index_dt_content_record",
            lambda uuid, **kw: calls.append(uuid) or True,
        )
        result = runner.invoke(
            main, ["dt", "index", "--selection", "--dt-content"],
        )
        assert result.exit_code == 0, result.output
        assert calls == []  # Gap 0: DT down -> exact skip behaviour
        assert "1 skipped" in result.output
