# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-fgxmk: ``find()`` has a stated order contract and an exact-title sibling.

Two facts about the catalog's full-text ``find`` that three test sites had
each rediscovered locally:

1. Matching is BROADER than a title lookup. ``plainto_tsquery('english',
   'Paper A')`` reduces to ``'paper'`` because "A" is an English stopword,
   so ``find("Paper A")`` also matches "Paper B". A caller writing
   ``find(title)[0]`` and believing it resolved *title* was resolving
   whichever sibling the heap returned first.
2. Order is now a contract: the engine orders ``/search`` results by
   tumbler (registration order within an owner; a text column, so string
   order). Before this it had no ``ORDER BY`` and reversed on a churned
   database.

``find_by_title_exact`` is the promoted resolution entry point. These tests
run against the real engine substrate through ``ActiveCatalog``, the same
fixture the migrated sites use.
"""
from __future__ import annotations

import pytest

from tests._catalog_fixture_ops import ActiveCatalog


@pytest.fixture
def cat() -> ActiveCatalog:
    return ActiveCatalog()


def _seed(cat: ActiveCatalog) -> dict[str, str]:
    owner = cat.register_owner("fgxmk-papers", "curator")
    tumblers: dict[str, str] = {}
    for title in ("Paper B", "Paper A", "Paper C"):
        tumblers[title] = str(cat.register(owner, title, content_type="paper"))
    return tumblers


def test_find_on_a_stopword_title_matches_its_siblings(cat: ActiveCatalog) -> None:
    """The trap, pinned by name: "Paper A" is 'paper' to the English tsquery."""
    _seed(cat)
    titles = sorted(e.title for e in cat.find("Paper A"))
    assert titles == ["Paper A", "Paper B", "Paper C"], titles


def test_find_results_are_ordered_by_tumbler(cat: ActiveCatalog) -> None:
    """Registration order B, A, C; the contract is tumbler order, not insertion.

    ``register`` assigns tumblers sequentially, so registration order and
    tumbler order coincide here — the ordering pin that distinguishes the
    two lives engine-side (``CatalogRepositoryTest``, inserted 9.3/9.1/9.2).
    This asserts the contract as the client sees it: sorted, and stable
    across repeated calls.
    """
    _seed(cat)
    first = [str(e.tumbler) for e in cat.find("Paper A")]
    second = [str(e.tumbler) for e in cat.find("Paper A")]
    assert first == sorted(first), first
    assert first == second


def test_find_by_title_exact_resolves_only_the_named_title(cat: ActiveCatalog) -> None:
    tumblers = _seed(cat)
    hits = cat.find_by_title_exact("Paper A")
    assert [(str(e.tumbler), e.title) for e in hits] == [(tumblers["Paper A"], "Paper A")]
    assert cat.find_by_title_exact("Paper Z") == []


def test_find_by_title_exact_honours_content_type(cat: ActiveCatalog) -> None:
    _seed(cat)
    assert [e.title for e in cat.find_by_title_exact("Paper A", content_type="paper")] == ["Paper A"]
    assert cat.find_by_title_exact("Paper A", content_type="code") == []


def test_a_title_made_only_of_stopwords_still_resolves_exactly(cat: ActiveCatalog) -> None:
    """The 'simple' tsquery leg keeps stopword lexemes, so the English leg
    dropping every token does not make the title unfindable."""
    owner = cat.register_owner("fgxmk-stopwords", "curator")
    t = str(cat.register(owner, "The A", content_type="paper"))
    assert [str(e.tumbler) for e in cat.find_by_title_exact("The A")] == [t]
