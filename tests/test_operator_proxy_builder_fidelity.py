# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verbatim-fidelity check: each ``scripts/bench/operator_proxy.py``
prompt builder must produce the EXACT SAME prompt SHAPE as the
corresponding ``operator_*`` function in ``src/nexus/mcp/core.py``
(nexus-nyry9.14 code-review round, 2026-08-21 — an earlier ``build_rank``
silently added an instruction sentence core.py's real prompt never gives,
undetected because nothing diffed the two).

Round-2 review finding (2026-08-21): the round-1 version of this module
only checked that core.py's literal fragments appeared, IN ORDER, as
SUBSTRINGS of the harness prompt (``str.find`` with an advancing cursor).
Empirically demonstrated (by the reviewer, and reproduced here) to pass
on a harness prompt with an EXTRA sentence inserted between two real
fragments — exactly the historical bug class this module exists to
prevent, since substring-containment-in-order says nothing about whether
the harness prompt contains something ELSE too.

Fixed with a GENERIC, symmetric check: extract each ``prompt = ...``
f-string's full sequence of literal-text and substitution-slot nodes from
BOTH core.py's function AND the harness's own builder (same AST method,
applied to both sides), then render each sequence by replacing every
substitution slot with an identical positional placeholder
(``⟦SUB0⟧``, ``⟦SUB1⟧``, ...) and asserting the two rendered strings are
EXACTLY equal. This is stronger than substring-containment in every
direction: an extra/missing/reordered literal segment on EITHER side, or
a one-word change to any literal segment in core.py, produces a
different rendered string and fails the comparison — there is no longer
an asymmetric "only catches core.py-side drift" or "only catches
harness-side removal" blind spot.

Substituted CONTENT (criterion/items/claim/evidence VALUES) is
intentionally not compared — the proxy's fixture differs from core.py's
own docstring examples on purpose; only the constant template shape
(which literal text appears where, and how many substitution slots sit
between them) must match.
"""
from __future__ import annotations

import ast
import inspect

from bench.operator_proxy import (
    build_check,
    build_extract,
    build_filter,
    build_groupby,
    build_rank,
    build_verify,
)

from nexus.mcp import core as mcp_core

_CASES = [
    (build_extract, mcp_core.operator_extract),
    (build_rank, mcp_core.operator_rank),
    (build_filter, mcp_core.operator_filter),
    (build_check, mcp_core.operator_check),
    (build_verify, mcp_core.operator_verify),
    (build_groupby, mcp_core.operator_groupby),
]


def _prompt_joined_str(func_or_source) -> ast.JoinedStr:
    """Locate the ``prompt = ...`` f-string assignment inside a function
    (or already-extracted source text) and return its ``ast.JoinedStr``
    node. Works for both a real ``def`` (core.py's ``operator_*``
    functions) and a plain function object (the harness's ``build_*``
    functions) since both are extracted via ``inspect.getsource``.
    """
    source = func_or_source if isinstance(func_or_source, str) else inspect.getsource(func_or_source)
    tree = ast.parse(source)
    fn_node = tree.body[0]
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "prompt" for t in node.targets
        ):
            assert isinstance(node.value, ast.JoinedStr), (
                f"'prompt = ...' assignment is not an f-string (JoinedStr): {ast.dump(node.value)}"
            )
            return node.value
    raise AssertionError("no 'prompt = ...' f-string assignment found")


def _render_with_positional_placeholders(joined: ast.JoinedStr) -> str:
    """Render a ``JoinedStr``'s literal text verbatim, substituting every
    FormattedValue (substitution slot) with an identical, POSITION-KEYED
    placeholder -- so two JoinedStrs render equal iff they have the same
    literal text in the same order with substitution slots in the same
    positions, regardless of what variable name each slot actually
    references (core.py and the harness builder don't always use matching
    local variable names for the same semantic slot, e.g. core.py's
    ``check_instruction`` vs the harness's ``claim`` -- irrelevant here
    since only the SHAPE is compared, never the substituted value)."""
    out: list[str] = []
    slot_index = 0
    for value in joined.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            out.append(f"⟦SUB{slot_index}⟧")
            slot_index += 1
        else:
            raise AssertionError(f"unsupported JoinedStr element: {ast.dump(value)}")
    return "".join(out)


def _prompt_shape(func) -> str:
    return _render_with_positional_placeholders(_prompt_joined_str(func))


class TestBuilderFidelity:
    def test_case_count_is_non_vacuous(self) -> None:
        """All 6 in-scope operators must actually be compared -- a
        shrunk _CASES list would silently reduce coverage without any
        other test noticing."""
        assert len(_CASES) == 6

    def test_prompt_shape_is_byte_identical_to_core_py(self) -> None:
        """The generic, symmetric check: full-string equality of the
        rendered (literal-text + positional-placeholder) shape. A
        one-word change to ANY literal segment in core.py, or an extra/
        missing/reordered segment on EITHER side, fails this for the
        affected builder."""
        for builder, core_fn in _CASES:
            core_shape = _prompt_shape(core_fn)
            harness_shape = _prompt_shape(builder)
            assert harness_shape == core_shape, (
                f"{builder.__name__} prompt SHAPE has drifted from "
                f"nexus.mcp.core.{core_fn.__name__}'s real prompt.\n"
                f"core.py shape:   {core_shape!r}\n"
                f"harness shape:   {harness_shape!r}"
            )

    def test_rank_builder_adds_no_instruction_core_never_gives(self) -> None:
        """Regression pin for the original historical finding: build_rank()
        must NOT contain any sentence instructing the model to preserve id
        tags -- core.py's real prompt gives no such instruction. Redundant
        with test_prompt_shape_is_byte_identical_to_core_py (which now
        also catches this generically) but kept as a documented,
        human-readable regression marker for the specific incident."""
        prompt, _schema = build_rank()
        assert "preserve" not in prompt.casefold()
        assert "verbatim in your output" not in prompt.casefold()

    def test_harness_prompt_shape_extraction_is_non_vacuous(self) -> None:
        """Guard against the extractor silently finding zero substitution
        slots or literal text for some future refactor -- a pass with
        nothing meaningfully checked is not a pass."""
        for builder, core_fn in _CASES:
            core_joined = _prompt_joined_str(core_fn)
            harness_joined = _prompt_joined_str(builder)
            assert any(
                isinstance(v, ast.Constant) and v.value.strip()
                for v in core_joined.values
            ), f"{core_fn.__name__}: extractor found no real literal text"
            assert any(
                isinstance(v, ast.FormattedValue) for v in harness_joined.values
            ), f"{builder.__name__}: extractor found no substitution slot at all"

    def test_shape_render_detects_a_synthetic_extra_sentence(self) -> None:
        """Direct regression test for the round-2 finding itself: a
        harness prompt with an EXTRA sentence inserted (mirroring the
        exact historical build_rank bug) must fail the shape comparison.
        Constructed synthetically here so the test doesn't depend on the
        bug still being present anywhere in the codebase."""
        core_shape = _prompt_shape(mcp_core.operator_rank)
        bad_source = (
            "def _fake_build_rank():\n"
            "    criterion = 'x'\n"
            "    items = []\n"
            "    prompt = (\n"
            "        f\"Rank the following items by {criterion}.\\n\"\n"
            "        f\"Return them in ranked order, best first. Preserve the tag.\\n\\n\"\n"
            "        f\"Items:\\n{items}\"\n"
            "    )\n"
            "    return prompt\n"
        )
        bad_shape = _render_with_positional_placeholders(_prompt_joined_str(bad_source))
        assert bad_shape != core_shape, (
            "the synthetic buggy prompt (with an inserted sentence) must "
            "NOT render equal to core.py's real shape -- if it does, the "
            "shape-equality check has the same blind spot the round-1 "
            "substring check had"
        )
