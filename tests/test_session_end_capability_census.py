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


class TestCapabilityCensusLogPath:
    def test_resolves_via_nexus_config_dir_not_hardcoded_home(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bead: 'resolve config dir properly, never hardcode HOME'."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))

        from nexus._session_end_census import capability_census_log_path

        assert capability_census_log_path() == cfg_dir / "capability_census.jsonl"

    def test_lives_alongside_routing_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same directory as ~/.config/nexus/routing_log.jsonl -- same
        precedent/location conventions (bead text)."""
        from nexus._session_end_census import capability_census_log_path
        from nexus.config import nexus_config_dir

        assert capability_census_log_path().parent == nexus_config_dir()


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


class TestLogRotation:
    """Size-gated rotation-by-atomic-rename (Sam-directed fix pass,
    2026-08-20): capability_census.jsonl grows without bound otherwise.
    Rotation, never trim-in-place -- see ``_rotate_log_if_oversized``'s own
    docstring for why a read-modify-write is a foot-cannon for a
    multi-writer append log (concurrent SessionEnd appenders can interleave
    a rewrite with another process's line-atomic append, clobbering it; a
    crash mid-rewrite loses the file outright)."""

    def test_undersize_log_is_left_untouched(self, tmp_path: pathlib.Path) -> None:
        from nexus._session_end_census import _rotate_log_if_oversized

        log_path = tmp_path / "capability_census.jsonl"
        log_path.write_text('{"session_id": "small"}\n')

        _rotate_log_if_oversized(log_path)

        assert log_path.read_text() == '{"session_id": "small"}\n'
        assert not log_path.with_name(log_path.name + ".1").exists()

    def test_oversize_log_rotates_via_atomic_rename(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus._session_end_census as mod

        monkeypatch.setattr(mod, "_LOG_ROTATION_MAX_BYTES", 10)
        log_path = tmp_path / "capability_census.jsonl"
        log_path.write_text("x" * 100)

        mod._rotate_log_if_oversized(log_path)

        assert not log_path.exists(), "the live path must be empty/gone after rotation"
        rotated = tmp_path / "capability_census.jsonl.1"
        assert rotated.exists()
        assert rotated.read_text() == "x" * 100

    def test_rotation_clobbers_prior_generation_not_accumulate(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exactly one older generation is retained -- ``.1`` is clobbered,
        never pushed to ``.2``, bounding total on-disk size at ~2x the cap."""
        import nexus._session_end_census as mod

        monkeypatch.setattr(mod, "_LOG_ROTATION_MAX_BYTES", 10)
        log_path = tmp_path / "capability_census.jsonl"
        (tmp_path / "capability_census.jsonl.1").write_text("STALE-OLD-GENERATION")
        log_path.write_text("FRESH" * 5)

        mod._rotate_log_if_oversized(log_path)

        rotated = tmp_path / "capability_census.jsonl.1"
        assert rotated.read_text() == "FRESH" * 5
        assert not (tmp_path / "capability_census.jsonl.2").exists()

    def test_concurrent_rotation_race_file_not_found_is_tolerated(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A second process's rename hitting FileNotFoundError (it already
        rotated the file away between our stat and our rename) is expected
        and must be silently swallowed, never raised."""
        import nexus._session_end_census as mod

        monkeypatch.setattr(mod, "_LOG_ROTATION_MAX_BYTES", 10)
        log_path = tmp_path / "capability_census.jsonl"
        log_path.write_text("x" * 100)

        def _simulated_concurrent_rotation(_src: object, _dst: object) -> None:
            raise FileNotFoundError("simulated: another process rotated first")

        monkeypatch.setattr(mod.os, "replace", _simulated_concurrent_rotation)

        mod._rotate_log_if_oversized(log_path)  # must not raise

    # nexus-gjv9b PART 1 writer swap: the two integration tests formerly
    # here (rotation-before-append, rotation-failure-never-breaks-append)
    # exercised _rotate_log_if_oversized through
    # write_session_capability_census -- a call chain that no longer
    # exists (see that function's own docstring: rotation has no caller
    # from this module any more, kept in place only for PART 3's deferred
    # deletion). _rotate_log_if_oversized itself is still fully covered,
    # directly, by the tests above and by TestRotationTOCTOUSerialization
    # below.


class TestRotationTOCTOUSerialization:
    """code-review Critical (nexus-g3jw6, fix pass, 2026-08-20): the
    TOCTOU double-rotation clobber.

    P1 stats the log oversize, rotates it into ``.1``, reopens and
    appends (recreating a small live file). P2 stat'd BEFORE P1's
    rotation (a stale, oversize observation) but calls ``os.replace``
    LATE, after P1 has already rotated and reappended -- the live path
    exists again, so P2's rename SUCCEEDS, clobbering P1's real ``.1``
    (irreplaceable history) with the small, near-empty file P1 just
    wrote.

    FIX: serialize rotators via a non-blocking advisory lock on a
    sidecar ``<name>.rotate.lock``, held across {re-stat, os.replace}.
    A rotator re-checks size UNDER THE LOCK before renaming -- a stale
    pre-lock observation is corrected by the time the rename actually
    happens. Losing the lock race (someone else is rotating right now)
    means skip entirely, not block and retry -- appends never wait on
    this. This eliminates the stale-observation rename BY CONSTRUCTION:
    the decision to rename is now made with fresh data, atomically with
    respect to every other rotator.
    """

    def test_stale_oversize_observation_does_not_clobber_a_fresher_rotation(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deterministic simulation of the interleaving: fakes ONLY the
        FIRST ``Path.stat()`` call against the log path (P2's cheap,
        pre-lock decision) as stale-oversize; every subsequent stat call
        --including the fix's re-check UNDER THE LOCK -- sees the TRUE,
        current, small size, exactly as a real re-stat after acquiring
        the lock would. Must fail against the pre-fix code (a single,
        unguarded stat+replace has no re-check to correct the stale
        observation)."""
        import nexus._session_end_census as mod

        monkeypatch.setattr(mod, "_LOG_ROTATION_MAX_BYTES", 10)
        log_path = tmp_path / "capability_census.jsonl"
        rotated = tmp_path / "capability_census.jsonl.1"

        log_path.write_text("OLD" * 50)  # 150 bytes, genuinely oversize

        # P1: a real, correct rotation -- establishes the valuable
        # history in .1.
        mod._rotate_log_if_oversized(log_path)
        assert not log_path.exists()
        assert rotated.read_text() == "OLD" * 50

        # P1 reopens + appends -- exactly what write_session_capability_census
        # does right after rotation. The live file is small again (2
        # bytes, well under the 10-byte test cap).
        log_path.write_text("x\n")
        fresh_live_content = log_path.read_text()
        assert log_path.stat().st_size < mod._LOG_ROTATION_MAX_BYTES

        # P2: simulate its stale pre-lock observation. The FIRST stat()
        # call against log_path returns a fake oversize result (999
        # bytes -- what P2 "saw" before P1 acted); every OTHER stat call
        # (on log_path or any other path) delegates to the real stat(),
        # so the fix's re-check-under-the-lock sees ground truth.
        real_stat = pathlib.Path.stat
        call_count = {"n": 0}

        class _StaleStatResult:
            st_size = 999

        def _stat_first_call_on_log_path_is_stale(self, *args, **kwargs):
            if self == log_path:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return _StaleStatResult()
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "stat", _stat_first_call_on_log_path_is_stale)

        mod._rotate_log_if_oversized(log_path)  # P2's rotation attempt

        # THE ASSERTION: P1's real, irreplaceable .1 history must
        # survive untouched. Pre-fix this fails -- P2's single unguarded
        # stat+replace clobbers .1 with the small live content.
        assert rotated.read_text() == "OLD" * 50, (
            "P2 clobbered P1's real rotated history with a stale "
            "oversize observation (the TOCTOU double-rotation bug, "
            "nexus-g3jw6)"
        )
        assert log_path.exists()
        assert log_path.read_text() == fresh_live_content
        assert call_count["n"] >= 2, (
            "the fix must re-stat AT LEAST once more under the lock -- "
            "a single stat() call cannot correct a stale observation"
        )

    def test_rotation_skips_entirely_when_lock_is_already_held(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-blocking acquire: a rotator that loses the lock race must
        skip immediately (no wait, no retry, no raise) -- someone else
        is already handling rotation."""
        import os as _os

        import nexus._session_end_census as mod
        from nexus._locking import lock_file, unlock_file

        monkeypatch.setattr(mod, "_LOG_ROTATION_MAX_BYTES", 10)
        log_path = tmp_path / "capability_census.jsonl"
        log_path.write_text("x" * 100)  # genuinely oversize

        lock_path = tmp_path / "capability_census.jsonl.rotate.lock"
        fd = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o644)
        held = _os.fdopen(fd, "r+")
        lock_file(held, blocking=True)  # simulate another process mid-rotation
        try:
            mod._rotate_log_if_oversized(log_path)  # must not raise, must not block

            assert log_path.read_text() == "x" * 100, "the contended rotator must not touch the live file"
            assert not (tmp_path / "capability_census.jsonl.1").exists()
        finally:
            unlock_file(held)
            held.close()

    def test_normal_rotation_still_works_under_the_lock(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression pin: the lock must not itself prevent an ordinary,
        uncontended rotation from happening."""
        import nexus._session_end_census as mod

        monkeypatch.setattr(mod, "_LOG_ROTATION_MAX_BYTES", 10)
        log_path = tmp_path / "capability_census.jsonl"
        log_path.write_text("x" * 100)

        mod._rotate_log_if_oversized(log_path)

        assert not log_path.exists()
        assert (tmp_path / "capability_census.jsonl.1").read_text() == "x" * 100


# nexus-gjv9b PART 1 writer swap: TestRotationFailureLogging and the
# three refire/dedup-through-write-session tests formerly here exercised
# write_session_capability_census's OLD JSONL-append call chain — gone
# now that the table's UPSERT-on-(tenant_id, session_id) semantics
# collapse re-fires server-side (see that function's own docstring).
# _is_duplicate_of_last_record itself is still directly covered below.

def test_guard_degrades_to_appending_when_the_tail_is_unreadable(tmp_path):
    """A census that cannot check must append, never drop."""
    from nexus._session_end_census import _is_duplicate_of_last_record

    assert _is_duplicate_of_last_record(tmp_path / "nope.jsonl", {"session_id": "s"}) is False
    corrupt = tmp_path / "c.jsonl"
    corrupt.write_text("{not json\n")
    assert _is_duplicate_of_last_record(corrupt, {"session_id": "s"}) is False
