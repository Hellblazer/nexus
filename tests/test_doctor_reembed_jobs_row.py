# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor's "Re-embed jobs" row (nexus-s08oe, RDR-210 Phase 6 Step 5).

The row reads the tenant's re-embed job table through the engine's status route
(``nexus.reembed_job.local_jobs``) and says what is in flight and what needs a person. The not-applicable
branches come first: a box that is not in local mode, an engine that cannot answer, and a table with nothing
running or failed all stay green, because a virgin box has none of these and must not warn.
"""
from __future__ import annotations

import pytest

from nexus import health
from nexus.health import HealthResult, _check_reembed_jobs

_LABEL = "Re-embed jobs"
_SRC = "docs__1-1__bge-base-en-v15-768__v1"
_TGT = "docs__1-1__voyage-model-a__v1"


def _job(job_id="j1", state="running", *, source=_SRC, target=_TGT, reason=None, error=None, moved=0, skipped=0,
         **extra) -> dict:
    return {
        "job_id": job_id, "source_collection": source, "target_collection": target, "state": state,
        "disposition": None, "failure_reason": reason, "last_error": error,
        "documents_moved": moved, "documents_skipped": skipped, **extra,
    }


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)


@pytest.fixture(autouse=True)
def _topics_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The topics read reaches the engine; every test here stubs it (no target is undiscovered) unless it says."""
    monkeypatch.setattr("nexus.reembed_job.undiscovered_targets", lambda jobs: [])


def _with_jobs(monkeypatch: pytest.MonkeyPatch, jobs) -> HealthResult:
    monkeypatch.setattr("nexus.reembed_job.local_jobs", lambda: jobs)
    rows = _check_reembed_jobs()
    assert len(rows) == 1 and rows[0].label == _LABEL
    return rows[0]


def _na(r: HealthResult) -> bool:
    return r.ok is True and not r.warn and "not applicable" in r.detail


# ── not applicable, and green ────────────────────────────────────────────────


def test_a_box_that_is_not_in_local_mode_is_not_applicable_and_never_reads_the_table(monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

    def boom():
        raise AssertionError("the job table was read outside local mode")

    monkeypatch.setattr("nexus.reembed_job.local_jobs", boom)
    (r,) = _check_reembed_jobs()
    assert _na(r) and "local" in r.detail


def test_an_empty_job_table_is_not_applicable(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [])
    assert _na(r) and "no re-embed jobs" in r.detail


def test_a_table_of_finished_jobs_is_not_applicable(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [_job("a", "done"), _job("b", "done_with_skips"), _job("c", "aborted")])
    assert _na(r) and "3 finished" in r.detail


@pytest.mark.parametrize("exc", [RuntimeError("engine down"), ConnectionError("refused")])
def test_an_unreadable_job_table_is_a_detail_never_an_exception(local, monkeypatch, exc) -> None:
    def unreadable():
        raise exc

    monkeypatch.setattr("nexus.reembed_job.local_jobs", unreadable)
    (r,) = _check_reembed_jobs()
    assert _na(r) and "unread" in r.detail and type(exc).__name__ in r.detail


def test_a_reader_that_returns_junk_is_a_detail_never_an_exception(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [None, "x", 3])  # type: ignore[list-item]
    assert r.ok is True and not r.warn


# ── running ──────────────────────────────────────────────────────────────────


def test_running_jobs_are_green_with_counts_by_state(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [
        _job("a", "running", moved=12, skipped=1),
        _job("b", "planned", source="docs__other"),
        _job("c", "cutover", source="docs__third"),
        _job("d", "paused", source="docs__fourth"),
        _job("e", "done", source="docs__old"),
    ])
    assert r.ok is True and not r.warn
    assert "4 re-embed job(s) in progress" in r.detail
    for part in ("running 1", "planned 1", "cutover 1", "paused 1"):
        assert part in r.detail, r.detail
    assert _SRC in r.detail and "moved 12" in r.detail
    assert "nx collection re-embed --status" in r.detail
    assert "docs__old" not in r.detail, "a finished job is history, not in flight"


def test_a_job_parked_in_a_blocked_cutover_warns_and_offers_only_the_abort_and_says_why(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [_job("jb", "cutover", error="cutover is blocked: the source is no longer registered")])
    assert r.ok is False and r.warn is True
    assert "blocked in cutover" in r.detail and "no longer registered" in r.detail and "jb" in r.detail
    # The state machine has no resume edge out of cutover (ReembedStateMachine.TRANSITIONS), so only an abort moves it.
    assert "a resume is refused in cutover" in r.detail
    assert r.fix_suggestions == [r.fix_suggestions[0]] and "--abort jb" in r.fix_suggestions[0]
    assert not any("--resume" in s for s in r.fix_suggestions)


def test_a_blocked_job_whose_delete_step_has_started_is_offered_no_abort(local, monkeypatch) -> None:
    """The engine refuses an abort once the cutover's delete step has started, so the row must not offer one."""
    r = _with_jobs(monkeypatch, [_job("jb", "cutover", error="cutover is blocked: x", cutover_step="delete_started")])
    assert r.warn is True and "started the delete step" in r.detail
    assert r.fix_suggestions == []


def test_blocked_jobs_are_capped_in_the_fixes_but_counted(local, monkeypatch) -> None:
    jobs = [_job(f"b{i}", "cutover", source=f"docs__s{i}", error="cutover is blocked: x") for i in range(8)]
    r = _with_jobs(monkeypatch, jobs)
    assert "8 job(s) blocked in cutover" in r.detail
    assert len(r.fix_suggestions) == health._MAX_FAILED_JOB_FIXES == 5


# ── failed ───────────────────────────────────────────────────────────────────


def test_a_failed_job_warns_with_its_reason_and_both_ways_out(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [_job("jx", "failed", reason="batch_failed", error="embed call timed out")])
    assert r.ok is False and r.warn is True, "a failed job holds its source's lock: soft warning, not fatal"
    assert "jx" in r.detail and _SRC in r.detail and "batch_failed" in r.detail and "embed call timed out" in r.detail
    assert any("nx collection re-embed --resume jx" in s for s in r.fix_suggestions)
    assert any("nx collection re-embed --abort jx" in s for s in r.fix_suggestions)


def test_a_token_cap_failure_names_the_flag_that_raises_the_cap(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [_job("jx", "failed", reason="token_cap")])
    assert any("--resume jx --token-cap" in s for s in r.fix_suggestions)


def test_a_profile_changed_failure_is_aborted_then_planned_again_not_resumed(local, monkeypatch) -> None:
    """The runner re-checks the profile on every entry into running, so a resume would fail the same way."""
    r = _with_jobs(monkeypatch, [_job("jx", "failed", reason="profile_changed")])
    assert any("--abort jx" in s and "nx collection re-embed --all" in s for s in r.fix_suggestions)
    assert not any("--resume jx" in s for s in r.fix_suggestions)


def test_failed_and_running_jobs_report_both(local, monkeypatch) -> None:
    r = _with_jobs(monkeypatch, [_job("a", "running"), _job("jx", "failed", reason="parity_mismatch", source="docs__f")])
    assert r.ok is False and r.warn is True
    assert "1 failed" in r.detail and "parity_mismatch" in r.detail and "running 1" in r.detail


def test_many_failed_jobs_are_capped_in_the_detail_and_the_fixes_but_counted(local, monkeypatch) -> None:
    jobs = [_job(f"j{i:02d}", "failed", reason="batch_failed", source=f"docs__s{i:02d}") for i in range(14)]
    r = _with_jobs(monkeypatch, jobs)
    assert "14 failed" in r.detail and "(+4 more)" in r.detail
    # Five jobs get fixes, two each (resume, abort): the cap is pinned to the number, not to a loose bound.
    assert health._MAX_FAILED_JOB_FIXES == 5 and len(r.fix_suggestions) == 10
    assert not any("j05" in s for s in r.fix_suggestions)


# ── registration ─────────────────────────────────────────────────────────────


def test_the_sweep_calls_the_row_unconditionally_and_once() -> None:
    """``run_health_checks`` extends its results with ``_check_reembed_jobs()`` as a top-level statement of the
    function body: not under an ``if``, ``try`` or loop (a wrapper that is false on a box would hide the row), and
    exactly once. Read from the AST, because running the whole sweep provisions a tenant and the row is already
    covered by calling it directly above."""
    import ast  # noqa: PLC0415 — local to the one test that reads the source tree
    import inspect  # noqa: PLC0415 — local to the one test that reads the source tree

    tree = ast.parse(inspect.getsource(health.run_health_checks))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)

    def calls_row(node: ast.AST) -> int:
        return sum(
            1 for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_check_reembed_jobs"
        )

    assert calls_row(func) == 1, "the row is registered exactly once"
    top_level = [st for st in func.body if isinstance(st, ast.Expr) and calls_row(st) == 1]
    assert len(top_level) == 1, "and the call is a top-level statement of the function body, not conditional"


def test_a_virgin_box_with_no_engine_is_not_applicable(monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)

    def no_engine():
        raise ConnectionError("no engine on a virgin box")

    monkeypatch.setattr("nexus.reembed_job.local_jobs", no_engine)
    (r,) = _check_reembed_jobs()
    assert _na(r)


# ── finished jobs whose target has no topics yet (RDR-210 Technical Design 4) ──


def test_a_done_target_with_no_discover_since_warns_and_names_the_discover_command(local, monkeypatch) -> None:
    monkeypatch.setattr("nexus.reembed_job.undiscovered_targets", lambda jobs: [_TGT])
    r = _with_jobs(monkeypatch, [_job("a", "done", finished_at="2026-10-25T10:00:00Z")])
    assert r.ok is False and r.warn is True
    assert _TGT in r.detail and "no taxonomy discover" in r.detail
    assert r.fix_suggestions == [f"nx taxonomy discover --collection {_TGT}"]


def test_an_unreadable_topics_state_is_not_judged_and_says_so(local, monkeypatch) -> None:
    def unreadable(jobs):
        raise ConnectionError("taxonomy route down")

    monkeypatch.setattr("nexus.reembed_job.undiscovered_targets", unreadable)
    r = _with_jobs(monkeypatch, [_job("a", "done")])
    assert _na(r) and "not judged (ConnectionError)" in r.detail


def _done(target: str, finished_at: str, state: str = "done") -> dict:
    return _job(state=state, target=target, finished_at=finished_at)


@pytest.mark.parametrize(
    ("stamp", "count", "listed"),
    [
        (None, 20, True),                        # never discovered
        ("2026-10-25T09:00:00Z", 20, True),      # discovered before the job finished: the moved documents miss it
        ("2026-10-25T11:00:00Z", 20, False),     # discovered after
        (None, 4, False),                        # below the floor discover builds nothing at, so never asked for
        (None, 5, True),                         # at the floor
    ],
)
def test_undiscovered_compares_the_last_discover_with_the_job_end(stamp, count, listed) -> None:
    from nexus.reembed_job import undiscovered

    stamps = {_TGT: {"last_discover_at": stamp}} if stamp else {}
    got = undiscovered([_done(_TGT, "2026-10-25T10:00:00.250Z")], stamps, {_TGT: count}, 5)
    assert got == ([_TGT] if listed else [])


def test_undiscovered_reads_only_finished_jobs_and_the_newest_end_per_target() -> None:
    from nexus.reembed_job import undiscovered

    jobs = [
        _done(_TGT, "2026-10-25T08:00:00Z"),
        _done(_TGT, "2026-10-25T12:00:00Z", state="done_with_skips"),
        _job("r", "running", target="docs__1-1__voyage-model-b__v1"),
        _job("x", "aborted", target="docs__1-1__voyage-model-c__v1", finished_at="2026-10-25T08:00:00Z"),
    ]
    counts = {_TGT: 9, "docs__1-1__voyage-model-b__v1": 9, "docs__1-1__voyage-model-c__v1": 9}
    # A discover between the two jobs does not cover the second one's documents.
    assert undiscovered(jobs, {_TGT: {"last_discover_at": "2026-10-25T10:00:00Z"}}, counts, 5) == [_TGT]


def test_undiscovered_targets_reads_stamps_and_counts_for_finished_targets_only(monkeypatch) -> None:
    import nexus.reembed_job as rj

    monkeypatch.undo()  # drop the autouse stub of the function under test
    seen: dict = {}

    class Tax:
        def get_last_discover_stamps(self, collections):
            seen["stamps"] = list(collections)
            return {}

        def close(self):
            seen["closed"] = True

    class T3:
        def count(self, name):
            seen.setdefault("counts", []).append(name)
            return 7

    monkeypatch.setattr("nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore", Tax)
    monkeypatch.setattr("nexus.db.make_t3", lambda: T3())
    got = rj.undiscovered_targets([_done(_TGT, "2026-10-25T10:00:00Z"), _job("r", "running")])
    assert got == [_TGT]
    assert seen == {"stamps": [_TGT], "closed": True, "counts": [_TGT]}


def test_undiscovered_targets_reads_nothing_without_a_finished_job(monkeypatch) -> None:
    import nexus.reembed_job as rj

    monkeypatch.undo()

    def boom(*a, **k):
        raise AssertionError("read the engine with no finished job")

    monkeypatch.setattr("nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore", boom)
    monkeypatch.setattr("nexus.db.make_t3", boom)
    assert rj.undiscovered_targets([_job("r", "running"), _job("f", "failed")]) == []
