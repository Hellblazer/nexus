# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.3 (nexus-p2m1x.8): the pieces ``nx index repo``'s row-first target model is built from.

The engine-backed journeys are in ``tests/integration/test_rdr229_p23_repo_model_from_row.py``. These pin
the three knobs it added, on doubles, in the default loop:

* ``doc_indexer._target_model(..., name_model=)``: what "the name decides" answers when the name carries
  no model; never over a row.
* ``indexer._repo_target_model``: that knob is set only for a name with no model token (a legacy name).
* ``StalenessCache.model_known``: a cache whose collection's row could not be read lets the content hash
  alone decide, through ``check_staleness`` (the unchanged-content skip of every repo-run indexer).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.doc_indexer import _target_model
from nexus.indexer import _repo_target_model
from nexus.indexer_utils import StalenessCache, check_staleness

_BGE = "bge-base-en-v15-768"
_MINI = "minilm-l6-v2-384"
_CONFORMANT = f"docs__owner1__{_BGE}__v1"
_LEGACY = "docs__legacyname"


class _Store(MagicMock):
    """A T3 double that reads its row through its own resolver, as ``HttpVectorClient`` does."""

    row: dict | None = None
    fails: bool = False

    def _resolve_collection_row(self, name: str, **_kw) -> dict | None:
        if self.fails:
            raise ConnectionError("listing unreachable")
        return self.row


def _store(row: dict | None = None, *, fails: bool = False) -> _Store:
    store = _Store()
    store.row, store.fails = row, fails
    return store


# ── _target_model(name_model=) ───────────────────────────────────────────────


def test_the_row_wins_over_name_model() -> None:
    assert _target_model(_store({"embedding_model": _BGE}), _LEGACY, name_model=_MINI) == (_BGE, True)


def test_no_row_answers_name_model_when_given() -> None:
    assert _target_model(_store(None), _LEGACY, name_model=_MINI) == (_MINI, True)


def test_a_row_without_a_model_answers_name_model_flagged_as_a_guess() -> None:
    """The row is the source of the model, so a row with none is Unreadable (RDR-229 P2.2): the answer is the
    name's, flagged, as for a failed listing, and no stamp is judged against it."""
    assert _target_model(_store({"content_type": "docs"}), _LEGACY, name_model=_MINI) == (_MINI, False)


def test_an_unreadable_row_answers_name_model_flagged_as_a_guess() -> None:
    assert _target_model(_store(fails=True), _LEGACY, name_model=_MINI) == (_MINI, False)


def test_a_store_with_no_resolver_answers_name_model() -> None:
    assert _target_model(object(), _LEGACY, name_model=_MINI) == (_MINI, True)


def test_without_name_model_the_name_derivation_answers() -> None:
    assert _target_model(_store(None), _CONFORMANT) == (_BGE, True)
    assert _target_model(_store(fails=True), _CONFORMANT) == (_BGE, False)


# ── _repo_target_model ───────────────────────────────────────────────────────


def test_a_conformant_name_never_takes_the_legacy_model() -> None:
    # The name's own token answers when no row does; the legacy model is for a name with none.
    assert _repo_target_model(_store(None), _CONFORMANT, legacy_model=_MINI) == (_BGE, True)


def test_a_legacy_name_takes_the_legacy_model_unless_the_row_answers() -> None:
    assert _repo_target_model(_store(None), _LEGACY, legacy_model=_MINI) == (_MINI, True)
    assert _repo_target_model(_store({"embedding_model": _BGE}), _LEGACY, legacy_model=_MINI) == (_BGE, True)
    assert _repo_target_model(_store(fails=True), _LEGACY, legacy_model=_MINI) == (_MINI, False)


def test_a_rows_model_wins_over_a_conformant_names_token() -> None:
    # What a rename leaves: the name says bge, the row says minilm. The engine embeds with the row.
    assert _repo_target_model(_store({"embedding_model": _MINI}), _CONFORMANT, legacy_model=None) == (_MINI, True)


# ── StalenessCache.model_known through check_staleness ───────────────────────


def _cache(stored_model: str, *, model_known: bool) -> StalenessCache:
    cache = StalenessCache()
    cache.by_doc_id["1.1.1"] = ("hash-a", stored_model)
    cache.model_known = model_known
    return cache


def _stale(cache: StalenessCache, *, content_hash: str = "hash-a", target: str = _BGE) -> bool:
    """``check_staleness`` answers True when the file is current (so the run skips it)."""
    return not check_staleness(None, "a.py", content_hash, target, doc_id="1.1.1", cache=cache)


def test_a_known_model_must_match_the_stored_stamp() -> None:
    assert _stale(_cache(_MINI, model_known=True)) is True
    assert _stale(_cache(_BGE, model_known=True)) is False


def test_an_unknown_model_lets_the_content_hash_alone_decide() -> None:
    assert _stale(_cache(_MINI, model_known=False)) is False, "the stamp is the row's model, the target a guess"
    assert _stale(_cache(_MINI, model_known=False), content_hash="hash-b") is True, "changed content is still stale"


def test_the_cache_defaults_to_a_known_model() -> None:
    assert StalenessCache().model_known is True
