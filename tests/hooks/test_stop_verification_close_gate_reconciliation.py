# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The close-gate reconciliation backstop (nexus-dgl8g).

Sam's DECIDED note (``bd show nexus-dgl8g``): an ADVISORY check inside the
existing Stop verification hook, never blocking, that lists beads moved to
``closed`` during THIS session with no ``review-completed`` marker naming
both reviewers in this session's T1 scratch, and reports a named
undeclared-close count.

Follow-up from nexus-2b24o: the PreToolUse close gate matches enumerated
close spellings in the command TEXT, so a close whose content is off the
command line entirely (``bd batch -f``, ``bd import <file>``, a
dynamically-built ``bd sql``) is structurally invisible to it. This is the
detective backstop for exactly that gap -- it reads bd's own record of
what closed, not the command that closed it.

These are in-process module tests (mirrors ``test_pre_close_verification_module.py``'s
``_coverage`` monkeypatch pattern) rather than a real T1/bd stack: bd is a
real fake binary on ``PATH`` (this module's own pattern, matching
``test_stop_verification_module.py``'s unused ``_fake_bin`` helper), and
T1 coverage is monkeypatched at ``pre_close_verification._coverage`` --
the exact reader this bead's own instructions say to reuse rather than
reimplement, so faking its OUTPUT (not the T1 scan underneath it) is the
right seam.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.hooks import stop_verification as hook


# --- fixtures ---------------------------------------------------------------


def _fake_bd(tmp_path: Path, closed_json: str, *, in_progress_output: str = "") -> Path:
    """A one-file ``bd`` on PATH: ``--json`` gets *closed_json*, anything
    else (e.g. ``_beads_in_progress``'s own ``bd list --status=in_progress``
    call, which some ``run()``-level tests also trigger) gets
    *in_progress_output* and exit 0.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    p = bin_dir / "bd"
    p.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *--json*) cat <<'EOF'\n"
        f"{closed_json}\n"
        "EOF\n"
        "  ;;\n"
        f"  *) printf '%s' '{in_progress_output}' ;;\n"
        "esac\n"
    )
    p.chmod(0o755)
    return bin_dir


def _fake_bd_absent(tmp_path: Path) -> Path:
    """An empty bin dir: ``shutil.which('bd')`` finds nothing on it."""
    bin_dir = tmp_path / "emptybin"
    bin_dir.mkdir(exist_ok=True)
    return bin_dir


def _write_transcript(tmp_path: Path, session_start_iso: str, *, name: str = "transcript.jsonl") -> str:
    """A minimal transcript: a leading line with NO timestamp (matches a
    real session's first ``{"type":"mode",...}`` row), then one that
    carries the session's start.
    """
    path = tmp_path / name
    lines = [
        json.dumps({"type": "mode", "mode": "normal", "sessionId": "s1"}),
        json.dumps({"type": "file-history-snapshot", "timestamp": session_start_iso}),
    ]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _bd_row(bead_id: str, closed_at: str) -> dict:
    return {"id": bead_id, "status": "closed", "closed_at": closed_at}


#: The bare minimum PATH a fake ``bd`` script's own ``cat``/heredoc still
#: needs to run -- real ``bd`` lives at ``/opt/homebrew/bin/bd`` on this
#: box, well outside both entries, so appending them never leaks it in.
_MINIMAL_SHELL_PATH = "/bin:/usr/bin"


@pytest.fixture
def isolated_path(monkeypatch):
    """Every test in this module controls PATH explicitly -- a stray real
    ``bd``/``nx`` reachable from the ambient dev-box PATH must never leak
    into a "bd unavailable" or "no beads closed" assertion. The fake
    ``bd`` dir is prepended (not a full replacement) so its own script
    body can still find ``sh``'s external commands (``cat``, for its
    heredoc) -- a bare fakebin-only PATH makes the fake script itself
    fail with "command not found", which reads identically to "bd
    unavailable" and silently hid every non-trivial case here.
    """
    def _set(bin_dir: Path) -> None:
        monkeypatch.setenv("PATH", f"{bin_dir}:{_MINIMAL_SHELL_PATH}")
    return _set


# --- _session_start_dt --------------------------------------------------


class TestSessionStartDt:
    def test_reads_the_first_timestamped_line(self, tmp_path):
        transcript = _write_transcript(tmp_path, "2026-09-24T10:00:00.000Z")
        dt = hook._session_start_dt(transcript)
        assert dt is not None
        assert dt.year == 2026 and dt.month == 9 and dt.day == 24
        assert dt.hour == 10

    def test_none_for_empty_path(self):
        assert hook._session_start_dt("") is None

    def test_none_for_missing_file(self, tmp_path):
        assert hook._session_start_dt(str(tmp_path / "nope.jsonl")) is None

    def test_none_when_no_line_carries_a_timestamp(self, tmp_path):
        path = tmp_path / "notimestamp.jsonl"
        path.write_text('{"type": "mode", "mode": "normal"}\n{"type": "other"}\n')
        assert hook._session_start_dt(str(path)) is None

    def test_junk_lines_are_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "junk.jsonl"
        path.write_text(
            "not json at all\n"
            '["a", "list", "not", "a", "dict"]\n'
            + json.dumps({"timestamp": "2026-09-24T11:30:00Z"}) + "\n"
        )
        dt = hook._session_start_dt(str(path))
        assert dt is not None
        assert dt.hour == 11


# --- _bd_closed_since -----------------------------------------------------


class TestBdClosedSince:
    def test_filters_to_the_session_window(self, tmp_path, isolated_path):
        """A bead closed BEFORE the session started is ignored; one closed
        after is reported. Covers the bead's "close before the session
        window (ignored)" scenario at the reader level."""
        rows = json.dumps([
            _bd_row("nexus-before", "2026-09-24T09:00:00Z"),
            _bd_row("nexus-after", "2026-09-24T10:30:00Z"),
        ])
        isolated_path(_fake_bd(tmp_path, rows))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        ids = hook._bd_closed_since(session_start)
        assert ids == ["nexus-after"]

    def test_none_when_bd_absent(self, tmp_path, isolated_path):
        isolated_path(_fake_bd_absent(tmp_path))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        assert hook._bd_closed_since(session_start) is None

    def test_none_on_unparseable_output(self, tmp_path, isolated_path):
        isolated_path(_fake_bd(tmp_path, "not json"))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        assert hook._bd_closed_since(session_start) is None

    def test_empty_list_when_bd_answers_with_nothing_closed(self, tmp_path, isolated_path):
        isolated_path(_fake_bd(tmp_path, "[]"))
        session_start = hook._exp._parse_iso("2026-09-24T10:00:00Z")
        assert hook._bd_closed_since(session_start) == []


# --- _undeclared_close_warning (the integrated backstop) ------------------


class TestUndeclaredCloseWarning:
    """Each test corresponds to one of the bead's named coverage
    scenarios."""

    def _payload(self, tmp_path, session_start_iso: str = "2026-09-24T10:00:00Z") -> dict:
        return {
            "session_id": "s1",
            "transcript_path": _write_transcript(tmp_path, session_start_iso),
        }

    def test_no_session_id_is_silent(self, tmp_path):
        assert hook._undeclared_close_warning({"transcript_path": "irrelevant"}) == ""

    def test_no_transcript_path_is_silent(self):
        """No window to check against is not evidence of anything amiss --
        matches this file's other missing-signal branches."""
        assert hook._undeclared_close_warning({"session_id": "s1"}) == ""

    def test_guard_mode_off_disables_the_whole_check(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", "off")
        payload = self._payload(tmp_path)
        assert hook._undeclared_close_warning(payload) == ""

    def test_bd_unavailable_reports_cannot_check(self, tmp_path, isolated_path):
        isolated_path(_fake_bd_absent(tmp_path))
        payload = self._payload(tmp_path)
        warning = hook._undeclared_close_warning(payload)
        assert "could not check" in warning
        assert "WARNING" in warning

    def test_no_beads_closed_in_window_is_silent(self, tmp_path, isolated_path):
        isolated_path(_fake_bd(tmp_path, "[]"))
        payload = self._payload(tmp_path)
        assert hook._undeclared_close_warning(payload) == ""

    def test_close_before_session_window_is_ignored_end_to_end(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-old", "2026-09-24T09:00:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        # If this were consulted it would report "missing" -- proving the
        # window filter, not the coverage check, is what kept it out.
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {b: "missing" for b in ids}},
        )
        payload = self._payload(tmp_path)
        assert hook._undeclared_close_warning(payload) == ""

    def test_covered_bead_with_both_reviewer_marker_is_not_reported(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-good", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-good": "covered"}},
        )
        payload = self._payload(tmp_path)
        assert hook._undeclared_close_warning(payload) == ""

    def test_bead_with_no_marker_is_reported_by_id(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-bad": "missing"}},
        )
        payload = self._payload(tmp_path)
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-bad" in warning
        assert "1 bead" in warning

    def test_partial_marker_incomplete_is_reported_as_undeclared(self, tmp_path, isolated_path, monkeypatch):
        """nexus-e3mak: a marker naming only one reviewer does not count as
        coverage -- ``_coverage`` already encodes this as ``incomplete``,
        and this backstop must not treat it as clean."""
        rows = json.dumps([_bd_row("nexus-half", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-half": "incomplete"}},
        )
        payload = self._payload(tmp_path)
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-half" in warning

    def test_override_close_states_the_limitation_rather_than_hiding_it(self, tmp_path, isolated_path, monkeypatch):
        """An evidence-only override close (NX_REVIEW_GATE_OVERRIDE=1) is
        legitimate and LOOKS IDENTICAL to a genuinely undeclared one here --
        neither bd nor T1 records the override was used. Per the bead: say
        so in the line rather than silently excluding it (which would read
        as "checked and clean")."""
        rows = json.dumps([_bd_row("nexus-override", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-override": "missing"}},
        )
        payload = self._payload(tmp_path)
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-override" in warning
        assert "NX_REVIEW_GATE_OVERRIDE" in warning
        assert "cannot be told apart" in warning

    def test_t1_unreachable_reports_cannot_check_not_undeclared(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-unk", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": False, "status": {"nexus-unk": "uncertain"}},
        )
        payload = self._payload(tmp_path)
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-unk" in warning
        assert "could not verify" in warning
        assert "no review-completed marker naming both reviewers" not in warning

    def test_deadline_status_reported_as_cannot_check_not_undeclared(self, tmp_path, isolated_path, monkeypatch):
        rows = json.dumps([_bd_row("nexus-slow", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-slow": "deadline"}},
        )
        payload = self._payload(tmp_path)
        warning = hook._undeclared_close_warning(payload)
        assert "nexus-slow" in warning
        assert "could not verify" in warning
        assert "no review-completed marker naming both reviewers" not in warning

    def test_passes_session_id_through_to_coverage_reader(self, tmp_path, isolated_path, monkeypatch):
        """Reuses ``pre_close_verification._coverage``'s reader against
        THIS session's own T1 scope, not whatever the module-level
        ``_SESSION_ID`` slot happens to hold."""
        rows = json.dumps([_bd_row("nexus-x", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        seen = {}

        def _fake_coverage(ids, session_id=""):
            seen["session_id"] = session_id
            return {"t1_reachable": True, "status": {b: "covered" for b in ids}}

        monkeypatch.setattr("nexus.hooks.pre_close_verification._coverage", _fake_coverage)
        payload = self._payload(tmp_path)
        hook._undeclared_close_warning(payload)
        assert seen["session_id"] == "s1"


# --- run() integration: still only ever approves ---------------------------


class TestRunIntegration:
    def test_run_includes_the_backstop_warning_in_the_reason(self, tmp_path, isolated_path, monkeypatch):
        # Isolate the RDR-184 ledger this run() also reads (via
        # _reconcile_warning) from any real ~/.local/state/nexus ledger a
        # session id of "s1" might collide with.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-bad": "missing"}},
        )
        # on_stop stays default (unset -> not True), so only the RDR-184-
        # family warnings (reconcile + this backstop) can appear.
        monkeypatch.setattr(hook, "_read_config", lambda: {})
        payload = {
            "session_id": "s1",
            "transcript_path": _write_transcript(tmp_path, "2026-09-24T10:00:00Z"),
        }
        result = hook.run(payload)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert "nexus-bad" in parsed.get("reason", "")

    def test_run_never_denies_even_with_undeclared_closes(self, tmp_path, isolated_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
        rows = json.dumps([_bd_row("nexus-bad", "2026-09-24T10:30:00Z")])
        isolated_path(_fake_bd(tmp_path, rows))
        monkeypatch.setattr(
            "nexus.hooks.pre_close_verification._coverage",
            lambda ids, session_id="": {"t1_reachable": True, "status": {"nexus-bad": "missing"}},
        )
        monkeypatch.setattr(hook, "_read_config", lambda: {"on_stop": True})
        payload = {
            "session_id": "s1",
            "transcript_path": _write_transcript(tmp_path, "2026-09-24T10:00:00Z"),
        }
        result = hook.run(payload)
        parsed = json.loads(result.stdout)
        assert parsed["decision"] == "approve"
        assert result.exit_code == 0
