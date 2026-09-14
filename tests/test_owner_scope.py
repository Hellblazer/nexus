"""GH #1527 (nexus-qiah5): a scope may be an owner name, not only a tumbler."""

from __future__ import annotations

import pytest

from nexus.catalog.owner_scope import OwnerScopeError, resolve_owner_scope
from nexus.catalog.tumbler import Tumbler


class _Cat:
    def __init__(
        self,
        by_name: dict[str, list[str]],
        types: dict[str, str] | None = None,
    ) -> None:
        self._by_name = by_name
        self._types = types or {}
        self.asked: list[str] = []
        self.shown: list[str] = []

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        self.asked.append(name)
        return [Tumbler.parse(t) for t in self._by_name.get(name, [])]

    def get_owner_by_prefix(self, tumbler_prefix: str) -> dict | None:
        self.shown.append(tumbler_prefix)
        owner_type = self._types.get(tumbler_prefix)
        if owner_type is None:
            return None
        return {"tumbler_prefix": tumbler_prefix, "owner_type": owner_type}


def test_dotted_tumbler_passes_through_without_a_lookup() -> None:
    cat = _Cat({})
    assert resolve_owner_scope(cat, "1.61") == "1.61"
    assert resolve_owner_scope(cat, "1.61.7") == "1.61.7"
    assert cat.asked == []


def test_empty_scope_is_returned_empty() -> None:
    assert resolve_owner_scope(_Cat({}), "") == ""
    assert resolve_owner_scope(_Cat({}), "   ") == ""


def test_owner_name_resolves_to_its_tumbler() -> None:
    cat = _Cat({"aip-unified-recs-intelligent": ["1.61"]})
    assert resolve_owner_scope(cat, "aip-unified-recs-intelligent") == "1.61"
    assert cat.asked == ["aip-unified-recs-intelligent"]


def test_unknown_name_is_a_named_error() -> None:
    with pytest.raises(OwnerScopeError, match="neither a dotted tumbler .* nor the name"):
        resolve_owner_scope(_Cat({}), "no-such-repo")


def test_lenient_mode_returns_an_unknown_name_unchanged() -> None:
    """``nx_answer(scope=...)`` also accepts a corpus name, so an unknown
    name is left for the next layer instead of refused here."""
    cat = _Cat({})
    assert resolve_owner_scope(cat, "knowledge", strict=False) == "knowledge"
    with pytest.raises(OwnerScopeError):
        resolve_owner_scope(_Cat({"shared": ["1.3", "1.9"]}), "shared", strict=False)


def test_ambiguous_name_lists_candidates() -> None:
    cat = _Cat({"shared": ["1.3", "1.9"]}, {"1.3": "curator", "1.9": "person"})
    with pytest.raises(OwnerScopeError, match=r"ambiguous.*1\.3, 1\.9"):
        resolve_owner_scope(cat, "shared")


def test_shared_name_prefers_the_single_repo_owner() -> None:
    # GH #1544: `1.48 repo canon-chat` and `1.49 curator canon-chat` (the
    # curator behind knowledge__canon-chat) share the name; the caller's
    # name is the repo's.
    cat = _Cat({"canon-chat": ["1.48", "1.49"]}, {"1.48": "repo", "1.49": "curator"})
    assert resolve_owner_scope(cat, "canon-chat") == "1.48"
    assert sorted(cat.shown) == ["1.48", "1.49"]


def test_shared_name_prefers_the_repo_owner_regardless_of_position() -> None:
    cat = _Cat({"canon-chat": ["1.49", "1.48"]}, {"1.48": "repo", "1.49": "curator"})
    assert resolve_owner_scope(cat, "canon-chat") == "1.48"


def test_unique_name_never_consults_owner_type() -> None:
    cat = _Cat({"solo": ["1.7"]}, {"1.7": "curator"})
    assert resolve_owner_scope(cat, "solo") == "1.7"
    assert cat.shown == []


def test_shared_name_with_an_unshowable_owner_row_stays_ambiguous() -> None:
    # A row the catalog cannot show is not a repo; nothing captures the name.
    cat = _Cat({"shared": ["1.3", "1.9"]}, {"1.3": "curator"})
    with pytest.raises(OwnerScopeError, match="ambiguous"):
        resolve_owner_scope(cat, "shared")


def test_lenient_mode_never_lets_an_owner_capture_a_corpus_scope() -> None:
    """Review of the GH #1527 landing (Significant): an owner named
    "knowledge" must not turn nx_answer(scope="knowledge") into a tumbler
    scope; corpus prefixes and full collection names are corpus scopes by
    construction and skip the owner lookup entirely."""
    cat = _Cat({"knowledge": ["1.4"], "code": ["1.5"]})
    assert resolve_owner_scope(cat, "knowledge", strict=False) == "knowledge"
    assert resolve_owner_scope(cat, "code__", strict=False) == "code__"
    assert resolve_owner_scope(cat, "code__1-61__bge-base-en-v15-768__v1", strict=False) == \
        "code__1-61__bge-base-en-v15-768__v1"
    assert cat.asked == []
    # A subtree is strict: the same word IS looked up there.
    assert resolve_owner_scope(cat, "knowledge") == "1.4"


def test_all_is_a_corpus_scope_too() -> None:
    """Critique [25095]: "all" is the fifth reserved corpus keyword; an owner
    named "all" must not capture nx_answer(scope="all")."""
    cat = _Cat({"all": ["1.7"]})
    assert resolve_owner_scope(cat, "all", strict=False) == "all"
    assert cat.asked == []
