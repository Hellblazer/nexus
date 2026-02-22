"""CLI-layer tests for nx store, nx search, and nx collection commands."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required credential env vars."""
    monkeypatch.setenv("CHROMA_API_KEY", "test-chroma-key")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
    monkeypatch.setenv("CHROMA_TENANT", "test-tenant")
    monkeypatch.setenv("CHROMA_DATABASE", "test-db")


# ── _t3() factory error paths ─────────────────────────────────────────────────

def test_store_put_missing_chroma_api_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    src = tmp_path / "f.txt"
    src.write_text("content")

    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    assert "CHROMA_API_KEY" in result.output


def test_store_put_missing_voyage_api_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    src = tmp_path / "f.txt"
    src.write_text("content")

    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    assert "VOYAGE_API_KEY" in result.output


def test_store_put_missing_tenant(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "ck")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk")
    monkeypatch.delenv("CHROMA_TENANT", raising=False)
    monkeypatch.delenv("CHROMA_DATABASE", raising=False)
    src = tmp_path / "f.txt"
    src.write_text("content")

    result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code != 0
    assert "tenant" in result.output.lower() or "database" in result.output.lower()


# ── nx store put ──────────────────────────────────────────────────────────────

def test_store_put_stdin_requires_title(
    runner: CliRunner, env_creds, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: stdin input without --title is rejected with a clear error."""
    with patch("nexus.commands.store._t3"):
        result = runner.invoke(main, ["store", "put", "-"], input="some content")

    assert result.exit_code != 0
    assert "--title" in result.output


def test_store_put_stdin_with_title_succeeds(
    runner: CliRunner, env_creds
) -> None:
    """stdin input with --title stores the document."""
    mock_db = MagicMock()
    mock_db.put.return_value = "doc-id-abc"

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["store", "put", "-", "--title", "my-title.md"], input="content here"
        )

    assert result.exit_code == 0
    assert "doc-id-abc" in result.output
    mock_db.put.assert_called_once()
    call_kwargs = mock_db.put.call_args.kwargs
    assert call_kwargs["title"] == "my-title.md"
    assert call_kwargs["content"] == "content here"


def test_store_put_file_uses_filename_as_title(
    runner: CliRunner, env_creds, tmp_path
) -> None:
    """File store uses the filename as the default title."""
    src = tmp_path / "analysis.md"
    src.write_text("finding: important")
    mock_db = MagicMock()
    mock_db.put.return_value = "doc-id-xyz"

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "put", str(src)])

    assert result.exit_code == 0
    assert "doc-id-xyz" in result.output
    call_kwargs = mock_db.put.call_args.kwargs
    assert call_kwargs["title"] == "analysis.md"


def test_store_put_file_not_found(
    runner: CliRunner, env_creds
) -> None:
    with patch("nexus.commands.store._t3"):
        result = runner.invoke(main, ["store", "put", "/no/such/file.txt"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "File not found" in result.output


def test_store_put_invalid_ttl_shows_error(
    runner: CliRunner, env_creds, tmp_path
) -> None:
    """Invalid TTL format is rejected with a clear CLI error (not a traceback)."""
    src = tmp_path / "f.txt"
    src.write_text("content")

    with patch("nexus.commands.store._t3"):
        result = runner.invoke(main, ["store", "put", str(src), "--ttl", "5z"])

    assert result.exit_code != 0
    assert "5z" in result.output


# ── nx store list ─────────────────────────────────────────────────────────────

def test_store_list_empty_collection(runner: CliRunner, env_creds) -> None:
    """No entries in collection prints a friendly 'No entries' message."""
    mock_db = MagicMock()
    mock_db.list_store.return_value = []

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "list"])

    assert result.exit_code == 0
    assert "No entries" in result.output
    mock_db.list_store.assert_called_once()


def test_store_list_shows_entries(runner: CliRunner, env_creds) -> None:
    """Entries are displayed with id, title, ttl, and indexed date."""
    mock_db = MagicMock()
    mock_db.list_store.return_value = [
        {
            "id": "abc123def456",
            "title": "analysis.md",
            "tags": "security,audit",
            "ttl_days": 0,
            "expires_at": "",
            "indexed_at": "2026-02-22T10:00:00+00:00",
        },
        {
            "id": "fff000aaa111",
            "title": "temp-notes.md",
            "tags": "",
            "ttl_days": 30,
            "expires_at": "2026-03-24T10:00:00+00:00",
            "indexed_at": "2026-02-22T11:00:00+00:00",
        },
    ]

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "list"])

    assert result.exit_code == 0
    assert "abc123def456" in result.output
    assert "analysis.md" in result.output
    assert "permanent" in result.output
    assert "fff000aaa111" in result.output
    assert "temp-notes.md" in result.output
    assert "2026-03-24" in result.output


def test_store_list_shows_tags(runner: CliRunner, env_creds) -> None:
    """Tags are shown in brackets when present."""
    mock_db = MagicMock()
    mock_db.list_store.return_value = [
        {
            "id": "aabbccdd1234",
            "title": "doc.md",
            "tags": "arch,decision",
            "ttl_days": 0,
            "expires_at": "",
            "indexed_at": "2026-02-22T00:00:00+00:00",
        }
    ]

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "list"])

    assert result.exit_code == 0
    assert "arch,decision" in result.output


def test_store_list_custom_collection(runner: CliRunner, env_creds) -> None:
    """--collection flag selects the right collection name."""
    mock_db = MagicMock()
    mock_db.list_store.return_value = []

    with patch("nexus.commands.store._t3", return_value=mock_db):
        runner.invoke(main, ["store", "list", "--collection", "knowledge__notes"])

    call_args = mock_db.list_store.call_args
    assert call_args[0][0] == "knowledge__notes"


def test_store_list_limit_flag(runner: CliRunner, env_creds) -> None:
    """--limit is forwarded to list_store."""
    mock_db = MagicMock()
    mock_db.list_store.return_value = []

    with patch("nexus.commands.store._t3", return_value=mock_db):
        runner.invoke(main, ["store", "list", "--limit", "10"])

    call_args = mock_db.list_store.call_args
    assert call_args[1].get("limit") == 10 or call_args[0][1] == 10


def test_store_expire_reports_count(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.expire.return_value = 3

    with patch("nexus.commands.store._t3", return_value=mock_db):
        result = runner.invoke(main, ["store", "expire"])

    assert result.exit_code == 0
    assert "3" in result.output


# ── nx collection ─────────────────────────────────────────────────────────────

def test_collection_list_empty(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])

    assert result.exit_code == 0
    assert "No collections" in result.output


def test_collection_list_shows_names_and_counts(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [
        {"name": "code__myrepo", "count": 42},
        {"name": "knowledge__sec", "count": 7},
    ]

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "list"])

    assert result.exit_code == 0
    assert "code__myrepo" in result.output
    assert "42" in result.output
    assert "knowledge__sec" in result.output
    assert "7" in result.output


def test_collection_info_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "info", "no-such-collection"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_collection_verify_not_found(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "verify", "missing"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── nx search ────────────────────────────────────────────────────────────────

def test_search_no_matching_corpus(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "my query", "--corpus", "code"])

    assert result.exit_code == 0
    assert "No matching collections" in result.output


def test_search_no_results(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 5}]
    mock_db.search.return_value = []

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "my query", "--corpus", "knowledge"])

    assert result.exit_code == 0
    assert "No results" in result.output


def test_search_displays_results(runner: CliRunner, env_creds) -> None:
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_db.search.return_value = [
        {
            "id": "abc12345-0000-0000-0000-000000000000",
            "content": "security finding here",
            "distance": 0.123,
            "title": "sec.md",
            "tags": "security",
            "source_path": "./sec.md",
            "line_start": 1,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "security", "--corpus", "knowledge"])

    assert result.exit_code == 0
    assert "security finding here" in result.output


# ── nexus-ani: collection delete --yes flag ───────────────────────────────────

def test_collection_delete_yes_skips_prompt(runner: CliRunner, env_creds) -> None:
    """--yes flag skips interactive confirmation."""
    mock_db = MagicMock()

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__test", "--yes"])

    assert result.exit_code == 0, result.output
    mock_db.delete_collection.assert_called_once_with("knowledge__test")
    assert "Deleted" in result.output


def test_collection_delete_without_yes_prompts(runner: CliRunner, env_creds) -> None:
    """Without --yes, a confirmation prompt is shown (user declines via 'n')."""
    mock_db = MagicMock()

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__test"], input="n\n")

    # User said no → aborted, collection NOT deleted
    mock_db.delete_collection.assert_not_called()


def test_collection_delete_confirm_flag_alias(runner: CliRunner, env_creds) -> None:
    """--confirm is a supported alias for --yes and skips the confirmation prompt."""
    mock_db = MagicMock()

    with patch("nexus.commands.collection._t3", return_value=mock_db):
        result = runner.invoke(main, ["collection", "delete", "knowledge__test", "--confirm"])

    assert result.exit_code == 0
    mock_db.delete_collection.assert_called_once_with("knowledge__test")


# ── nexus-2pw: --content flag ─────────────────────────────────────────────────

def test_search_content_flag_shows_chunk_text(runner: CliRunner, env_creds) -> None:
    """--content flag prints matched chunk text as a separate indented line under each result."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    # Use a single-line chunk so format_plain emits exactly one result line;
    # the --content flag should then emit a second indented line below it.
    mock_db.search.return_value = [
        {
            "id": "abc1",
            "content": "UNIQUE_CHUNK_BODY",
            "distance": 0.1,
            "source_path": "./sec.md",
            "line_start": 5,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["search", "security", "--corpus", "knowledge", "--content"]
        )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    # The indented content line must start with two spaces
    indented = [ln for ln in lines if ln.startswith("  ") and "UNIQUE_CHUNK_BODY" in ln]
    assert indented, (
        f"Expected an indented line containing 'UNIQUE_CHUNK_BODY'. Got output:\n{result.output}"
    )


def test_search_content_flag_absent_no_chunk_text(runner: CliRunner, env_creds) -> None:
    """Without --content flag, chunk text is NOT printed inline."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    chunk_text = "Unique chunk text that only appears when content flag is set."
    mock_db.search.return_value = [
        {
            "id": "abc2",
            "content": chunk_text,
            "distance": 0.1,
            "source_path": "./sec.md",
            "line_start": 5,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["search", "security", "--corpus", "knowledge"]
        )

    # Without --content the plain formatter emits path:line:content, so
    # the text IS in the output (format_plain embeds it).  What must NOT
    # happen is an extra indented copy appearing below the result line.
    lines = result.output.splitlines()
    indented = [ln for ln in lines if ln.startswith("  ") and chunk_text in ln]
    assert indented == [], "No indented content line should appear without --content"


def test_search_content_flag_truncates_long_text(runner: CliRunner, env_creds) -> None:
    """--content truncates chunk text at ~200 chars and appends '...'."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    long_content = "A" * 300  # well over 200 chars
    mock_db.search.return_value = [
        {
            "id": "abc3",
            "content": long_content,
            "distance": 0.1,
            "source_path": "./long.md",
            "line_start": 1,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["search", "query", "--corpus", "knowledge", "--content"]
        )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    indented = [ln for ln in lines if ln.startswith("  ")]
    assert indented, "Expected indented content line"
    content_line = indented[0]
    # Must end with ellipsis and not be longer than ~205 chars (200 + "  " + "...")
    assert content_line.endswith("..."), f"Expected '...' suffix, got: {content_line!r}"
    assert len(content_line) <= 210, f"Content line too long: {len(content_line)}"


def test_search_content_flag_short_text_no_ellipsis(runner: CliRunner, env_creds) -> None:
    """--content does NOT add '...' when chunk text is 200 chars or fewer."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 1}]
    short_content = "Short enough."
    mock_db.search.return_value = [
        {
            "id": "abc4",
            "content": short_content,
            "distance": 0.1,
            "source_path": "./short.md",
            "line_start": 1,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["search", "query", "--corpus", "knowledge", "--content"]
        )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    indented = [ln for ln in lines if ln.startswith("  ")]
    assert indented, "Expected indented content line"
    assert not indented[0].endswith("..."), "Short text should not have ellipsis"
    assert short_content in indented[0]


# ── nexus-u4e: [path] positional argument ─────────────────────────────────────

def test_search_path_scopes_where_filter(runner: CliRunner, env_creds, tmp_path) -> None:
    """[path] argument applies Python-side path filtering; $startswith must NOT be sent to ChromaDB."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_db.search.return_value = []  # empty is fine; we test the where kwarg

    src_dir = tmp_path / "src"
    src_dir.mkdir()

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["search", "query", str(src_dir), "--corpus", "knowledge"]
        )

    assert result.exit_code == 0
    assert mock_db.search.called, "Expected at least one search() call"
    # Path scoping is Python-side after retrieval; $startswith must not reach ChromaDB
    actual_call = mock_db.search.call_args
    where_filter = actual_call.kwargs.get("where")
    assert "$startswith" not in str(where_filter), (
        f"$startswith must not be passed to ChromaDB (invalid operator); got where={where_filter}"
    )


def test_search_path_filters_results_by_file_path(runner: CliRunner, env_creds, tmp_path) -> None:
    """Two chunks at different paths: scoped search returns only the matching one."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()

    # The mock returns both results; Python-side path filtering in search_cmd
    # keeps only the result whose file_path / source_path starts with src_dir.
    def fake_search(query, collection_names, n_results=10, where=None):
        return [
            {
                "id": "r1",
                "content": "inside src",
                "distance": 0.1,
                "source_path": str(src_dir / "file.py"),
                "line_start": 1,
                "file_path": str(src_dir / "file.py"),
            },
            {
                "id": "r2",
                "content": "outside src",
                "distance": 0.2,
                "source_path": str(other_dir / "file.py"),
                "line_start": 1,
                "file_path": str(other_dir / "file.py"),
            },
        ]

    mock_db.search.side_effect = fake_search

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(
            main, ["search", "query", str(src_dir), "--corpus", "knowledge"]
        )

    assert result.exit_code == 0
    assert "inside src" in result.output
    assert "outside src" not in result.output


def test_search_no_path_returns_all(runner: CliRunner, env_creds) -> None:
    """Without [path], search() is called without a where filter (None)."""
    mock_db = MagicMock()
    mock_db.list_collections.return_value = [{"name": "knowledge__sec", "count": 2}]
    mock_db.search.return_value = [
        {
            "id": "r1",
            "content": "result one",
            "distance": 0.1,
            "source_path": "./a.py",
            "line_start": 1,
        }
    ]

    with patch("nexus.commands.search_cmd._t3", return_value=mock_db):
        result = runner.invoke(main, ["search", "query", "--corpus", "knowledge"])

    assert result.exit_code == 0
    actual_call = mock_db.search.call_args
    where_filter = actual_call.kwargs.get("where") if actual_call.kwargs else None
    if where_filter is None and actual_call.args and len(actual_call.args) > 3:
        where_filter = actual_call.args[3]
    assert where_filter is None, (
        f"Expected no where filter when path is absent, got: {where_filter}"
    )
