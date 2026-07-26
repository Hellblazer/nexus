# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx_answer must not stuff free text into typed catalog-filter bindings.

Regression for the defect observed live on 2026-07-25/26: a research
question routed through ``nx_answer`` matched builtin plan 14
(``type-scoped-search``), which declares
``required_bindings: ["question", "content_type"]``. The caller supplied
no ``content_type``, so the auto-alias filled it with the entire question
string. Step 1 then ran

    query(question=$question, content_type=$content_type, corpus="all")

with ``content_type="Which indexed papers discuss intermediate
representations for compiling natural-language queries?"`` — a catalog
metadata filter whose real domain is ``code`` / ``paper`` / ``rdr`` /
``knowledge``. It matched zero documents and the run reported "the plan's
retrieval steps returned zero results", while the identical query with no
``content_type`` returned the correct paper as the top hit.

The auto-alias itself is correct and load-bearing for free-text bindings
(``concept``, ``area``, ``topic``); without it library plans failed at
dispatch with ``missing required bindings``. The defect is that it did not
distinguish free-text bindings from typed ones.

Binding ``""`` (rather than omitting) is deliberate and depends on two
verified properties:

* ``plan_run``'s gate is presence-only — ``name not in bindings``
  (``runner.py:668-672``) — so ``""`` satisfies a required binding.
* the retrieval tools coerce empty filters away — ``content_type or None``
  (``core.py:1730-1731``) — so ``""`` means "no filter", not "match ''".
"""

from __future__ import annotations

from nexus.mcp.core import _TYPED_FILTER_BINDINGS, _autoalias_bindings

QUESTION = "Which indexed papers discuss intermediate representations?"


def test_free_text_binding_still_receives_the_question() -> None:
    """The behaviour the auto-alias was added for must survive."""
    out = _autoalias_bindings(
        required=["concept"], run_bindings={"intent": QUESTION},
        defaults={}, question=QUESTION,
    )
    assert out["concept"] == QUESTION


def test_typed_filter_binding_is_emptied_not_stuffed() -> None:
    """The defect: content_type must never receive the question text."""
    out = _autoalias_bindings(
        required=["question", "content_type"],
        run_bindings={"intent": QUESTION}, defaults={}, question=QUESTION,
    )
    assert out["content_type"] == "", (
        "content_type was filled with free text — this is the exact "
        "condition that made plan 14 retrieve zero documents"
    )
    # presence is what plan_run's gate checks, so it must still be there
    assert "content_type" in out
    # and the free-text one alongside it is unaffected
    assert out["question"] == QUESTION


def test_caller_supplied_values_are_never_overwritten() -> None:
    """An explicit content_type must win over both the alias and the blank."""
    out = _autoalias_bindings(
        required=["content_type"],
        run_bindings={"content_type": "paper"}, defaults={},
        question=QUESTION,
    )
    assert out["content_type"] == "paper"


def test_plan_defaults_are_respected() -> None:
    """A binding carrying a plan default is left for the runner to resolve."""
    out = _autoalias_bindings(
        required=["content_type"], run_bindings={}, defaults={"content_type": "rdr"},
        question=QUESTION,
    )
    assert "content_type" not in out


def test_every_typed_filter_binding_is_covered() -> None:
    """Each name in the typed set is blanked, not stuffed — no partial cover."""
    out = _autoalias_bindings(
        required=sorted(_TYPED_FILTER_BINDINGS), run_bindings={},
        defaults={}, question=QUESTION,
    )
    for name in _TYPED_FILTER_BINDINGS:
        assert out[name] == "", f"{name} received free text"


def test_numeric_bindings_are_in_the_typed_set() -> None:
    """limit/depth are numeric; a question string there is nonsense too."""
    assert {"limit", "depth"} <= _TYPED_FILTER_BINDINGS


def test_the_live_plan14_shape_end_to_end() -> None:
    """Exact reproduction of the observed failure's binding computation."""
    out = _autoalias_bindings(
        required=["question", "content_type"],
        run_bindings={"intent": QUESTION, "_nx_scope": "knowledge__semantic-operators"},
        defaults={}, question=QUESTION,
    )
    # the retrieval step would resolve content_type -> "" -> None -> no filter
    assert out["content_type"] == ""
    assert out["_nx_scope"] == "knowledge__semantic-operators"
