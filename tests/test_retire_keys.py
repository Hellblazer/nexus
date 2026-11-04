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

    def chunk_keys_sweep(self, **kw):
        self.calls.append(("sweep", kw))
        request_gate.before_request()   # what the transport does before each request
        if self.error is not None:
            raise self.error
        if kw.get("dry_run", True):
            return self.report
        return self.applies.pop(0)

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

    def test_a_credential_bound_to_another_tenant_stops_the_verb_before_any_write(self) -> None:
        client = FakeClient(_dry_report(tenant="somebody-else"))
        result = _run(client, "--tenant", TENANT, "--apply")
        assert result.exit_code == rk.EXIT_USAGE
        assert "somebody-else" in result.output
        assert all(kw["dry_run"] for _, kw in client.calls), "no apply request was sent"

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
        import urllib.error

        client = FakeClient(_dry_report(), error=urllib.error.URLError(TimeoutError("timed out")))
        result = _run(client, "--add-check", "--apply")
        assert result.exit_code == rk.EXIT_FAILED and "may have landed" in result.output

    def test_a_timed_out_validate_says_the_engine_may_still_be_validating(self) -> None:
        result = _run(FakeClient(_dry_report(), error=TimeoutError("timed out")), "--validate", "--apply")
        assert result.exit_code == rk.EXIT_FAILED
        assert "may still be running" in result.output and "validate" in result.output.lower()

    def test_a_connection_refused_is_not_called_a_timeout(self) -> None:
        import urllib.error

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
        "service/src/test/java/dev/nexus/service/ChunkKeysTestBase.java",
        "service/src/test/java/dev/nexus/service/ChunkKeySweepIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/ChunkConstraintRoutesIntegrationTest.java",
        "service/src/test/java/dev/nexus/service/db/ManifestWriteRetryGateTest.java",
        "service/src/test/java/dev/nexus/service/db/TenantKeyedStateGateTest.java",
        "service/src/test/java/dev/nexus/service/db/ChunksWriterLastWrittenAtScanTest.java",
        "service/src/test/java/dev/nexus/service/EgressGuardCensus.java",
    }
    NAMES = ("chunk_keys_sweep", "chunk_keys_add_constraints", "chunk_keys_validate_constraints",
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
