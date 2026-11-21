# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.7 (nexus-p2m1x.12): ``resolve_corpus`` stage 3 reads the row tuple.

A typed ``<content_type>__<owner>[__<embedding_model>]`` value is matched against each
candidate's catalog row (``content_type`` and ``owner_id`` equal, ``embedding_model``
equal when the value names one, ``lifecycle_state == "live"``). Only when no row matches
does the old label-prefix match run, because a repo collection's row ``owner_id`` is the
tumbler segment (``1-15``, nexus-6pbwx), so the slug a user types in
``code__<slug>-<hash8>`` is a label, not an owner. The fallback never resurrects a
collection the tuple stage rejected by lifecycle.

``get_collection_row`` is stubbed the way the stage-2 tests in ``test_corpus.py`` do.
"""
from __future__ import annotations

import pytest

import nexus.mcp_infra as mi
from nexus.corpus import resolve_corpus


def _row(content_type: str, owner_id: str, model: str, state: str = "live") -> dict:
    return {
        "content_type": content_type,
        "owner_id": owner_id,
        "embedding_model": model,
        "lifecycle_state": state,
    }


def _stub_rows(monkeypatch, rows: dict[str, dict]) -> list[str]:
    """Serve *rows* from ``get_collection_row`` and record which names were asked for."""
    asked: list[str] = []

    def get_row(name: str, **_kw) -> dict | None:
        asked.append(name)
        return rows.get(name)

    monkeypatch.setattr(mi, "get_collection_row", get_row)
    return asked


def test_a_knowledge_short_form_finds_its_four_segment_row(monkeypatch) -> None:
    rows = {
        "knowledge__foo__voyage-context-3__v1": _row("knowledge", "foo", "voyage-context-3"),
        "knowledge__bar__voyage-context-3__v1": _row("knowledge", "bar", "voyage-context-3"),
        "code__foo__voyage-code-3__v1": _row("code", "foo", "voyage-code-3"),
    }
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("knowledge__foo", list(rows)) == ["knowledge__foo__voyage-context-3__v1"]


def test_the_row_decides_not_the_name(monkeypatch) -> None:
    """A collection whose NAME does not start with the typed value but whose ROW names the
    typed (content_type, owner) is found; one whose name starts with it but whose row names
    another owner is not. The label-prefix match would get both of these wrong."""
    rows = {
        "knowledge__renamed__voyage-context-3__v1": _row("knowledge", "foo", "voyage-context-3"),
        "knowledge__foo__bge-base-en-v15-768__v1": _row("knowledge", "elsewhere", "bge-base-en-v15-768"),
    }
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("knowledge__foo", list(rows)) == ["knowledge__renamed__voyage-context-3__v1"]


def test_a_third_segment_narrows_by_the_rows_embedding_model(monkeypatch) -> None:
    rows = {
        "knowledge__foo__voyage-context-3__v1": _row("knowledge", "foo", "voyage-context-3"),
        "knowledge__foo__bge-base-en-v15-768__v1": _row("knowledge", "foo", "bge-base-en-v15-768"),
    }
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("knowledge__foo__bge-base-en-v15-768", list(rows)) == [
        "knowledge__foo__bge-base-en-v15-768__v1",
    ]
    assert resolve_corpus("knowledge__foo", list(rows)) == list(rows)


def test_a_repo_slug_short_form_falls_back_to_the_label_prefix(monkeypatch) -> None:
    """A repo row's owner_id is the tumbler segment, so ``code__<slug>-<hash8>`` matches no
    tuple; it still resolves through the label-prefix fallback, as it always did."""
    repo = "code__arcaneum-2ad2825c__bge-base-en-v15-768__v1"
    rows = {
        repo: _row("code", "1-15", "bge-base-en-v15-768"),
        "code__other-11112222__bge-base-en-v15-768__v1": _row("code", "1-16", "bge-base-en-v15-768"),
    }
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("code__arcaneum-2ad2825c", list(rows)) == [repo]


def test_the_fallback_runs_when_no_row_backs_a_name(monkeypatch) -> None:
    """A name with no row (or an unreadable listing: ``get_collection_row`` answers ``None``)
    cannot match a tuple, so a short form for it resolves by label, not to nothing."""
    name = "knowledge__foo__voyage-context-3__v1"
    _stub_rows(monkeypatch, {})
    assert resolve_corpus("knowledge__foo", [name]) == [name]


@pytest.mark.parametrize("state", ["quarantine", "dormant", "disputed"])
def test_a_non_live_sibling_is_excluded_by_lifecycle(monkeypatch, state: str) -> None:
    live = "knowledge__foo__voyage-context-3__v1"
    sibling = "quarantine-knowledge__foo__voyage-context-3__v1"
    rows = {
        live: _row("knowledge", "foo", "voyage-context-3"),
        sibling: _row("knowledge", "foo", "voyage-context-3", state),
    }
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("knowledge__foo", list(rows)) == [live]


@pytest.mark.parametrize("state", ["quarantine", "dormant", "disputed"])
def test_the_fallback_does_not_resurrect_a_collection_the_tuple_rejected(monkeypatch, state: str) -> None:
    """The only collection for the typed value is not live. Its name carries the label
    prefix, but the tuple stage already rejected it by lifecycle; the fallback must not
    hand it back."""
    name = "knowledge__foo__voyage-context-3__v1"
    _stub_rows(monkeypatch, {name: _row("knowledge", "foo", "voyage-context-3", state)})
    assert resolve_corpus("knowledge__foo", [name]) == []


def test_a_bare_non_content_type_word_is_a_label_prefix_and_reads_no_row(monkeypatch) -> None:
    rows = {
        "myrepo__x": _row("code", "myrepo", "voyage-code-3"),
        "other__y": _row("code", "other", "voyage-code-3"),
    }
    asked = _stub_rows(monkeypatch, rows)
    assert resolve_corpus("myrepo", list(rows)) == ["myrepo__x"]
    assert asked == []


def test_a_value_that_names_a_row_but_no_tuple_matches_returns_empty(monkeypatch) -> None:
    rows = {"knowledge__foo__voyage-context-3__v1": _row("knowledge", "foo", "voyage-context-3")}
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("knowledge__nope", list(rows)) == []
    # a model that no row carries narrows to nothing, and the label prefix cannot match it either
    assert resolve_corpus("knowledge__foo__bge-base-en-v15-768", list(rows)) == []


def test_a_four_segment_value_that_is_not_an_exact_name_is_never_a_tuple(monkeypatch) -> None:
    """Version is not part of the tuple, so ``...__v2`` must not match the ``__v1`` row."""
    rows = {"knowledge__foo__voyage-context-3__v1": _row("knowledge", "foo", "voyage-context-3")}
    _stub_rows(monkeypatch, rows)
    assert resolve_corpus("knowledge__foo__voyage-context-3__v2", list(rows)) == []
