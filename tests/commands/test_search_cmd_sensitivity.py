# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx search`` output posture for sensitive hits (RDR-243 Phase 3 Step 3.2, nexus-rfvyy.8.3).

Q2: output read through Claude Code's Bash tool reaches the session model, so under Claude Code
(``nexus.session.started_by_claude_code``) a sensitive hit is replaced by one stub line and counted, in every
output format; in a terminal the hit prints with a ``[sensitive]`` badge. ``--json`` carries a ``sensitive``
bool on every item either way.

The hits come from a patched ``search_cross_corpus`` (the engine's per-hit ``sensitive`` and the catalog's
``sensitive_owners`` arrive as result metadata); the canary text must never reach stdout when withheld.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.sensitive_output import BADGE, stub_line, withheld_note
from nexus.types import SearchResult
from tests.conftest import patched_mcp_infra_t3
from tests.test_search_cmd import _CLOUD_ENV, _LOAD_CFG, _mock_t3


CANARY = "ZEBRA-CANARY-7731 the secret body"
SECRET_TITLE = "Secret Merger Memo"
SECRET_PATH = "private/merger.md"
PLAIN_BODY = "an ordinary public paragraph"
OWNER = "1.2.3"


def _hits() -> list[SearchResult]:
    sensitive = SearchResult(
        id="s1", content=CANARY, distance=0.1, collection="knowledge__test",
        metadata={
            "sensitive": True, "sensitive_owners": [OWNER, "1.10.1"],
            "_display_path": SECRET_PATH, "title": SECRET_TITLE, "line_start": 1,
        },
    )
    plain = SearchResult(
        id="p1", content=PLAIN_BODY, distance=0.2, collection="knowledge__test",
        metadata={"sensitive": False, "_display_path": "public/notes.md", "title": "Notes", "line_start": 1},
    )
    return [sensitive, plain]


class _NoStorageT2:
    """Stands in for ``T2Database`` around the patched ``search_cross_corpus``, which never reads the taxonomy or
    the telemetry store it is handed. What these tests pin is how a result is printed, so no tenant is minted."""

    def __init__(self, *_a, **_kw) -> None:
        self.taxonomy = self.telemetry = None

    def __enter__(self) -> "_NoStorageT2":
        return self

    def __exit__(self, *_exc) -> None:
        return None


@pytest.fixture
def run_search(monkeypatch: pytest.MonkeyPatch):
    for k, v in _CLOUD_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)

    def _run(*args: str, hits: list[SearchResult] | None = None):
        mock = _mock_t3()
        pool = _hits() if hits is None else hits
        with patch("nexus.commands.search_cmd._t3", return_value=mock), patched_mcp_infra_t3(mock), \
             patch("nexus.commands.search_cmd.search_cross_corpus", return_value=pool), \
             patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG), \
             patch("nexus.db.t2.T2Database", _NoStorageT2):
            return CliRunner().invoke(main, ["search", "query", "--corpus", "knowledge", *args])

    return _run


def _assert_nothing_leaks(out: str) -> None:
    for secret in (CANARY, SECRET_TITLE, SECRET_PATH, "secret body"):
        assert secret not in out, f"{secret!r} reached stdout:\n{out}"


# --- Under Claude Code: the stub line and the count ---------------------------------------------------------


@pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"])
@pytest.mark.parametrize("flags", [[], ["--content"], ["--compact"], ["--vimgrep"], ["-A", "2"]])
def test_claude_code_withholds_in_every_text_format(run_search, monkeypatch, marker, flags) -> None:
    monkeypatch.setenv(marker, "1")
    result = run_search(*flags)
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert stub_line(OWNER) in out  # first owner in TUMBLER order (1.2.3 before 1.10.1)
    assert withheld_note(1) in out
    _assert_nothing_leaks(out)
    assert PLAIN_BODY in out, "the non-sensitive hit still prints"


def test_claude_code_withholds_files_listing(run_search, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    result = run_search("--files")
    assert result.exit_code == 0, result.output
    assert stub_line(OWNER) in result.stdout and withheld_note(1) in result.stdout
    _assert_nothing_leaks(result.stdout)
    assert "public/notes.md" in result.stdout


def test_claude_code_withholds_under_bat(run_search, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    with patch("nexus.commands.search_cmd._is_bat_installed", return_value=True), \
         patch("nexus.formatters.run_bounded", side_effect=FileNotFoundError):
        result = run_search("--bat")
    assert result.exit_code == 0, result.output
    assert stub_line(OWNER) in result.stdout and withheld_note(1) in result.stdout
    _assert_nothing_leaks(result.stdout)


def test_stub_omits_the_tumbler_when_no_owner_resolves(run_search, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    hit = _hits()[0]
    hit.metadata.pop("sensitive_owners")
    result = run_search(hits=[hit])
    assert stub_line("") in result.stdout
    _assert_nothing_leaks(result.stdout)


def test_flag_alone_makes_a_hit_sensitive(run_search, monkeypatch) -> None:
    """The engine's ``sensitive`` field with no resolved owner still withholds (fail closed)."""
    monkeypatch.setenv("CLAUDECODE", "1")
    hit = _hits()[0]
    hit.metadata.pop("sensitive_owners")
    result = run_search(hits=[hit, _hits()[1]])
    assert withheld_note(1) in result.stdout
    _assert_nothing_leaks(result.stdout)


def test_owners_alone_make_a_hit_sensitive(run_search, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    hit = _hits()[0]
    hit.metadata["sensitive"] = False
    result = run_search(hits=[hit])
    assert withheld_note(1) in result.stdout
    _assert_nothing_leaks(result.stdout)


def test_no_sensitive_hit_prints_no_note(run_search, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    result = run_search(hits=[_hits()[1]])
    assert BADGE not in result.stdout and "withheld" not in result.stdout
    assert PLAIN_BODY in result.stdout


# --- --json ---------------------------------------------------------------------------------------------------


def test_json_under_claude_code_withholds_but_keeps_the_flag(run_search, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    result = run_search("--json")
    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)
    assert len(items) == 2, "results never thin silently"
    withheld, plain = items
    assert withheld["sensitive"] is True
    assert withheld["sensitive_owner"] == OWNER
    assert withheld["stub"] == stub_line(OWNER)
    assert "content" not in withheld and "title" not in withheld
    _assert_nothing_leaks(result.stdout)
    assert plain["sensitive"] is False and plain["content"] == PLAIN_BODY


def test_json_in_a_terminal_carries_sensitive_per_hit(run_search) -> None:
    result = run_search("--json")
    assert result.exit_code == 0, result.output
    items = json.loads(result.stdout)
    by_id = {i["id"]: i for i in items}
    assert by_id["s1"]["sensitive"] is True and by_id["s1"]["content"] == CANARY
    assert by_id["p1"]["sensitive"] is False


def test_json_sensitive_is_a_bool_even_when_the_engine_sent_none(run_search) -> None:
    hit = _hits()[1]
    hit.metadata.pop("sensitive")
    items = json.loads(run_search("--json", hits=[hit]).stdout)
    assert items[0]["sensitive"] is False


# --- In a terminal: the badge ---------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--content"], ["--compact"], ["--vimgrep"], ["-A", "2"]])
def test_terminal_badges_a_sensitive_hit_and_shows_its_content(run_search, flags) -> None:
    result = run_search(*flags)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    badged = [ln for ln in lines if BADGE in ln]
    assert badged, result.stdout
    assert "withheld" not in result.stdout
    plain_lines = [ln for ln in lines if PLAIN_BODY in ln]
    assert plain_lines and all(BADGE not in ln for ln in plain_lines)


def test_terminal_plain_shows_the_canary_with_the_badge(run_search) -> None:
    out = run_search().stdout
    assert any(BADGE in ln and SECRET_PATH in ln for ln in out.splitlines())
    assert CANARY in out


def test_terminal_vimgrep_keeps_the_quickfix_prefix(run_search) -> None:
    out = run_search("--vimgrep").stdout
    line = next(ln for ln in out.splitlines() if SECRET_PATH in ln)
    assert line.startswith(f"{SECRET_PATH}:1:0:"), line
    assert BADGE in line


def test_terminal_files_listing_stays_pipeable(run_search) -> None:
    result = run_search("--files")
    assert result.exit_code == 0, result.output
    assert SECRET_PATH in result.stdout.splitlines()
    assert BADGE not in result.stdout


def test_terminal_shows_the_reader_clause_once_when_content_is_shown(run_search) -> None:
    from nexus.context_annotations import SENSITIVE_READER_CLAUSE

    out = run_search().stdout
    assert out.count(SENSITIVE_READER_CLAUSE) == 1


def test_terminal_without_a_sensitive_hit_prints_no_clause(run_search) -> None:
    from nexus.context_annotations import SENSITIVE_READER_CLAUSE

    out = run_search(hits=[_hits()[1]]).stdout
    assert SENSITIVE_READER_CLAUSE not in out and BADGE not in out


def test_blank_marker_is_a_terminal(run_search, monkeypatch) -> None:
    """``started_by_claude_code`` ignores a blank value, so a blank marker badges rather than withholds."""
    monkeypatch.setenv("CLAUDECODE", "  ")
    out = run_search().stdout
    assert CANARY in out and BADGE in out
