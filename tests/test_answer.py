"""Tests for nexus.answer — answer synthesis edge cases."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.answer import _haiku_refine
from nexus.types import SearchResult


# ── _haiku_refine empty response guard (line 75-76) ──────────────────────────

def test_haiku_refine_empty_response() -> None:
    """When Anthropic returns empty content list, _haiku_refine returns {"done": True}."""
    results = [
        SearchResult(
            id="r1",
            content="some content about authentication",
            distance=0.2,
            collection="docs__test",
            metadata={"source_path": "auth.py", "line_start": 1},
        ),
    ]

    mock_msg = MagicMock()
    mock_msg.content = []  # empty content list

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    with patch("nexus.answer._anthropic_client", return_value=mock_client):
        result = _haiku_refine("how does auth work?", results)

    assert result == {"done": True}
