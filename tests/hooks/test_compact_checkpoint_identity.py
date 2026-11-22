# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Phase 2: the compaction checkpoint finds the board entry by its identity: line, and by nothing else.

Split from tests/hooks/test_compact_checkpoint.py, which is at its file-size ceiling.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "compact_checkpoint.py"
SID = "11111111-2222-3333-4444-555555555555"
NAME = "linda"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("compact_checkpoint_identity_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._load_siblings()  # main() does this inside its guard; the unit tests call functions directly
    return module


class TestIdentityLine:
    """RDR-253 Phase 2: the checkpoint finds the board entry by its identity: line after /clear; the session: line is
    not read."""

    STALE = "99999999-8888-4777-8666-555555555555"

    def _drive(self, mod, monkeypatch, bodies: dict[str, str], identity: str):
        searches: list[str] = []

        def fake(args, cwd, timeout):
            if args[1] == "search":
                searches.append(args[2])
                rows = [f"[{rid}] threads/{title}  (-, 2026-11-18T20:00:00Z)" for rid, (title, _b) in bodies.items()]
                return "\n".join(rows), None
            if args[1] == "get":
                return bodies[args[2]][1], None
            return "", None

        monkeypatch.setattr(mod, "_run_nx", fake)
        return mod._board_entry("", None, mod._BOARD_TIMEOUT_S, identity), searches

    def test_the_entry_is_found_by_identity_when_its_session_line_is_stale(self, mod, monkeypatch) -> None:
        body = f"session: {self.STALE}\nidentity: i12\n## Work\nstate: going\n"
        (title, got, how, error), searches = self._drive(mod, monkeypatch, {"7": ("baton", body)}, "i12")
        assert (title, how, error) == ("baton", "identity", None) and got == body
        assert searches == ["identity: i12"], "the identity search comes first and ends the lookup"

    def test_an_identity_only_entry_is_found_with_no_session_line(self, mod, monkeypatch) -> None:
        body = "identity: i12\n## Work\nstate: going\n"
        (title, got, how, error), _searches = self._drive(mod, monkeypatch, {"7": ("baton", body)}, "i12")
        assert (title, how, error) == ("baton", "identity", None) and got == body

    def test_an_entry_naming_another_identity_is_not_ours_even_with_our_session_line(self, mod, monkeypatch) -> None:
        theirs = f"session: {SID}\nidentity: i7\n## Theirs\nstate: x\n"
        (title, _got, how, error), searches = self._drive(mod, monkeypatch, {"8": ("other", theirs)}, "i12")
        assert (title, how) == ("", "") and error
        assert searches == ["identity: i12"], "the session id is never searched for"

    def test_an_entry_with_only_a_session_line_is_not_found(self, mod, monkeypatch) -> None:
        legacy = f"session: {SID}\n## Work\nstate: going\n"
        (title, _got, how, error), searches = self._drive(mod, monkeypatch, {"9": ("baton", legacy)}, "i12")
        assert (title, how) == ("", "") and error
        assert searches == ["identity: i12"]

    def test_with_no_identity_nothing_is_searched_and_no_entry_is_found(self, mod, monkeypatch) -> None:
        legacy = f"session: {SID}\n## Work\nstate: going\n"
        (title, body, how, error), searches = self._drive(mod, monkeypatch, {"9": ("baton", legacy)}, "")
        assert (title, body, how) == ("", "", "") and "no token file binds this session" in error
        assert searches == []

    def test_the_title_fallback_takes_only_an_entry_whose_identity_line_matches(self, mod, monkeypatch) -> None:
        bodies = {"theirs": "identity: i7\n## Theirs\nstate: x\n", "older": "session: x\n## Older\nstate: x\n",
                  "ours": "identity: i12\n## Ours\nstate: x\n"}
        current = {"body": ""}

        def fake(args, cwd, timeout):
            if args[1] == "search":
                return "", None
            if args[1] == "list":
                return "[5] threads/linda  (-, 2026-11-18T20:00:00Z)", None
            return current["body"], None

        monkeypatch.setattr(mod, "_run_nx", fake)
        current["body"] = bodies["theirs"]
        title, _body, how, error = mod._board_entry(NAME, None, mod._BOARD_TIMEOUT_S, "i12")
        assert (title, how) == ("", "") and "names another identity (i7)" in error
        current["body"] = bodies["older"]
        title, _body, how, error = mod._board_entry(NAME, None, mod._BOARD_TIMEOUT_S, "i12")
        assert (title, how) == ("", "") and "has no identity: line" in error
        current["body"] = bodies["ours"]
        title, body, how, error = mod._board_entry(NAME, None, mod._BOARD_TIMEOUT_S, "i12")
        assert (title, how, error) == (NAME, "exact title", None) and body == bodies["ours"]
        # with no identity of our own the title finds nothing either: no entry is ours without the line
        title, _body, how, _error = mod._board_entry(NAME, None, mod._BOARD_TIMEOUT_S, "")
        assert (title, how) == ("", "")

    def test_the_identity_lookup_reads_the_token_files_by_held_session_id(self, tmp_path) -> None:
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import _identity_lookup  # noqa: PLC0415
        finally:
            sys.path.remove(str(SCRIPT.parent))
        directory = tmp_path / "identity"
        directory.mkdir()
        (directory / "4242.1.token").write_text(json.dumps({
            "identity": "i12", "generation": 3, "token": "tok", "session_id": SID, "session_ids": [self.STALE, SID],
            "pid": 4242, "started_at": 1,
        }), encoding="utf-8")
        (directory / "pending.1.2").write_text("{}", encoding="utf-8")  # not a token file name
        assert _identity_lookup.lookup(self.STALE.upper(), tmp_path) == ("i12", "tok")
        assert _identity_lookup.lookup("00000000-0000-4000-8000-000000000000", tmp_path) == ("", "")
        assert _identity_lookup.lookup(SID, tmp_path / "absent") == ("", "")


def test_the_no_checkpoint_notice_tells_the_session_to_look_for_its_identity_line(mod) -> None:
    """A session with no checkpoint is told how to find its entry: by ``identity:``, never by a ``session:`` line
    (RDR-253 Phase 2)."""
    text = mod._no_restore_notice()
    assert "`identity:` line" in text and "nx identity show" in text
    assert "session:" not in text and "session name" not in text
