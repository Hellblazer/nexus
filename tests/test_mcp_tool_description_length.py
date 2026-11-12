# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP tool description length, measured the way the harness receives it (split out of
tests/test_mcp_tool_description_lint.py under its file-size ceiling).

The harness cuts a tool description at 2048 characters. FastMCP sends the function's
``__doc__``: Python 3.12 keeps the docstring's indentation, 3.13 strips it at compile time,
so the measure is the docstring as written in source, the longest form any supported
runtime sends (GH #1524 follow-up; develop red at d9ac568a6).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import warnings

import pytest

from tests.test_mcp_tool_description_lint import (
    HARNESS_TRUNCATION_LIMIT,
    TARGET_LENGTH,
    _iter_tool_fns,
)

pytestmark = pytest.mark.lint

# ── Length ───────────────────────────────────────────────────────────────


# What the harness receives is the tool description as the running Python built it, and the
# runtimes differ: 3.12 keeps a docstring's indentation, 3.13 strips it at compile time. A lint
# run on 3.13 measured the shorter form and passed a search() description that CI's 3.12 measured
# at 2074 (develop red at d9ac568a6). The measure below is the longest form any supported
# runtime sends: the docstring as written in the source (which ``ast`` keeps raw on both).


def _indented_tool():
    """A long description, indented as written in source.

        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
    """


def _source_docstring(fn: object) -> str | None:
    """*fn*'s docstring exactly as written in its source, indentation kept (``None`` when the
    source cannot be read)."""
    try:
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]  # type: ignore[arg-type]
    except (OSError, TypeError, SyntaxError, IndexError):
        return None
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    return ast.get_docstring(node, clean=False)


def _sent_length(description: str, fn: object, *, raw_docstring: str | None = None) -> int:
    """The length of *description* as the longest-sending supported runtime sends it. A description
    taken from the docstring is measured as written in source (3.12 sends that; 3.13 strips the
    indentation); one passed explicitly is measured as given."""
    raw = raw_docstring if raw_docstring is not None else _source_docstring(fn)
    if raw and inspect.cleandoc(raw) == inspect.cleandoc(description):
        return max(len(description), len(raw))
    return len(description)


def _over_limit(rows: list[tuple[str, str, str, object]], limit: int) -> list[tuple[str, str, int]]:
    """(server, tool, sent length) for every row over *limit*, by :func:`_sent_length`: the one
    rule both length checks apply."""
    return [(s, n, k) for s, n, d, fn in rows if (k := _sent_length(d, fn)) > limit]


def test_the_measure_is_the_source_docstring_on_every_python() -> None:
    raw = ast.get_docstring(ast.parse(textwrap.dedent(inspect.getsource(_indented_tool))).body[0], clean=False)
    assert raw is not None and raw != inspect.cleandoc(raw), "the fixture must carry indentation"
    # As a 3.12 runtime sends it (raw), and as a 3.13 runtime sends it (stripped): one measure.
    for runtime_desc in (raw, inspect.cleandoc(raw)):
        assert _sent_length(runtime_desc, _indented_tool) == len(raw)


def test_a_docstring_over_the_cut_only_with_its_indentation_is_over() -> None:
    # Over the limit as 3.12 sends it, under as 3.13 sends it: the lint must call it over on both.
    body = "    The quick brown fox jumps over the lazy dog, again and again and again.\n"
    raw = "Long tool.\n\n" + body * 28
    stripped = inspect.cleandoc(raw)
    assert len(stripped) <= HARNESS_TRUNCATION_LIMIT < len(raw), (len(stripped), len(raw))

    assert _sent_length(stripped, len, raw_docstring=raw) > HARNESS_TRUNCATION_LIMIT
    assert _sent_length(raw, len, raw_docstring=raw) > HARNESS_TRUNCATION_LIMIT


def test_a_tool_whose_source_cannot_be_read_is_measured_as_given() -> None:
    # A builtin (or a closure whose docstring is set at runtime) has no readable source: the
    # description's own length, never 0.
    assert _source_docstring(len) is None
    assert _sent_length("x" * 3000, len) == 3000


def test_an_explicit_description_is_measured_as_given() -> None:
    assert _sent_length("short, set by description=", _indented_tool) == len("short, set by description=")


def test_no_description_exceeds_the_harness_truncation_limit() -> None:
    over = _over_limit(_iter_tool_fns(), HARNESS_TRUNCATION_LIMIT)
    assert not over, (
        f"{len(over)} tool description(s) exceed the {HARNESS_TRUNCATION_LIMIT}-char "
        f"harness truncation cut -- the harness silently cuts these mid-sentence, "
        f"dropping any parameter docs written into the docstring past that point:\n  "
        + "\n  ".join(f"{s}::{n} ({L} chars)" for s, n, L in over)
    )


def test_descriptions_stay_within_the_target_length() -> None:
    over = _over_limit(_iter_tool_fns(), TARGET_LENGTH)
    if over:
        warnings.warn(
            f"{len(over)} tool description(s) exceed the {TARGET_LENGTH}-char shape "
            "target (still under the hard harness cut): "
            + ", ".join(f"{s}::{n} ({L} chars)" for s, n, L in over),
            stacklevel=1,
        )


def _planted_over_only_with_indentation():
    """Planted tool.

        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
        The quick brown fox jumps over the lazy dog, again and again and again.
    """


def test_planted_oversized_description_is_detected() -> None:
    """Non-vacuity, through the rule the lint applies (_over_limit): a plain oversized description,
    and one that is under the cut as 3.13 sends it but over as 3.12 sends it, are both flagged."""
    raw = _source_docstring(_planted_over_only_with_indentation)
    assert raw is not None
    stripped = inspect.cleandoc(raw)
    assert len(stripped) <= HARNESS_TRUNCATION_LIMIT < len(raw), (len(stripped), len(raw))
    rows = [
        ("nexus", "plain", "x" * (HARNESS_TRUNCATION_LIMIT + 1), len),
        ("nexus", "indented", stripped, _planted_over_only_with_indentation),
        ("nexus", "short", "fine", len),
    ]
    assert [n for _s, n, _k in _over_limit(rows, HARNESS_TRUNCATION_LIMIT)] == ["plain", "indented"]


def test_most_tools_are_measured_by_their_source_docstring() -> None:
    """Non-vacuity floor for the raw path (nexus-moht0): a tool whose source docstring cannot be
    read falls back to its description, which on 3.13 is the stripped text the lint used to miss.
    52 of 57 tools take the raw path today; the 5 others pass ``description=`` or set the
    docstring at runtime."""
    rows = _iter_tool_fns()
    raw_path = [n for _s, n, d, fn in rows
                if (r := _source_docstring(fn)) and inspect.cleandoc(r) == inspect.cleandoc(d)]
    assert len(raw_path) >= 45, f"only {len(raw_path)} of {len(rows)} tools measured by their source docstring"
