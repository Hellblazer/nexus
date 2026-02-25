"""Extended answer.py tests — haiku_answer, answer_mode, refine edge cases."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.answer import _haiku_answer, _haiku_refine, answer_mode
from nexus.types import SearchResult


def _results() -> list[SearchResult]:
    return [
        SearchResult(
            id="r1",
            content="Auth uses JWT tokens with 24h expiry.",
            distance=0.15,
            collection="code__myrepo",
            metadata={"source_path": "src/auth.py", "line_start": 42, "line_end": 60},
        ),
        SearchResult(
            id="r2",
            content="Password hashing uses bcrypt with cost factor 12.",
            distance=0.25,
            collection="docs__myrepo",
            metadata={"source_path": "docs/security.md", "line_start": 10},
        ),
    ]


def _mock_client(text: str) -> MagicMock:
    mock_msg = MagicMock()
    mock_block = MagicMock()
    mock_block.text = text
    mock_msg.content = [mock_block]
    client = MagicMock()
    client.messages.create.return_value = mock_msg
    return client


# ── _haiku_answer ────────────────────────────────────────────────────────────

def test_haiku_answer_returns_synthesis() -> None:
    client = _mock_client('Auth uses JWT. <cite i="0">')
    with patch("nexus.answer._anthropic_client", return_value=client):
        result = _haiku_answer("how does auth work?", _results())

    assert "JWT" in result
    assert 'cite i="0"' in result


def test_haiku_answer_empty_content_returns_empty() -> None:
    mock_msg = MagicMock()
    mock_msg.content = []
    client = MagicMock()
    client.messages.create.return_value = mock_msg

    with patch("nexus.answer._anthropic_client", return_value=client):
        result = _haiku_answer("query", _results())

    assert result == ""


def test_haiku_answer_empty_results() -> None:
    """Works even with empty results list."""
    client = _mock_client("No relevant sources found.")
    with patch("nexus.answer._anthropic_client", return_value=client):
        result = _haiku_answer("query", [])

    assert result == "No relevant sources found."


# ── _haiku_refine ────────────────────────────────────────────────────────────

def test_haiku_refine_done() -> None:
    client = _mock_client('{"done": true}')
    with patch("nexus.answer._anthropic_client", return_value=client):
        result = _haiku_refine("query", _results())

    assert result == {"done": True}


def test_haiku_refine_query_refinement() -> None:
    client = _mock_client('{"query": "JWT token expiry configuration"}')
    with patch("nexus.answer._anthropic_client", return_value=client):
        result = _haiku_refine("auth", _results())

    assert result == {"query": "JWT token expiry configuration"}


def test_haiku_refine_malformed_json_returns_done() -> None:
    """Malformed JSON from Haiku defaults to done."""
    client = _mock_client("This is not JSON at all")
    with patch("nexus.answer._anthropic_client", return_value=client):
        result = _haiku_refine("query", _results())

    assert result == {"done": True}


# ── answer_mode ──────────────────────────────────────────────────────────────

def test_answer_mode_returns_synthesis_with_citations() -> None:
    """Full answer_mode returns synthesis text + citation footer."""
    client = _mock_client('Auth uses JWT tokens. <cite i="0">')
    with patch("nexus.answer._anthropic_client", return_value=client):
        output = answer_mode("how does auth work?", _results())

    # Should contain synthesis
    assert "JWT" in output
    # Should contain citation footer
    assert "src/auth.py" in output
    assert "docs/security.md" in output
    # Match percentage
    assert "85.0% match" in output  # 1.0 - 0.15 = 0.85 → 85.0%


def test_answer_mode_citation_line_range() -> None:
    """Citation shows line range when line_end is present."""
    client = _mock_client("answer text")
    with patch("nexus.answer._anthropic_client", return_value=client):
        output = answer_mode("query", _results())

    # r1 has line_start=42, line_end=60 → should show "42-60"
    assert "42-60" in output


def test_answer_mode_citation_single_line() -> None:
    """Citation shows single line when line_end is missing."""
    client = _mock_client("answer")
    results = [_results()[1]]  # r2 has no line_end
    with patch("nexus.answer._anthropic_client", return_value=client):
        output = answer_mode("query", results)

    # r2 has line_start=10, no line_end → should show "10"
    assert "10" in output
    assert "10-" not in output.split("docs/security.md")[1].split("\n")[0]


def test_answer_mode_empty_synthesis() -> None:
    """Empty synthesis still produces citation footer."""
    mock_msg = MagicMock()
    mock_msg.content = []
    client = MagicMock()
    client.messages.create.return_value = mock_msg

    with patch("nexus.answer._anthropic_client", return_value=client):
        output = answer_mode("query", _results())

    assert "src/auth.py" in output  # footer still present


def test_answer_mode_missing_metadata_uses_defaults() -> None:
    """Results with missing metadata use ? defaults."""
    results = [
        SearchResult(id="r1", content="hello", distance=0.5, collection="c", metadata={}),
    ]
    client = _mock_client("answer")
    with patch("nexus.answer._anthropic_client", return_value=client):
        output = answer_mode("query", results)

    assert "?:?" in output  # source_path=?, line_start=?
