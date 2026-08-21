# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-y6ywo: no inline ``$var``/``$stepN.field`` tokens in builtin plans.

``_resolve_value`` (src/nexus/plans/runner.py) substitutes a ``$var`` or
``$stepN.field`` reference ONLY when the arg VALUE IS EXACTLY that single
token — its own docstring says "no inline interpolation". A token embedded
inside a longer string (e.g. ``"key themes about $intent in these chunks"``)
is never substituted and reaches the operator as literal text: nothing
errors, nothing logs, the prompt is just quietly less grounded than the
plan author intended.

Five shipped builtin plans shipped exactly that shape (abstract-themes,
debug-default, plan-author-default, plan-promote-propose, review-default);
a sixth (hybrid-factual-lookup) was found independently while building this
lint and fixed alongside them. This test makes the shape unshippable: it
scans every ``args``/``scope`` value in every builtin plan's ``plan_json``
for a dollar-token-shaped substring that is not the value's entire content.

Deliberately NOT reusing ``runner._scan_var_refs``: that function mirrors
``_resolve_value``'s own resolution-eligibility traversal (it does not
recurse into dict values, because ``_resolve_value`` itself never resolves
inside a dict) for a different purpose — nexus-pucte's binding-validation
gate. This lint's scan recurses into dict values too, so it catches an
INLINE dollar-token wherever one is textually reachable in args/scope,
including inside a nested dict — but that recursion does NOT close the
gap it might look like it closes. ``_resolve_value`` never resolves
*inside* a dict-valued arg AT ALL — dicts fall through its
``isinstance(value, str)`` / ``isinstance(value, list)`` checks to a bare
``return value`` unchanged, regardless of what's inside. So a WHOLE-VALUE
``$var`` sitting inside a nested dict arg (e.g.
``args: {options: {threshold: "$threshold"}}``) is exactly as dead at
runtime as an inline one would be, but this lint does NOT flag it —
flagging a bare ``$var`` would mean banning the one shape that IS
legitimate everywhere else the runtime looks, and this lint has no way to
tell "inside a dict" apart from "inside a resolvable arg" without
duplicating the runtime's own traversal decision. No builtin template
currently nests a dict inside an ``args`` value, so this is a latent gap,
not an active one; closing it for real would mean teaching
``_resolve_value`` to recurse into dicts (a runtime semantics change,
out of scope for this bead's option (b)/(c) — see the bead's option (a)
discussion), not widening this matcher.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.lint

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"

#: Matches a dollar-token shaped exactly like the two forms
#: ``_resolve_value`` recognizes: ``$stepN.field`` and ``$var``. The
#: step-reference alternative is listed FIRST so ``$step1.tumblers`` matches
#: as one token rather than the bare-var alternative claiming just
#: ``$step1`` and leaving ``.tumblers`` behind (Python ``re`` alternation
#: takes the first branch that succeeds at a given position, no
#: longest-match backtracking across alternatives). A digit or punctuation
#: immediately after ``$`` (``$5.00``, ``$(cmd)``, ``${VAR}``, ``$1``)
#: matches neither branch and is correctly never a candidate — this is
#: what makes the lint grammar-aware rather than a bare ``"$" in value``
#: character ban.
_TOKEN_RE = re.compile(r"\$(?:step\d+\.[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)")


def find_inline_var_refs(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Return ``(path, value)`` for every string reachable from *value*
    that contains a ``$var``/``$stepN.field``-shaped token which is NOT
    the string's entire content.

    Recurses through dicts and lists to REACH every string, then applies
    the SAME per-string inline-vs-whole-value check regardless of where
    the string was found: a string is flagged when exactly one token
    match exists but its span does not cover the whole string, or when
    more than one token-shaped substring is present (e.g. the
    two-variable ``verb=$target_verb, concept=$concept`` case) — either
    way, at least one token is not standing alone as the whole value, which
    is precisely the shape ``_resolve_value`` never substitutes.

    NOT covered (see the module docstring): a whole-value ``$var`` string
    found while recursing through a dict is NOT flagged, because as a
    standalone token it's the legitimate shape everywhere else — but
    ``_resolve_value`` never resolves anything nested inside a dict-valued
    arg regardless of shape, so such a token is dead at runtime too. This
    function does not attempt to distinguish "reachable and resolvable"
    from "reachable but inside a dict the runtime ignores"; it only
    distinguishes whole-value from inline for whatever string it reaches.
    """
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, sub in value.items():
            found.extend(find_inline_var_refs(sub, f"{path}.{key}"))
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            found.extend(find_inline_var_refs(sub, f"{path}[{i}]"))
    elif isinstance(value, str):
        matches = list(_TOKEN_RE.finditer(value))
        if not matches:
            return found
        whole_value_token = (
            len(matches) == 1 and matches[0].span() == (0, len(value))
        )
        if not whole_value_token:
            found.append((path, value))
    return found


# ── Synthetic proof (non-vacuity: the real-tree count is zero after the
# fix, so a test that only scans the tree proves nothing about the
# detector actually working) ────────────────────────────────────────────


def test_detects_inline_var_embedded_in_longer_string() -> None:
    """Positive sample: the exact shipped shape (abstract-themes.yml,
    pre-fix) — a ``$var`` token embedded inside surrounding prose."""
    hits = find_inline_var_refs(
        "key themes and findings about $intent in these chunks",
        path="steps[2].args.reducer",
    )
    assert hits == [
        ("steps[2].args.reducer", "key themes and findings about $intent in these chunks"),
    ]


def test_detects_two_inline_vars_in_one_string() -> None:
    """Positive sample: the plan-author-default.yml shape — two distinct
    tokens embedded in one string, neither standing alone."""
    hits = find_inline_var_refs(
        "plan template for verb=$target_verb, concept=$concept",
        path="steps[3].args.outline",
    )
    assert len(hits) == 1
    assert hits[0][0] == "steps[3].args.outline"


def test_ignores_legitimate_whole_value_var() -> None:
    """Negative control: a value that IS exactly one ``$var`` token — the
    shape ``_resolve_value`` DOES substitute — must not be flagged."""
    assert find_inline_var_refs("$intent") == []


def test_ignores_legitimate_whole_value_stepref() -> None:
    """Negative control: a value that IS exactly one ``$stepN.field``
    token — also substituted correctly — must not be flagged."""
    assert find_inline_var_refs("$step2.groups") == []


def test_ignores_dollar_price_not_a_variable() -> None:
    """Negative control: a dollar sign that is not a variable at all (a
    price). The matcher is grammar-aware, not a bare '$' character ban —
    a digit immediately after '$' matches neither token alternative."""
    assert find_inline_var_refs("Estimated cost: $5.00 per call") == []


def test_ignores_shell_snippet_not_a_variable() -> None:
    """Negative control: a dollar sign in a shell idiom that is not a
    nexus plan variable. ``$(...)`` command substitution has punctuation
    immediately after '$', which matches neither token alternative."""
    assert find_inline_var_refs("Run cleanup via $(rm -rf tmp/)") == []


def test_recurses_into_nested_dict_and_list_values() -> None:
    """The detector must reach values nested under dict keys and inside
    lists, not just top-level string args — an inline token buried in a
    nested structure is exactly as silently-literal as a top-level one."""
    nested = {
        "outer": {
            "inner_list": [
                "fine value",
                "$whole_token",
                "not fine: $var embedded here",
            ],
        },
    }
    hits = find_inline_var_refs(nested)
    assert len(hits) == 1
    assert hits[0][1] == "not fine: $var embedded here"


# ── Real-tree gate ───────────────────────────────────────────────────────


@pytest.mark.skipif(
    not _BUILTIN_DIR.exists(),
    reason="conexus/plans/builtin/ dir is absent - defensive skip",
)
def test_no_builtin_plan_has_an_inline_var_token() -> None:
    """Fail the build on an inline dollar-token in ANY builtin plan
    template's ``plan_json.steps[].args``/``.scope`` — the shape must not
    ship again (nexus-y6ywo option (c)).

    Non-vacuity: assert at least one *.yml file was actually scanned, so a
    misconfigured or emptied builtin dir reads as a failure, not a silent
    pass (nexus-moht0 vacuous-gate doctrine).
    """
    yaml_files = sorted(_BUILTIN_DIR.glob("*.yml"))
    assert yaml_files, (
        f"{_BUILTIN_DIR} contains no *.yml files — nothing was scanned; "
        "this must not read as a passing lint"
    )

    violations: list[str] = []
    for path in yaml_files:
        raw = yaml.safe_load(path.read_text())
        plan_json = raw.get("plan_json") or {}
        steps = plan_json.get("steps") or []
        for step_index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            for surface in ("args", "scope"):
                sub = step.get(surface)
                if sub is None:
                    continue
                for ref_path, value in find_inline_var_refs(
                    sub, f"steps[{step_index}].{surface}",
                ):
                    violations.append(f"{path.name}: {ref_path} = {value!r}")

    assert violations == [], (
        "Inline $var/$stepN.field token(s) found in builtin plan args/scope "
        "— _resolve_value only substitutes a value that IS EXACTLY one "
        "token, so this reaches the operator as literal text with no "
        "error and no log. Move the surrounding prose into a sibling arg "
        "the operator already accepts, or reduce to a whole-value token "
        "(nexus-y6ywo):\n" + "\n".join(violations)
    )
