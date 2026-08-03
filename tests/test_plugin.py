"""AC2–AC6: session hooks, memory summary, doctor checks."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME and XDG paths to tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── AC2: SessionStart — UUID4 session ID ──────────────────────────────────────

def test_session_start_writes_session_file(runner: CliRunner, fake_home: Path) -> None:
    """nx hook session-start calls write_claude_session_id with a UUID4 session ID."""
    import re
    captured: dict[str, str] = {}

    original_write = __import__("nexus.session", fromlist=["write_claude_session_id"]).write_claude_session_id

    def _capture(session_id: str) -> None:
        captured["session_id"] = session_id
        original_write(session_id)

    with patch("nexus.hooks.write_claude_session_id", side_effect=_capture):
        result = runner.invoke(main, ["hook", "session-start"])

    assert result.exit_code == 0, result.output
    assert "session_id" in captured, "write_claude_session_id was not called"
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        captured["session_id"],
    ), f"Not a UUID4: {captured['session_id']!r}"


def test_session_start_prints_ready_message(runner: CliRunner, fake_home: Path) -> None:
    """nx hook session-start prints 'Nexus ready' with session ID."""
    result = runner.invoke(main, ["hook", "session-start"])
    assert "Nexus ready" in result.output
    assert "session" in result.output.lower()


# ── Behavior 4: hook and CLI use the same getsid(0) anchor ───────────────────

def test_hook_and_cli_use_same_getsid_anchor(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """session_start() passes a UUID4 to write_claude_session_id."""
    import re
    from nexus.hooks import session_start

    written: dict[str, str] = {}

    def _capture(session_id: str) -> None:
        written["session_id"] = session_id

    with patch("nexus.hooks.write_claude_session_id", side_effect=_capture):
        session_start()

    assert "session_id" in written
    assert re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        written["session_id"],
    ), f"Recovered session ID is not a UUID4: {written['session_id']!r}"


# ── AC3: SessionStart memory summary ─────────────────────────────────────────

def test_session_start_outputs_session_id(
    runner: CliRunner, fake_home: Path
) -> None:
    """SessionStart outputs session ID (T2 memory surfaced by separate hook)."""
    result = runner.invoke(main, ["hook", "session-start"])

    assert "Nexus ready" in result.output


# ── GH #576 Phase F: subprocess SessionStart skip-sweep ─────────────────────








# ── AC5: SessionEnd flush + expire ────────────────────────────────────────────







def test_session_end_runs_expire(runner: CliRunner, fake_home: Path) -> None:
    """SessionEnd routes its T2 flush + expire through the daemon
    (mcp_infra.t2_index_write) and still runs the TTL expire sweep
    (RDR-128 P3 — the flush no longer opens memory.db directly)."""
    mock_t2 = MagicMock()
    mock_t2.expire.return_value = 3
    mock_t2.memory.put.return_value = 1

    def _run(write_fn):
        # Stand in for the daemon route: run the write_fn against the mock.
        return write_fn(mock_t2)

    with patch("nexus.hooks._open_t1", return_value=MagicMock(flagged_entries=lambda: [])):
        with patch("nexus.mcp_infra.t2_index_write", _run):
            result = runner.invoke(main, ["hook", "session-end"])

    mock_t2.expire.assert_called_once()


# ── AC6: nx doctor ────────────────────────────────────────────────────────────

def test_doctor_shows_all_checks(runner: CliRunner, fake_home: Path) -> None:
    """nx doctor runs all required service checks and reports status."""
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code in (0, 1), result.output
    output_lower = result.output.lower()
    assert "t3 mode" in output_lower
    assert "ripgrep" in output_lower or "rg" in output_lower
    assert "git" in output_lower


def test_doctor_missing_voyage_key_reports_warning(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nx doctor REPORTS an unset VOYAGE_API_KEY without failing on it.

    nexus-aqbrk: this asserted ``exit_code == 1`` and was GREEN ON THE SQLITE
    ARM FOR AN UNRELATED REASON. Probed on both arms: sqlite exits 1 because
    of "✗ Vector service (/v1/vectors): not reachable" — nothing to do with
    the credential — while on the engine arm that service IS reachable, the
    check passes, doctor exits 0, and the assertion failed. The docstring's
    claim had been false since the credential rows became informational; a
    different failing check was masking it.

    IT ALSO CONTRADICTED tests/test_doctor_cmd.py::test_doctor_missing_
    credentials_informational, which asserts in so many words that "absent
    creds are never a failing/fatal doctor result (the exit-1 false-positive
    on migrated installs)" per RDR-155 P4a.2 / RDR-188. The suite held both
    expectations at once and only the accident above kept them from colliding.

    Now asserts what the product actually contracts: the row is REPORTED, and
    it is NOT fatal.
    """
    monkeypatch.setenv("NX_LOCAL", "0")  # force cloud mode
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    result = runner.invoke(main, ["doctor"])
    assert "VOYAGE_API_KEY" in result.output or "voyage" in result.output.lower()
    # The credential line is informational (✓), never the fatal ✗ shape.
    voyage_lines = [
        ln for ln in result.output.splitlines() if "VOYAGE_API_KEY" in ln
    ]
    assert voyage_lines, result.output
    assert not any("\u2717" in ln for ln in voyage_lines), (
        f"an absent Voyage key must not render as a FAILED check: {voyage_lines}"
    )


def test_doctor_missing_chroma_key_reports_warning(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CHROMA_API_KEY row is GONE — retired with the migration machinery.

    nexus-aqbrk: this asserted ``exit_code == 1`` (same coincidental green as
    the Voyage test above) AND that CHROMA_API_KEY appears in the output. The
    second claim is now false by design: tests/test_doctor_cmd.py
    ::test_doctor_missing_credentials_informational asserts the opposite —
    ``"CHROMA_API_KEY" not in result.output  # row deleted at P4b`` — because
    the CHROMA_* credential rows died with the migration machinery
    (nexus-nmw3i / c7aj3, RDR-155 P4b).

    The old form survived only because of its ``or "chroma" in output.lower()``
    escape hatch, which any unrelated mention of chroma satisfies. Re-pointed
    at the surviving invariant so this file agrees with test_doctor_cmd instead
    of contradicting it.
    """
    monkeypatch.setenv("NX_LOCAL", "0")  # force cloud mode
    monkeypatch.delenv("CHROMA_API_KEY", raising=False)
    result = runner.invoke(main, ["doctor"])
    assert "CHROMA_API_KEY" not in result.output, (
        "the CHROMA_API_KEY credential row was retired at RDR-155 P4b; its "
        "reappearance means the migration machinery is back"
    )


def test_doctor_ripgrep_present(runner: CliRunner, fake_home: Path) -> None:
    """nx doctor checks for ripgrep on PATH."""
    with (
        patch("nexus.health.shutil.which", return_value="/usr/bin/rg"),
        # nexus-l2ku5 round 2 (CRITICAL, code-review): the broad `which`
        # fake above now also routes the REAL MCP entry-point handshake at
        # /usr/bin/rg, a real spawn attempt against a non-MCP binary that
        # silently flips doctor's exit code 0 -> 1 under this test's weak
        # assertion. Stub the probe — real handshake behavior belongs to
        # tests/test_health_mcp_entrypoints.py.
        patch("nexus.health._probe_mcp_server", return_value=(True, "stubbed")),
    ):
        result = runner.invoke(main, ["doctor"])
    assert "rg" in result.output or "ripgrep" in result.output.lower()
