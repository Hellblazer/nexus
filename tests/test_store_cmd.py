# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
    monkeypatch.setenv("CHROMA_TENANT", "test-tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")


@pytest.fixture
def mock_store(env_creds):
    db = MagicMock()
    with patch("nexus.commands.store._t3", return_value=db):
        yield db


@pytest.fixture
def mock_collection(env_creds):
    db = MagicMock()
    with patch("nexus.commands.collection._t3", return_value=db):
        yield db


@pytest.fixture
def mock_search(env_creds):
    db = MagicMock()
    with patch("nexus.commands.search_cmd._t3", return_value=db):
        yield db


def _search_result(content="chunk", **overrides):
    base = {"id": "abc1", "content": content, "distance": 0.1,
            "source_path": "./sec.md", "line_start": 1}
    base.update(overrides)
    return base


def _store_entry(id="aabbccdd1234", title="doc.md", tags="", ttl_days=0,
                 expires_at="", indexed_at="2026-02-22T00:00:00+00:00"):
    return {"id": id, "title": title, "tags": tags, "ttl_days": ttl_days,
            "expires_at": expires_at, "indexed_at": indexed_at}


# ── _t3() factory error paths ───────────────────────────────────────────────

@pytest.mark.parametrize("missing_key,present,cred_override,expect", [
    ("CHROMA_API_KEY", {"VOYAGE_API_KEY": "vk", "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d"},
     {"chroma_database": "d", "voyage_api_key": "vk", "chroma_api_key": ""}, "chroma_api_key"),
    ("VOYAGE_API_KEY", {"CHROMA_API_KEY": "ck", "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d"},
     None, "voyage_api_key"),
    ("CHROMA_DATABASE", {"CHROMA_API_KEY": "ck", "VOYAGE_API_KEY": "vk"},
     None, "chroma_database"),
])
def test_store_put_missing_credential(runner, monkeypatch, tmp_path,
                                      missing_key, present, cred_override, expect):
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.delenv(missing_key, raising=False)
    if "CHROMA_TENANT" not in present:
        monkeypatch.delenv("CHROMA_TENANT", raising=False)
    for k, v in present.items():
        monkeypatch.setenv(k, v)
    src = tmp_path / "f.txt"
    src.write_text("content")
    if cred_override:
        with patch("nexus.config.get_credential", side_effect=lambda k: cred_override.get(k, "")):
            result = runner.invoke(main, ["store", "put", str(src)])
    else:
        result = runner.invoke(main, ["store", "put", str(src)])
    assert result.exit_code != 0
    assert expect in result.output.lower()


def test_store_put_tenant_optional(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    monkeypatch.setenv("CHROMA_DATABASE", "mydb")
    src = tmp_path / "f.txt"
    src.write_text("content")
    with patch("nexus.commands.store._t3") as mt3:
        db = MagicMock()
        db.__enter__ = MagicMock(return_value=db)
        db.__exit__ = MagicMock(return_value=False)
        db.put.return_value = "doc-id-1"
        mt3.return_value = db
        result = runner.invoke(main, ["store", "put", str(src), "--title", "test"])
    assert result.exit_code == 0, result.output


# ── nx store put ─────────────────────────────────────────────────────────────

def test_store_put_stdin_requires_title(runner, mock_store):
    result = runner.invoke(main, ["store", "put", "-"], input="some content")
    assert result.exit_code != 0
    assert "--title" in result.output


def test_store_put_stdin_with_title_succeeds(runner, mock_store):
    mock_store.put.return_value = "doc-id-abc"
    result = runner.invoke(main, ["store", "put", "-", "--title", "my-title.md"], input="content here")
    assert result.exit_code == 0
    assert "doc-id-abc" in result.output
    mock_store.put.assert_called_once()
    kw = mock_store.put.call_args.kwargs
    assert kw["title"] == "my-title.md"
    assert kw["content"] == "content here"


def test_store_put_file_uses_filename_as_title(runner, mock_store, tmp_path):
    src = tmp_path / "analysis.md"
    src.write_text("finding: important")
    mock_store.put.return_value = "doc-id-xyz"
    result = runner.invoke(main, ["store", "put", str(src)])
    assert result.exit_code == 0
    assert "doc-id-xyz" in result.output
    assert mock_store.put.call_args.kwargs["title"] == "analysis.md"


def test_store_put_file_not_found(runner, mock_store):
    result = runner.invoke(main, ["store", "put", "/no/such/file.txt"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "File not found" in result.output


def test_store_put_invalid_ttl_shows_error(runner, mock_store, tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("content")
    result = runner.invoke(main, ["store", "put", str(src), "--ttl", "5z"])
    assert result.exit_code != 0
    assert "5z" in result.output


# ── nx store list ────────────────────────────────────────────────────────────

def test_store_list_empty_collection(runner, mock_store):
    mock_store.list_store.return_value = []
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "No entries" in result.output
    mock_store.list_store.assert_called_once()


def test_store_list_shows_entries_and_tags(runner, mock_store):
    mock_store.list_store.return_value = [
        _store_entry(id="abc123def456", title="analysis.md", tags="security,audit"),
        _store_entry(id="fff000aaa111", title="temp-notes.md", ttl_days=30,
                     expires_at="2026-03-24T10:00:00+00:00", indexed_at="2026-02-22T11:00:00+00:00"),
        _store_entry(tags="arch,decision"),
    ]
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    for text in ("abc123def456", "analysis.md", "permanent", "fff000aaa111",
                 "temp-notes.md", "2026-03-24", "security,audit", "arch,decision"):
        assert text in result.output


def test_store_list_custom_collection(runner, mock_store):
    mock_store.list_store.return_value = []
    runner.invoke(main, ["store", "list", "--collection", "knowledge__notes"])
    assert mock_store.list_store.call_args[0][0] == "knowledge__notes"


def test_store_list_limit_flag(runner, mock_store):
    mock_store.list_store.return_value = []
    runner.invoke(main, ["store", "list", "--limit", "10"])
    ca = mock_store.list_store.call_args
    assert ca[1].get("limit") == 10 or ca[0][1] == 10


def test_store_list_shows_16char_ids(runner, mock_store):
    mock_store.list_store.return_value = [_store_entry(id="abcdef1234567890ff")]
    result = runner.invoke(main, ["store", "list"])
    assert result.exit_code == 0
    assert "abcdef1234567890" in result.output


def test_store_expire_reports_count(runner, mock_store):
    mock_store.expire.return_value = 3
    result = runner.invoke(main, ["store", "expire"])
    assert result.exit_code == 0
    assert "3" in result.output


# ── nx collection ────────────────────────────────────────────────────────────

def test_collection_list_empty(runner, mock_collection):
    mock_collection.list_collections.return_value = []
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    assert "No collections" in result.output


def test_collection_list_shows_names_and_counts(runner, mock_collection):
    mock_collection.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42}, {"name": "knowledge__sec", "count": 7}]
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0
    for text in ("code__myrepo", "42", "knowledge__sec", "7"):
        assert text in result.output


@pytest.mark.parametrize("subcmd,args", [("info", ["no-such-collection"]), ("verify", ["missing"])])
def test_collection_not_found(runner, mock_collection, subcmd, args):
    mock_collection.list_collections.return_value = []
    result = runner.invoke(main, ["collection", subcmd] + args)
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


@pytest.mark.parametrize("flag", ["--yes", "--confirm"])
def test_collection_delete_with_flag(runner, mock_collection, flag):
    result = runner.invoke(main, ["collection", "delete", "knowledge__test", flag])
    assert result.exit_code == 0, result.output
    mock_collection.delete_collection.assert_called_once_with("knowledge__test")


def test_collection_delete_without_yes_prompts(runner, mock_collection):
    runner.invoke(main, ["collection", "delete", "knowledge__test"], input="n\n")
    mock_collection.delete_collection.assert_not_called()


# ── nx search ────────────────────────────────────────────────────────────────

def test_search_no_matching_corpus(runner, mock_search):
    mock_search.list_collections.return_value = []
    result = runner.invoke(main, ["search", "my query", "--corpus", "code"])
    assert result.exit_code == 0
    assert "no matching collections" in result.output.lower()


def test_search_no_results(runner, mock_search):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 5}]
    mock_search.search.return_value = []
    result = runner.invoke(main, ["search", "my query", "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_displays_results(runner, mock_search):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_search.search.return_value = [
        _search_result(content="security finding here", id="abc12345-0000",
                       distance=0.123, title="sec.md", tags="security")]
    result = runner.invoke(main, ["search", "security", "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert "security finding here" in result.output


@pytest.mark.parametrize("content_flag,content_text,expect_indented", [
    (True, "UNIQUE_CHUNK_BODY", True),
    (False, "Unique chunk text that only appears when content flag is set.", False),
])
def test_search_content_flag_presence(runner, mock_search, content_flag, content_text, expect_indented):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    mock_search.search.return_value = [_search_result(content=content_text)]
    args = ["search", "security", "--corpus", "knowledge"]
    if content_flag:
        args.append("--content")
    result = runner.invoke(main, args)
    assert result.exit_code == 0
    indented = [ln for ln in result.output.splitlines() if ln.startswith("  ") and content_text in ln]
    assert bool(indented) == expect_indented


@pytest.mark.parametrize("text,expect_ellipsis,max_len", [
    ("A" * 300, True, 210), ("Short enough.", False, None)])
def test_search_content_flag_truncation(runner, mock_search, text, expect_ellipsis, max_len):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    mock_search.search.return_value = [_search_result(content=text)]
    result = runner.invoke(main, ["search", "query", "--corpus", "knowledge", "--content"])
    assert result.exit_code == 0
    indented = [ln for ln in result.output.splitlines() if ln.startswith("  ")]
    assert indented
    assert indented[0].endswith("...") == expect_ellipsis
    if max_len:
        assert len(indented[0]) <= max_len
    if not expect_ellipsis:
        assert text in indented[0]


# ── [path] positional argument ───────────────────────────────────────────────

def test_search_path_scopes_where_filter(runner, mock_search, tmp_path):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_search.search.return_value = []
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    result = runner.invoke(main, ["search", "query", str(src_dir), "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert mock_search.search.called
    assert "$startswith" not in str(mock_search.search.call_args.kwargs.get("where"))


def test_search_path_filters_results_by_file_path(runner, mock_search, tmp_path):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    def fake_search(query, collection_names, n_results=10, where=None):
        return [
            _search_result(content="inside src", id="r1",
                           source_path=str(src_dir / "file.py"), file_path=str(src_dir / "file.py")),
            _search_result(content="outside src", id="r2", distance=0.2,
                           source_path=str(other_dir / "file.py"), file_path=str(other_dir / "file.py")),
        ]

    mock_search.search.side_effect = fake_search
    result = runner.invoke(main, ["search", "query", str(src_dir), "--corpus", "knowledge"])
    assert result.exit_code == 0
    assert "inside src" in result.output
    assert "outside src" not in result.output


def test_search_no_path_returns_all(runner, mock_search):
    mock_search.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_search.search.return_value = [_search_result(content="result one")]
    result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])
    assert result.exit_code == 0
    ca = mock_search.search.call_args
    where_filter = ca.kwargs.get("where") if ca.kwargs else None
    if where_filter is None and ca.args and len(ca.args) > 3:
        where_filter = ca.args[3]
    assert where_filter is None


# ── nx store delete ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("found,exit_ok,expect_text", [(True, True, "Deleted"), (False, False, "not found")])
def test_store_delete_by_id(runner, mock_store, found, exit_ok, expect_text):
    mock_store.delete_by_id.return_value = found
    result = runner.invoke(main, ["store", "delete", "--collection", "knowledge", "--id", "abcdef1234567890"])
    assert (result.exit_code == 0) == exit_ok, result.output
    assert expect_text in result.output


@pytest.mark.parametrize("ids,exit_ok,expect_text", [
    (["id1", "id2"], True, "Deleted 2"), ([], False, "not found")])
def test_store_delete_by_title(runner, mock_store, ids, exit_ok, expect_text):
    mock_store.find_ids_by_title.return_value = ids
    result = runner.invoke(main, ["store", "delete", "--collection", "knowledge",
                                  "--title", "doc.md", "--yes"])
    if exit_ok:
        assert result.exit_code == 0, result.output
        mock_store.batch_delete.assert_called_once()
    else:
        assert result.exit_code != 0
    assert expect_text in result.output or "No entries" in result.output


def test_store_delete_missing_collection_rejected(runner, env_creds):
    result = runner.invoke(main, ["store", "delete", "--id", "abc"])
    assert result.exit_code != 0


# ── nx store get ─────────────────────────────────────────────────────────────

def test_store_get_happy(runner, mock_store):
    mock_store.get_by_id.return_value = {
        "id": "abcdef1234567890", "content": "Important knowledge content here",
        "title": "finding.md", "tags": "arch,review", "indexed_at": "2026-03-09T10:00:00+00:00"}
    result = runner.invoke(main, ["store", "get", "abcdef1234567890"])
    assert result.exit_code == 0, result.output
    for text in ("abcdef1234567890", "finding.md", "arch,review", "Important knowledge content here"):
        assert text in result.output


def test_store_get_not_found(runner, mock_store):
    mock_store.get_by_id.return_value = None
    result = runner.invoke(main, ["store", "get", "nonexistent12345"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_store_get_json_output(runner, mock_store):
    mock_store.get_by_id.return_value = {
        "id": "abcdef1234567890", "content": "test content",
        "title": "doc.md", "tags": "test", "indexed_at": "2026-03-09"}
    result = runner.invoke(main, ["store", "get", "abcdef1234567890", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == "abcdef1234567890"
    assert data["content"] == "test content"


def test_store_get_custom_collection(runner, mock_store):
    mock_store.get_by_id.return_value = {"id": "abc123", "content": "x", "title": "t"}
    runner.invoke(main, ["store", "get", "abc123", "-c", "code__myrepo"])
    mock_store.get_by_id.assert_called_once_with("code__myrepo", "abc123")
