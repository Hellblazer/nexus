# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx_answer must refuse to guess a typed catalog-filter binding.

Regression for the defect observed live on 2026-07-25/26: a research
question routed through ``nx_answer`` matched builtin plan 14
(``type-scoped-search``), which declares
``required_bindings: ["question", "content_type"]``. The caller supplied
no ``content_type``, so the auto-alias filled it with the entire question
string. The retrieval then filtered on a 90-character sentence against a
domain of ``code`` / ``paper`` / ``rdr`` / ``knowledge`` and matched zero
documents, while the identical query with no ``content_type`` returned
the correct paper as its top hit.

The plan is not at fault — its own description says it is "useful when the
caller knows the artefact kind", so requiring ``content_type`` is correct.
The fault was upstream: a type-scoped plan was selected for a
type-agnostic question, and the alias concealed the mismatch by inventing
a value.

Two remedies were considered. Binding ``""`` (no filter) restores results
but lets a plan that MEANT to be type-scoped silently widen. The chosen
behaviour is to fail loudly: nx_answer will not guess a value whose domain
is enumerated or numeric. The run is recorded as a failure so a
chronically mis-matched plan accrues a real failure rate and stops being
promoted (``plans/promote.py`` gates on success/(success+failure)).

The alias remains correct and load-bearing for FREE-TEXT bindings
(``concept``, ``area``, ``topic``) — without it, library plans failed at
dispatch with ``missing required bindings``.
"""

from __future__ import annotations

import pytest

from nexus.mcp.core import PlanBindingUnsatisfiableError, _autoalias_bindings
# The taxonomy has ONE home (nexus-0yrjr review): importing it from
# nexus.mcp.core would let a re-duplicated copy pass this suite.
from nexus.plans.schema import TYPED_FILTER_BINDINGS

QUESTION = "Which indexed papers discuss intermediate representations?"


def test_free_text_binding_still_receives_the_question() -> None:
    """The behaviour the auto-alias was added for must survive."""
    out = _autoalias_bindings(
        required=["concept"], run_bindings={"intent": QUESTION},
        defaults={}, question=QUESTION, plan_id=1, plan_name="x",
    )
    assert out["concept"] == QUESTION


def test_unsatisfiable_typed_binding_raises() -> None:
    """The defect: content_type must never be guessed from free text."""
    with pytest.raises(PlanBindingUnsatisfiableError) as exc:
        _autoalias_bindings(
            required=["question", "content_type"],
            run_bindings={"intent": QUESTION}, defaults={},
            question=QUESTION, plan_id=14, plan_name="type-scoped-search",
        )
    assert exc.value.binding == "content_type"
    assert exc.value.plan_id == 14


def test_the_error_is_actionable() -> None:
    """A caller reading only the message must know what went wrong."""
    with pytest.raises(PlanBindingUnsatisfiableError) as exc:
        _autoalias_bindings(
            required=["content_type"], run_bindings={}, defaults={},
            question=QUESTION, plan_id=14, plan_name="type-scoped-search",
        )
    msg = str(exc.value)
    assert "content_type" in msg
    assert "type-scoped-search" in msg
    assert "14" in msg
    # names the domain so the caller knows what a legal value looks like
    assert "paper" in msg


def test_caller_supplied_typed_value_is_accepted() -> None:
    """An explicit content_type is the satisfied path — no raise."""
    out = _autoalias_bindings(
        required=["content_type"], run_bindings={"content_type": "paper"},
        defaults={}, question=QUESTION, plan_id=14, plan_name="t",
    )
    assert out["content_type"] == "paper"


def test_plan_default_satisfies_a_typed_binding() -> None:
    """A plan default is a legitimate source — no raise, no injection."""
    out = _autoalias_bindings(
        required=["content_type"], run_bindings={},
        defaults={"content_type": "rdr"}, question=QUESTION,
        plan_id=14, plan_name="t",
    )
    assert "content_type" not in out  # runner resolves it from defaults


def test_every_typed_filter_binding_refuses_the_question() -> None:
    """No partial cover — each typed name raises rather than being stuffed."""
    for name in sorted(TYPED_FILTER_BINDINGS):
        with pytest.raises(PlanBindingUnsatisfiableError):
            _autoalias_bindings(
                required=[name], run_bindings={}, defaults={},
                question=QUESTION, plan_id=1, plan_name="p",
            )


def test_numeric_bindings_are_in_the_typed_set() -> None:
    """limit/depth are numeric; a question string there is nonsense too."""
    assert {"limit", "depth"} <= TYPED_FILTER_BINDINGS


def test_free_text_bindings_are_unaffected_by_a_typed_sibling() -> None:
    """The raise happens before any partial binding is handed to the runner."""
    with pytest.raises(PlanBindingUnsatisfiableError):
        _autoalias_bindings(
            required=["concept", "content_type"], run_bindings={},
            defaults={}, question=QUESTION, plan_id=1, plan_name="p",
        )
