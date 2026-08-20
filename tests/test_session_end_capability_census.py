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
    def test_appends_record_to_durable_jsonl(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VERIFICATION 1 continued: the record is FOUND IN THE DURABLE
        LOG afterwards -- the log assertion is the real one."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))
        sid = "sess-durable-log"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        from nexus._session_end_census import capability_census_log_path, write_session_capability_census

        record = write_session_capability_census(sid)

        assert record is not None
        assert record["session_id"] == sid
        assert record["capabilities"]["skill"] == 0

        log_path = capability_census_log_path()
        assert log_path.exists()
        lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert any(r["session_id"] == sid and r["capabilities"]["skill"] == 0 for r in lines)

    def test_appends_record_via_a_single_write_call(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """code-review Important #2 (fix pass, 2026-08-20): concurrent
        SessionEnd appenders from multiple Claude Code sessions must not
        interleave partial lines. The codebase's own precedent for this
        exact problem (``conexus/hooks/scripts/routing/_lib.py::log_routing_event``)
        writes the whole ``json.dumps(...) + \"\\n\"`` string in ONE
        ``fh.write()`` call -- two separate ``write()`` calls (payload,
        then newline) rely on CPython's buffering happening to coalesce
        them into one OS write, which is an implementation detail, not a
        guarantee."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))
        sid = "sess-single-write"
        _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])

        writes: list[str] = []

        class _RecordingFile:
            def __enter__(self) -> "_RecordingFile":
                return self

            def __exit__(self, *exc_info: object) -> bool:
                return False

            def write(self, data: str) -> int:
                writes.append(data)
                return len(data)

        original_open = pathlib.Path.open

        def fake_open(self: pathlib.Path, *args: object, **kwargs: object):  # noqa: ANN001, ANN002, ANN003
            if self.name == "capability_census.jsonl":
                return _RecordingFile()
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "open", fake_open)

        from nexus._session_end_census import write_session_capability_census

        write_session_capability_census(sid)

        assert len(writes) == 1, f"expected exactly one fh.write() call, got {len(writes)}: {writes!r}"
        assert writes[0].endswith("\n")
        # The single write is itself one complete, parseable JSON line --
        # exactly the shape a concurrent reader/appender must see, never
        # a half-written payload with the newline pending in a second call.
        json.loads(writes[0])

    def test_appends_multiple_sessions_without_clobbering(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))

        for sid in ("sess-a", "sess-b"):
            _write_transcript(project_dir / f"{sid}.jsonl", [_tool_use_record("Bash")])
            write_result = __import__(
                "nexus._session_end_census", fromlist=["write_session_capability_census"],
            ).write_session_capability_census(sid)
            assert write_result is not None

        from nexus._session_end_census import capability_census_log_path

        lines = [
            json.loads(line)
            for line in capability_census_log_path().read_text().splitlines()
            if line.strip()
        ]
        assert {r["session_id"] for r in lines} == {"sess-a", "sess-b"}

    def test_no_session_id_resolvable_is_a_silent_noop(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        import nexus.session
        monkeypatch.setattr(nexus.session, "read_claude_session_id", lambda: None)

        from nexus._session_end_census import capability_census_log_path, write_session_capability_census

        result = write_session_capability_census()

        assert result is None
        assert not capability_census_log_path().exists()

    def test_blindspot_record_is_appended_too(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A BLINDSPOT session still gets written -- it is a real, durable
        record of the failure to measure, not dropped."""
        cfg_dir = tmp_path / "cfgdir"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.setenv("NX_CENSUS_PROJECT_DIR", str(project_dir))

        from nexus._session_end_census import capability_census_log_path, write_session_capability_census

        record = write_session_capability_census("sess-blindspot-durable")

        assert record is not None
        assert record["blindspot"] is True
        lines = [
            json.loads(line)
            for line in capability_census_log_path().read_text().splitlines()
            if line.strip()
        ]
        assert any(r.get("blindspot") is True for r in lines)
