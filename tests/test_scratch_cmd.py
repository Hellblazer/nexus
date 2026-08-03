"""CLI-layer tests for nx scratch commands — covers wiring gaps in commands/scratch.py."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import click
import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so T1 uses a tmp directory, not ~/.config/nexus/scratch.

    Also patches CLAUDE_SESSION_FILE to a per-test path (it's precomputed at
    import time so HOME env var alone is insufficient) and seeds a unique
    session ID so EphemeralClient fallback doesn't leak state between tests.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_SESSION_PID", raising=False)
    # Redirect the precomputed CLAUDE_SESSION_FILE to a per-test path.
    claude_session_file = tmp_path / ".config" / "nexus" / "current_session"
    claude_session_file.parent.mkdir(parents=True, exist_ok=True)
    # Write a unique session ID so each test gets isolated scratch state.
    claude_session_file.write_text(str(uuid4()))
    monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", claude_session_file)
    return tmp_path


# ── put from stdin ────────────────────────────────────────────────────────────

def test_scratch_put_from_stdin(runner: CliRunner, fake_home: Path) -> None:
    """When content is '-', scratch put reads from stdin."""
    with patch("nexus.session.os.getsid", return_value=99900):
        result = runner.invoke(main, ["scratch", "put", "-"], input="hello from stdin\n")

    assert result.exit_code == 0, result.output
    assert "Stored:" in result.output


# ── get success ──────────────────────────────────────────────────────────────




# ── get missing entry ─────────────────────────────────────────────────────────

def test_scratch_get_missing_entry_shows_error(runner: CliRunner, fake_home: Path) -> None:
    """get with non-existent ID raises ClickException (exit code 1, 'Not found')."""
    with patch("nexus.session.os.getsid", return_value=99901):
        result = runner.invoke(main, ["scratch", "get", "nonexistent-id-000"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── search no results ─────────────────────────────────────────────────────────

def test_scratch_search_no_results(runner: CliRunner, fake_home: Path) -> None:
    """search with no matches shows 'No results.' message."""
    with patch("nexus.session.os.getsid", return_value=99902):
        result = runner.invoke(main, ["scratch", "search", "nonexistent query"])

    assert result.exit_code == 0
    assert "No results." in result.output


def test_scratch_search_service_backend_result_has_no_distance_key(
    runner: CliRunner, fake_home: Path
) -> None:
    """SERVICE-backend search results carry no 'distance' key (FTS, not cosine
    similarity — HttpScratchStore/ScratchRepository never populate one). The
    CLI must not KeyError formatting a result shaped this way."""
    fake_store = MagicMock()
    fake_store.search.return_value = [
        {"id": "abc12345-full-uuid", "tags": "t1-bench", "content": "matched FTS row"},
    ]
    with patch("nexus.commands.scratch._t1", return_value=fake_store):
        result = runner.invoke(main, ["scratch", "search", "matched"])

    assert result.exit_code == 0, result.output
    assert "matched FTS row" in result.output


# ── flag nonexistent ──────────────────────────────────────────────────────────

def test_scratch_flag_nonexistent_raises(runner: CliRunner, fake_home: Path) -> None:
    """flag with bad ID raises ClickException.

    nexus-wzkzr: flag now resolves the ID through the same prefix-resolution
    helper ``get`` uses (``_resolve_entry_id``), so a clean miss surfaces its
    "not found" phrasing rather than the backend's raw KeyError text.
    """
    with patch("nexus.session.os.getsid", return_value=99903):
        result = runner.invoke(main, ["scratch", "flag", "bad-id-000"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── flag/unflag prefix resolution (nexus-wzkzr) ────────────────────────────────
#
# Before this fix, flag/unflag posted the raw CLI argument straight to the
# backend, which has no prefix fallback of its own (unlike get's
# _resolve_entry_id path) — the 8-char id 'scratch list' prints could never
# drive flag/unflag. These pin the fix at the CLI boundary.


def test_scratch_flag_resolves_id_prefix(runner: CliRunner, fake_home: Path) -> None:
    """flag accepts the 8-char prefix 'scratch list' displays, not just the
    full UUID obtainable only from the one-time 'Stored:' line."""
    with patch("nexus.session.os.getsid", return_value=99908):
        put_result = runner.invoke(main, ["scratch", "put", "flag me by prefix"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]
        prefix = doc_id[:8]

        flag_result = runner.invoke(main, ["scratch", "flag", prefix])
        assert flag_result.exit_code == 0, flag_result.output
        assert f"Flagged: {doc_id}" in flag_result.output

        list_result = runner.invoke(main, ["scratch", "list"])
        assert f"[{prefix}]" in list_result.output
        assert "flagged=True" in list_result.output


def test_scratch_unflag_resolves_id_prefix(runner: CliRunner, fake_home: Path) -> None:
    """unflag accepts the same 8-char prefix as flag/get/delete."""
    with patch("nexus.session.os.getsid", return_value=99909):
        put_result = runner.invoke(main, ["scratch", "put", "unflag me by prefix"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]
        prefix = doc_id[:8]

        flag_result = runner.invoke(main, ["scratch", "flag", doc_id])
        assert flag_result.exit_code == 0, flag_result.output

        unflag_result = runner.invoke(main, ["scratch", "unflag", prefix])
        assert unflag_result.exit_code == 0, unflag_result.output
        assert f"Unflagged: {doc_id}" in unflag_result.output

        list_result = runner.invoke(main, ["scratch", "list"])
        assert "flagged=False" in list_result.output


def test_scratch_flag_ambiguous_prefix_errors_cleanly(runner: CliRunner) -> None:
    """A prefix matching more than one entry must error, not guess.

    Uses a fake store (mirrors ``test_scratch_search_service_backend_...``)
    so the collision is deterministic rather than depending on random UUID
    generation happening to share a prefix.
    """
    fake_store = MagicMock()
    fake_store.list_entries.return_value = [
        {"id": "aaaa1111-full-uuid-one", "tags": "", "content": "one", "flagged": False},
        {"id": "aaaa2222-full-uuid-two", "tags": "", "content": "two", "flagged": False},
    ]
    with patch("nexus.commands.scratch._t1", return_value=fake_store):
        result = runner.invoke(main, ["scratch", "flag", "aaaa"])

    assert result.exit_code != 0
    assert "ambiguous" in result.output.lower()
    fake_store.flag.assert_not_called()


def test_scratch_flag_full_uuid_still_works(runner: CliRunner, fake_home: Path) -> None:
    """The full UUID (not just a prefix) must keep working for flag."""
    with patch("nexus.session.os.getsid", return_value=99911):
        put_result = runner.invoke(main, ["scratch", "put", "flag by full id"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]

        flag_result = runner.invoke(main, ["scratch", "flag", doc_id])
        assert flag_result.exit_code == 0, flag_result.output
        assert f"Flagged: {doc_id}" in flag_result.output


# ── unflag success ────────────────────────────────────────────────────────────

def test_scratch_unflag_success(runner: CliRunner, fake_home: Path) -> None:
    """unflag command works on a previously-flagged entry."""
    with patch("nexus.session.os.getsid", return_value=99904):
        # Put an entry, capture the ID
        put_result = runner.invoke(main, ["scratch", "put", "will be flagged"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]

        # Flag it
        flag_result = runner.invoke(main, ["scratch", "flag", doc_id])
        assert flag_result.exit_code == 0, flag_result.output
        assert "Flagged:" in flag_result.output

        # Unflag it
        unflag_result = runner.invoke(main, ["scratch", "unflag", doc_id])
        assert unflag_result.exit_code == 0, unflag_result.output
        assert "Unflagged:" in unflag_result.output


# ── unflag nonexistent ────────────────────────────────────────────────────────

def test_scratch_unflag_nonexistent_raises(runner: CliRunner, fake_home: Path) -> None:
    """unflag with bad ID raises ClickException.

    nexus-wzkzr: see ``test_scratch_flag_nonexistent_raises`` — same
    resolve-then-not-found phrasing shift.
    """
    with patch("nexus.session.os.getsid", return_value=99905):
        result = runner.invoke(main, ["scratch", "unflag", "bad-id-000"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# ── promote success ───────────────────────────────────────────────────────────

def test_scratch_promote_success(runner: CliRunner, fake_home: Path) -> None:
    """promote command copies T1 entry to T2 successfully."""
    with patch("nexus.session.os.getsid", return_value=99906):
        # Put an entry
        put_result = runner.invoke(main, ["scratch", "put", "promote me"])
        assert put_result.exit_code == 0, put_result.output
        doc_id = put_result.output.strip().split("Stored: ")[1]

        # Promote it to T2
        promote_result = runner.invoke(
            main, ["scratch", "promote", doc_id, "-p", "testproj", "-t", "notes.md"]
        )
        assert promote_result.exit_code == 0, promote_result.output
        assert "Promoted" in promote_result.output
        assert "testproj/notes.md" in promote_result.output


# ── promote nonexistent ──────────────────────────────────────────────────────

def test_scratch_promote_nonexistent_raises(runner: CliRunner, fake_home: Path) -> None:
    """promote with bad ID raises ClickException."""
    with patch("nexus.session.os.getsid", return_value=99907):
        result = runner.invoke(
            main, ["scratch", "promote", "bad-id-000", "-p", "proj", "-t", "t.md"]
        )

    assert result.exit_code != 0
    assert "No scratch entry" in result.output


# ── nexus-st9u: nx scratch delete ────────────────────────────────────────────




def test_scratch_delete_not_found(runner: CliRunner, fake_home: Path) -> None:
    """delete with an unknown ID prefix exits non-zero."""
    with patch("nexus.session.os.getsid", return_value=99941):
        result = runner.invoke(main, ["scratch", "delete", "00000000"])
        assert result.exit_code != 0
        assert "not found" in result.output


# ── nexus-s6e55: nx scratch clear is confirm-gated ──────────────────────────
#
# Every sibling destructive verb (store delete, memory delete, the catalog
# gc family) prompts + previews before destroying data; clear previously
# wiped the whole session's T1 unconditionally with zero guard — a stray
# invocation mid-orchestration destroys every sibling agent's findings and
# any entries flagged for SessionEnd flush to T2.


def test_scratch_clear_bare_invocation_prompts_and_aborts(
    runner: CliRunner, fake_home: Path
) -> None:
    """Without -y, a declined confirmation aborts (rc != 0) and leaves
    entries intact."""
    with patch("nexus.session.os.getsid", return_value=99950):
        put_result = runner.invoke(main, ["scratch", "put", "must survive"])
        assert put_result.exit_code == 0, put_result.output

        clear_result = runner.invoke(main, ["scratch", "clear"], input="n\n")
        assert clear_result.exit_code != 0
        assert "Found 1 entry" in clear_result.output

        list_result = runner.invoke(main, ["scratch", "list"])
        assert "must survive" in list_result.output


def test_scratch_clear_yes_flag_clears_without_prompting(
    runner: CliRunner, fake_home: Path
) -> None:
    """-y / --yes skips the prompt and clears immediately."""
    with patch("nexus.session.os.getsid", return_value=99951):
        put_result = runner.invoke(main, ["scratch", "put", "will be cleared"])
        assert put_result.exit_code == 0, put_result.output

        clear_result = runner.invoke(main, ["scratch", "clear", "--yes"])
        assert clear_result.exit_code == 0, clear_result.output
        assert "Cleared 1 entry" in clear_result.output

        list_result = runner.invoke(main, ["scratch", "list"])
        assert "will be cleared" not in list_result.output


def test_scratch_clear_preview_counts_flagged_entries(
    runner: CliRunner, fake_home: Path
) -> None:
    """The preview names how many entries are flagged for SessionEnd flush
    to T2 — the wave-2 escalation: clear silently destroyed flush-flagged
    entries with no callout."""
    with patch("nexus.session.os.getsid", return_value=99952):
        plain = runner.invoke(main, ["scratch", "put", "plain entry"])
        flagged = runner.invoke(main, ["scratch", "put", "flagged entry"])
        assert plain.exit_code == 0 and flagged.exit_code == 0
        flagged_id = flagged.output.strip().split("Stored: ")[1]

        flag_result = runner.invoke(main, ["scratch", "flag", flagged_id])
        assert flag_result.exit_code == 0, flag_result.output

        clear_result = runner.invoke(main, ["scratch", "clear"], input="n\n")
        assert clear_result.exit_code != 0
        assert "Found 2 entries" in clear_result.output
        assert "1 flagged for T2 flush" in clear_result.output

        list_result = runner.invoke(main, ["scratch", "list"])
        assert "plain entry" in list_result.output
        assert "flagged entry" in list_result.output


def test_scratch_clear_no_entries_short_circuits(
    runner: CliRunner, fake_home: Path
) -> None:
    """Nothing to clear: no prompt, clean exit."""
    with patch("nexus.session.os.getsid", return_value=99953):
        result = runner.invoke(main, ["scratch", "clear"])
        assert result.exit_code == 0, result.output
        assert "No scratch entries." in result.output



# ── friendly error when T1 is unresolvable (nexus-gff3g) ─────────────────────

def test_scratch_list_friendly_error_when_t1_unresolvable(
    runner: CliRunner,
) -> None:
    """nx scratch must print a clean hint, not a raw traceback, when the
    session-id diverges from the MCP's lease key and no T1 lease resolves.

    Pre-fix the T1ServerNotFoundError propagated unhandled and click dumped
    the full Python stack; the user got a wall of traceback where a one-line
    fix belongs.
    """
    from nexus.db.t1 import T1ServerNotFoundError

    with patch(
        "nexus.commands.scratch.get_t1_database",
        side_effect=T1ServerNotFoundError("T1 not configured for this process."),
    ):
        result = runner.invoke(main, ["scratch", "list"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "NX_T1_ISOLATED=1" in result.output
    assert "T1 not configured" in result.output


def test_scratch_put_service_mode_no_endpoint_clean_error(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nexus-0l5ym: in service mode (NX_STORAGE_BACKEND=service) with no
    reachable nexus-service, `nx scratch put` must give a clean ClickException
    (exit 1, no raw RuntimeError traceback) with NX_T1_ISOLATED guidance."""
    monkeypatch.setenv("NX_STORAGE_BACKEND", "service")
    for var in ("NX_SERVICE_URL", "NX_SERVICE_PORT", "NX_SERVICE_TOKEN",
                # The suite-wide autouse fixture sets NX_T1_ISOLATED=1; since
                # the nexus-h8rf6 fix, isolation WINS over service routing, so
                # this service-path test must clear it too.
                "NX_T1_HOST", "NX_T1_PORT", "NX_T1_ISOLATED"):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(main, ["scratch", "put", "hello"])

    assert result.exit_code == 1
    # The fix converts HttpScratchStore's raw RuntimeError into a clean
    # ClickException — no RuntimeError should escape to the CLI runner.
    assert not isinstance(result.exception, RuntimeError), result.exception
    assert "NX_T1_ISOLATED" in result.output
