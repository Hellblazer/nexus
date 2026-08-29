# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-50l6y: repo-wide census closing the operator-arg-name class.

The 3 targeted tests in ``tests/test_builtin_plan_operator_arg_names.py``
prove the 3 KNOWN instances (hybrid-factual-lookup.yml's generate step,
review-default.yml + document-default.yml's compare steps) are fixed —
but they name exactly those 3 files. The bead's own comment thread asked
TWICE for the generic close: "a lint asserting every plan step's args are
parameters the named operator actually accepts... without it the fifth
instance ships the next time someone writes a plan from memory." A
future builtin template (or an edit to any of the other 9 untouched
ones) with a mismatched operator arg name would ship silently broken
through the 3 point tests with zero warning.

This lint walks EVERY ``conexus/plans/builtin/*.yml`` step, resolves the
operator it dispatches through the SAME two tables the runtime uses
(``_OPERATOR_TOOL_MAP``, ``_INPUTS_TARGET`` — src/nexus/plans/runner.py),
and asserts every arg key is either:

  * a real parameter of the resolved operator's ``nexus.mcp.core``
    function (via ``inspect.signature`` — the actual source of truth,
    not a hand-maintained copy of each operator's param list), or
  * the ``inputs`` alias, when ``_INPUTS_TARGET`` maps the resolved
    operator to a real parameter (the nexus-yis0 pre-hydration path), or
  * ``ids``/``collections``, which the runner's auto-hydration branch
    consumes and strips for ANY operator step before dispatch
    (unconditional on ``resolved_tool in _OPERATOR_RESOLVED_TOOLS and
    "ids" in args`` — see ``_hydrate_operator_args``; ``abstract-
    themes.yml``'s groupby step is the one live builtin using this
    path today), or
  * ``template`` for ``operator_extract``, which the runner special-
    cases into ``fields`` when given as a dict (no live builtin uses
    this path today, but the runtime supports it, so the census must
    not flag it as though it didn't).

Same shape as the z0idx lint (``tests/test_log_call_message_kwarg_
lint.py``): AST-free here (YAML + ``inspect.signature`` is the right
introspection tool for this class, the way AST was right for the
Python-source class), self-falsifying per nexus-moht0 (inject a real
unmapped arg into a REAL builtin file, assert RED naming the file/
step/arg, revert, assert GREEN again), and non-vacuous (asserts the
scan actually walked at least as many files as the directory holds
today, so a path bug that silently examines zero files cannot pass).
"""
from __future__ import annotations

import inspect
import pathlib

import pytest
import yaml

from nexus.plans.runner import (
    _INPUTS_TARGET,
    _OPERATOR_RESOLVED_TOOLS,
    _OPERATOR_TOOL_MAP,
)

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
BUILTIN_DIR = REPO_ROOT / "conexus" / "plans" / "builtin"

#: Historical floor (12 files, 2026-08-28) proving the scan walked the
#: real directory rather than a hardcoded subset or nothing at all. A
#: future file addition only raises the live glob count, which still
#: satisfies ``>=``; a scan that silently finds 0 or 3 files (the exact
#: vacuous-census shape code-review-expert's round-1 FAIL was about)
#: fails this floor immediately.
_MIN_BUILTIN_FILE_COUNT = 12

#: Consumed by the runner's auto-hydration branch for ANY operator step
#: (unconditional on ``resolved_tool in _OPERATOR_RESOLVED_TOOLS and "ids"
#: in args`` — see ``_hydrate_operator_args``), never a parameter of the
#: operator function itself.
_ALWAYS_ALLOWED_OPERATOR_ARGS = frozenset({"ids", "collections"})


def _operator_signature(resolved_tool: str) -> inspect.Signature | None:
    """The real ``inspect.Signature`` of *resolved_tool* in
    ``nexus.mcp.core``, or ``None`` if it isn't there / isn't callable /
    isn't introspectable — mirrors ``_default_dispatcher``'s own
    ``getattr`` + ``inspect.signature`` resolution exactly, so this
    census can never see a different operator surface than the runner
    actually dispatches against.
    """
    from nexus.mcp import core as mcp_core  # noqa: PLC0415 — deferred: heavy/optional dep, matches runner.py's own pattern

    fn = getattr(mcp_core, resolved_tool, None)
    if fn is None or not callable(fn):
        return None
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _step_violations(
    filename: str, step_index: int, tool: object, args: object,
) -> list[str]:
    """Violations for one plan step, or ``[]`` if the step is fine or out
    of scope (not an operator dispatch — search/traverse/store_get_many/
    query/search_metadata_scoped all resolve to themselves, which is
    never in ``_OPERATOR_RESOLVED_TOOLS``, so they are skipped by
    design: this census is scoped to the operator arg-name class, the
    same way the coordinator's ask named "resolve the operator it
    dispatches").
    """
    if not isinstance(tool, str):
        return []
    resolved = _OPERATOR_TOOL_MAP.get(tool, tool)
    if resolved not in _OPERATOR_RESOLVED_TOOLS:
        return []

    sig = _operator_signature(resolved)
    if sig is None:
        return [
            f"{filename} step {step_index} (tool={tool!r} -> {resolved!r}): "
            f"operator not found in nexus.mcp.core"
        ]

    accepts_any_kwarg = any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )
    if accepts_any_kwarg:
        return []

    if not isinstance(args, dict):
        return []

    known = set(sig.parameters.keys())
    aliased_inputs_ok = resolved in _INPUTS_TARGET
    violations: list[str] = []
    for key in args:
        if key in known:
            continue
        if key in _ALWAYS_ALLOWED_OPERATOR_ARGS:
            continue
        if key == "inputs" and aliased_inputs_ok:
            continue
        if resolved == "operator_extract" and key == "template":
            continue
        violations.append(
            f"{filename} step {step_index} (tool={tool!r} -> {resolved!r}): "
            f"arg {key!r} is not a parameter of {resolved!r} "
            f"(known: {sorted(known)}) and not a recognised alias"
        )
    return violations


def census(plan_dir: pathlib.Path) -> tuple[list[str], int, int]:
    """Walk every ``*.yml`` in *plan_dir*. Returns
    ``(violations, files_scanned, operator_steps_examined)``.
    """
    violations: list[str] = []
    files_scanned = 0
    operator_steps_examined = 0
    for path in sorted(plan_dir.glob("*.yml")):
        files_scanned += 1
        raw = yaml.safe_load(path.read_text()) or {}
        steps = ((raw.get("plan_json") or {}).get("steps")) or []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            tool = step.get("tool")
            resolved = _OPERATOR_TOOL_MAP.get(tool, tool) if isinstance(tool, str) else None
            if resolved in _OPERATOR_RESOLVED_TOOLS:
                operator_steps_examined += 1
            violations.extend(
                _step_violations(path.name, i, tool, step.get("args")),
            )
    return violations, files_scanned, operator_steps_examined


# ── Unit tests for the detector itself (prove it, not just the tree) ──────


class TestCensusDetectsTheKnownBugShapes:
    def test_flags_generate_step_with_outline_and_with_citations(
        self, tmp_path,
    ) -> None:
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "generate", "args": {"outline": "x", "with_citations": True}},
            ]},
        }))
        violations, _, _ = census(tmp_path)
        joined = " ".join(violations)
        assert "outline" in joined
        assert "with_citations" in joined

    def test_flags_compare_step_with_criterion(self, tmp_path) -> None:
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "compare", "args": {"criterion": "x", "inputs": "y"}},
            ]},
        }))
        violations, _, _ = census(tmp_path)
        assert any("criterion" in v for v in violations)
        # 'inputs' IS a valid alias for compare (-> items); must not also
        # be flagged.
        assert not any("'inputs'" in v for v in violations)


class TestCensusNegativeControls:
    """Non-vacuity for the detector itself: proves it does not simply
    flag every step, or every args dict."""

    def test_does_not_flag_rank_step_with_criterion(self, tmp_path) -> None:
        """criterion IS operator_rank's real second positional param —
        must not be confused with compare's identically-named-looking
        but WRONG usage above."""
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "rank", "args": {"criterion": "x", "inputs": "y"}},
            ]},
        }))
        violations, _, _ = census(tmp_path)
        assert violations == []

    def test_does_not_flag_groupby_step_using_ids_and_collections(
        self, tmp_path,
    ) -> None:
        """abstract-themes.yml's live shape: ids/collections are consumed
        by the runner's auto-hydration branch, not a groupby parameter,
        and must never be flagged as unmapped."""
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "groupby", "args": {
                    "ids": "$step1.ids", "collections": "$step1.collections",
                    "key": "topic",
                }},
            ]},
        }))
        violations, _, _ = census(tmp_path)
        assert violations == []

    def test_does_not_flag_extract_with_template_dict(self, tmp_path) -> None:
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "extract", "args": {
                    "inputs": "$step1.contents", "template": {"a": 1},
                }},
            ]},
        }))
        violations, _, _ = census(tmp_path)
        assert violations == []

    def test_does_not_flag_non_operator_retrieval_steps(self, tmp_path) -> None:
        """search/traverse/store_get_many are out of scope by design —
        this census is the operator-arg-name class only."""
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "search", "args": {"totally_made_up_arg": "x"}},
                {"tool": "traverse", "args": {"another_bogus_one": "y"}},
                {"tool": "store_get_many", "args": {"whatever": "z"}},
            ]},
        }))
        violations, _, _ = census(tmp_path)
        assert violations == []

    def test_counts_operator_steps_examined(self, tmp_path) -> None:
        (tmp_path / "p.yml").write_text(yaml.dump({
            "plan_json": {"steps": [
                {"tool": "search", "args": {"query": "x"}},
                {"tool": "rank", "args": {"items": "x", "criterion": "y"}},
                {"tool": "compare", "args": {"items": "x", "focus": "y"}},
            ]},
        }))
        _, files_scanned, operator_steps = census(tmp_path)
        assert files_scanned == 1
        assert operator_steps == 2  # rank + compare; search is not an operator


class TestCensusSelfFalsifies:
    """nexus-moht0 vacuous-gate doctrine: inject a real violation into a
    REAL builtin plan file, assert the scan goes RED naming the file/
    step/arg, then revert and assert GREEN again."""

    def test_injected_unmapped_arg_in_a_real_builtin_is_caught(self) -> None:
        target = BUILTIN_DIR / "abstract-themes.yml"
        original = target.read_text()
        raw = yaml.safe_load(original)
        steps = raw["plan_json"]["steps"]
        rank_or_compare_index = next(
            i for i, s in enumerate(steps)
            if isinstance(s, dict) and s.get("tool") in ("rank", "compare", "summarize", "extract", "groupby", "aggregate")
        )
        steps[rank_or_compare_index]["args"]["totally_unmapped_probe_arg"] = "x"
        poisoned = yaml.dump(raw, sort_keys=False)
        try:
            target.write_text(poisoned)
            violations, _, _ = census(BUILTIN_DIR)
            assert any(
                "abstract-themes.yml" in v and "totally_unmapped_probe_arg" in v
                for v in violations
            ), f"injected violation was not caught: {violations}"
        finally:
            target.write_text(original)
        # GREEN again post-revert.
        violations_after, _, _ = census(BUILTIN_DIR)
        assert not any("totally_unmapped_probe_arg" in v for v in violations_after)


# ── The actual gate ────────────────────────────────────────────────────────


class TestBuiltinPlanOperatorArgCensus:
    def test_census_examines_the_whole_directory_not_a_subset(self) -> None:
        """Non-vacuity: the scan must have walked at least as many files
        as the directory holds today. A scan wired to a hardcoded subset
        (or a path bug examining zero files) fails this immediately."""
        _, files_scanned, operator_steps_examined = census(BUILTIN_DIR)
        live_count = len(list(BUILTIN_DIR.glob("*.yml")))
        assert files_scanned == live_count
        assert files_scanned >= _MIN_BUILTIN_FILE_COUNT, (
            f"expected to scan >= {_MIN_BUILTIN_FILE_COUNT} builtin plan "
            f"files, only found {files_scanned} — census may be pointed "
            f"at the wrong directory"
        )
        assert operator_steps_examined > 0, (
            "zero operator-dispatched steps examined across the entire "
            "builtin plan library — the census matched nothing, which "
            "is the vacuous-gate shape this test exists to catch"
        )

    def test_every_builtin_plan_step_args_match_its_resolved_operator(
        self,
    ) -> None:
        violations, _, _ = census(BUILTIN_DIR)
        assert violations == [], (
            f"builtin plan step(s) pass an arg name their resolved "
            f"operator does not accept (nexus-50l6y class): {violations}"
        )
