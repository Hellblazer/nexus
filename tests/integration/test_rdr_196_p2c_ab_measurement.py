# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-196 .p2c (nexus-nyry9.16): all-strong baseline vs tiered candidate
A/B on the quality proxy + Phase 1 cost telemetry, per operator.

Real money. Dispatches N_PER_ARM real claude -p calls per arm per
operator (filter/groupby/rank/extract), plus N_PER_ARM per arm for the
inline planner (force_dynamic=True). See T2
``nexus_rdr/196-phase2-ab-measurement-REGISTERED-2026-08-21`` for the
pre-registered refutation criteria (timestamped BEFORE the first run
below) and the mutable working entry
``nexus_rdr/196-phase2-ab-measurement`` for results.

check/verify are OUT of scope here: both are strong-tier in EVERY arm
per ``OPERATOR_MODEL_TIER`` (.p2b), so there is no tiering delta to
measure for them. aggregate/summarize/compare/generate are OUT per the
.p2a scope limit (no quality proxy exists — bead comment, 2026-08-21).
operator_verify is additionally flagged UNDECIDABLE on the .p2a proxy
(thin margin, n=2) — not run here either.

Run explicitly (NOT part of any default suite run):
    uv run pytest -m integration -p no:xdist -s \
        tests/integration/test_rdr_196_p2c_ab_measurement.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pytest

_BENCH_PARENT = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_BENCH_PARENT) not in sys.path:
    sys.path.insert(0, str(_BENCH_PARENT))
from bench.operator_proxy_ab import (  # noqa: E402
    stitch_run_id,
    total_dispatch_cost,
    total_dispatch_count,
)

pytestmark = [pytest.mark.integration, pytest.mark.lived_in]

#: Overridable via env for a cheap pilot run before spending the full
#: measurement budget (nexus-nyry9.16 DO 2: "same box, same day" — the
#: pilot and the full run must still be the same invocation shape, only
#: N and scope shrink).
N_PER_ARM = int(os.environ.get("NX_P2C_N_PER_ARM", "3"))
_OPERATORS = tuple(
    op for op in os.environ.get("NX_P2C_OPERATORS", "filter,groupby,rank,extract").split(",")
    if op
)
_INCLUDE_PLANNER = os.environ.get("NX_P2C_INCLUDE_PLANNER", "1") == "1"

_TIER_ENV = "NX_OPERATOR_MODEL_TIERING"

#: Portable default (code-review fix, T2 [23144] Critical #2): raw
#: results go under ``bench/out/`` (gitignored, same convention as
#: .p2a's ``scripts/bench/operator_proxy.py --out``), never a
#: session-specific scratchpad path. ``NX_P2C_OUT_PATH`` overrides for a
#: caller that wants a different location (e.g. a parallel run).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OUT_PATH = Path(
    os.environ.get(
        "NX_P2C_OUT_PATH",
        str(_REPO_ROOT / "bench" / "out" / "nyry9_16_ab_results.json"),
    )
)


def _claude_auth_available() -> bool:
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return bool(data.get("loggedIn") or data.get("isLoggedIn"))
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module", autouse=True)
def _skip_without_auth():
    if not _claude_auth_available():
        pytest.skip("claude auth not available — skipping live A/B measurement")


def _plan_for(op: str) -> tuple[str, str, dict]:
    """Mirrors scripts/bench/operator_proxy.py's builders (.p2a) exactly
    so the candidate/baseline outputs are scoreable with the SAME
    metric functions against the SAME fixture."""
    from bench.operator_proxy import FIXTURE_ITEMS

    items_json = json.dumps(FIXTURE_ITEMS)
    if op == "filter":
        args = {"items": items_json, "criterion": "the item's category is 'hooks'"}
    elif op == "groupby":
        args = {"items": items_json, "key": "category"}
    elif op == "rank":
        tagged = json.dumps([f"[{it['id']}] {it['title']}" for it in FIXTURE_ITEMS])
        args = {
            "items": tagged,
            "criterion": (
                "how directly the RDR bears on storage/identity "
                "mechanisms (most direct first)"
            ),
        }
    elif op == "extract":
        args = {"inputs": items_json, "fields": "id,category,year"}
    else:
        raise ValueError(op)
    question = (
        f"RDR-196 .p2c A/B fixture run: {op} operator on the frozen "
        f"8-item fixture set (nexus-nyry9.16)"
    )
    plan_json = json.dumps({
        "steps": [{"tool": op, "args": args}],
        "tools_used": [f"operator_{op}"],
        "outcome_notes": "RDR-196 .p2c A/B measurement fixture plan",
    })
    return question, plan_json, args


@pytest.mark.asyncio
async def test_p2c_ab_measurement(request) -> None:
    from nexus.mcp.core import nx_answer, plan_save
    from nexus.mcp_infra import t2_ctx
    from nexus.operators.dispatch import ambient_usage_sink

    # MERGE mode (nexus-nyry9.16 budget discipline): a prior invocation's
    # results at _OUT_PATH are loaded and appended to rather than
    # overwritten, so a multi-invocation measurement (smaller N_PER_ARM
    # slices run separately to keep spend visible between calls) doesn't
    # discard already-paid-for dispatches.
    if _OUT_PATH.exists():
        results = json.loads(_OUT_PATH.read_text())
        results.setdefault("operators", {})
        results.setdefault("planner", {"baseline": [], "candidate": []})
    else:
        results = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "operators": {},
            "planner": {"baseline": [], "candidate": []},
        }
    results["n_per_arm"] = N_PER_ARM

    # ── Save the tiering-relevant operator plans (plan-HIT path — no
    # inline-planner cost for the operator arm) ────────────────────────
    questions: dict[str, str] = {}
    for op in _OPERATORS:
        question, plan_json, _args = _plan_for(op)
        questions[op] = question
        save_result = plan_save(
            query=question, plan_json=plan_json, verb="analyze",
            project="nexus", outcome="success",
            tags="rdr196,p2c,ab-measurement,nexus-nyry9.16",
        )
        assert "Error" not in save_result, save_result

    async def _run_once(question: str, *, tiering: bool, force_dynamic: bool = False):
        if tiering:
            os.environ[_TIER_ENV] = "1"
        else:
            os.environ.pop(_TIER_ENV, None)
        sink: list = []
        t0 = time.monotonic()
        with ambient_usage_sink(sink):
            result = await nx_answer(
                question=question, structured=True, force_dynamic=force_dynamic,
            )
        elapsed_s = time.monotonic() - t0
        os.environ.pop(_TIER_ENV, None)
        return result, sink, elapsed_s

    # ── tiering-relevant operators × 2 arms × N_PER_ARM ─────────────────
    for op in _OPERATORS:
        results["operators"].setdefault(op, {"baseline": [], "candidate": []})
        for arm, tiering in (("baseline", False), ("candidate", True)):
            _base_index = len(results["operators"][op][arm])
            for i in range(N_PER_ARM):
                # Round-2 review gap (T2 [23144] Significant #4): the SAME
                # question string is reused across both arms and all
                # N_PER_ARM indices of this operator, so a post-hoc
                # question-text lookup cannot tell records apart. Snapshot
                # the persisted row ids that exist BEFORE this dispatch so
                # stitch_run_id() can identify the exact new row after.
                with t2_ctx() as db:
                    before_ids = {
                        r.get("id")
                        for r in db.telemetry.query_nx_answer_runs(limit=200).get("rows", [])
                    }
                result, sink, elapsed_s = await _run_once(
                    questions[op], tiering=tiering,
                )
                try:
                    parsed_final = json.loads(result.get("final_text", "")) \
                        if isinstance(result, dict) else None
                except (TypeError, ValueError):
                    parsed_final = None
                with t2_ctx() as db:
                    after_rows = db.telemetry.query_nx_answer_runs(
                        limit=200, include_steps=True,
                    ).get("rows", [])
                record = {
                    "run_index": _base_index + i,
                    "elapsed_s": elapsed_s,
                    "cost_usd": result.get("cost_usd") if isinstance(result, dict) else None,
                    "steps": result.get("steps") if isinstance(result, dict) else None,
                    "ambient_usage": [asdict(u) for u in sink],
                    "final_output": parsed_final,
                    "question": questions[op],
                    "persisted_run_id": stitch_run_id(before_ids, after_rows, questions[op]),
                }
                # Plain operator-tiering row: no separate planner
                # dispatch exists, so total == cost_usd (is_planner_row=
                # False) — see scripts/bench/operator_proxy_ab.py.
                record["total_cost_usd"] = total_dispatch_cost(record, is_planner_row=False)
                record["total_dispatch_count"] = total_dispatch_count(record, is_planner_row=False)
                results["operators"][op][arm].append(record)
                print(
                    f"[{op}/{arm}][{i}] cost={record['total_cost_usd']} "
                    f"elapsed={elapsed_s:.1f}s "
                    f"models={[s.get('model') for s in (record['steps'] or [])]}"
                )
                # Incremental flush (nexus-nyry9.16 budget discipline): a
                # crash/timeout on dispatch N must not discard the real
                # money already spent on dispatches 1..N-1.
                _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
                _OUT_PATH.write_text(json.dumps(results, indent=2, default=str))

    # ── inline planner row: force_dynamic=True bypasses plan-match ─────
    if _INCLUDE_PLANNER:
        planner_question_template = (
            "RDR-196 .p2c A/B inline-planner fixture run #{i}: "
            "what does RDR-086 say about chunk content-hash identity?"
        )
        for arm, tiering in (("baseline", False), ("candidate", True)):
            _base_index = len(results["planner"][arm])
            for i in range(N_PER_ARM):
                # planner_question_template embeds the index but NOT the
                # arm, so baseline/candidate at the same i still share
                # question text — same stitching gap as the operator
                # loop above (T2 [23144] Significant #4).
                q = planner_question_template.format(i=_base_index + i)
                with t2_ctx() as db:
                    before_ids = {
                        r.get("id")
                        for r in db.telemetry.query_nx_answer_runs(limit=200).get("rows", [])
                    }
                result, sink, elapsed_s = await _run_once(
                    q, tiering=tiering, force_dynamic=True,
                )
                with t2_ctx() as db:
                    after_rows = db.telemetry.query_nx_answer_runs(
                        limit=200, include_steps=True,
                    ).get("rows", [])
                record = {
                    "run_index": _base_index + i,
                    "elapsed_s": elapsed_s,
                    "cost_usd": result.get("cost_usd") if isinstance(result, dict) else None,
                    "steps": result.get("steps") if isinstance(result, dict) else None,
                    "ambient_usage": [asdict(u) for u in sink],
                    "question": q,
                    "persisted_run_id": stitch_run_id(before_ids, after_rows, q),
                }
                # Planner row: ``cost_usd`` alone UNDERCOUNTS for a run
                # whose downstream step was isolated (not bundled) — see
                # scripts/bench/operator_proxy_ab.py's module docstring
                # for the ambient-sink-shadowing bug this works around.
                # ``total_cost_usd`` is the corrected figure; ``cost_usd``
                # is kept as-is (raw, uncorrected) for audit.
                record["total_cost_usd"] = total_dispatch_cost(record, is_planner_row=True)
                record["total_dispatch_count"] = total_dispatch_count(record, is_planner_row=True)
                results["planner"][arm].append(record)
                print(
                    f"[planner/{arm}][{i}] total_cost={record['total_cost_usd']} "
                    f"(raw step cost_usd={record['cost_usd']}) "
                    f"elapsed={elapsed_s:.1f}s "
                    f"ambient_models={[u.model for u in sink]} "
                    f"ambient_costs={[u.cost_usd for u in sink]}"
                )
                _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
                _OUT_PATH.write_text(json.dumps(results, indent=2, default=str))

    # ── cross-reference persisted nx_answer_runs rows for run-id citation ──
    # MERGE, never overwrite (code-review fix, T2 [23144] Significant #4):
    # each pytest invocation of this test mints its OWN fresh, EPHEMERAL
    # T2 tenant (t2_service_env / _pin_t2_substrate), so a query here only
    # ever sees THIS invocation's own rows — a prior bug that did
    # ``results["persisted_rows_by_question"] = by_question`` (a plain
    # overwrite) silently discarded every earlier invocation's run-id
    # citations the moment a later invocation's cross-reference query ran,
    # which is why the first live measurement round ended up with a
    # citation for only the LAST operator invoked. dict.update() folds
    # this invocation's rows in alongside whatever prior invocations
    # already wrote to the merged _OUT_PATH, keyed by question text (each
    # operator/question is queried at most once per invocation, so no
    # legitimate collision exists to lose).
    with t2_ctx() as db:
        page = db.telemetry.query_nx_answer_runs(limit=200, include_steps=True)
    rows = page.get("rows", []) if isinstance(page, dict) else []
    by_question: dict[str, list] = {}
    for row in rows:
        by_question.setdefault(row.get("question", ""), []).append(row)
    results.setdefault("persisted_rows_by_question", {}).update(by_question)

    results["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n=== RESULTS WRITTEN: {_OUT_PATH} ===")
