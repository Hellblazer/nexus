# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus._session_end_census (nexus-h33x8.3).

Covers the bead's four VERIFICATION items to the extent they belong to
this module (the durable-JSONL record builder + writer):

1. A session with zero Skill calls produces a record with
   ``capabilities["skill"] == 0``, found in the durable JSONL afterwards.
2. Visibility is out of scope for this module -- settled by source in
   ``_session_end_launcher`` (see that module's docstring); not
   re-tested here.
3. NON-VACUITY: an unreadable/absent transcript yields a BLINDSPOT
   record (``blindspot: True``), never a zeroed one.
4. hooks.json is untouched by this module entirely (asserted in
   ``test_session_end_launcher.py``).
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest


def _tool_use_record(name: str, ts: str = "2026-08-20T00:00:00Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "name": name, "input": {}}]},
    }


def _write_transcript(path: pathlib.Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


class TestBuildCapabilityCensusRecord:
    def test_zero_skill_calls_recorded_as_measured_zero(self, tmp_path: pathlib.Path) -> None:
        """VERIFICATION 1: a session with zero Skill calls produces a
        record containing skill=0 -- a MEASURED zero, not a blindspot."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-zero-skill"
        _write_transcript(
            project_dir / f"{sid}.jsonl",
            [_tool_use_record("Bash"), _tool_use_record("Bash"), _tool_use_record("Read")],
        )

        from nexus._session_end_census import build_capability_census_record

        record = build_capability_census_record(project_dir, sid)

        assert record["blindspot"] is False
        assert record["session_id"] == sid
        assert record["capabilities"]["skill"] == 0
        assert record["capabilities"]["baseline"] == 3
        assert record["total_calls"] == 3

    def test_capabilities_orchestrator_and_subagent_split_is_computed(
        self, tmp_path: pathlib.Path,
    ) -> None:
        """nexus-gjv9b PART 3 prerequisite: the record carries the
        orchestrator/subagent-split dimension the transcript-walk reader
        already has, alongside (not instead of) the merged
        ``capabilities`` total."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-scope-split"
        _write_transcript(
            project_dir / f"{sid}.jsonl",
            [_tool_use_record("Bash"), _tool_use_record("Skill")],
        )
        sub_dir = project_dir / sid / "subagents"
        sub_dir.mkdir(parents=True)
        _write_transcript(
            sub_dir / "agent-a1.jsonl",
            [_tool_use_record("mcp__plugin_conexus_nexus__search")],
        )

        from nexus._session_end_census import build_capability_census_record
        from nexus.census import CAPABILITIES

        record = build_capability_census_record(project_dir, sid)

        assert record["blindspot"] is False
        assert set(record["capabilities_orchestrator"]) == set(CAPABILITIES)
        assert set(record["capabilities_subagent"]) == set(CAPABILITIES)
        assert record["capabilities_orchestrator"]["baseline"] == 1
        assert record["capabilities_orchestrator"]["skill"] == 1
        assert record["capabilities_orchestrator"]["search_query"] == 0
        assert record["capabilities_subagent"]["search_query"] == 1
        assert record["capabilities_subagent"]["baseline"] == 0
        # the merged total is unchanged -- the split is additive detail,
        # never a replacement for the existing precedent.
        assert record["capabilities"]["baseline"] == 1
        assert record["capabilities"]["skill"] == 1
        assert record["capabilities"]["search_query"] == 1

    def test_genuinely_zero_tool_calls_is_a_measured_zero_not_blindspot(
        self, tmp_path: pathlib.Path,
    ) -> None:
        """code-review Important #1 (fix pass, 2026-08-20): a session whose
        transcript is READABLE and PARSEABLE but made literally zero tool
        calls of any kind (``nexus.census.UNMEASURABLE_NO_TOOL_USE``) is a
        MEASURED fact -- the session used nothing -- not a measurement
        failure. It must produce a real all-zero ``capabilities`` record,
        not collapse into the same blindspot bucket as an
        unreadable/missing transcript (verification 3 covers that
        DIFFERENT case; this one must NOT be blindspot)."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-truly-idle"
        # A record that parses fine (records_seen > 0, errors == 0) but
        # carries no assistant tool_use block at all -- exactly the
        # UNMEASURABLE_NO_TOOL_USE precedence branch in census_session.
        _write_transcript(
            project_dir / f"{sid}.jsonl",
            [{"type": "user", "timestamp": "2026-08-20T00:00:00Z", "message": {"content": "hi"}}],
        )

        from nexus._session_end_census import build_capability_census_record
        from nexus.census import CAPABILITIES

        record = build_capability_census_record(project_dir, sid)

        assert record["blindspot"] is False
        assert record["session_id"] == sid
        assert record["capabilities"] == dict.fromkeys(CAPABILITIES, 0)
        assert record["dispatches"] == 0
        assert record["total_calls"] == 0
        assert "unmeasurable_reason" not in record
        # nexus-gjv9b PART 3 prerequisite: a measured zero is a real zero
        # at BOTH scopes, not merely the merged total.
        assert record["capabilities_orchestrator"] == dict.fromkeys(CAPABILITIES, 0)
        assert record["capabilities_subagent"] == dict.fromkeys(CAPABILITIES, 0)

    def test_reports_counts_not_verdicts(self, tmp_path: pathlib.Path) -> None:
        """Bead: 'REPORT COUNTS, NOT VERDICTS' -- no advisory text field."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-counts-only"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        from nexus._session_end_census import build_capability_census_record

        record = build_capability_census_record(project_dir, sid)

        for value in record.values():
            if isinstance(value, str):
                assert "should have" not in value.lower()
                assert "you should" not in value.lower()

    def test_dispatch_count_reuses_h33x8_2_recognizer(self, tmp_path: pathlib.Path) -> None:
        """Reuses census_session_dispatches (nexus-h33x8.2) rather than
        re-deriving a dispatch count from raw Agent tool_use counts."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-dispatches"
        agent_block = {
            "type": "assistant",
            "timestamp": "2026-08-20T00:00:00Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Agent",
                        "input": {"subagent_type": "developer"},
                    },
                ],
            },
        }
        _write_transcript(project_dir / f"{sid}.jsonl", [agent_block, agent_block])

        from nexus._session_end_census import build_capability_census_record
        from nexus.census import census_session_dispatches

        record = build_capability_census_record(project_dir, sid)
        expected = len(census_session_dispatches(project_dir, sid).dispatches)

        assert record["dispatches"] == expected
        assert record["dispatches"] >= 1

    def test_missing_transcript_yields_blindspot_not_zero(self, tmp_path: pathlib.Path) -> None:
        """VERIFICATION 3 (absent variant): no transcript file at all must
        never render as a clean zero."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-never-existed"

        from nexus._session_end_census import build_capability_census_record

        record = build_capability_census_record(project_dir, sid)

        assert record["blindspot"] is True
        assert record["session_id"] == sid
        assert "capabilities" not in record
        assert record["unmeasurable_reason"]
        # nexus-gjv9b PART 3 prerequisite: a blindspot record carries no
        # scope split either -- nothing was measured at either scope.
        assert "capabilities_orchestrator" not in record
        assert "capabilities_subagent" not in record

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod permission semantics")
    def test_unreadable_transcript_yields_blindspot_not_zero(
        self, tmp_path: pathlib.Path,
    ) -> None:
        """VERIFICATION 3 (unreadable variant): a transcript that exists
        but cannot be read must also never render as a clean zero."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        sid = "sess-unreadable"
        transcript = project_dir / f"{sid}.jsonl"
        _write_transcript(transcript, [_tool_use_record("Bash")])
        os.chmod(transcript, 0)
        try:
            if os.access(transcript, os.R_OK):
                pytest.skip("running as a user/root that bypasses chmod 0 (e.g. root)")

            from nexus._session_end_census import build_capability_census_record

            record = build_capability_census_record(project_dir, sid)

            assert record["blindspot"] is True
            assert "capabilities" not in record
            assert record["unmeasurable_reason"]
        finally:
            os.chmod(transcript, 0o644)

    def test_no_advisory_verdict_language_present(self, tmp_path: pathlib.Path) -> None:
        """Bead: no 'you should have used X' language anywhere in the
        blindspot path either."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        from nexus._session_end_census import build_capability_census_record

        record = build_capability_census_record(project_dir, "sess-missing")
        rendered = json.dumps(record).lower()
        assert "should have" not in rendered


class TestWriteSessionCapabilityCensus:
    """nexus-gjv9b PART 1 writer swap: the durable write target moved from
    the JSONL log to the PG-backed ``capability_census`` engine table.
    These tests exercise :func:`write_session_capability_census`'s two
    halves at the seam (``_post_capability_census``): the record is
    always BUILT and returned regardless of write outcome, and the write
    itself is HTTP-first with a metered-drop fallback, never a JSONL
    append (see that function's own docstring for the design decision).
    """

    def test_measures_and_posts_via_http(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VERIFICATION 1 continued: the record is POSTED to the engine
        table -- the post-call assertion is the real one now."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))
        sid = "sess-durable-log"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        import nexus._session_end_census as mod

        posted: list[dict] = []
        monkeypatch.setattr(mod, "_post_capability_census", posted.append)

        record = mod.write_session_capability_census(sid)

        assert record is not None
        assert record["session_id"] == sid
        assert record["capabilities"]["skill"] == 0
        assert posted == [record]

    def test_write_failure_degrades_to_metered_drop_never_raises(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Service-down (or an old engine 404ing the route) must never
        propagate past :func:`write_session_capability_census` -- the
        design decision is a metered drop, never a JSONL fallback."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))
        sid = "sess-write-fails"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        import nexus._session_end_census as mod

        def _boom(base_url_unused: str) -> tuple[str, str]:
            raise RuntimeError("service unreachable")

        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint", _boom,
        )
        drops: list[dict] = []
        monkeypatch.setattr(
            "nexus.dropped_writes.record_drop",
            lambda **kw: drops.append(kw),
        )

        record = mod.write_session_capability_census(sid)  # must not raise

        assert record is not None
        assert record["session_id"] == sid
        assert len(drops) == 1
        assert drops[0]["hook"] == "capability_census"

    def test_404_from_the_engine_is_metered_with_cause_route_absent(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-gjv9b review fold-in round 4: a plugin cut can ship this
        writer ahead of the paired engine tag -- the cloud engine has no
        capability_census route yet, so every SessionEnd 404s until the
        engine catches up. Must classify as route_absent (version skew),
        not a generic failure -- lets the REAL dropped_writes.record_drop
        run (not a spy) so its own classify_drop_cause fallback is what
        is under test here."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        drop_path = tmp_path / "drops.jsonl"
        monkeypatch.setenv("NX_DROPPED_WRITES_LOG_PATH", str(drop_path))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))
        sid = "sess-404-route-absent"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        import nexus._session_end_census as mod

        def _boom() -> tuple[str, str]:
            # resolve_service_endpoint() takes no arguments; mirrors
            # RefreshableHttpStoreMixin._raise_for_status's exact message
            # shape for a 404 -- classify_drop_cause matches on the
            # literal "HTTP 404" substring, never a re-dispatch. (The
            # sibling fixtures in this file declare an unused
            # base_url_unused parameter that resolve_service_endpoint()
            # never actually passes -- harmless there since they never
            # assert on the resulting cause, but this test does, so the
            # signature must match the real call.)
            raise RuntimeError(
                "HttpTelemetryStore.record_capability_census failed: "
                "HTTP 404: Not Found"
            )

        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint", _boom,
        )

        record = mod.write_session_capability_census(sid)  # must not raise

        assert record is not None
        lines = drop_path.read_text().splitlines()
        assert len(lines) == 1
        dropped = json.loads(lines[0])
        assert dropped["hook"] == "capability_census"
        assert dropped["cause"] == "route_absent"

    def test_write_failure_also_logs_a_structlog_warning(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Review fix (nexus-gjv9b fold-in): a dropped write must be
        diagnosable from the logs, not just countable via nx doctor's
        drop meter -- _write_capability_census's own except-Exception
        never fires for this path (write_session_capability_census
        never raises), so the warning must be logged AT the point of
        failure, inside _post_capability_census itself."""
        import logging

        import structlog
        from structlog.testing import capture_logs

        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))
        sid = "sess-write-fails-logged"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        import nexus._session_end_census as mod

        def _boom(base_url_unused: str) -> tuple[str, str]:
            raise RuntimeError("service unreachable")

        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint", _boom,
        )
        monkeypatch.setattr(
            "nexus.dropped_writes.record_drop", lambda **kw: None,
        )

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
        try:
            with capture_logs() as cap:
                record = mod.write_session_capability_census(sid)  # must not raise
        finally:
            structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

        assert record is not None
        events = [entry["event"] for entry in cap]
        assert "capability_census_write_dropped" in events, events
        dropped_entry = next(e for e in cap if e["event"] == "capability_census_write_dropped")
        # nexus-gjv9b review fold-in round 3, code-review item 3: a
        # dropped BLINDSPOT row is a materially different diagnosis than
        # a dropped measured row -- must be readable from the same log
        # line, not just cross-referenced from the record separately.
        assert "blindspot" in dropped_entry, dropped_entry
        assert dropped_entry["blindspot"] is record.get("blindspot")

    def test_post_forwards_scope_split_to_the_store(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """nexus-gjv9b PART 3 prerequisite: ``_post_capability_census``
        must forward ``capabilities_orchestrator``/``capabilities_subagent``
        from the built record straight through to
        ``HttpTelemetryStore.record_capability_census`` -- the wire half
        of the writer swap, exercised without a live engine (the real
        HTTP round trip is covered separately by
        ``CapabilityCensusAndRoutingEventsHandlerTest`` on the Java side
        and ``test_http_t2_store_parity.py`` on this side)."""
        monkeypatch.setattr(
            "nexus.db.service_endpoint.resolve_service_endpoint",
            lambda: ("http://engine.invalid", "static-token"),
        )

        class _FakeDataTokenManager:
            def bearer_for(self, base_url: str, tenant: str) -> None:
                return None

        monkeypatch.setattr(
            "nexus.db.data_token.get_data_token_manager", _FakeDataTokenManager,
        )

        calls: list[dict] = []

        class _FakeStore:
            def __init__(self, *, base_url: str, _token: str) -> None:
                self.base_url = base_url

            def record_capability_census(self, **kwargs) -> None:
                calls.append(kwargs)

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore", _FakeStore,
        )

        import nexus._session_end_census as mod

        record = {
            "session_id": "sess-forward-scope",
            "timestamp": "2026-09-05T00:00:00Z",
            "blindspot": False,
            "capabilities": {"skill": 1},
            "dispatches": 0,
            "total_calls": 1,
            "capabilities_orchestrator": {"skill": 1},
            "capabilities_subagent": {"skill": 0},
        }
        mod._post_capability_census(record)

        assert len(calls) == 1
        assert calls[0]["capabilities_orchestrator"] == {"skill": 1}
        assert calls[0]["capabilities_subagent"] == {"skill": 0}

    def test_no_session_id_resolvable_is_a_silent_noop(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        import nexus.session
        monkeypatch.setattr(nexus.session, "read_claude_session_id", lambda: None)

        import nexus._session_end_census as mod

        posted: list[dict] = []
        monkeypatch.setattr(mod, "_post_capability_census", posted.append)

        result = mod.write_session_capability_census()

        assert result is None
        assert posted == []

    def test_blindspot_record_is_posted_too(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A BLINDSPOT session still gets posted -- it is a real, durable
        record of the failure to measure, not dropped."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))

        import nexus._session_end_census as mod

        posted: list[dict] = []
        monkeypatch.setattr(mod, "_post_capability_census", posted.append)

        record = mod.write_session_capability_census("sess-blindspot-durable")

        assert record is not None
        assert record["blindspot"] is True
        assert posted == [record]

