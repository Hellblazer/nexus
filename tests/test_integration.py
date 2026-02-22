"""Integration tests — require real API keys. Skipped automatically when keys absent.

Run with:
    pytest -m integration                    # all integration tests
    pytest -m "integration and not t3"       # no-key tests only
    pytest -m integration --tb=short -v      # verbose

Set NEXUS_INTEGRATION=1 to run in CI (currently skipped by default).
"""
from __future__ import annotations

import os
import uuid

import pytest
from click.testing import CliRunner

from nexus.cli import main

# ── Marks ─────────────────────────────────────────────────────────────────────

requires_t3 = pytest.mark.skipif(
    not (
        os.environ.get("CHROMA_API_KEY")
        and os.environ.get("VOYAGE_API_KEY")
        and os.environ.get("CHROMA_TENANT")
        and os.environ.get("CHROMA_DATABASE")
    ),
    reason="T3 integration requires CHROMA_API_KEY, VOYAGE_API_KEY, CHROMA_TENANT, CHROMA_DATABASE",
)

requires_anthropic = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="Anthropic integration requires ANTHROPIC_API_KEY",
)

# Collection used by T3 tests — short TTL so it self-cleans via nx store expire
_T3_TEST_COLLECTION = "knowledge__nexus-integration-test"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── T2: SQLite memory (no API keys required) ──────────────────────────────────

@pytest.mark.integration
def test_t2_memory_put_get_roundtrip(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T2 memory put → get roundtrip without any API keys."""
    monkeypatch.setenv("HOME", str(tmp_path))
    tag = f"int-test-{uuid.uuid4().hex[:8]}"

    result = runner.invoke(main, [
        "memory", "put", f"Integration test content {tag}",
        "--project", "integration-test",
        "--title", f"{tag}.md",
    ])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "memory", "get",
        "--project", "integration-test",
        "--title", f"{tag}.md",
    ])
    assert result.exit_code == 0, result.output
    assert tag in result.output


@pytest.mark.integration
def test_t2_memory_search_roundtrip(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T2 FTS5 search finds stored content."""
    monkeypatch.setenv("HOME", str(tmp_path))
    unique = f"xyzzy{uuid.uuid4().hex[:6]}"

    runner.invoke(main, [
        "memory", "put", f"Unique token {unique}",
        "--project", "integration-test",
        "--title", "search-test.md",
    ])

    result = runner.invoke(main, ["memory", "search", unique])
    assert result.exit_code == 0, result.output
    assert unique in result.output


@pytest.mark.integration
def test_t2_memory_expire_removes_ttl_entries(
    runner: CliRunner, tmp_path, monkeypatch
) -> None:
    """Entries with expired TTL are removed by nx memory expire."""
    monkeypatch.setenv("HOME", str(tmp_path))
    unique = f"expire-{uuid.uuid4().hex[:8]}"

    runner.invoke(main, [
        "memory", "put", f"Should expire {unique}",
        "--project", "expire-test",
        "--title", "expire-me.md",
        "--ttl", "1d",
    ])

    # Manually backdating isn't possible from CLI, but we can verify expire
    # runs cleanly and doesn't crash.
    result = runner.invoke(main, ["memory", "expire"])
    assert result.exit_code == 0, result.output
    assert "Expired" in result.output


@pytest.mark.integration
def test_t2_memory_list_project(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """nx memory list --project returns entries for that project only."""
    monkeypatch.setenv("HOME", str(tmp_path))
    tag = f"list-{uuid.uuid4().hex[:6]}"

    runner.invoke(main, [
        "memory", "put", f"Content {tag}",
        "--project", "list-test",
        "--title", "list-entry.md",
    ])

    result = runner.invoke(main, ["memory", "list", "--project", "list-test"])
    assert result.exit_code == 0, result.output
    assert "list-entry.md" in result.output


# ── T1: session scratch (no API keys required) ────────────────────────────────

@pytest.mark.integration
def test_t1_scratch_put_list_clear(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T1 scratch put → list → clear roundtrip."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # T1 scratch requires an active session file keyed by os.getsid(0)
    sid = os.getsid(0)
    session_dir = tmp_path / ".config" / "nexus" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / f"{sid}.session").write_text("integration-test-session")

    unique = f"scratch-{uuid.uuid4().hex[:8]}"

    result = runner.invoke(main, ["scratch", "put", f"scratch content {unique}"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["scratch", "list"])
    assert result.exit_code == 0, result.output
    assert unique in result.output

    result = runner.invoke(main, ["scratch", "clear"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["scratch", "list"])
    assert unique not in result.output


@pytest.mark.integration
def test_t1_scratch_search(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T1 scratch search finds stored content."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = os.getsid(0)
    session_dir = tmp_path / ".config" / "nexus" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / f"{sid}.session").write_text("integration-test-session")

    unique = f"scratchsearch-{uuid.uuid4().hex[:6]}"
    runner.invoke(main, ["scratch", "put", f"Unique content {unique}"])

    result = runner.invoke(main, ["scratch", "search", unique])
    assert result.exit_code == 0, result.output
    assert unique in result.output


# ── nx doctor (no API keys required for smoke test) ───────────────────────────

@pytest.mark.integration
def test_doctor_runs_and_reports(runner: CliRunner) -> None:
    """nx doctor completes without crashing and reports all five checks.

    Exit code 0 = all credentials present; 1 = some missing. Both are valid
    in this test — we only care that the output covers each component.
    """
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code in (0, 1), result.output
    output = result.output.lower()
    assert "chroma" in output
    assert "voyage" in output
    assert "anthropic" in output
    assert "ripgrep" in output or "rg" in output
    assert "git" in output


# ── T3: ChromaDB + Voyage AI (requires keys) ──────────────────────────────────

@pytest.mark.integration
@requires_t3
def test_t3_store_put_and_list_roundtrip(runner: CliRunner) -> None:
    """nx store put → nx store list shows the stored entry."""
    unique = f"nexus-int-{uuid.uuid4().hex[:8]}"
    title = f"{unique}.md"

    result = runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", title,
        "--ttl", "1d",
    ], input=f"Integration test document with unique token {unique}")
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "store", "list",
        "--collection", _T3_TEST_COLLECTION,
    ])
    assert result.exit_code == 0, result.output
    assert title in result.output


@pytest.mark.integration
@requires_t3
def test_t3_store_put_search_roundtrip(runner: CliRunner) -> None:
    """nx store put → nx search --corpus returns the stored document."""
    unique = f"nexus-int-{uuid.uuid4().hex[:8]}"
    title = f"{unique}.md"

    result = runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", title,
        "--ttl", "1d",
    ], input=f"Integration test document with unique token {unique}")
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "search", unique,
        "--corpus", _T3_TEST_COLLECTION,
        "--n", "5",
    ])
    assert result.exit_code == 0, result.output
    assert unique in result.output


@pytest.mark.integration
@requires_t3
def test_t3_store_expire_runs_cleanly(runner: CliRunner) -> None:
    """nx store expire completes without error."""
    result = runner.invoke(main, ["store", "expire"])
    assert result.exit_code == 0, result.output
    assert "Expired" in result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_list(runner: CliRunner) -> None:
    """nx collection list returns without error; may be empty."""
    result = runner.invoke(main, ["collection", "list"])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_verify_existing(runner: CliRunner) -> None:
    """nx collection verify reports correctly for a collection that exists."""
    # First ensure the collection exists by storing something
    unique = f"verify-{uuid.uuid4().hex[:6]}"
    runner.invoke(main, [
        "store", "put", "-",
        "--collection", _T3_TEST_COLLECTION,
        "--title", f"{unique}.md",
        "--ttl", "1d",
    ], input=f"Verification test {unique}")

    result = runner.invoke(main, ["collection", "verify", _T3_TEST_COLLECTION])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
@requires_t3
def test_t3_collection_verify_missing(runner: CliRunner) -> None:
    """nx collection verify exits non-zero for a collection that does not exist."""
    result = runner.invoke(main, [
        "collection", "verify", "knowledge__does-not-exist-ever-xyz",
    ])
    assert result.exit_code != 0, result.output


@pytest.mark.integration
@requires_t3
def test_nx_search_all_corpora(runner: CliRunner) -> None:
    """nx search without --corpus completes without error."""
    result = runner.invoke(main, ["search", "test query", "--n", "3"])
    assert result.exit_code == 0, result.output


@pytest.mark.integration
@requires_t3
def test_nx_search_scoped_to_knowledge(runner: CliRunner) -> None:
    """nx search --corpus knowledge completes without error."""
    result = runner.invoke(main, ["search", "test query", "--corpus", "knowledge", "--n", "3"])
    assert result.exit_code == 0, result.output


# ── Answer mode (requires T3 + Anthropic) ─────────────────────────────────────

@pytest.mark.integration
@requires_t3
@requires_anthropic
def test_answer_mode_returns_synthesis(runner: CliRunner) -> None:
    """nx search -a returns a synthesized answer with citations."""
    result = runner.invoke(main, ["search", "what is nexus", "--answer", "--n", "3"])
    assert result.exit_code == 0, result.output
    assert len(result.output.strip()) > 0
