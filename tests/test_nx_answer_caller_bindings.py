# SPDX-License-Identifier: AGPL-3.0-or-later
"""A caller can supply typed plan bindings, restoring type-scoped plans.

Closes the reachability hole found by review (nexus-0yrjr). Refusing to
guess a typed binding was correct, but it left builtin plans that require
one — ``type-scoped-search`` (``content_type``) and ``find-by-author``
(``author``) — permanently unreachable, because ``nx_answer`` had no
parameter through which such a value could arrive and there is no exposed
``plan_run`` MCP tool (``conexus/skills/plan-first/SKILL.md``: "there are
no separately exposed plan_match / plan_run MCP tools"). Dead shipped
builtins are a silent capability loss — the same class the refusal was
built to prevent.

``bindings`` is a sibling of the existing ``dimensions`` and ``scope``
parameters, not a new verb: it folds into the journey the caller already
uses.

NO VALUE VALIDATION, deliberately. There is no closed domain to validate
against — ``metadata_schema.CONTENT_TYPES`` is ``{code, pdf, markdown,
prose}``, a DIFFERENT field's vocabulary, while the live catalog carries
at least ``code``, ``prose``, ``rdr``, ``blog_post``, ``paper`` and
``knowledge``, and grows as new content is indexed. Rejecting against an
invented set would refuse legitimate values. A caller that passes a
nonsense ``content_type`` gets an empty retrieval, which is the honest
consequence of an explicit choice rather than a value the system invented.
"""

from __future__ import annotations

from nexus.mcp.core import _nx_answer_caller_bindings

Q = "which rdrs discuss intermediate representations"


def test_bare_call_supplies_only_intent() -> None:
    run, avail = _nx_answer_caller_bindings(question=Q, scope="", bindings=None)
    assert run == {"intent": Q}
    assert avail == frozenset({"intent"})


def test_scope_is_declared_when_present() -> None:
    run, avail = _nx_answer_caller_bindings(
        question=Q, scope="rdr__nexus", bindings=None,
    )
    assert run["_nx_scope"] == "rdr__nexus"
    assert "_nx_scope" in avail


def test_caller_bindings_reach_the_runner_and_the_matcher() -> None:
    """The whole point: content_type must be both bound AND declared."""
    run, avail = _nx_answer_caller_bindings(
        question=Q, scope="", bindings={"content_type": "rdr"},
    )
    assert run["content_type"] == "rdr", "value never reached the runner"
    assert "content_type" in avail, (
        "the matcher was not told the binding is available, so it would "
        "still decline to offer the type-scoped plan"
    )


def test_intent_is_not_overwritable_by_a_caller_binding() -> None:
    """intent is the question; a caller must not shadow it."""
    run, _ = _nx_answer_caller_bindings(
        question=Q, scope="", bindings={"intent": "something else"},
    )
    assert run["intent"] == Q


def test_scope_is_not_overwritable_by_a_caller_binding() -> None:
    """_nx_scope is derived from the scope argument, not caller-injectable."""
    run, _ = _nx_answer_caller_bindings(
        question=Q, scope="rdr__nexus", bindings={"_nx_scope": "code__other"},
    )
    assert run["_nx_scope"] == "rdr__nexus"


def test_empty_bindings_dict_is_the_same_as_none() -> None:
    assert _nx_answer_caller_bindings(question=Q, scope="", bindings={}) == \
        _nx_answer_caller_bindings(question=Q, scope="", bindings=None)


def test_a_free_text_binding_may_also_be_supplied() -> None:
    """Nothing restricts the parameter to typed names."""
    run, avail = _nx_answer_caller_bindings(
        question=Q, scope="", bindings={"concept": "tumblers"},
    )
    assert run["concept"] == "tumblers"
    assert "concept" in avail


def test_none_valued_binding_is_dropped_not_declared() -> None:
    """A None slips through JSON tool args easily; it must not count as supplied."""
    run, avail = _nx_answer_caller_bindings(
        question=Q, scope="", bindings={"content_type": None},
    )
    assert "content_type" not in run
    assert "content_type" not in avail, (
        "declaring a None-valued binding as available would let the "
        "matcher offer a plan that then has nothing to bind"
    )
