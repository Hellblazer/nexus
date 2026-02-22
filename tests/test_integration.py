"""Integration tests — require real API keys. Skipped automatically when keys absent.

Run with: pytest -m integration
Skip by default in CI unless NEXUS_INTEGRATION=1 is set.
"""
import os
import uuid

import pytest
from click.testing import CliRunner

from nexus.cli import main

# ── Marks ─────────────────────────────────────────────────────────────────────

requires_t3 = pytest.mark.skipif(
    not (os.environ.get("CHROMA_API_KEY") and os.environ.get("VOYAGE_API_KEY")),
    reason="T3 integration requires CHROMA_API_KEY and VOYAGE_API_KEY",
)

requires_anthropic = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="Anthropic integration requires ANTHROPIC_API_KEY",
)


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


# ── T1: session scratch (no API keys required) ────────────────────────────────

@pytest.mark.integration
def test_t1_scratch_put_list_clear(runner: CliRunner, tmp_path, monkeypatch) -> None:
    """T1 scratch put → list → clear roundtrip."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # T1 scratch requires an active session file
    import os
    ppid = os.getppid()
    session_dir = tmp_path / ".config" / "nexus" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / f"{ppid}.session").write_text("integration-test-session")

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


# ── nx doctor (no API keys required for smoke test) ───────────────────────────

@pytest.mark.integration
def test_doctor_runs_and_reports(runner: CliRunner) -> None:
    """nx doctor completes without crashing and reports all five checks.

    Exit code is 0 when all credentials are present, 1 when any are missing.
    """
    result = runner.invoke(main, ["doctor"])
    # Exit code is now 1 when credentials are absent (the happy case in CI),
    # or 0 when all credentials are configured — both are acceptable here.
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
def test_t3_store_put_search_roundtrip(runner: CliRunner) -> None:
    """T3 store put → search returns the stored document."""
    unique = f"nexus-integration-{uuid.uuid4().hex[:8]}"
    collection = "knowledge__integration-test"

    result = runner.invoke(main, [
        "store", "put", "-",
        "--collection", collection,
        "--title", f"{unique}.md",
        "--ttl", "1d",
    ], input=f"Integration test document with unique token {unique}")
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, [
        "store", "search", unique,
        "--collection", collection,
        "--n", "5",
    ])
    assert result.exit_code == 0, result.output
    assert unique in result.output


@pytest.mark.integration
@requires_t3
def test_nx_search_returns_results(runner: CliRunner) -> None:
    """nx search returns results without crashing when T3 is available."""
    result = runner.invoke(main, ["search", "test query", "--n", "3"])
    # May return 0 results but should not error
    assert result.exit_code == 0, result.output


# ── Answer mode (requires T3 + Anthropic) ─────────────────────────────────────

@pytest.mark.integration
@requires_t3
@requires_anthropic
def test_answer_mode_returns_synthesis(runner: CliRunner) -> None:
    """nx search -a returns a synthesized answer with citations."""
    result = runner.invoke(main, ["search", "what is nexus", "--answer", "--n", "3"])
    assert result.exit_code == 0, result.output
    # Answer mode always produces some output
    assert len(result.output.strip()) > 0
