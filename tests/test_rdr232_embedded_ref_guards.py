# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.4: the embedded-step-reference scan (nexus-jypmj.1.19) at the points before a run.

A ``$stepN.field`` inside a longer string or an object is never substituted, so a plan holding one
fails at run time. The same raw-arg scan now also runs at ``plan_save``, in the plan matcher (a logged
exclusion), in ``nx plan hygiene`` and on the planner's output (one re-prompt on a plan miss).

Tenant-free: the library is a stub, ``_t2_index_write`` is patched, and the planner dispatch is a fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

import pytest
from structlog.testing import capture_logs

BAD_CONTEXT = "Code:\n$step3.contents\n\nManifest:\n$step4.contents"


def _plan(context) -> dict:
    return {
        "steps": [
            {"tool": "search", "args": {"query": "q", "corpus": "code"}},
            {"tool": "search", "args": {"query": "r", "corpus": "code"}},
            {
                "tool": "store_get_many",
                "args": {"ids": "$step1.ids", "collections": "$step1.collections"},
            },
            {
                "tool": "store_get_many",
                "args": {"ids": "$step2.ids", "collections": "$step2.collections"},
            },
            {"tool": "generate", "args": {"template": "explain", "context": context}},
        ]
    }


BAD = _plan(BAD_CONTEXT)
GOOD = _plan(["$step3.contents", "$step4.contents"])


# ── the shared scan ──────────────────────────────────────────────────────────


def test_the_scan_names_the_step_arg_and_ref_in_a_json_string_or_a_mapping() -> None:
    from nexus.plans.runner import PlanRunStepRefError, find_embedded_step_ref

    for plan in (json.dumps(BAD), BAD):
        err = find_embedded_step_ref(plan)
        assert isinstance(err, PlanRunStepRefError)
        assert err.ref == "$step3.contents"
        assert "step 5" in err.reason and "'context'" in err.reason


def test_the_scan_passes_a_clean_plan_and_tolerates_what_is_not_a_plan() -> None:
    from nexus.plans.runner import find_embedded_step_ref

    assert find_embedded_step_ref(json.dumps(GOOD)) is None
    for not_a_plan in (
        None,
        "",
        "not json",
        "[]",
        {},
        {"steps": "x"},
        {"steps": [3, {"args": "x"}, {}]},
    ):
        assert find_embedded_step_ref(not_a_plan) is None


# ── plan_save ────────────────────────────────────────────────────────────────


def _save(plan: dict) -> tuple[str, list]:
    from nexus.mcp.core import plan_save

    writes: list = []

    def fake_write(fn, **_kw):  # noqa: ARG001
        writes.append(fn)
        return 7

    with (
        patch("nexus.mcp.core._t2_index_write", fake_write),
        patch("nexus.mcp.core._record_tier_write"),
    ):
        return plan_save(query="q", plan_json=json.dumps(plan), verb="research"), writes


def test_plan_save_refuses_an_embedded_ref_and_names_it_and_writes_nothing() -> None:
    out, writes = _save(BAD)
    assert out.lower().startswith("plan not saved") or "Plan not saved" in out
    assert "$step3.contents" in out and "step 5" in out and "'context'" in out
    assert writes == [], "nothing reached the library"


def test_plan_save_still_saves_the_clean_plan() -> None:
    """Non-vacuity: the refusal above is the scan, not a broken fixture."""
    out, writes = _save(GOOD)
    assert out.startswith("Saved plan: [7]")
    assert len(writes) == 1


# ── the matcher ──────────────────────────────────────────────────────────────


class _Library:
    """The three calls ``plan_match`` makes: get_plan, search_plans, increment_match_metrics."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = {r["id"]: r for r in rows}
        self.bumped: list[int] = []

    def get_plan(self, plan_id: int):
        return self.rows.get(plan_id)

    def search_plans(self, intent: str, limit: int = 10, project: str = ""):  # noqa: ARG002
        return list(self.rows.values())

    def increment_match_metrics(self, plan_id: int, confidence=None) -> None:  # noqa: ARG002
        self.bumped.append(plan_id)


class _Cache:
    def __init__(self, hits: list[tuple[int, float]]) -> None:
        self._hits = hits

    @property
    def is_available(self) -> bool:
        return True

    def query(self, intent: str, n: int):  # noqa: ARG002
        return self._hits[:n]


def _row(plan_id: int, plan: dict, query: str) -> dict:
    return {
        "id": plan_id,
        "query": query,
        "plan_json": json.dumps(plan),
        "tags": "",
        "project": "nexus",
    }


@pytest.fixture
def info_logging():
    """INFO-level structlog for the test, and the previous configuration back afterwards."""
    import logging

    import structlog

    saved = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )
    yield
    structlog.configure(**saved)


@pytest.mark.parametrize("path", ["t1", "fts"])
def test_a_saved_plan_with_an_embedded_ref_is_excluded_logged_and_kept(
    path: str, info_logging
) -> None:
    from nexus.plans.matcher import plan_match

    lib = _Library(
        [
            _row(1, BAD, "explain the tumbler walk"),
            _row(2, GOOD, "explain the tumbler walk, clean"),
        ]
    )
    cache = _Cache([(1, 0.01), (2, 0.05)]) if path == "t1" else None
    with capture_logs() as logs:
        matches = plan_match(
            intent="explain the tumbler walk",
            library=lib,
            cache=cache,
            min_confidence=0.5,
        )
    ids = [m.plan_id for m in matches]
    assert 2 in ids, "non-vacuity: the clean sibling still matches"
    assert 1 not in ids, "the plan holding an embedded ref was offered"
    excluded = [
        e for e in logs if e.get("event") == "plan_match_embedded_step_ref_excluded"
    ]
    assert len(excluded) == 1, excluded
    assert (
        excluded[0]["detail"][0]["plan_id"] == 1
        and excluded[0]["detail"][0]["ref"] == "$step3.contents"
    )
    assert "nx plan hygiene" in excluded[0]["remedy"]
    assert lib.rows[1]["plan_json"] == json.dumps(BAD), (
        "the row is neither rewritten nor deleted"
    )
    assert 1 not in lib.bumped, "an excluded plan is not counted as matched"


# ── nx plan hygiene ──────────────────────────────────────────────────────────


class _HygieneLibrary:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_plans(self, limit: int, include_disabled: bool = False):  # noqa: ARG002
        return self._rows


def test_hygiene_reports_a_plan_with_an_embedded_ref_and_leaves_a_clean_one() -> None:
    from nexus.commands.plan import _hygiene_scan

    rows = [
        {**_row(1, BAD, "embedded"), "verb": "research"},
        {**_row(2, GOOD, "clean"), "verb": "research"},
    ]
    findings = _hygiene_scan(_HygieneLibrary(rows))
    assert [f["id"] for f in findings] == [1], findings
    assert (
        "$step3.contents" in findings[0]["reason"] and "step 5" in findings[0]["reason"]
    )


# ── the planner: one re-prompt with the error text ───────────────────────────


def _planner_run(monkeypatch, replies: list[dict]):
    """Run ``_nx_answer_plan_miss`` against a fake dispatch that replays *replies*, like the real
    ``claude_dispatch`` charging each call to the ambient usage sink."""
    import asyncio

    import nexus.mcp.core as core
    from nexus.operators.dispatch import DispatchUsage, ambient_usage_sink

    prompts: list[str] = []
    sink: list = []

    async def fake_dispatch(prompt, schema, timeout=300.0, model=None, **kw):  # noqa: ARG001
        prompts.append(prompt)
        from nexus.operators.dispatch import _ambient_usage_sink

        _ambient_usage_sink.get().append(
            DispatchUsage(
                model="m",
                cost_usd=0.25,
                input_tokens=1,
                output_tokens=1,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                duration_ms=1,
                duration_api_ms=1,
                num_turns=1,
            )
        )
        reply = replies[len(prompts) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", fake_dispatch)
    monkeypatch.setattr("nexus.mcp_infra.get_collection_names", lambda: [])

    async def go():
        with ambient_usage_sink(sink):
            return await core._nx_answer_plan_miss("a brand new question")

    match = asyncio.run(go())
    return match, prompts, core._planner_cost_usd(sink)


def test_a_planner_plan_with_an_embedded_ref_is_re_prompted_once_with_the_error_text(
    monkeypatch,
) -> None:
    match, prompts, cost = _planner_run(monkeypatch, [BAD, GOOD])
    assert len(prompts) == 2
    assert prompts[1].startswith(prompts[0]), (
        "the retry is the same prompt with the error appended"
    )
    assert (
        "$step3.contents" in prompts[1][len(prompts[0]) :]
        and "step 5" in prompts[1][len(prompts[0]) :]
    )
    assert json.loads(match.plan_json)["steps"][4]["args"]["context"] == [
        "$step3.contents",
        "$step4.contents",
    ]
    assert cost == pytest.approx(0.50), (
        "the second planner call's cost is part of the planner cost"
    )


def test_a_clean_first_plan_is_not_re_prompted(monkeypatch) -> None:
    match, prompts, cost = _planner_run(monkeypatch, [GOOD])
    assert len(prompts) == 1
    assert cost == pytest.approx(0.25)
    assert json.loads(match.plan_json) == GOOD


def test_a_second_bad_plan_is_returned_for_plan_run_to_refuse_and_is_not_retried_again(
    monkeypatch,
) -> None:
    from nexus.plans.runner import find_embedded_step_ref

    match, prompts, cost = _planner_run(
        monkeypatch, [BAD, _plan("again $step3.contents")]
    )
    assert len(prompts) == 2, "one retry, no more"
    assert find_embedded_step_ref(match.plan_json) is not None, (
        "plan_run refuses it exactly as before"
    )
    assert cost == pytest.approx(0.50)


_RETRY_HEAD = "\n\nYour previous plan was refused before it ran: "
_RETRY_TAIL = (
    "\nReturn the plan again with every argument exactly one $stepN.<field> reference, "
    "a list of them, or a literal with no reference in it."
)


def _braced_key_plan(key: str) -> dict:
    """A plan whose offending arg KEY is planner-controlled text. The refusal names the key (not the
    value), so the key is what reaches the retry prompt."""
    plan = _plan("x")
    plan["steps"][4]["args"] = {"template": "explain", key: "a $step3.contents b"}
    return plan


def test_the_error_text_is_appended_not_interpolated(monkeypatch) -> None:
    """Braces in the planner's own text must survive: the retry is concatenation, not str.format.
    The braces sit in the arg KEY, which is the part of the refusal that carries planner text."""
    from nexus.plans.runner import find_embedded_step_ref

    nasty = _braced_key_plan("{0}{question}%s{")
    error_text = str(find_embedded_step_ref(nasty))[:200]
    assert "{0}{question}%s{" in error_text, "non-vacuity: the braces are in the appended error text"
    match, prompts, _ = _planner_run(monkeypatch, [nasty, GOOD])
    assert len(prompts) == 2
    assert prompts[1] == prompts[0] + _RETRY_HEAD + error_text + _RETRY_TAIL
    assert json.loads(match.plan_json)["steps"][4]["args"]["context"] == [
        "$step3.contents",
        "$step4.contents",
    ]


def test_the_appended_error_text_is_capped(monkeypatch) -> None:
    """The arg key is the planner's own text and unbounded; the retry prompt takes 200 characters."""
    from nexus.plans.runner import find_embedded_step_ref

    long_key = "k" * 5000
    bad = _braced_key_plan(long_key)
    assert len(str(find_embedded_step_ref(bad))) > 5000
    _, prompts, _ = _planner_run(monkeypatch, [bad, GOOD])
    appended = prompts[1][len(prompts[0]) :]
    error_part = appended[len(_RETRY_HEAD) : len(appended) - len(_RETRY_TAIL)]
    assert appended.startswith(_RETRY_HEAD) and appended.endswith(_RETRY_TAIL)
    assert len(error_part) == 200
    assert "$step3.contents" in error_part, "the ref survives the cap"


@pytest.mark.parametrize(
    "second",
    [RuntimeError("operator timed out"), {"steps": []}, {"steps": [{"tool": "frobnicate", "args": {}}]}],
    ids=["dispatch_raises", "empty_plan", "no_dispatchable_tool"],
)
def test_a_failed_retry_leaves_the_first_plan_for_plan_run_to_refuse(
    monkeypatch, second
) -> None:
    """A retry that times out or returns nothing usable must not hide the real refusal: the first
    plan is returned, and its embedded ref is what plan_run reports, as before the retry existed."""
    from nexus.plans.runner import find_embedded_step_ref

    with capture_logs() as logs:
        match, prompts, _ = _planner_run(monkeypatch, [BAD, second])
    assert len(prompts) == 2, "the retry was attempted"
    assert json.loads(match.plan_json) == BAD
    err = find_embedded_step_ref(match.plan_json)
    assert err is not None and err.ref == "$step3.contents"
    failed = [e for e in logs if e.get("event") == "nx_answer_planner_step_ref_retry_failed"]
    assert len(failed) == 1


# ── the single_query fast path ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    ["$step1.ids", "see $step1.ids"],
    ids=["whole_value_of_a_step_that_never_ran", "inside_text"],
)
def test_the_single_query_fast_path_handles_a_step_ref_error_like_an_unresolved_var(
    question: str,
) -> None:
    """``resolve_step_bindings`` raises PlanRunStepRefError for a ref to a step that never ran (the
    only shape the matcher still lets reach this path) or inside text; the fast path bypasses
    plan_run, so it must refuse that loudly rather than fall to the generic handler."""
    import asyncio
    from unittest.mock import MagicMock

    from nexus.mcp.core import nx_answer
    from nexus.plans.match import Match

    plan = {
        "steps": [
            {
                "tool": "query",
                "args": {"question": question, "corpus": "code", "limit": 5},
            }
        ]
    }
    best = Match(
        plan_id=9,
        name="one-query",
        description="d",
        confidence=0.9,
        dimensions={},
        tags="",
        plan_json=json.dumps(plan),
        required_bindings=[],
        optional_bindings=[],
        default_bindings={},
        parent_dims=None,
    )
    db = MagicMock()
    queried: list = []

    def fake_query(**kwargs):
        queried.append(kwargs)
        return "x"

    with (
        capture_logs() as logs,
        patch("nexus.plans.matcher.plan_match", return_value=[best]),
        patch("nexus.mcp.core._t2_ctx") as t2_ctx,
        patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)),
        patch("nexus.mcp.core.scratch", return_value="ok"),
        patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None),
        patch("nexus.mcp.core.query", fake_query),
    ):
        t2_ctx.return_value.__enter__.return_value = db
        out = asyncio.run(nx_answer(question="q", structured=False))
    assert queried == [], "query() is never called with a literal $step token"
    assert "$step1.ids" in str(out)
    assert any(e.get("event") == "nx_answer_single_query_step_ref" for e in logs), [
        e.get("event") for e in logs
    ]


# ── the seed loader and the builtin plans ────────────────────────────────────

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"


class _SeedLibrary:
    """What ``load_seed_directory`` asks of a library, without a tenant."""

    def __init__(self) -> None:
        self.saved: list[dict] = []

    def get_plan_by_dimensions(self, project: str, dimensions: str):  # noqa: ARG002
        return None

    def list_plans(self, limit: int, project: str = "", include_disabled: bool = False):  # noqa: ARG002
        return []

    def save_plan(self, **kwargs) -> int:
        self.saved.append(kwargs)
        return len(self.saved)

    def delete_plan(self, plan_id: int) -> None:  # noqa: ARG002
        raise AssertionError("nothing to delete")


def _template(name: str, plan_json: dict) -> dict:
    return {
        "name": name,
        "description": f"Embedded-ref seed fixture {name}.",
        "dimensions": {"verb": "research", "scope": "global", "strategy": name},
        "tags": "builtin-template,test-fixture",
        "plan_json": plan_json,
    }


def test_the_seed_loader_refuses_a_template_with_an_embedded_ref_by_name(tmp_path) -> None:
    from nexus.plans.seed_loader import load_seed_directory

    (tmp_path / "bad.yml").write_text(yaml.safe_dump(_template("bad-embedded", BAD)))
    (tmp_path / "good.yml").write_text(yaml.safe_dump(_template("good-clean", GOOD)))
    lib = _SeedLibrary()
    result = load_seed_directory(tmp_path, library=lib)
    assert result.inserted == ["good.yml"], "non-vacuity: the clean sibling still seeds"
    assert [e[0].rsplit("/", 1)[-1] for e in result.errors] == ["bad.yml"], result.errors
    message = result.errors[0][1]
    assert "bad-embedded" in message and "$step3.contents" in message and "step 5" in message
    assert [row["name"] for row in lib.saved] == ["good-clean"], "nothing was saved for the bad plan"


def test_no_builtin_plan_holds_an_embedded_step_ref() -> None:
    """The census of 2026-10-26, as a pin: every shipped template passes the runner's own scan."""
    from nexus.plans.runner import find_embedded_step_ref

    paths = sorted(_BUILTIN_DIR.glob("*.yml"))
    assert len(paths) >= 12, f"walked too few builtin plans: {len(paths)}"
    flagged, steps_seen = [], 0
    for path in paths:
        plan = yaml.safe_load(path.read_text())["plan_json"]
        steps_seen += len(plan["steps"])
        if (err := find_embedded_step_ref(plan)) is not None:
            flagged.append((path.name, err.ref))
    assert steps_seen >= 44, f"walked too few steps: {steps_seen}"
    assert flagged == []
