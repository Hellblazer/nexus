# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (nexus-rfvyy.8), item 2: ``nx store get`` / ``list`` / ``list --docs`` and the
demoted MCP ``collection_info`` sample under the output posture.

Q2: what a CLI verb prints under Claude Code reaches the session model, so a sensitive note is the one-line stub
(counted) there and carries a ``[sensitive]`` badge in a terminal. An unreadable catalog makes every row sensitive.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.catalog.store_hook import DocumentListing
from nexus.cli import main
from nexus.context_annotations import SENSITIVE_READER_CLAUSE
from nexus.sensitive_output import BADGE, stub_line
from tests.test_store_cmd import env_creds, mock_store, runner  # noqa: F401 - fixtures

CANARY = "CANARY-p3fix-storecli-4417"
SECRET_TITLE = f"{CANARY} board minutes"
BODY = f"{CANARY} the body of the secret note"
CH_S = "a" * 64
CH_C = "b" * 64
COLLECTION = "knowledge__test__voyage-context-3__v1"


def _owner(tumbler: str, title: str, *, sensitive: bool) -> SimpleNamespace:
    return SimpleNamespace(tumbler=tumbler, title=title, sensitive=sensitive, meta={}, indexed_at="2026-11-01", alias_of="")


def _rows() -> list[dict]:
    return [
        {"id": CH_C, "title": "Gardening", "indexed_at": "2026-11-01"},
        {"id": CH_S, "title": SECRET_TITLE, "indexed_at": "2026-11-01", "sensitive": True},
    ]


def _owners() -> dict[str, list]:
    return {CH_C: [_owner("1.3.1", "Gardening", sensitive=False)], CH_S: [_owner("1.2.7", SECRET_TITLE, sensitive=True)]}


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.fixture
def claude_code(terminal, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")


def _list_args(mock_store, rows: list[dict]) -> list[str]:
    mock_store.list_store.return_value = rows
    mock_store.collection_info.return_value = {"count": len(rows)}
    return ["store", "list", "-c", COLLECTION]


def test_store_list_stubs_the_sensitive_row_under_claude_code(mock_store, runner, claude_code) -> None:
    args = _list_args(mock_store, _rows())
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=_owners()):
        out = runner.invoke(main, args).output
    assert CANARY not in out and CH_S not in out
    assert stub_line("1.2.7") in out and "1 sensitive hit withheld" in out
    assert "Gardening" in out


def test_store_list_withholds_every_row_when_the_catalog_cannot_be_read(mock_store, runner, claude_code) -> None:
    args = _list_args(mock_store, _rows())
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=None):
        out = runner.invoke(main, args).output
    assert CANARY not in out and "Gardening" not in out
    assert out.count("content withheld") == 2


def test_store_list_in_a_terminal_badges_the_sensitive_row(mock_store, runner, terminal) -> None:
    args = _list_args(mock_store, _rows())
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=_owners()):
        out = runner.invoke(main, args).output
    assert SECRET_TITLE[:40] in out and SENSITIVE_READER_CLAUSE in out
    body = out.replace(SENSITIVE_READER_CLAUSE, "")
    assert [ln for ln in body.splitlines() if BADGE in ln] == [next(ln for ln in body.splitlines() if CH_S in ln)]


def test_store_list_docs_stubs_and_sorts_a_sensitive_document_last(mock_store, runner, claude_code) -> None:
    mock_store.collection_info.return_value = {"count": 5}
    docs = [
        DocumentListing(key="1.2.7", title=SECRET_TITLE, handle=CH_S, chunks=3, entry={}, indexed_at="2026-11-01", sensitive=True),
        DocumentListing(key="1.3.1", title="Gardening", handle=CH_C, chunks=2, entry={}, indexed_at="2026-11-01"),
    ]
    with patch("nexus.catalog.store_hook.group_documents", return_value=(docs, "")):
        out = runner.invoke(main, ["store", "list", "--docs", "-c", COLLECTION]).output
    lines = out.splitlines()
    assert CANARY not in out and CH_S not in out
    assert next(i for i, ln in enumerate(lines) if "Gardening" in ln) < next(i for i, ln in enumerate(lines) if stub_line("1.2.7") in ln)
    assert "1 sensitive hit withheld" in out


@pytest.fixture(autouse=True)
def _no_split_note_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx store get`` asks the catalog whether the note was split; no tenant is minted for that here."""
    monkeypatch.setattr("nexus.commands.store._split_note_text", lambda *_a, **_k: None)


def _get_args(mock_store, entry: dict) -> list[str]:
    mock_store.get_by_id.return_value = entry
    return ["store", "get", CH_S, "-c", COLLECTION]


def test_store_get_withholds_a_sensitive_note_under_claude_code(mock_store, runner, claude_code) -> None:
    args = _get_args(mock_store, {"id": CH_S, "content": BODY, "title": SECRET_TITLE, "sensitive": True})
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value={CH_S: [_owner("1.2.7", SECRET_TITLE, sensitive=True)]}):
        out = runner.invoke(main, args).output
        js = runner.invoke(main, [*args, "--json"]).output
    for text in (out, js):
        assert CANARY not in text and CH_S not in text
        assert stub_line("1.2.7") in text


def test_store_get_withholds_when_the_catalog_cannot_be_read(mock_store, runner, claude_code) -> None:
    args = _get_args(mock_store, {"id": CH_S, "content": BODY, "title": SECRET_TITLE})
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value=None):
        out = runner.invoke(main, args).output
    assert CANARY not in out and "content withheld" in out


def test_store_get_in_a_terminal_prints_the_note_with_a_badge(mock_store, runner, terminal) -> None:
    args = _get_args(mock_store, {"id": CH_S, "content": BODY, "title": SECRET_TITLE, "sensitive": True})
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value={CH_S: [_owner("1.2.7", SECRET_TITLE, sensitive=True)]}):
        out = runner.invoke(main, args).output
    assert BODY in out and out.splitlines()[0] == BADGE and SENSITIVE_READER_CLAUSE in out


def test_store_get_of_a_clean_note_is_unchanged_under_claude_code(mock_store, runner, claude_code) -> None:
    args = _get_args(mock_store, {"id": CH_S, "content": "plain body", "title": "Plain", "sensitive": False})
    with patch("nexus.catalog.store_hook.live_owner_entries", return_value={CH_S: [_owner("1.3.1", "Plain", sensitive=False)]}):
        out = runner.invoke(main, args).output
    assert "plain body" in out and "withheld" not in out and BADGE not in out


# ── the demoted MCP collection_info sample ───────────────────────────────────


def test_mcp_collection_info_stubs_a_sensitive_sample_title() -> None:
    from nexus.mcp.core import collection_info
    from nexus.mcp_server import _inject_t3, _reset_singletons

    t3 = MagicMock()
    t3.collection_info.return_value = {"count": 2, "metadata": {}}
    t3.list_store.return_value = _rows()
    _inject_t3(t3)
    try:
        with patch("nexus.catalog.store_hook.live_owner_entries", return_value=_owners()):
            out = collection_info(COLLECTION)
        with patch("nexus.catalog.store_hook.live_owner_entries", return_value=None):
            down = collection_info(COLLECTION)
    finally:
        _reset_singletons()
    assert CANARY not in out and "Gardening" in out and stub_line("1.2.7") in out
    assert CANARY not in down and "Gardening" not in down
