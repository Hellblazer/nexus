# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx t3 retire-keys`` (RDR-236 Phase 4 Step 3, bead nexus-6pfgb.43), the client half of the engine's
``/v1/admin/chunk-keys`` routes.

Three layers, each owning what it can prove:

* the plan the client renders from the key table (what the engine must receive, since it cannot name the ROW keys);
* the wire: ``HttpVectorClient`` posts the right body to the right path, a dry run is not a write and an apply is
  (the production-write guard), and a table-wide step always is;
* the CLI against a stubbed client: the dry-run default, the tenant check, the apply loop, the refusals, the pacing
  gate and the exit codes. A stub is right for these because each is a branch on the engine's answer, which
  ``ChunkKeySweepIntegrationTest`` and ``ChunkConstraintRoutesIntegrationTest`` pin against a real database.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands.t3 import t3
from nexus.commands.t3_cmds import retire_keys as rk
from nexus.db import http_vector_client as hv
from nexus.db import request_gate
from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError
from nexus.tables import chunk_metadata_keys as ckm

TENANT = "tenant-a"


# ── the plan ─────────────────────────────────────────────────────────────────


class TestPlan:
    def test_the_allowed_keys_are_the_chunk_and_row_keys_of_the_table(self) -> None:
        plan = ckm.engine_plan()
        assert plan["allowed_keys"] == sorted(ckm.keys_of("CHUNK") | ckm.keys_of("ROW"))
        assert set(ckm.keys_of("CHUNK")) <= set(plan["allowed_keys"])

    def test_no_document_position_or_legacy_key_is_allowed(self) -> None:
        assert set(ckm.engine_plan()["allowed_keys"]).isdisjoint(ckm.non_chunk_row_keys())

    def test_the_bypass_prefixes_are_the_tables_and_quarantine_is_one(self) -> None:
        plan = ckm.engine_plan()
        assert plan["bypass_prefixes"] == list(ckm.BYPASS_PREFIXES)
        assert "quarantine-" in plan["bypass_prefixes"]

    def test_the_quarantine_implication_names_two_row_keys(self) -> None:
        by, at = ckm.engine_plan()["quarantine_implies"]
        assert by != at
        assert {by, at} <= ckm.keys_of("ROW")

    def test_the_engine_pins_what_the_plan_renders(self) -> None:
        """The engine holds the ROW keys by digest and the bypass set exactly (ChunkKeyPlan): the generated Java
        constants must be the digests of what ``engine_plan`` sends, or every request would be a 400."""
        import gen_chunk_metadata_keys_java as gen  # noqa: PLC0415 — scripts/ is on the test pythonpath (pyproject)

        plan = ckm.engine_plan()
        rows = [k for k in plan["allowed_keys"] if k not in ckm.keys_of("CHUNK")]
        assert rows == sorted(ckm.keys_of("ROW")) and len(rows) >= 4
        source = gen.OUTPUT.read_text(encoding="utf-8")
        assert f'ROW_KEYS_SHA256 =\n        "{gen.digest(rows)}"' in source
        assert f'QUARANTINE_IMPLIES_SHA256 =\n        "{gen.digest(plan["quarantine_implies"])}"' in source
        assert sorted(plan["bypass_prefixes"]) == sorted(["quarantine-", "taxonomy" + "_" * 2])


# ── the wire ─────────────────────────────────────────────────────────────────


def _client() -> HttpVectorClient:
    client = HttpVectorClient.__new__(HttpVectorClient)
    client._tenant = "tenant-x"
    return client


class TestWire:
    def test_a_dry_run_posts_the_plan_with_dry_run_true_and_is_not_a_write(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write") as guard:
            _client().chunk_keys_sweep()
        path, body = post.call_args.args
        assert path == "/v1/admin/chunk-keys/sweep"
        assert body == {**ckm.engine_plan(), "dry_run": True}
        assert post.call_args.kwargs["tenant"] == "tenant-x"
        guard.assert_not_called()

    def test_an_apply_sends_its_fields_only_when_given_and_goes_through_the_write_guard(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write") as guard:
            _client().chunk_keys_sweep(
                dry_run=False, accept={"keyless_notes": 3}, collection="c", batch_size=10, max_batches=2,
                budget_seconds=5, resume={"cursor": None, "collections": ["c"], "blocked": [], "remaining": 4},
            )
        _, body = post.call_args.args
        assert body["dry_run"] is False
        assert body["accept"] == {"keyless_notes": 3}, "a residue is accepted up to a number of rows, not by name alone"
        assert body["resume"] == {"cursor": None, "collections": ["c"], "blocked": [], "remaining": 4}
        assert (body["collection"], body["batch_size"], body["max_batches"], body["budget_seconds"]) == ("c", 10, 2, 5)
        assert "measure_batch" not in body
        guard.assert_called_once()

    def test_the_target_tenant_rides_in_the_body_only_when_named(self) -> None:
        """nexus-6pfgb.43 (root names the target): an old client sends no ``tenant`` and a new engine reads that as the
        credential's own tenant; the verb names it every time."""
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write"):
            _client().chunk_keys_sweep()
            _client().chunk_keys_sweep(tenant="nexus")
            _client().chunk_keys_sweep(dry_run=False, tenant="gate-xr789")
        unnamed, dry, applied = (c.args[1] for c in post.call_args_list)
        assert "tenant" not in unnamed
        assert dry["tenant"] == "nexus" and dry["dry_run"] is True
        assert applied["tenant"] == "gate-xr789" and applied["dry_run"] is False

    def test_the_operator_label_rides_in_the_body_only_when_given(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write"):
            _client().chunk_keys_sweep(tenant="nexus")
            _client().chunk_keys_sweep(tenant="nexus", operator="sam-laptop")
        plain, labelled = (c.args[1] for c in post.call_args_list)
        assert "operator" not in plain
        assert labelled["operator"] == "sam-laptop"

    def test_a_first_call_sends_neither_accept_nor_resume_when_given_none(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write"):
            _client().chunk_keys_sweep(dry_run=False)
        _, body = post.call_args.args
        assert "accept" not in body and "resume" not in body

    def test_the_guard_runs_before_the_request_and_a_refusal_sends_nothing(self) -> None:
        with patch.object(hv, "_guard_chunk_keys_write", side_effect=RuntimeError("refused")), \
                patch.object(hv, "_post") as post:
            with pytest.raises(RuntimeError, match="refused"):
                _client().chunk_keys_sweep(dry_run=False)
            with pytest.raises(RuntimeError, match="refused"):
                _client().chunk_keys_add_constraints()
            with pytest.raises(RuntimeError, match="refused"):
                _client().chunk_keys_validate_constraints()
        post.assert_not_called()

    def test_the_guard_is_the_production_write_guard_on_the_resolved_endpoint(self) -> None:
        with patch.object(hv, "_resolve_endpoint", return_value=("http://svc", "tok")), \
                patch("nexus.db.service_endpoint.guard_production_write") as guard:
            hv._guard_chunk_keys_write()
        guard.assert_called_once_with("http://svc")

    def test_add_constraints_posts_the_plan_and_replace_only_when_asked(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write") as guard:
            _client().chunk_keys_add_constraints()
            _client().chunk_keys_add_constraints(replace=True)
        first, second = post.call_args_list
        assert first.args == ("/v1/admin/chunk-keys/add-constraints", ckm.engine_plan())
        assert second.args[1] == {**ckm.engine_plan(), "replace": True}
        assert guard.call_count == 2

    def test_validate_posts_the_plan_and_gets_the_long_socket_timeout(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write"):
            _client().chunk_keys_validate_constraints()
        assert post.call_args.args == ("/v1/admin/chunk-keys/validate-constraints", ckm.engine_plan())
        assert post.call_args.kwargs["timeout"] == hv._CHUNK_KEYS_VALIDATE_TIMEOUT_S
        assert hv._CHUNK_KEYS_VALIDATE_TIMEOUT_S > 30 * 60, "the engine allows the VALIDATE 30 minutes"

    def test_add_gets_the_same_long_socket_timeout_as_validate(self) -> None:
        # The add runs the whole-table probe (10 min statement bound), a scoped count and a census per known tenant,
        # then the DDL, inside the one request: the sweep's 300 s socket cut it off while the engine went on.
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write"):
            _client().chunk_keys_add_constraints()
        assert post.call_args.kwargs["timeout"] == hv._CHUNK_KEYS_ADD_TIMEOUT_S
        assert hv._CHUNK_KEYS_ADD_TIMEOUT_S >= hv._CHUNK_KEYS_VALIDATE_TIMEOUT_S
        assert hv._CHUNK_KEYS_ADD_TIMEOUT_S > hv._CHUNK_KEYS_TIMEOUT_S


class TestDryRunJob:
    """nexus-v75nw: the dry run of a large tenant outlasts the public edge's ~30 s origin timeout (four 504s on the
    production tenant), so ``chunk_keys_dry_run`` starts it as an engine job and polls. The transport is stubbed at
    ``_post`` / ``_get``; the engine half (the job lifecycle, and that the job's counts equal the synchronous ones) is
    ``ChunkKeySweepAsyncIntegrationTest``."""

    JOB = "11111111-2222-3333-4444-555555555555"
    PATH = f"/v1/admin/chunk-keys/sweep/jobs/{JOB}"

    @staticmethod
    def _started() -> dict:
        return {"job_id": TestDryRunJob.JOB, "tenant": "nexus", "state": "running", "stage": "queued", "elapsed_ms": 0}

    @staticmethod
    def _running(stage: str, ms: int) -> dict:
        return {"job_id": TestDryRunJob.JOB, "tenant": "nexus", "state": "running", "stage": stage, "elapsed_ms": ms}

    @staticmethod
    def _ended(status: int, body: dict, state: str = "done") -> dict:
        return {"job_id": TestDryRunJob.JOB, "tenant": "nexus", "state": state, "stage": state, "elapsed_ms": 61_000,
                "http_status": status, "body": body}

    class Clock:
        """A fake monotonic clock that a fake sleep advances: no test waits."""

        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        def monotonic(self) -> float:
            return self.now

    def _run(self, post, get, *, clock: "TestDryRunJob.Clock | None" = None, **kw):
        clock = clock or self.Clock()
        events: list[dict] = []
        with patch.object(hv, "_post", side_effect=post), patch.object(hv, "_get", side_effect=get):
            result = _client().chunk_keys_dry_run(
                tenant="nexus", progress=events.append, sleep=clock.sleep, monotonic=clock.monotonic, **kw)
        return result, events, clock

    def test_start_job_asks_for_async_on_a_dry_run_and_on_an_apply_and_the_plain_call_is_unchanged(self) -> None:
        with patch.object(hv, "_post", return_value={}) as post, patch.object(hv, "_guard_chunk_keys_write") as guard:
            _client().chunk_keys_sweep(start_job=True)
            guard.assert_not_called()
            _client().chunk_keys_sweep(dry_run=False, start_job=True)
            guard.assert_called_once()   # an apply job is a write: the production-write guard runs before the start
            _client().chunk_keys_sweep()
        started, apply, plain = (c.args[1] for c in post.call_args_list)
        assert started["async"] is True and started["dry_run"] is True
        assert apply["async"] is True and apply["dry_run"] is False
        assert "async" not in plain, "the synchronous call is unchanged"

    def test_a_dry_run_longer_than_the_edge_completes_by_polling_where_the_synchronous_call_504s(self) -> None:
        """The simulation: the synchronous request is cut by the edge after ~30 s (504); the job takes 50 s of polling."""
        report = _dry_report(tenant="nexus")

        def post(path, body, **kw):
            if body.get("async"):
                return self._started()
            raise VectorServiceError("POST ... HTTP 504", code=504)   # the edge

        polls = iter([self._running("census 1/3: knowledge__a__m__v1", 4_000), self._running("precondition:keyless_notes", 22_000),
                      self._running("precondition:wrong_width_rows", 41_000), self._ended(200, report)])

        with patch.object(hv, "_post", side_effect=post), patch.object(hv, "_guard_chunk_keys_write"):
            with pytest.raises(VectorServiceError) as direct:
                _client().chunk_keys_sweep(tenant="nexus")
            assert direct.value.code == 504, "positive control: the synchronous call does not fit the edge"

        result, events, clock = self._run(post, lambda path, **kw: next(polls), poll_schedule=(10.0,))

        assert result == report, "the job's answer is the synchronous answer"
        assert clock.sleeps == [10.0, 10.0, 10.0], "polled every interval until the job ended"
        assert [e["event"] for e in events] == ["started", "poll", "poll", "poll", "poll"]
        assert events[2]["stage"] == "precondition:keyless_notes"

    def test_the_poll_goes_to_the_jobs_route_of_the_job_the_start_named(self) -> None:
        paths: list[str] = []

        def get(path, **kw):
            paths.append(path)
            return self._ended(200, _dry_report())

        self._run(lambda p, b, **kw: self._started(), get)
        assert paths == [self.PATH]

    def test_an_engine_that_predates_the_job_answers_the_dry_run_itself_and_the_client_says_so(self) -> None:
        report = _dry_report(tenant="nexus")   # no job_id: the old engine ignored the field
        calls: list[str] = []

        result, events, clock = self._run(lambda p, b, **kw: report, lambda path, **kw: calls.append(path))

        assert result == report
        assert calls == [], "nothing to poll"
        assert events == [{"event": "synchronous"}]
        assert clock.sleeps == []

    def test_the_wait_is_bounded_and_the_timeout_names_the_job_that_goes_on(self) -> None:
        result_calls = {"n": 0}

        def get(path, **kw):
            result_calls["n"] += 1
            return self._running("precondition:keyless_ttl_rows", result_calls["n"] * 5_000)

        clock = self.Clock()
        with pytest.raises(hv.ChunkKeysDryRunTimeout) as err:
            self._run(lambda p, b, **kw: self._started(), get, clock=clock, max_wait_s=30.0, poll_schedule=(10.0,))

        assert err.value.job_id == self.JOB and err.value.reason == "sweep_job_still_running"
        assert isinstance(err.value, VectorServiceError)
        assert self.JOB in str(err.value) and "again to join" in str(err.value)
        assert clock.now == 30.0 and result_calls["n"] == 4, "polled at 0, 10, 20 and 30 s, then stopped waiting"

    def test_a_job_that_failed_raises_what_the_synchronous_call_would_have(self) -> None:
        body = {"tenant": "nexus", "error": "the dry run failed: DataAccessException", "reason": "sweep_failed",
                "dry_run": True, "rows_changed": False}

        with pytest.raises(VectorServiceError) as err:
            self._run(lambda p, b, **kw: self._started(), lambda path, **kw: self._ended(500, body, state="failed"))

        assert err.value.code == 500 and err.value.reason == "sweep_failed"
        assert err.value.engine_body == body

    def test_a_job_whose_audit_row_failed_raises_with_the_counts_in_the_body(self) -> None:
        body = {**_dry_report(tenant="nexus"), "reason": "audit_write_failed", "error": "the outcome row", "rows_changed": False}

        with pytest.raises(VectorServiceError) as err:
            self._run(lambda p, b, **kw: self._started(), lambda path, **kw: self._ended(500, body, state="failed"))

        assert err.value.reason == "audit_write_failed"
        assert err.value.engine_body["totals"] == body["totals"]

    def test_a_bad_poll_is_not_the_jobs_failure_but_five_in_a_row_are(self) -> None:
        report = _dry_report(tenant="nexus")
        seq = iter([VectorServiceError("504", code=504), urllib.error.URLError("reset"), self._running("sampling", 1),
                    TimeoutError("slow"), self._ended(200, report)])

        def get(path, **kw):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        result, _, _ = self._run(lambda p, b, **kw: self._started(), get)
        assert result == report, "the failures were not consecutive: the count of them restarts on a good poll"

        def always_504(path, **kw):
            raise VectorServiceError("504", code=504)

        with pytest.raises(VectorServiceError) as err:
            self._run(lambda p, b, **kw: self._started(), always_504)
        assert err.value.code == 504

    def test_a_poll_the_engine_refuses_for_a_client_reason_is_not_retried(self) -> None:
        calls = {"n": 0}

        def get(path, **kw):
            calls["n"] += 1
            raise VectorServiceError("403", code=403)

        with pytest.raises(VectorServiceError):
            self._run(lambda p, b, **kw: self._started(), get)
        assert calls["n"] == 1

    def test_a_job_the_engine_lost_is_started_again_once(self) -> None:
        report = _dry_report(tenant="nexus")
        posts = {"n": 0}
        gets = iter([VectorServiceError("404", code=404, reason="sweep_job_not_found"), self._ended(200, report)])

        def post(path, body, **kw):
            posts["n"] += 1
            return self._started()

        def get(path, **kw):
            item = next(gets)
            if isinstance(item, Exception):
                raise item
            return item

        result, events, _ = self._run(post, get)

        assert result == report and posts["n"] == 2
        assert [e["event"] for e in events if e["event"] in ("started", "restarted")] == ["started", "restarted"]

    def test_the_poll_backs_off_2_4_8_16_then_30_seconds_capped(self) -> None:
        """bob, 2026-11-07: the edge logs every root-credential request and pages security at 20 in 5 minutes."""
        ended = self._ended(200, _dry_report(tenant="nexus"))
        seq = iter([self._running("precondition:keyless_notes", 1_000)] * 12 + [ended])

        _, _, clock = self._run(lambda p, b, **kw: self._started(), lambda path, **kw: next(seq))

        assert clock.sleeps == [2.0, 4.0, 8.0, 16.0] + [30.0] * 8, "the schedule, exactly, the last entry repeated"

    def test_at_steady_state_a_long_dry_run_sends_at_most_ten_polls_in_any_five_minutes(self) -> None:
        clock = self.Clock()
        poll_times: list[float] = []

        def get(path, **kw):
            poll_times.append(clock.now)
            if clock.now < 20 * 60:
                return self._running("precondition:wrong_width_rows", int(clock.now * 1000))
            return self._ended(200, _dry_report(tenant="nexus"))

        self._run(lambda p, b, **kw: self._started(), get, clock=clock)

        assert poll_times[:6] == [0.0, 2.0, 6.0, 14.0, 30.0, 60.0]
        steady = [t for t in poll_times if t >= 60.0]
        assert len(steady) >= 30, "the window below is not empty"
        worst = max(sum(1 for u in steady if t <= u < t + 300.0) for t in steady)
        assert worst <= 10, f"{worst} polls in a five-minute window at steady state"
        assert sum(1 for t in poll_times if t < 300.0) <= 14, "the first five minutes, with the back-off ramp"

    def test_a_429_on_a_poll_counts_toward_the_failure_allowance_it_does_not_abort_at_once(self) -> None:
        report = _dry_report(tenant="nexus")
        seq = iter([VectorServiceError("429", code=429), self._ended(200, report)])

        def get(path, **kw):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        result, _, _ = self._run(lambda p, b, **kw: self._started(), get)
        assert result == report, "one 429 is a bad poll, not the job's failure"

        calls = {"n": 0}

        def always_429(path, **kw):
            calls["n"] += 1
            raise VectorServiceError("429", code=429)

        with pytest.raises(VectorServiceError) as err:
            self._run(lambda p, b, **kw: self._started(), always_429)
        assert err.value.code == 429 and calls["n"] == 5, "five in a row end the wait, as for a 5xx"

    def test_a_job_lost_twice_is_an_error_not_a_loop(self) -> None:
        def get(path, **kw):
            raise VectorServiceError("404", code=404, reason="sweep_job_not_found")

        posts = {"n": 0}

        def post(path, body, **kw):
            posts["n"] += 1
            return self._started()

        with pytest.raises(VectorServiceError) as err:
            self._run(post, get)
        assert err.value.reason == "sweep_job_not_found" and posts["n"] == 2

    def test_a_start_the_engine_refuses_is_raised_and_never_joined(self) -> None:
        """409 ``sweep_job_running`` is a DIFFERENT request in flight: its counts would answer another question."""
        refusal = VectorServiceError("409", code=409, reason="sweep_job_running", engine_body={"job_id": "other"})

        def post(path, body, **kw):
            raise refusal

        gets: list[str] = []
        with pytest.raises(VectorServiceError) as err:
            self._run(post, lambda path, **kw: gets.append(path))
        assert err.value is refusal and gets == []


class TestApplyJob:
    """nexus-v75nw round 3: the apply as an engine job. The verb used to loop synchronous calls, a root-credential
    request every ~8 s (about 37 in 5 minutes, over the edge's alarm of 20); now one start and a back-off poll. The
    transport is stubbed at ``_post`` / ``_get``; the engine half (the loop equals the synchronous one, the residue
    refusal, the cancel boundary, the cap) is ``ChunkKeySweepAsyncIntegrationTest``."""

    JOB = "aaaaaaaa-2222-3333-4444-555555555555"
    PATH = f"/v1/admin/chunk-keys/sweep/jobs/{JOB}"
    CANCEL = PATH + "/cancel"

    @staticmethod
    def _started() -> dict:
        return {"job_id": TestApplyJob.JOB, "tenant": "nexus", "kind": "apply", "state": "running", "stage": "queued",
                "elapsed_ms": 0}

    @staticmethod
    def _running(stage: str, ms: int, rows: int = 0) -> dict:
        return {"job_id": TestApplyJob.JOB, "tenant": "nexus", "kind": "apply", "state": "running", "stage": stage,
                "elapsed_ms": ms, "batches": rows // 1000, "rows_swept": rows, "remaining_rows": 9_000 - rows}

    @staticmethod
    def _ended(status: int, body: dict) -> dict:
        return {"job_id": TestApplyJob.JOB, "tenant": "nexus", "kind": "apply", "state": "done", "stage": "done",
                "elapsed_ms": 61_000, "http_status": status, "body": body}

    def _run(self, post, get, *, clock=None, **kw):
        clock = clock or TestDryRunJob.Clock()
        events: list[dict] = []
        with patch.object(hv, "_post", side_effect=post), patch.object(hv, "_get", side_effect=get), \
                patch.object(hv, "_guard_chunk_keys_write") as guard:
            result = _client().chunk_keys_apply(
                tenant="nexus", progress=events.append, sleep=clock.sleep, monotonic=clock.monotonic, **kw)
        return result, events, clock, guard

    def test_the_start_is_one_write_guarded_post_with_async_and_the_apply_fields_and_no_resume(self) -> None:
        bodies: list[dict] = []

        def post(path, body, **kw):
            bodies.append(body)
            return self._started()

        _, _, _, guard = self._run(post, lambda path, **kw: self._ended(200, {"done": True, "rows_swept": 3}),
                                   accept={"keyless_notes": 2}, max_batches=10, budget_seconds=8, batch_size=500)

        assert len(bodies) == 1
        assert bodies[0]["async"] is True and bodies[0]["dry_run"] is False
        assert bodies[0]["accept"] == {"keyless_notes": 2} and bodies[0]["max_batches"] == 10
        assert bodies[0]["budget_seconds"] == 8 and bodies[0]["batch_size"] == 500
        assert "resume" not in bodies[0], "the engine's job takes no token; it recounts"
        guard.assert_called_once()

    def test_an_apply_longer_than_the_edge_completes_by_polling_at_the_dry_runs_back_off(self) -> None:
        outcome = {"tenant": "nexus", "done": True, "rows_swept": 9_000, "remaining_rows": 0, "batches": 9, "calls": 4}
        polls = iter([self._running("precondition:keyless_notes", 4_000), self._running("sweeping", 30_000, 3_000),
                      self._running("sweeping", 90_000, 6_000), self._ended(200, outcome)])

        result, events, clock, _ = self._run(lambda p, b, **kw: self._started(), lambda path, **kw: next(polls))

        assert result.job_id == self.JOB and result.body == outcome
        assert clock.sleeps == [2.0, 4.0, 8.0], "the same 2, 4, 8, 16, 30 s schedule as the dry run"
        assert [e["event"] for e in events] == ["started", "poll", "poll", "poll", "poll"]
        assert events[3]["rows_swept"] == 6_000 and events[3]["remaining_rows"] == 3_000 and events[3]["batches"] == 6

    def test_a_three_hour_apply_sends_at_most_ten_polls_in_any_five_minutes_where_the_loop_sent_37(self) -> None:
        """The edge alarms at 20 root-credential requests in 5 minutes; the old loop sent a call every ~8 s."""
        clock = TestDryRunJob.Clock()
        polled_at: list[float] = []

        def get(path, **kw):
            polled_at.append(clock.now)
            if clock.now >= 3 * 3600:
                return self._ended(200, {"done": True, "rows_swept": 1})
            return self._running("sweeping", int(clock.now * 1000), 1000)

        self._run(lambda p, b, **kw: self._started(), get, clock=clock)

        window = [sum(1 for t in polled_at if start <= t < start + 300) for start in range(0, 3 * 3600, 30)]
        assert max(window) <= 10 + 4, "the first minutes poll faster while the schedule ramps (14 in the first 5)"
        assert max(window[12:]) <= 10, "steady state: one poll every 30 s"
        assert len(polled_at) < 400, "a 3-hour apply is a few hundred requests, not the loop's thousands"

    def test_a_job_that_ended_in_a_refusal_raises_what_the_synchronous_apply_would_have(self) -> None:
        body = {"reason": "sweep_preconditions_not_met", "error": "the sweep preconditions are not met",
                "blocking": ["homeless_documents"]}

        with pytest.raises(VectorServiceError) as err:
            self._run(lambda p, b, **kw: self._started(), lambda path, **kw: self._ended(409, body))

        assert err.value.code == 409 and err.value.reason == "sweep_preconditions_not_met"
        assert err.value.engine_body["blocking"] == ["homeless_documents"]

    def test_an_engine_that_predates_the_apply_job_answers_the_first_call_itself_and_the_caller_continues(self) -> None:
        first = _apply(4, done=False, remaining=2)

        result, events, clock, _ = self._run(lambda p, b, **kw: first, lambda path, **kw: pytest.fail("no poll"))

        assert result.job_id is None and result.body == first, "the first call of the synchronous loop, with its resume"
        assert [e["event"] for e in events] == ["synchronous"] and clock.sleeps == []

    def test_a_wait_that_runs_out_names_the_job_that_keeps_sweeping(self) -> None:
        with pytest.raises(hv.ChunkKeysDryRunTimeout) as err:
            self._run(lambda p, b, **kw: self._started(), lambda path, **kw: self._running("sweeping", 1, 1000),
                      max_wait_s=70.0)
        assert err.value.job_id == self.JOB and err.value.what == "apply"
        assert "keeps sweeping" in str(err.value) and err.value.reason == "sweep_job_still_running"

    def test_the_default_wait_outlasts_the_engines_four_hour_cap(self) -> None:
        assert hv._CHUNK_KEYS_APPLY_MAX_WAIT_S > 4 * 3600
        assert hv._CHUNK_KEYS_APPLY_MAX_WAIT_S > hv._CHUNK_KEYS_JOB_MAX_WAIT_S

    def test_a_lost_job_is_started_again_once_and_twice_is_an_error(self) -> None:
        posts = {"n": 0}

        def post(path, body, **kw):
            posts["n"] += 1
            return self._started()

        def get(path, **kw):
            raise VectorServiceError("404", code=404, reason="sweep_job_not_found")

        with pytest.raises(VectorServiceError) as err:
            self._run(post, get)
        assert err.value.reason == "sweep_job_not_found" and posts["n"] == 2

    def test_a_start_the_engine_refuses_is_raised_and_never_joined(self) -> None:
        refusal = VectorServiceError("409", code=409, reason="sweep_job_running", engine_body={"job_id": "other"})

        def post(path, body, **kw):
            raise refusal

        gets: list[str] = []
        with pytest.raises(VectorServiceError) as err:
            self._run(post, lambda path, **kw: gets.append(path))
        assert err.value is refusal and gets == []

    # ---- Ctrl-C --------------------------------------------------------------------------------------------

    def _interrupting(self, interrupts: int, ended: dict | None = None):
        """A clock whose sleep raises KeyboardInterrupt the first ``interrupts`` times, then behaves."""
        clock = TestDryRunJob.Clock()
        left = {"n": interrupts}
        real = clock.sleep

        def sleep(seconds: float) -> None:
            if left["n"] > 0:
                left["n"] -= 1
                raise KeyboardInterrupt
            real(seconds)

        clock.sleep = sleep   # type: ignore[method-assign]
        posts: list[str] = []

        def post(path, body, **kw):
            posts.append(path)
            return self._started()

        state = {"gets": 0}

        def get(path, **kw):
            state["gets"] += 1
            if ended is not None and state["gets"] >= 3:
                return self._ended(200, ended)
            return self._running("sweeping", state["gets"] * 1000, 1000 * state["gets"])

        return clock, post, get, posts

    def test_ctrl_c_answered_cancel_posts_the_cancel_and_goes_on_polling_for_the_outcome(self) -> None:
        cancelled = {"tenant": "nexus", "done": False, "stopped": "cancelled", "cancelled": True, "rows_swept": 2_000,
                     "remaining_rows": 7_000}
        clock, post, get, posts = self._interrupting(1, ended=cancelled)
        asked: list[str] = []

        result, events, _, _ = self._run(post, get, clock=clock, on_interrupt=lambda job_id: asked.append(job_id) or True)

        assert asked == [self.JOB]
        assert posts == ["/v1/admin/chunk-keys/sweep", self.CANCEL], "the cancel goes to the job's cancel route"
        assert result.body == cancelled, "the wait read the outcome the engine stopped with"
        assert "cancel_requested" in [e["event"] for e in events]

    def test_ctrl_c_answered_leave_it_raises_left_running_and_sends_no_cancel(self) -> None:
        clock, post, get, posts = self._interrupting(1)

        with pytest.raises(hv.ChunkKeysJobLeftRunning) as err:
            self._run(post, get, clock=clock, on_interrupt=lambda job_id: False)

        assert err.value.job_id == self.JOB and err.value.reason == "sweep_job_left_running"
        assert posts == ["/v1/admin/chunk-keys/sweep"], "the job was not cancelled"

    def test_ctrl_c_with_nobody_to_ask_leaves_the_job_running(self) -> None:
        clock, post, get, posts = self._interrupting(1)
        with pytest.raises(hv.ChunkKeysJobLeftRunning):
            self._run(post, get, clock=clock)
        assert posts == ["/v1/admin/chunk-keys/sweep"]

    def test_a_second_ctrl_c_while_the_cancel_is_pending_leaves_the_job_alone(self) -> None:
        clock, post, get, posts = self._interrupting(2)
        with pytest.raises(hv.ChunkKeysJobLeftRunning):
            self._run(post, get, clock=clock, on_interrupt=lambda job_id: True)
        assert posts.count(self.CANCEL) == 1, "asked once; the second interrupt does not ask again"

    def test_a_job_the_engine_lost_after_a_cancel_was_asked_is_never_started_again(self) -> None:
        """nexus-v75nw S1: Ctrl-C then cancel, then the poll finds the job gone (engine restart or expiry). A restart
        here would recount and sweep the rest of the tenant after the operator said stop."""
        clock, post, _, posts = self._interrupting(1)
        gets = {"n": 0}

        def get(path, **kw):
            gets["n"] += 1
            if gets["n"] >= 2:
                raise VectorServiceError("404", code=404, reason="sweep_job_not_found")
            return self._running("sweeping", 1000, 1000)

        with pytest.raises(hv.ChunkKeysJobLeftRunning) as err:
            self._run(post, get, clock=clock, on_interrupt=lambda job_id: True)

        assert err.value.reason == "sweep_job_gone_after_cancel" and err.value.job_id == self.JOB
        assert "not started again" in str(err.value)
        assert posts == ["/v1/admin/chunk-keys/sweep", self.CANCEL], "one start, one cancel: no second apply start"

    def test_a_cancel_post_that_finds_the_job_gone_is_not_a_reason_to_start_it_again(self) -> None:
        clock, _, get, posts = self._interrupting(1)

        def post(path, body, **kw):
            posts.append(path)
            if path == self.CANCEL:
                raise VectorServiceError("404", code=404, reason="sweep_job_not_found")
            return self._started()

        with pytest.raises(hv.ChunkKeysJobLeftRunning) as err:
            self._run(post, get, clock=clock, on_interrupt=lambda job_id: True)

        assert err.value.reason == "sweep_job_gone_after_cancel"
        assert posts == ["/v1/admin/chunk-keys/sweep", self.CANCEL]

    def test_ctrl_c_inside_the_cancel_post_prints_the_job_id_and_leaves_it(self) -> None:
        clock, _, get, posts = self._interrupting(1)

        def post(path, body, **kw):
            posts.append(path)
            if path == self.CANCEL:
                raise KeyboardInterrupt
            return self._started()

        with pytest.raises(hv.ChunkKeysJobLeftRunning) as err:
            self._run(post, get, clock=clock, on_interrupt=lambda job_id: True)

        assert err.value.job_id == self.JOB and self.JOB in str(err.value), "not a raw KeyboardInterrupt"
        assert err.value.reason == "sweep_job_left_running"

    def test_a_dry_run_that_is_interrupted_is_left_running_too(self) -> None:
        clock = TestDryRunJob.Clock()
        real = clock.sleep

        def sleep(seconds: float) -> None:
            raise KeyboardInterrupt

        clock.sleep = sleep   # type: ignore[method-assign]
        with patch.object(hv, "_post", return_value=TestDryRunJob._started()), \
                patch.object(hv, "_get", return_value=TestDryRunJob._running("sampling", 10)):
            with pytest.raises(hv.ChunkKeysJobLeftRunning) as err:
                _client().chunk_keys_dry_run(tenant="nexus", sleep=clock.sleep, monotonic=clock.monotonic)
        assert err.value.what == "dry run" and real is not None

    def test_cancel_is_a_post_to_the_cancel_route_of_that_job(self) -> None:
        with patch.object(hv, "_post", return_value={"cancel_accepted": True}) as post:
            out = _client().chunk_keys_cancel_job(self.JOB)
        assert out == {"cancel_accepted": True}
        assert post.call_args.args == (self.CANCEL, {}) and post.call_args.kwargs["tenant"] == "tenant-x"


# ── the CLI ──────────────────────────────────────────────────────────────────


def _dry_report(*, tenant: str = TENANT, blocking: list[str] | None = None, sweepable: int = 5) -> dict:
    return {
        "tenant": tenant,
        "collections": [{
            "collection": "knowledge__a__m__v1", "retired_key_rows": sweepable + 1, "wrong_width_rows": 1,
            "sweepable_rows": sweepable, "legacy_unmanifested": 0, "blocked": None,
        }],
        "totals": {"retired_key_rows": sweepable + 1, "sweepable_rows": sweepable, "blocked_rows": 0,
                   "wrong_width_rows": 1},
        "preconditions": {"keyless_notes": 2, "generated_nulls": 3, "null_span_rows": 2, "wrong_width_rows": 1},
        "precondition_details": {
            "generated_nulls": {"by_content_type": {"code": 2, "(none)": 1},
                                "missing_key": {"source_format": 1, "tags": 3, "category": 0}},
            "null_span_rows": {"by_field": {"line_start": 1, "line_end": 0, "chunk_start_char": 0, "chunk_end_char": 1}},
        },
        "acceptable_residues": ["keyless_notes", "generated_nulls", "null_span_rows"],
        "accepted": {},
        "blocking": blocking if blocking is not None else [],
        "applicable": not blocking,
        "batch_size": 1000,
        "batch_cost": {"samples": [{"collection": "knowledge__a__m__v1", "rows": sweepable, "select_ms": 3}],
                       "select_ms": 3, "update_ms": None, "projected_seconds": 0.0,
                       "projection_is_lower_bound": True},
    }


class FakeClient:
    """The three engine calls, recording each."""

    def __init__(self, report: dict, applies: list[dict] | None = None, error: Exception | None = None) -> None:
        self.report = report
        self.applies = list(applies or [])
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.dry_run_extras = []
        self.progress_events = []
        self.apply_job_results = []
        self.apply_job_events = []
        self.apply_job_kw = []

    def chunk_keys_sweep(self, **kw):
        self.calls.append(("sweep", kw))
        request_gate.before_request()   # what the transport does before each request
        if self.error is not None:
            raise self.error
        if kw.get("dry_run", True):
            return self.report
        return self.applies.pop(0)

    dry_run_extras: list[dict] = []   # replaced per instance in __init__

    def chunk_keys_dry_run(self, **kw):
        """The dry run the verb starts (the engine runs it as a background job): the stub answers it from its
        ``chunk_keys_sweep``, so the recorded call keeps the shape every CLI test reads, and keeps the job-only
        arguments (``progress``, ``max_wait_s``) apart."""
        progress = kw.pop("progress", None)
        extras = {k: kw.pop(k) for k in ("max_wait_s",) if k in kw}
        self.dry_run_extras.append({"progress": progress, **extras})
        for event in self.progress_events:
            progress(event)
        return self.chunk_keys_sweep(dry_run=True, **kw)

    progress_events: list[dict] = []

    #: What ``chunk_keys_apply`` answers, one per call: a ``ChunkKeysApplyResult`` (the engine's apply job), an
    #: Exception to raise, or empty for an engine that predates the apply job (the stub then makes the first call of the
    #: synchronous loop itself, from ``applies``, exactly as the real client does when the answer has no ``job_id``).
    apply_job_results: list = []
    apply_job_events: list[dict] = []
    apply_job_kw: list[dict] = []

    def chunk_keys_apply(self, **kw):
        progress = kw.pop("progress", None)
        self.apply_job_kw.append({"progress": progress, **kw})
        for event in self.apply_job_events:
            progress(event)
        if self.apply_job_results:
            result = self.apply_job_results.pop(0)
            if isinstance(result, Exception):
                raise result
            self.calls.append(("apply_job", kw))
            request_gate.before_request()
            return result
        kw.pop("on_interrupt", None)
        kw.pop("max_wait_s", None)
        progress({"event": "synchronous"})
        return hv.ChunkKeysApplyResult(self.chunk_keys_sweep(dry_run=False, resume=None, **kw), None)

    def chunk_keys_add_constraints(self, **kw):
        self.calls.append(("add", kw))
        if self.error is not None:
            raise self.error
        return self.add_result

    add_result: dict = {
        "tenants_checked": ["default", "cc-a", "cc-ghost"], "table_rows_examined": 77,
        "left_behind": [{"tenant": "cc-blocked", "blocked_collections": [
            {"collection": "knowledge__x__m__v1", "retired_key_rows": 4, "blocked": "legacy_unmanifested"}]}],
        "constraints": [
            {"name": "chunks_metadata_keys_check", "action": "added", "validated": False},
            {"name": "chunks_embedding_model_check", "action": "present_differs", "validated": False,
             "expected_definition": "CHECK (((metadata ->> 'embedding_model'::text) = embedding_model))"},
            {"name": "chunks_chunk_text_hash_check", "action": "replaced", "validated": False,
             "warning": "was VALID and is now NOT VALID until validate-constraints runs again"},
        ],
    }

    def chunk_keys_validate_constraints(self, **kw):
        self.calls.append(("validate", kw))
        if self.error is not None:
            raise self.error
        return {"tenants_checked": ["default", "cc-a"], "table_rows_examined": 5,
                "constraints": [{"name": "chunks_chash_octet_check", "action": "validated", "validated": True}]}


def _run(client: FakeClient, *args: str):
    with patch.object(rk, "_make_t3", return_value=client):
        return CliRunner().invoke(t3, ["retire-keys", *args])


def _apply(rows: int, *, done: bool, remaining: int = 0, locked: int = 0, marker: str = "m") -> dict:
    return {"tenant": TENANT, "dry_run": False, "rows_swept": rows, "batches": 1, "done": done,
            "remaining_rows": remaining, "locked_skipped_rows": locked, "first_batch_ms": 12, "elapsed_ms": 20,
            "blocked_collections": [],
            "resume": None if done else {"cursor": {"collection": "c", "chash": "ab" * 32}, "collections": ["c"],
                                         "blocked": [], "remaining": remaining, "marker": marker}}


class TestCli:
    def test_the_default_is_a_dry_run_that_prints_the_counts_and_sends_no_apply(self) -> None:
        client = FakeClient(_dry_report())
        result = _run(client, "--tenant", TENANT)
        assert result.exit_code == 0, result.output
        assert [kw["dry_run"] for _, kw in client.calls] == [True]
        assert "knowledge__a__m__v1" in result.output
        assert "5 to sweep" in result.output and "1 with a wrong-width chash" in result.output
        assert "keyless_notes: 2 (acceptable residue)" in result.output
        assert "Dry run: nothing written" in result.output

    def test_the_dry_run_is_the_engine_job_and_its_progress_is_printed_as_the_stage_changes(self) -> None:
        client = FakeClient(_dry_report())
        client.progress_events = [
            {"event": "started", "job_id": "j-1"},
            {"event": "poll", "job_id": "j-1", "state": "running", "stage": "census 1/2: c", "elapsed_ms": 2_000},
            {"event": "poll", "job_id": "j-1", "state": "running", "stage": "census 1/2: c", "elapsed_ms": 4_000},
            {"event": "poll", "job_id": "j-1", "state": "running", "stage": "precondition:keyless_notes", "elapsed_ms": 9_000},
            {"event": "poll", "job_id": "j-1", "state": "done", "stage": "done", "elapsed_ms": 11_000},
        ]
        result = _run(client, "--tenant", TENANT)
        assert result.exit_code == 0, result.output
        assert "dry run started as engine job j-1" in result.output
        assert result.output.count("dry run running: census 1/2: c") == 1, "one line per stage, not one per poll"
        assert "dry run running: precondition:keyless_notes (9 s)" in result.output
        assert "dry run done: done (11 s)" in result.output
        assert "5 to sweep" in result.output, "the counts are printed as before"

    def test_an_apply_never_passes_the_dry_runs_counts_on_it_counts_for_itself(self) -> None:
        """nexus-v75nw round 3: the dry run hands out no continuation. A report that carried a ``resume`` is not
        forwarded to the apply (the engine would count again anyway), and nothing of it is printed."""
        token = {"cursor": None, "collections": ["knowledge__a__m__v1"], "blocked": [], "remaining": 5, "sig": "ab" * 32}
        client = FakeClient({**_dry_report(), "resume": token}, applies=[_apply(5, done=True)])

        result = _run(client, "--tenant", TENANT, "--apply", "--json")

        assert result.exit_code == 0, result.output
        applies = [kw for _, kw in client.calls if not kw.get("dry_run", True)]
        assert applies[0]["resume"] is None, "the first apply call carries no token: it counts every precondition"

    def test_an_apply_runs_as_the_engine_job_and_the_verb_sends_no_call_loop(self) -> None:
        done = {**_apply(7, done=True), "calls": 3, "batches": 6, "audit": {"actor": "root:aa", "tenant": TENANT,
                                                                           "intent_id": 5, "outcome_id": 6}}
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysApplyResult(done, "j-apply")]
        client.apply_job_events = [
            {"event": "started", "job_id": "j-apply"},
            {"event": "poll", "job_id": "j-apply", "state": "running", "stage": "precondition:keyless_notes",
             "elapsed_ms": 2_000, "batches": 0, "rows_swept": 0, "remaining_rows": None},
            {"event": "poll", "job_id": "j-apply", "state": "running", "stage": "sweeping", "elapsed_ms": 9_000,
             "batches": 4, "rows_swept": 4_000, "remaining_rows": 3_000},
            {"event": "poll", "job_id": "j-apply", "state": "running", "stage": "sweeping", "elapsed_ms": 11_000,
             "batches": 5, "rows_swept": 5_000, "remaining_rows": 2_000},
        ]

        result = _run(client, "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2", "--json")

        assert result.exit_code == 0, result.output
        assert [kw["dry_run"] for name, kw in client.calls if name == "sweep"] == [True], \
            "one dry run and no synchronous apply call: the loop is the engine's"
        kw = client.apply_job_kw[0]
        assert kw["accept"] == {"keyless_notes": 2} and kw["tenant"] == TENANT
        assert kw["max_batches"] == rk._MAX_BATCHES and kw["budget_seconds"] == rk._BUDGET_SECONDS
        assert "resume" not in kw, "an apply job takes no token"
        assert kw["on_interrupt"] is rk._ask_cancel
        doc = json.loads(result.stdout[result.stdout.index("{"):])
        assert doc["applied"] == {"rows_swept": 7, "calls": 3, "remaining_rows": 0, "locked_skipped_rows": 0,
                                  "blocked_collections": [], "job_id": "j-apply"}
        assert {"call": "apply job", "actor": "root:aa", "tenant": TENANT, "intent_id": 5, "outcome_id": 6} in doc["audit"]
        assert "apply started as engine job j-apply" in result.output
        assert "apply running: precondition:keyless_notes" in result.output
        assert "apply running: sweeping [4 batches, 4000 rows swept, 3000 left] (9 s)" in result.output
        assert result.output.count("apply running: sweeping") == 1, "a stage line per change, not per poll"

    def test_the_human_summary_of_an_apply_job_names_the_rows_the_calls_and_the_audit_rows(self) -> None:
        done = {**_apply(7, done=True), "calls": 3, "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 5,
                                                              "outcome_id": 6}}
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysApplyResult(done, "j-apply")]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == 0, result.output
        assert "Swept 7 rows in 3 calls; 0 left." in result.output
        assert "gc_audit rows 5 (intent) and 6 (outcome)" in result.output

    def test_an_apply_job_the_engine_refused_on_a_residue_is_the_preconditions_exit_with_the_counts(self) -> None:
        refusal = VectorServiceError("job ended in HTTP 409", code=409, reason="sweep_preconditions_not_met", engine_body={
            "blocking": ["homeless_documents"], "report": {"preconditions": {"homeless_documents": 2}, "accepted": {}},
            "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 70, "outcome_id": 71}})
        client = FakeClient(_dry_report())
        client.apply_job_results = [refusal]

        result = _run(client, "--tenant", TENANT, "--apply")

        assert result.exit_code == rk.EXIT_PRECONDITION, result.output
        assert "homeless_documents=2" in result.output
        assert "gc_audit rows 70 (intent) and 71 (outcome)" in result.output

    @pytest.mark.parametrize(("stopped", "needle"), [("cancelled", "Cancelled at a batch boundary"),
                                                     ("wall_clock_cap", "4-hour limit")])
    def test_an_apply_job_that_stopped_early_says_why_keeps_the_rows_and_exits_1(self, stopped: str, needle: str) -> None:
        body = {**_apply(40, done=False, remaining=60), "stopped": stopped, "cancelled": stopped == "cancelled",
                "calls": 2, "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 5, "outcome_id": 6}}
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysApplyResult(body, "j-stop")]

        result = _run(client, "--tenant", TENANT, "--apply")

        assert result.exit_code == rk.EXIT_FAILED, result.output
        assert needle in result.output and "40 rows swept, 60 left" in result.output and "j-stop" in result.output
        assert "gc_audit rows 5 (intent) and 6 (outcome)" in result.output, "the audit rows are printed before the exit"

    def test_an_apply_job_that_stalled_on_locked_rows_is_the_stalled_exit(self) -> None:
        body = {**_apply(0, done=False, remaining=3, locked=3), "stalled": True, "calls": 3}
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysApplyResult(body, "j-stall")]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_STALLED, result.output
        assert "locked by a writer" in result.output

    def test_an_apply_job_that_answers_for_another_tenant_is_the_old_engine_exit(self) -> None:
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysApplyResult({**_apply(1, done=True), "tenant": "someone-else"}, "j-x")]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_NO_ROUTE, result.output

    def test_a_wait_that_ran_out_on_an_apply_does_not_say_nothing_was_written(self) -> None:
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysDryRunTimeout("j-long", 15300, "apply")]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "j-long" in result.output and "keeps sweeping" in result.output and "Rows may already have been swept" in result.output
        assert "Nothing was written" not in result.output

    def test_ctrl_c_that_leaves_the_apply_running_prints_the_job_and_how_to_rejoin_it(self) -> None:
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysJobLeftRunning("j-left", "apply")]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "j-left" in result.output and "still running in the engine" in result.output
        assert "Run the same command again to join it" in result.output

    def test_a_job_lost_after_a_cancel_is_reported_with_its_id_and_exits_failed(self) -> None:
        client = FakeClient(_dry_report())
        client.apply_job_results = [hv.ChunkKeysJobLeftRunning("j-gone", "apply", gone=True)]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "j-gone" in result.output and "not started again" in result.output
        assert "batches it committed are kept" in result.output

    def test_a_lost_apply_job_is_not_called_a_dry_run_and_says_the_batches_are_kept(self) -> None:
        client = FakeClient(_dry_report())
        client.apply_job_results = [VectorServiceError("404", code=404, reason="sweep_job_not_found")]
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "apply job" in result.output and "batches it committed are kept" in result.output

    def test_an_apply_that_finds_its_own_apply_running_rejoins_it_without_a_dry_run_report(self) -> None:
        """The wait ran out (or Ctrl-C left it): the same command's dry run is 409 against the running apply, so the verb
        asks for the same apply, which the engine joins when the request is the same."""
        running = VectorServiceError("409", code=409, reason="sweep_job_running",
                                     engine_body={"job_id": "j-run", "kind": "apply"})
        client = FakeClient(_dry_report(), error=None)
        orig = client.chunk_keys_dry_run

        def dry(**kw):
            raise running

        client.chunk_keys_dry_run = dry   # type: ignore[method-assign]
        client.apply_job_results = [hv.ChunkKeysApplyResult({**_apply(9, done=True), "calls": 2}, "j-run")]

        result = _run(client, "--tenant", TENANT, "--apply")

        assert result.exit_code == 0, result.output
        assert "already running as engine job j-run" in result.output
        assert "Swept 9 rows" in result.output
        assert orig is not None

    def test_a_dry_run_blocked_by_a_running_apply_is_refused_with_the_jobs_id(self) -> None:
        running = VectorServiceError("409", code=409, reason="sweep_job_running",
                                     engine_body={"job_id": "j-run", "kind": "apply"})
        result = _run(FakeClient(_dry_report(), error=running), "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_FAILED
        assert "j-run" in result.output and "already running a sweep" in result.output and "Nothing was written" in result.output

    def test_a_different_request_while_a_dry_run_counts_is_refused_not_joined(self) -> None:
        running = VectorServiceError("409", code=409, reason="sweep_job_running",
                                     engine_body={"job_id": "j-dry", "kind": "dry_run"})
        result = _run(FakeClient(_dry_report(), error=running), "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "j-dry" in result.output and "dry run of this tenant" in result.output
        assert "joining it" not in result.output, "only an apply is rejoined"

    def test_an_engine_without_the_apply_job_continues_the_synchronous_loop_from_the_first_answer(self) -> None:
        """Old engine: ``async`` is ignored, so the start request IS the first call of the loop (it swept in that
        request). The verb must carry that answer's continuation into call 2, not start over."""
        client = FakeClient(_dry_report(), applies=[_apply(4, done=False, remaining=2, marker="after-1"),
                                                    _apply(2, done=True)])
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == 0, result.output
        assert "predates the background apply" in result.output
        applies = [kw for _, kw in client.calls if not kw["dry_run"]]
        assert len(applies) == 2 and applies[0]["resume"] is None and applies[1]["resume"]["marker"] == "after-1"
        assert "Swept 6 rows in 2 calls" in result.output

    def test_wait_seconds_reaches_the_apply_job_too_and_is_sent_only_when_given(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(1, done=True), _apply(1, done=True)])
        _run(client, "--tenant", TENANT, "--apply")
        _run(client, "--tenant", TENANT, "--apply", "--wait-seconds", "7200")
        assert "max_wait_s" not in client.apply_job_kw[0]
        assert client.apply_job_kw[1]["max_wait_s"] == 7200.0

    def test_ctrl_c_without_a_terminal_leaves_the_job_running_and_with_one_asks(self) -> None:
        with patch.object(rk.sys.stdin, "isatty", return_value=False):
            assert rk._ask_cancel("j-1") is False
        with patch.object(rk.sys.stdin, "isatty", return_value=True), \
                patch.object(rk.click, "confirm", return_value=True) as confirm:
            assert rk._ask_cancel("j-1") is True
            assert "j-1" in confirm.call_args.args[0] and confirm.call_args.kwargs["default"] is False
        with patch.object(rk.sys.stdin, "isatty", return_value=True), \
                patch.object(rk.click, "confirm", side_effect=rk.click.Abort()):
            assert rk._ask_cancel("j-1") is False, "a second Ctrl-C at the prompt leaves it running"

    def test_an_engine_without_the_job_falls_back_to_the_synchronous_count_and_says_so(self) -> None:
        client = FakeClient(_dry_report())
        client.progress_events = [{"event": "synchronous"}]
        result = _run(client, "--tenant", TENANT)
        assert result.exit_code == 0, result.output
        assert "predates the background dry run" in result.output and "5 to sweep" in result.output

    def test_wait_seconds_bounds_the_polling_and_is_sent_only_when_given(self) -> None:
        client = FakeClient(_dry_report())
        _run(client, "--tenant", TENANT)
        assert "max_wait_s" not in client.dry_run_extras[0]
        _run(client, "--tenant", TENANT, "--wait-seconds", "90")
        assert client.dry_run_extras[1]["max_wait_s"] == 90.0

    def test_a_wait_that_ran_out_exits_1_and_says_the_job_goes_on_and_nothing_was_written(self) -> None:
        client = FakeClient(_dry_report(), error=hv.ChunkKeysDryRunTimeout("j-9", 1800))
        result = _run(client, "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_FAILED
        assert "j-9" in result.output and "goes on in the engine" in result.output and "Nothing was written" in result.output
        assert "does not carry the retire-keys routes" not in result.output

    def test_a_lost_job_is_not_mistaken_for_an_engine_without_the_routes(self) -> None:
        lost = VectorServiceError("404", code=404, reason="sweep_job_not_found")
        result = _run(FakeClient(_dry_report(), error=lost), "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_FAILED, "404 is EXIT_NO_ROUTE only when the route is what is missing"
        assert "no longer holds the dry-run job" in result.output

    def test_the_dry_run_says_its_cost_figure_is_the_select_only_lower_bound(self) -> None:
        result = _run(FakeClient(_dry_report()), "--tenant", TENANT)
        assert "nothing written or locked" in result.output
        assert "LOWER BOUND" in result.output and "not measured" in result.output
        assert "rolled back" not in result.output, "no UPDATE is run, not even a rolled-back one"

    def test_the_dry_run_prints_the_breakdown_of_the_two_residues_that_have_one(self) -> None:
        result = _run(FakeClient(_dry_report()), "--tenant", TENANT)
        assert "generated_nulls by content type: code=2, (none)=1; missing key: source_format=1, tags=3, category=0" \
            in result.output
        assert "null_span_rows by column: line_start=1, line_end=0, chunk_start_char=0, chunk_end_char=1" \
            in result.output

    def test_the_dry_run_points_a_nonzero_null_span_rows_at_the_span_fill_rung(self) -> None:
        """nexus-l8o33 (review r1): the count is the rung's to lower, so the dry run says which rung and what stays."""
        result = _run(FakeClient(_dry_report()), "--tenant", TENANT)
        assert "manifest-span-fill" in result.output and "span keys hold 0" in result.output
        zero = _dry_report()
        zero["preconditions"] = {**zero["preconditions"], "null_span_rows": 0}
        assert "manifest-span-fill" not in _run(FakeClient(zero), "--tenant", TENANT).output

    def test_dry_run_and_apply_together_are_a_usage_error(self) -> None:
        client = FakeClient(_dry_report())
        result = _run(client, "--tenant", TENANT, "--dry-run", "--apply")
        assert result.exit_code == rk.EXIT_USAGE
        assert client.calls == []

    def test_the_tenant_is_required_for_a_sweep(self) -> None:
        client = FakeClient(_dry_report())
        result = _run(client, "--apply")
        assert result.exit_code == rk.EXIT_USAGE
        assert client.calls == []

    def test_an_engine_that_answers_for_another_tenant_is_too_old_to_sweep_it_one_line_exit_4(self) -> None:
        """An engine that predates the target field ignores ``tenant`` and answers for the credential's own tenant
        (a root token bound to ``default``). The verb says so in one line and sends no apply."""
        client = FakeClient(_dry_report(tenant="default"))
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_NO_ROUTE
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1, result.output
        assert "cannot sweep another tenant" in lines[0]
        assert "'default'" in lines[0] and repr(TENANT) in lines[0]
        # Actionable on a cloud whose client credentials are mint-locked: the engine version, both halves, the root
        # token, and no advice to use a tenant's own token (which cannot be minted there).
        assert "engine-service-v0.1.178" in lines[0] and "--tenant" in lines[0] and "root service token" in lines[0]
        assert "own NX_SERVICE_TOKEN" not in lines[0]
        assert "Traceback" not in result.output
        assert all(kw["dry_run"] for _, kw in client.calls), "no apply request was sent"

    def test_the_target_is_sent_on_the_dry_run_and_on_every_apply_call(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(4, done=False, remaining=2), _apply(2, done=True)])
        result = _run(client, "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2")
        assert result.exit_code == 0, result.output
        assert [kw["tenant"] for _, kw in client.calls] == [TENANT, TENANT, TENANT]

    def test_an_apply_answer_that_names_another_tenant_stops_the_loop(self) -> None:
        wrong = {**_apply(4, done=False, remaining=2), "tenant": "default"}
        client = FakeClient(_dry_report(), applies=[wrong, _apply(2, done=True)])
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_NO_ROUTE
        assert len([c for c in client.calls if not c[1]["dry_run"]]) == 1, "the loop stopped at the wrong answer"

    @pytest.mark.parametrize("reason", ["tenant_mismatch", "unknown_tenant"])
    def test_a_target_the_engine_refuses_is_a_usage_exit_that_prints_the_engines_reason(self, reason: str) -> None:
        refusal = VectorServiceError(
            "refused", code=403 if reason == "tenant_mismatch" else 422, reason=reason,
            engine_body={"error": f"engine says {reason}", "reason": reason},
        )
        client = FakeClient(_dry_report(), error=refusal)
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_USAGE
        assert f"engine says {reason}" in result.output

    def test_the_operator_label_is_sent_on_the_dry_run_and_every_apply_call(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(4, done=False, remaining=2), _apply(2, done=True)])
        result = _run(client, "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2", "--operator", "sam-laptop")
        assert result.exit_code == 0, result.output
        assert [kw["operator"] for _, kw in client.calls] == ["sam-laptop"] * 3
        bare = FakeClient(_dry_report())
        _run(bare, "--tenant", TENANT)
        assert bare.calls[0][1]["operator"] is None

    def test_the_operator_label_is_refused_on_the_table_wide_steps_which_record_no_audit(self) -> None:
        client = FakeClient(_dry_report())
        result = _run(client, "--add-check", "--apply", "--operator", "sam")
        assert result.exit_code == rk.EXIT_USAGE and client.calls == []

    def test_the_audit_row_ids_and_actor_the_engine_returns_are_printed_for_a_dry_run(self) -> None:
        report = {**_dry_report(), "audit": {"actor": "root:0123456789abcdef", "tenant": TENANT, "intent_id": 41,
                                              "outcome_id": 42}}
        result = _run(FakeClient(report), "--tenant", TENANT)
        assert result.exit_code == 0, result.output
        assert "gc_audit rows 41 (intent) and 42 (outcome)" in result.output
        assert "actor root:0123456789abcdef" in result.output

    def test_an_apply_prints_every_calls_audit_rows_and_json_carries_them(self) -> None:
        def audit(i: int) -> dict:
            return {"actor": "root:aa", "tenant": TENANT, "intent_id": i, "outcome_id": i + 1}
        applies = [{**_apply(4, done=False, remaining=2), "audit": audit(11)}, {**_apply(2, done=True), "audit": audit(13)}]
        dry = {**_dry_report(), "audit": audit(9)}
        text = _run(FakeClient(dry, applies=list(applies)), "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2")
        assert text.exit_code == 0, text.output
        for label, ids in (("dry run", "9 (intent) and 10"), ("call 1", "11 (intent) and 12"),
                           ("call 2", "13 (intent) and 14")):
            assert f"audit ({label}): gc_audit rows {ids} (outcome)" in text.output, text.output
        doc = json.loads(_run(FakeClient(dry, applies=list(applies)), "--tenant", TENANT, "--apply",
                              "--accept", "keyless_notes=2", "--json").stdout)
        assert [(a["call"], a["intent_id"]) for a in doc["audit"]] == [("dry run", 9), ("call 1", 11), ("call 2", 13)]

    def test_an_audit_write_failure_says_rows_were_changed_prints_the_counts_the_resume_hint_and_exits_6(self) -> None:
        """The engine swept and could not write the outcome row (500 audit_write_failed): the old verb printed one
        'refused' line. The operator must read that the rows changed, how many, and what to do next."""
        failing = VectorServiceError("POST ... HTTP 500", code=500, reason="audit_write_failed", engine_body={
            "error": "the sweep ran but its outcome audit row could not be written; this call's counts are in this "
                     "answer",
            "reason": "audit_write_failed", "rows_swept": 7, "rows_changed": True, "done": False,
            "resume": {"sig": "s"}, "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 31,
                                              "outcome_id": None}})

        class SecondApplyFails(FakeClient):
            def chunk_keys_sweep(self, **kw):
                if not kw.get("dry_run", True) and sum(1 for _, k in self.calls if not k.get("dry_run", True)) == 1:
                    self.calls.append(("sweep", kw))
                    raise failing
                return super().chunk_keys_sweep(**kw)

        client = SecondApplyFails(_dry_report(), applies=[_apply(4, done=False, remaining=9)])
        result = _run(client, "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2")
        assert result.exit_code == rk.EXIT_AUDIT == 6
        assert "7 rows swept by this call (4 more in the earlier calls of this run)" in result.output
        assert "WERE changed" in result.output
        assert "gc_audit id 31" in result.output and "no outcome row" in result.output
        assert "re-running the same command" in result.output
        assert "nx catalog gc-audit list --operation retire_keys_sweep" in result.output
        assert "Traceback" not in result.output

    def test_an_audit_write_failure_on_a_dry_run_says_no_row_was_changed(self) -> None:
        failing = VectorServiceError("POST ... HTTP 500", code=500, reason="audit_write_failed", engine_body={
            "error": "x", "reason": "audit_write_failed", "rows_changed": False, "dry_run": True,
            "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 5, "outcome_id": None}})
        result = _run(FakeClient(_dry_report(), error=failing), "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_AUDIT
        assert "no row was changed" in result.output and "WERE changed" not in result.output
        assert "?" not in result.output and "rows swept" not in result.output, result.output

    def test_a_sweep_that_failed_part_way_reports_the_partial_counts_and_exits_6(self) -> None:
        failing = VectorServiceError("POST ... HTTP 500", code=500, reason="sweep_failed", engine_body={
            "error": "the sweep failed after 1 rows in 1 batches", "reason": "sweep_failed", "rows_swept": 1,
            "rows_changed": True, "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 5, "outcome_id": 6}})
        result = _run(FakeClient(_dry_report(), error=failing), "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_AUDIT
        assert "1 rows swept by this call" in result.output and "WERE changed" in result.output
        assert "confirmed batches" in result.output and "in flight at the failure may also have committed" in result.output
        assert "gc_audit id 6" in result.output

    def test_a_sweep_failure_whose_outcome_row_also_failed_does_not_print_id_none(self) -> None:
        failing = VectorServiceError("POST ... HTTP 500", code=500, reason="sweep_failed", engine_body={
            "error": "the sweep failed after 0 rows in 0 batches", "reason": "sweep_failed", "rows_swept": 0,
            "rows_changed": False, "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 5,
                                             "outcome_id": None}})
        result = _run(FakeClient(_dry_report(), error=failing), "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_AUDIT
        assert "None" not in result.output, result.output
        assert "outcome row could not be written either" in result.output and "gc_audit id 5" in result.output
        assert "may still have committed" in result.output   # rows_changed false: still no promise of untouched rows

    def test_the_audit_read_hint_names_the_row_level_security_and_the_sql_route(self) -> None:
        failing = VectorServiceError("POST ... HTTP 500", code=500, reason="audit_write_failed", engine_body={
            "error": "x", "reason": "audit_write_failed", "rows_changed": False, "dry_run": True,
            "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 5, "outcome_id": None}})
        out = _run(FakeClient(_dry_report(), error=failing), "--tenant", TENANT).output
        for needle in ("row-level security", "BYPASSRLS", "SET nexus.tenant", "--limit N"):
            assert needle in out, (needle, out)

    @pytest.mark.parametrize("reason", ["audit_unavailable", "audit_intent_failed"])
    def test_a_refusal_before_any_read_says_nothing_was_changed_and_exits_1(self, reason: str) -> None:
        refusal = VectorServiceError("refused", code=503 if reason == "audit_unavailable" else 500, reason=reason,
                                     engine_body={"error": f"engine says {reason}", "reason": reason})
        result = _run(FakeClient(_dry_report(), error=refusal), "--tenant", TENANT)
        assert result.exit_code == rk.EXIT_FAILED
        assert f"engine says {reason}" in result.output and "No chunk row was read or changed" in result.output

    def test_a_preconditions_refusal_prints_the_audit_rows_it_left(self) -> None:
        refusal = VectorServiceError("refused", code=409, reason="sweep_preconditions_not_met", engine_body={
            "error": "the sweep preconditions are not met", "reason": "sweep_preconditions_not_met",
            "blocking": ["keyless_notes"], "report": {"preconditions": {"keyless_notes": 3}, "accepted": {}},
            "audit": {"actor": "root:aa", "tenant": TENANT, "intent_id": 70, "outcome_id": 71}})
        result = _run(FakeClient(_dry_report(), error=refusal), "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_PRECONDITION
        assert "gc_audit rows 70 (intent) and 71 (outcome)" in result.output

    def test_apply_loops_until_the_engine_says_done_and_sums_the_rows(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(4, done=False, remaining=2), _apply(2, done=True)])
        result = _run(client, "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2")
        assert result.exit_code == 0, result.output
        modes = [kw["dry_run"] for _, kw in client.calls]
        assert modes == [True, False, False]
        assert client.calls[0][1]["measure_batch"] is False, "the identity check does not pay for a measured batch"
        assert client.calls[1][1]["accept"] == {"keyless_notes": 2}
        assert client.calls[1][1]["budget_seconds"] == rk._BUDGET_SECONDS
        assert "Swept 6 rows in 2 calls" in result.output

    def test_the_continuation_the_engine_returns_is_sent_back_and_the_first_call_sends_none(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(4, done=False, remaining=2, marker="after-1"),
                                                    _apply(2, done=False, remaining=1, marker="after-2"),
                                                    _apply(1, done=True)])
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == 0, result.output
        applies = [kw for _, kw in client.calls if not kw["dry_run"]]
        assert applies[0]["resume"] is None, "the first call does the checks"
        assert applies[1]["resume"]["marker"] == "after-1", "the next call carries the previous call's resume"
        assert applies[2]["resume"]["marker"] == "after-2"
        assert applies[1]["resume"]["cursor"]["chash"] == "ab" * 32

    def test_locked_rows_the_engine_skipped_are_reported_and_a_run_that_cannot_finish_stops(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(0, done=False, remaining=1, locked=1) for _ in range(5)])
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_STALLED
        assert "1 skipped: locked by a writer" in result.output
        assert "locked by a writer" in result.output.splitlines()[-1]

    @pytest.mark.parametrize("bad", ["keyless_notes", "keyless_notes=", "=3", "keyless_notes=many", "keyless_notes=-1"])
    def test_accept_wants_a_name_and_a_number(self, bad: str) -> None:
        client = FakeClient(_dry_report())
        result = _run(client, "--tenant", TENANT, "--accept", bad)
        assert result.exit_code == rk.EXIT_USAGE
        assert "NAME=N" in result.output
        assert client.calls == []

    def test_accept_is_not_for_the_table_wide_steps(self) -> None:
        client = FakeClient(_dry_report())
        assert _run(client, "--add-check", "--apply", "--accept", "keyless_notes=1").exit_code == rk.EXIT_USAGE
        assert client.calls == []

    def test_apply_is_not_sent_while_the_engine_reports_a_blocking_precondition(self) -> None:
        client = FakeClient(_dry_report(blocking=["keyless_notes"]))
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_PRECONDITION
        assert "keyless_notes" in result.output and "--accept" in result.output
        assert all(kw["dry_run"] for _, kw in client.calls)

    def test_an_apply_the_engine_refuses_after_the_dry_run_names_the_blocking_counts(self) -> None:
        refusal = VectorServiceError(
            "409", code=409, reason="sweep_preconditions_not_met",
            engine_body={"error": "no", "reason": "sweep_preconditions_not_met", "blocking": ["keyless_notes"],
                         "report": {"preconditions": {"keyless_notes": 7}}},
        )
        client = FakeClient(_dry_report(), applies=[])
        client.chunk_keys_sweep = lambda **kw: (_ for _ in ()).throw(refusal) if not kw.get("dry_run", True) \
            else _dry_report()   # type: ignore[method-assign]
        result = _run(client, "--tenant", TENANT, "--apply", "--accept", "keyless_notes=2")
        assert result.exit_code == rk.EXIT_PRECONDITION
        assert "keyless_notes=7" in result.output and "--accept NAME=N" in result.output

    def test_an_apply_that_sweeps_nothing_and_never_finishes_is_stopped(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(0, done=False, remaining=9) for _ in range(5)])
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_STALLED
        assert len([c for c in client.calls if not c[1]["dry_run"]]) == rk._STALL_LIMIT

    def test_an_engine_without_the_routes_is_exit_4(self) -> None:
        client = FakeClient(_dry_report(), error=VectorServiceError("404", code=404))
        assert _run(client, "--tenant", TENANT).exit_code == rk.EXIT_NO_ROUTE

    def test_json_carries_one_document(self) -> None:
        client = FakeClient(_dry_report())
        result = _run(client, "--tenant", TENANT, "--json")
        doc = json.loads(result.stdout)
        assert doc["dry_run"] is True and doc["report"]["tenant"] == TENANT
        assert doc["tenant"] == TENANT, "the document names the tenant that was asked for"

    def test_max_rps_installs_the_request_gate_for_the_whole_run(self) -> None:
        client = FakeClient(_dry_report(), applies=[_apply(5, done=True)])
        seen: list[request_gate.RequestGate] = []
        real = request_gate.RequestGate

        def spy(max_rps=None, **kw):
            gate = real(max_rps, sleep=lambda _s: None)
            seen.append(gate)
            return gate

        with patch.object(request_gate, "RequestGate", spy):
            result = _run(client, "--tenant", TENANT, "--apply", "--max-rps", "2")
        assert result.exit_code == 0, result.output
        assert seen[0].max_rps == 2.0
        assert seen[0].count == 2, "the dry run and the one apply call each passed the gate"

    def test_the_table_wide_steps_need_apply_and_no_tenant(self) -> None:
        client = FakeClient(_dry_report())
        for flag in ("--add-check", "--validate"):
            result = _run(client, flag)
            assert result.exit_code == rk.EXIT_USAGE, "a step that did nothing must not exit 0 in a script"
            assert "--apply" in result.output
        assert client.calls == [], "without --apply nothing is sent"
        assert _run(client, "--add-check", "--tenant", TENANT, "--apply").exit_code == rk.EXIT_USAGE
        assert _run(client, "--add-check", "--validate", "--apply").exit_code == rk.EXIT_USAGE
        assert _run(client, "--replace", "--apply", "--tenant", TENANT).exit_code == rk.EXIT_USAGE

    def test_add_check_and_validate_call_their_routes(self) -> None:
        client = FakeClient(_dry_report())
        added = _run(client, "--add-check", "--replace", "--apply")
        assert added.exit_code == 0, added.output
        assert "chunks_metadata_keys_check: added" in added.output
        validated = _run(client, "--validate", "--apply")
        assert validated.exit_code == 0, validated.output
        assert [c[0] for c in client.calls] == ["add", "validate"]
        assert client.calls[0][1] == {"replace": True}

    def test_add_check_names_the_tenants_checked_what_it_left_behind_and_every_warning(self) -> None:
        added = _run(FakeClient(_dry_report()), "--add-check", "--apply")
        assert added.exit_code == 0, added.output
        assert "checked 3 tenants: default, cc-a, cc-ghost" in added.output
        assert "77 chunk rows examined" in added.output
        assert "Left behind" in added.output and "refuses ANY UPDATE" in added.output
        assert "tenant cc-blocked: knowledge__x__m__v1 (legacy_unmanifested), 4 rows" in added.output
        assert "chunks_embedding_model_check: present_differs" in added.output
        assert "Expected: CHECK (((metadata ->> 'embedding_model'::text) = embedding_model))" in added.output
        assert "--replace" in added.output
        assert "WARNING: was VALID and is now NOT VALID" in added.output

    def test_replace_help_says_a_valid_constraint_becomes_not_valid(self) -> None:
        text = CliRunner().invoke(t3, ["retire-keys", "--help"]).output
        flat = " ".join(text.split())
        assert "was VALID is NOT VALID until --validate runs again" in flat

    def test_a_table_wide_refusal_names_the_tenants(self) -> None:
        refusal = VectorServiceError(
            "409", code=409, reason="tenant_unswept",
            engine_body={"error": "refused", "reason": "tenant_unswept",
                         "tenants": [{"tenant": "cc-a", "unswept_rows": 3}]},
        )
        client = FakeClient(_dry_report(), error=refusal)
        result = _run(client, "--add-check", "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "cc-a" in result.output and "unswept_rows=3" in result.output

    def test_a_refusal_names_every_tenant_the_engine_checked(self) -> None:
        refusal = VectorServiceError(
            "409", code=409, reason="tenant_unswept",
            engine_body={"error": "refused", "reason": "tenant_unswept",
                         "tenants": [{"tenant": "cc-ghost", "unswept_rows": 2}],
                         "tenants_checked": ["default", "cc-a", "cc-ghost"]},
        )
        result = _run(FakeClient(_dry_report(), error=refusal), "--add-check", "--apply")
        assert "tenants checked (3): default, cc-a, cc-ghost" in result.output


    def test_a_timed_out_add_says_it_may_have_landed_and_that_a_rerun_is_safe(self) -> None:
        client = FakeClient(_dry_report(), error=TimeoutError("timed out"))
        result = _run(client, "--add-check", "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "may have landed" in result.output
        assert "rerun" in result.output.lower()

    def test_a_timed_out_add_through_urllib_is_recognised_too(self) -> None:
        client = FakeClient(_dry_report(), error=urllib.error.URLError(TimeoutError("timed out")))
        result = _run(client, "--add-check", "--apply")
        assert result.exit_code == rk.EXIT_FAILED and "may have landed" in result.output

    def test_a_timed_out_validate_says_the_engine_may_still_be_validating(self) -> None:
        result = _run(FakeClient(_dry_report(), error=TimeoutError("timed out")), "--validate", "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "may still be running" in result.output and "validate" in result.output.lower()

    def test_a_connection_refused_is_not_called_a_timeout(self) -> None:
        client = FakeClient(_dry_report(), error=urllib.error.URLError(ConnectionRefusedError("refused")))
        result = _run(client, "--add-check", "--apply")
        assert "may have landed" not in result.output
        assert result.exception is not None   # unchanged: the original error propagates

    def test_an_apply_whose_resume_the_engine_did_not_accept_says_so(self) -> None:
        first = _apply(5, done=False, remaining=3)
        second = {**_apply(3, done=True), "resume_ignored": True}
        result = _run(FakeClient(_dry_report(), applies=[first, second]), "--tenant", TENANT, "--apply")
        assert result.exit_code == 0
        assert "resume not accepted" in result.output


def test_the_verb_is_registered_under_t3() -> None:
    assert "retire-keys" in t3.commands


# ── no automatic trigger ─────────────────────────────────────────────────────


class TestNoAutomaticTrigger:
    """Slice C of nexus-6pfgb.43 (the upgrade-ladder rung that sweeps every install) ships only after a cloud dry
    run has been read. Until it does, only the verb may reach the routes: no hook, rung, doctor row or daemon calls
    them, and the engine mounts them for a request to reach and runs them from no schedule."""

    #: Where the routes, the client methods and the verb may be named: the client (defines the three methods), the
    #: verb, the group that registers it, the key table's docstring, the engine classes that implement and mount
    #: the routes, and the tests of all of them. Anything else in the repository that names one is a caller.
    NAMING_ALLOWED = {
        "src/nexus/db/http_vector_client.py",
        "src/nexus/commands/t3_cmds/retire_keys.py",
        "src/nexus/commands/t3.py",
        "src/nexus/tables/chunk_metadata_keys.py",
        "service/src/main/java/dev/nexus/service/NexusService.java",
        "service/src/main/java/dev/nexus/service/vectors/ChunkKeyPlan.java",
        "service/src/main/java/dev/nexus/service/vectors/ChunkKeySweep.java",
        "service/src/main/java/dev/nexus/service/http/ChunkKeysAdminHandler.java",
        "tests/test_retire_keys.py",
        "service/src/main/java/dev/nexus/service/http/ChunkKeysSweepJobs.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepAsyncIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/http/ChunkKeysSweepJobsTest.java",
        # reads the engine's null_span_rows through the dry run (nothing is written) to compare it with the span-fill rung's summary
        "tests/upgrade/test_legacy_rows_walk_substrate.py",
        "service/src/test/java/dev/nexus/service/ChunkKeysTestBase.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepRootTargetIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/ChunkConstraintRoutesIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/db/ManifestWriteRetryGateTest.java",
        "service/src/test/java/dev/nexus/service/db/TenantKeyedStateGateTest.java",
        "service/src/test/java/dev/nexus/service/db/ChunksWriterLastWrittenAtScanTest.java",
        "service/src/test/java/dev/nexus/service/EgressGuardCensus.java",
    }
    NAMES = ("chunk_keys_sweep", "chunk_keys_dry_run", "chunk_keys_add_constraints", "chunk_keys_validate_constraints",
             "/v1/admin/chunk-keys", "admin/chunk-keys", "retire-keys", "retire_keys")
    #: Not scanned: version control, environments and build output, and prose (docs, the site, any *.md) that only
    #: describes the verb. Everything else is: src, scripts, conexus, .github, deploy, hooks, tests, the engine.
    SKIP_DIRS = {".git", ".venv", "node_modules", "target", "__pycache__", "docs", "web", ".mypy_cache",
                 ".ruff_cache", ".pytest_cache", ".beads", ".claude", "build", "dist"}
    #: Directories the brief names; each that exists must have been scanned (a gate that scanned none is vacuous).
    NAMED_ROOTS = ("src", "scripts", "conexus", ".github", "deploy")

    def _scan(self) -> tuple[dict[str, set[str]], dict[str, int]]:
        import os  # noqa: PLC0415 — only this scan walks the tree

        root = Path(__file__).resolve().parent.parent
        seen: dict[str, set[str]] = {}
        scanned: dict[str, int] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fname in filenames:
                path = Path(dirpath) / fname
                if path.suffix == ".md":
                    continue
                try:
                    if path.stat().st_size > 3_000_000:
                        continue
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                rel = path.relative_to(root).as_posix()
                top = rel.split("/", 1)[0]
                scanned[top] = scanned.get(top, 0) + 1
                hits = {n for n in self.NAMES if n in text}
                if hits:
                    seen[rel] = hits
        return seen, scanned

    def test_nothing_in_the_repository_names_the_routes_but_the_verb_the_engine_and_their_tests(self) -> None:
        seen, scanned = self._scan()
        for top in self.NAMED_ROOTS:
            if (Path(__file__).resolve().parent.parent / top).is_dir():
                assert scanned.get(top, 0) > 0, f"the scan looked at nothing under {top}/"
        assert sum(scanned.values()) > 1000, "the scan examined too few files to mean anything"
        assert set(seen) <= self.NAMING_ALLOWED, \
            f"a file other than the verb, the engine and their tests names the routes: {sorted(set(seen) - self.NAMING_ALLOWED)}"
        assert {"src/nexus/db/http_vector_client.py", "src/nexus/commands/t3_cmds/retire_keys.py"} <= set(seen), \
            "the scan cannot see the files that define the routes' client: it is not looking where it says"

    def test_the_engines_main_sources_name_the_classes_only_where_they_are_built_and_bound(self) -> None:
        root = Path(__file__).resolve().parent.parent / "service" / "src" / "main" / "java"
        callers = {
            p.name for p in root.rglob("*.java")
            if any(tok in p.read_text(encoding="utf-8")
                   for tok in ("ChunkKeySweep", "ChunkConstraintAdmin", "ChunkKeysAdminHandler"))
        }
        # The classes themselves, the handler that mounts them, and the two places that build and bind them.
        assert callers == {"ChunkKeySweep.java", "ChunkConstraintAdmin.java", "ChunkKeysAdminHandler.java",
                           "NexusService.java", "Main.java"}

    def test_nexus_service_and_main_never_invoke_the_operations_or_schedule_them(self) -> None:
        """Not a file whitelist: every line of NexusService and Main that names the sweep, the admin or the handler
        (and the two lines either side) is read, and none may call an operation on them or sit beside a scheduler."""
        import re  # noqa: PLC0415 — only this check needs a regex

        root = Path(__file__).resolve().parent.parent / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service"
        mention = re.compile(r"\b(ChunkKeySweep|ChunkConstraintAdmin|ChunkKeysAdminHandler|chunkConstraintAdmin)\b")
        operations = (".dryRun(", ".apply(", ".probe(", ".collectionsOf(", ".validate(", ".add(", ".states(",
                      ".preconditions(", ".preconditionReport(", ".run(", ".submit(", ".execute(")
        forbidden_words = ("schedul", "Executor", "Runnable", "startup", "Timer", "Thread")
        examined = 0
        for name in ("NexusService.java", "Main.java"):
            lines = (root / name).read_text(encoding="utf-8").splitlines()
            hit_lines = [i for i, line in enumerate(lines) if mention.search(line)]
            assert hit_lines, f"{name} no longer builds or binds the routes' classes: the check is looking at nothing"
            for i in hit_lines:
                for j in range(max(0, i - 2), min(len(lines), i + 3)):
                    stripped = lines[j].strip()
                    if stripped.startswith(("//", "*", "/*")):
                        continue
                    examined += 1
                    assert not any(op in lines[j] for op in operations), f"{name}:{j + 1} invokes an operation: {stripped}"
                    assert not any(w in lines[j] for w in forbidden_words), \
                        f"{name}:{j + 1} sits beside a scheduler or startup hook: {stripped}"
                    assert not re.search(r"\bchunkConstraintAdmin\s*\.", lines[j]), \
                        f"{name}:{j + 1} calls on the admin: {stripped}"
        assert examined >= 8, "the check read too few lines to mean anything"


# ── the verb against the real engine ─────────────────────────────────────────


def _chash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _metadata(tenant: str, collection: str, chash: str) -> dict:
    from tests._chunk_seed import _lit, _pg_state, _psql_superuser  # noqa: PLC0415 — the substrate helpers load only for the engine-backed test

    raw = _psql_superuser(
        _pg_state(),
        "SELECT metadata::text FROM nexus.chunks "
        f"WHERE tenant_id = {_lit(tenant)} AND collection = {_lit(collection)} AND chash = decode({_lit(chash)}, 'hex')",
    )
    return json.loads(raw)


def _last_written(tenant: str, collection: str, chash: str) -> str:
    from tests._chunk_seed import _lit, _pg_state, _psql_superuser  # noqa: PLC0415 — the substrate helpers load only for the engine-backed test

    return _psql_superuser(
        _pg_state(),
        "SELECT last_written_at::text FROM nexus.chunks "
        f"WHERE tenant_id = {_lit(tenant)} AND collection = {_lit(collection)} AND chash = decode({_lit(chash)}, 'hex')",
    )


def test_the_verb_end_to_end_against_the_real_engine(t2_service_env) -> None:
    """The dry run counts, the apply sweeps exactly the retired keys without moving ``last_written_at``, a rerun finds
    nothing, and the wrong tenant is refused: through the CLI, the client and the engine's own routes."""
    from tests._chunk_seed import seed_chunks_direct  # noqa: PLC0415 — the substrate helpers load only for the engine-backed test

    tenant = t2_service_env
    collection = "knowledge__retire-e2e__bge-base-en-v15-768__v1"
    ids = [_chash(f"{collection}/{i}") for i in range(3)]
    seed_chunks_direct(
        collection, ids, [f"text {i}" for i in range(3)],
        [{"chunk_text_hash": h, "title": f"t{i}", "tags": "a,b", "doc_id": "nodoc", "chunk_index": i}
         for i, h in enumerate(ids)],
        tenant=tenant,
    )
    stamps = [_last_written(tenant, collection, h) for h in ids]
    runner = CliRunner()

    dry = runner.invoke(t3, ["retire-keys", "--tenant", tenant, "--json"])
    assert dry.exit_code == 0, dry.output
    report = json.loads(dry.stdout)["report"]
    assert report["tenant"] == tenant
    assert [(c["collection"], c["retired_key_rows"]) for c in report["collections"]] == [(collection, 3)]
    assert report["batch_cost"]["projection_is_lower_bound"] is True and report["batch_cost"]["update_ms"] is None
    assert report["batch_cost"]["samples"][0]["rows"] == 3, "the sampled batch SELECT saw the three rows"
    assert _metadata(tenant, collection, ids[0])["title"] == "t0", "the dry run wrote nothing"

    wrong = runner.invoke(t3, ["retire-keys", "--tenant", "somebody-else", "--apply"])
    assert wrong.exit_code == rk.EXIT_USAGE, wrong.output
    assert _metadata(tenant, collection, ids[0])["title"] == "t0", "the wrong tenant stopped the verb before any write"

    applied = runner.invoke(t3, ["retire-keys", "--tenant", tenant, "--apply", "--json"])
    assert applied.exit_code == 0, applied.output
    doc = json.loads(applied.stdout[applied.stdout.index("{"):])
    assert doc["applied"]["rows_swept"] == 3 and doc["applied"]["remaining_rows"] == 0
    for h in ids:
        assert set(_metadata(tenant, collection, h)) == {"chunk_text_hash"}
    assert [_last_written(tenant, collection, h) for h in ids] == stamps, "last_written_at is the reaper's grace anchor"

    again = runner.invoke(t3, ["retire-keys", "--tenant", tenant, "--apply", "--json"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout[again.stdout.index("{"):])["applied"]["rows_swept"] == 0
