# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 2 (nexus-jypmj.2.8): the replay script's classification, on fixture rows.

``scripts/bench/rdr232_replay.py`` reads recorded ``nx_answer`` runs and says what the answer-kind gate would
exclude. These tests drive its pure core (:func:`classify_rows`) with hand-built run rows and a hand-built plan
library: every skip reason, every verdict, the broad-request reuse numbers, the library-level alternative
count, the redaction of question text, and the non-vacuity exit. No service, no model call, no engine.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bench import rdr232_replay as replay
from nexus.mcp.core import (
    NX_ANSWER_CONTINUATION_MARKER_PREFIX,
    NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX,
)
from nexus.plans.answer_kind import PlanAnswerKind, declaration_payload, grown_plan_json
from nexus.plans.eligibility import eligible_matches
from nexus.plans.request_coverage import derive_request_coverage

Q_LOOKUP = "What does the tuple_renew operation do to the attempts counter?"
Q_LISTING = "Which RDRs discuss catalog link types?"
Q_SYNTH = "Synthesize RDR-084, LIST PARSE Canonical Consolidation. State the locked architecture and the evidence supporting it."
Q_COMPARE = "Compare the distributed bloom filter paper and the hex bloom paper on set reconciliation: how does each handle hash-function cost?"
Q_AUDIT = "Audit RDR-210 against its implementation: which decisions are implemented, which are still pending?"
Q_DEBUG = "Why does traverse return no chunk ids when the catalog manifest lookup fails?"
Q_UNKNOWN = "What techniques improve memory efficiency in LLM agent systems?"

_CID = "0b9f6a52-3c1d-4f5e-8a77-1d2c3b4a5f60"


def _curated(kind: PlanAnswerKind) -> str:
    return json.dumps({"steps": [], "answer_kind": declaration_payload(kind, "curated")})


def _plan(pid: int, name: str, tags: str, plan_json: str, *, disabled: bool = False) -> dict:
    return {"id": pid, "name": name, "tags": tags, "plan_json": plan_json,
            "disabled_at": "2026-10-01T00:00:00Z" if disabled else None}


def _library() -> list[dict]:
    builtin = "builtin-template,rdr-097"
    return [
        _plan(1, "lookup", builtin, _curated(PlanAnswerKind.LOOKUP)),
        _plan(2, "synth", builtin, _curated(PlanAnswerKind.SYNTHESIS)),
        _plan(3, "grown", "ad-hoc,grown", grown_plan_json('{"steps": []}', route_kind=None, route_source=None)),
        _plan(4, "undeclared", builtin, '{"steps": []}'),
        _plan(5, "listing", builtin, _curated(PlanAnswerKind.DISCOVERY_LISTING)),
        _plan(6, "disabled-synth", builtin, _curated(PlanAnswerKind.SYNTHESIS), disabled=True),
    ]


def _run(rid: int, question: str | None, plan_id: int | None, final_text: str = "ok") -> dict:
    return {"id": rid, "question": question, "plan_id": plan_id, "matched_confidence": 0.9, "step_count": 2,
            "final_text": final_text, "created_at": "2026-10-20T00:00:00Z"}


def _rows() -> list:
    return [
        _run(1, Q_LOOKUP, 1),                      # lookup on a lookup plan: admitted
        _run(2, Q_SYNTH, 1),                       # synthesis on a lookup plan: kind_mismatch
        _run(3, Q_COMPARE, 3),                     # comparison on a grown plan: untrusted_unknown_plan
        _run(4, Q_UNKNOWN, 5),                     # unknown request on a narrow curated plan
        _run(5, Q_AUDIT, 4),                       # audit on an undeclared builtin
        _run(6, Q_DEBUG, 2),                       # debug on a synthesis plan: admitted
        _run(7, Q_LISTING, 5),                     # listing on a listing plan: admitted
        _run(8, Q_LOOKUP, 1, f"{NX_ANSWER_CONTINUATION_MARKER_PREFIX} continuation_id={_CID}]"),  # handoff: a run
        # skipped, one of each
        _run(20, Q_LOOKUP, 1, f"{NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX} continuation_id={_CID} ok=True]"),
        _run(21, "   ", 1),
        _run(22, None, 1),
        _run(23, Q_LOOKUP, None),
        _run(24, Q_LOOKUP, 0),
        _run(25, Q_LOOKUP, 999),
        "not a row",
    ]


def test_question_fixtures_have_the_kinds_the_tests_assume() -> None:
    """The fixture questions' kinds come from the real extraction; if the extractor drifts, this fails first."""
    got = {q: derive_request_coverage(q).answer_kind for q in
           (Q_LOOKUP, Q_LISTING, Q_SYNTH, Q_COMPARE, Q_AUDIT, Q_DEBUG, Q_UNKNOWN)}
    assert got == {
        Q_LOOKUP: "lookup", Q_LISTING: "discovery/listing", Q_SYNTH: "synthesis", Q_COMPARE: "comparison",
        Q_AUDIT: "audit", Q_DEBUG: "debug", Q_UNKNOWN: "unknown",
    }


def test_every_skip_reason_is_counted_by_name() -> None:
    rep = replay.classify_rows(_rows(), _library())
    assert rep["rows"]["read"] == 15
    assert rep["rows"]["examined"] == 8
    assert rep["rows"]["skipped"] == {
        "unreadable_row": 1, "continuation_report": 1, "no_question": 2,
        "no_plan_id": 1, "inline_planned": 1, "plan_not_in_library": 1,
    }
    assert set(rep["rows"]["skipped"]) == set(replay.SKIP_REASONS)
    assert rep["rows"]["read"] == rep["rows"]["examined"] + rep["rows"]["skipped_total"]
    assert rep["rows"]["examined_continuation_handoffs"] == 1


def test_verdicts_and_reasons_per_run() -> None:
    rep = replay.classify_rows(_rows(), _library())
    by_run = {d["run_id"]: d for d in rep["runs"]}
    expect = {
        1: ("lookup", True, None, "curated_builtin"),
        2: ("synthesis", False, "kind_mismatch", "curated_builtin"),
        3: ("comparison", False, "untrusted_unknown_plan", "grown"),
        4: ("unknown", False, "unknown_request_narrow_plan", "curated_builtin"),
        5: ("audit", False, "undeclared_builtin", "undeclared_builtin"),
        6: ("debug", True, None, "curated_builtin"),
        7: ("discovery/listing", True, None, "curated_builtin"),
        8: ("lookup", True, None, "curated_builtin"),
    }
    assert {rid: (d["request_kind"], d["admitted"], d["reason"], d["plan_bucket"]) for rid, d in by_run.items()} == expect
    assert rep["excluded"]["count"] == 4
    assert rep["excluded"]["share"] == 0.5
    assert rep["excluded"]["by_reason"] == {
        "kind_mismatch": 1, "undeclared_builtin": 1, "unknown_request_narrow_plan": 1, "untrusted_unknown_plan": 1,
    }
    assert rep["request_kinds"]["unknown"] == 1
    assert rep["unknown_rate"] == 0.125


def test_broad_request_reuse_counts_grown_and_builtin_before_and_after() -> None:
    reuse = replay.classify_rows(_rows(), _library())["broad_request_reuse"]
    assert reuse["examined_broad"] == 4  # synthesis, comparison, audit, debug
    # the grown plan served one broad request before the gate and none after
    assert reuse["by_plan_bucket"]["grown"] == {"before": 1, "after": 0, "dropped": 1}
    # curated builtins served two (synthesis on plan 1, debug on plan 2); only the debug one survives
    assert reuse["by_plan_bucket"]["curated_builtin"] == {"before": 2, "after": 1, "dropped": 1}
    assert reuse["by_plan_bucket"]["undeclared_builtin"] == {"before": 1, "after": 0, "dropped": 1}
    assert set(reuse["by_plan_bucket"]) == set(replay.BUCKETS)  # zero buckets are present, not absent


def test_library_level_alternative_counts_only_other_enabled_plans() -> None:
    rep = replay.classify_rows(_rows(), _library())
    assert rep["alternatives"]["library_level"] == {
        "excluded_with_library_alternative": 4, "excluded_without_library_alternative": 0,
    }
    assert rep["alternatives"]["matcher_branch_recorded"] is False
    assert rep["alternatives"]["candidate_list_recorded"] is False
    # a library whose only other plans are disabled or the selected one has no alternative
    only = [p for p in _library() if p["id"] in (1, 6)]
    rep2 = replay.classify_rows([_run(1, Q_SYNTH, 1)], only)
    assert rep2["alternatives"]["library_level"] == {
        "excluded_with_library_alternative": 0, "excluded_without_library_alternative": 1,
    }


def test_replay_verdict_equals_the_gates_own_filter() -> None:
    """The replay judges through the gate's enforcing filter; pin that it never drifts from eligible_matches."""
    for plan in _library():
        for kind in ("lookup", "synthesis", "discovery/listing", "comparison", "audit", "debug", "unknown"):
            admitted, _, _ = replay.judge(kind, plan)
            quiet = eligible_matches(
                PlanAnswerKind(kind), [SimpleNamespace(tags=plan["tags"], plan_json=plan["plan_json"])],
            )
            assert admitted == bool(quiet), (plan["name"], kind)


def test_report_carries_no_question_text() -> None:
    text = json.dumps(replay.classify_rows(_rows(), _library()))
    for q in (Q_LOOKUP, Q_LISTING, Q_SYNTH, Q_COMPARE, Q_AUDIT, Q_DEBUG, Q_UNKNOWN):
        assert q not in text
        assert q[:25] not in text


def test_zero_examined_raises_and_names_the_skips() -> None:
    rows = [_run(1, None, 1), _run(2, Q_LOOKUP, 0), _run(3, Q_LOOKUP, 999)]
    with pytest.raises(replay.NoRowsExamined) as err:
        replay.classify_rows(rows, _library())
    assert "0 of 3 rows examined" in str(err.value)
    with pytest.raises(replay.NoRowsExamined):
        replay.classify_rows([], _library())


def test_main_exits_2_without_writing_when_nothing_is_examined(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(replay, "_read_rows", lambda since, last: ([_run(1, None, 1)], {"returned": 1}))
    monkeypatch.setattr(replay, "_read_plans", lambda path: (_library(), "fixture"))
    out = tmp_path / "report.json"
    assert replay.main(["--out", str(out)]) == 2
    assert not out.exists()
    assert "FAIL" in capsys.readouterr().err


def test_main_writes_a_report_and_show_questions_goes_to_stderr_only(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(replay, "_read_rows", lambda since, last: (_rows(), {"returned": len(_rows())}))
    monkeypatch.setattr(replay, "_read_plans", lambda path: (_library(), "fixture"))
    out = tmp_path / "report.json"
    assert replay.main(["--out", str(out), "--show-questions"]) == 0
    assert Q_SYNTH not in out.read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert Q_LOOKUP[:40] in err
    assert json.loads(out.read_text(encoding="utf-8"))["rows"]["examined"] == 8
