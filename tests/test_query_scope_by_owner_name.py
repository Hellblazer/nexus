"""GH #1527 (nexus-qiah5): ``query(subtree=...)`` and ``nx_answer(scope=...)``
accept a registered owner's name, resolved through the owner table, and
report an unknown or ambiguous name by name instead of failing on a bare
tumbler parse."""

from __future__ import annotations

from unittest.mock import patch

from nexus.catalog.tumbler import Tumbler
from nexus.mcp.core import query


class _Cat:
    def __init__(self, by_name: dict[str, list[str]]) -> None:
        self._by_name = by_name

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        return [Tumbler.parse(t) for t in self._by_name.get(name, [])]


def test_query_reports_an_unknown_owner_name_by_name() -> None:
    with patch("nexus.mcp.core._get_catalog", return_value=_Cat({})):
        out = query(question="anything", subtree="no-such-repo")
    assert isinstance(out, str)
    assert out.startswith("Error: subtree 'no-such-repo' is neither a dotted tumbler"), out


def test_query_reports_an_ambiguous_owner_name_with_candidates() -> None:
    with patch("nexus.mcp.core._get_catalog", return_value=_Cat({"shared": ["1.3", "1.9"]})):
        out = query(question="anything", subtree="shared")
    assert "ambiguous" in out and "1.3, 1.9" in out, out


def test_query_still_refuses_a_document_level_subtree_after_resolution() -> None:
    """The depth guard runs on the RESOLVED value, so a dotted document
    address is still refused exactly as before."""
    with patch("nexus.mcp.core._get_catalog", return_value=_Cat({})):
        out = query(question="anything", subtree="1.61.7")
    assert "document-level address" in out, out
