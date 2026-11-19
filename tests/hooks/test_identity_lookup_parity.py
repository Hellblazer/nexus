# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 5: the plugin's stdlib identity lookup and ``nexus.identity.board`` read one rule.

The hook scripts cannot import ``nexus``, so ``conexus/hooks/scripts/_identity_lookup.py`` restates the token-file
read and the identity-line pattern. These pin the two to the same answers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from nexus.identity import board

SCRIPTS = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
OLD, NEW, OTHER = (
    "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002", "cccccccc-0000-4000-8000-000000000003",
)


@pytest.fixture
def lookup():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import _identity_lookup  # noqa: PLC0415
    finally:
        sys.path.remove(str(SCRIPTS))
    return _identity_lookup


def _token(directory: Path, pid: int, record: dict, mtime: float | None = None) -> None:
    path = directory / f"{pid}.1.token"
    path.write_text(json.dumps({"generation": 1, "pid": pid, "started_at": 1, **record}), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.mark.parametrize(
    "body",
    [
        "identity: i12\n", "session: x\nidentity: i12\n## T\n", "identity:\ti3\r\n", "Identity: i12\n",
        "identity: i12 extra\n", "identity: nonsense\n", "  identity: i12\n", "", "## T\nidentity: i4\n",
    ],
)
def test_both_read_the_identity_line_as_the_engine_fence_does(lookup, body: str) -> None:
    assert lookup.identity_of(body) == board.identity_of(body)


def test_both_resolve_the_same_identity_from_the_same_token_files(lookup, tmp_path: Path) -> None:
    directory = tmp_path / "identity"
    directory.mkdir()
    _token(directory, 1, {"identity": "i12", "token": "t", "session_id": NEW, "session_ids": [OLD, NEW]})
    _token(directory, 2, {"identity": "i9", "session_id": NEW}, mtime=1)  # stale: older than the live one
    _token(directory, 3, {"identity": "not-an-id", "session_id": OTHER})
    (directory / "4.1.token").write_text("{broken", encoding="utf-8")
    for sid in (NEW, OLD, OLD.upper(), OTHER, "", "dddddddd-0000-4000-8000-000000000004"):
        assert lookup.lookup(sid, tmp_path)[0] == board.identity_for_session(sid, tmp_path), sid
    assert lookup.lookup(OLD, tmp_path) == ("i12", "t")


def test_both_withhold_an_expired_token_but_still_name_the_identity(lookup, tmp_path: Path) -> None:
    directory = tmp_path / "identity"
    directory.mkdir()
    _token(directory, 1, {"identity": "i12", "token": "old", "expires_at": 1.0, "session_id": NEW})
    assert lookup.lookup(NEW, tmp_path) == ("i12", "")
    assert board.binding_for_session(NEW, tmp_path) == ("i12", "")


def test_a_file_whose_current_session_is_the_wanted_one_outranks_a_newer_file_that_only_lists_it(
    lookup, tmp_path: Path
) -> None:
    """``/clear`` in seat A, then a ``/resume`` of that session id under seat B: A's token file still lists the id in
    ``session_ids`` (and is the newer file), but B's file names it as its current session. B owns it."""
    directory = tmp_path / "identity"
    directory.mkdir()
    _token(directory, 1, {"identity": "i9", "token": "seat-a", "session_id": NEW, "session_ids": [OLD, NEW]}, mtime=2000)
    _token(directory, 2, {"identity": "i12", "token": "seat-b", "session_id": OLD, "session_ids": [OLD]}, mtime=1000)
    assert lookup.lookup(OLD, tmp_path) == ("i12", "seat-b")
    assert board.binding_for_session(OLD, tmp_path) == ("i12", "seat-b")
    # the id seat A currently holds still finds seat A
    assert lookup.lookup(NEW, tmp_path) == ("i9", "seat-a")
    assert board.identity_for_session(NEW, tmp_path) == "i9"
    # equal rank (both current, or both merely listed) falls back to the newest file
    _token(directory, 3, {"identity": "i20", "session_id": OTHER, "session_ids": [OTHER]}, mtime=1)
    _token(directory, 4, {"identity": "i21", "session_id": OTHER, "session_ids": [OTHER]}, mtime=5)
    assert lookup.lookup(OTHER, tmp_path)[0] == board.identity_for_session(OTHER, tmp_path) == "i21"


def test_both_withhold_a_token_inside_the_expiry_margin(lookup, tmp_path: Path) -> None:
    import time  # noqa: PLC0415

    from nexus.identity import token_contract  # noqa: PLC0415

    assert lookup.EXPIRY_MARGIN_S == token_contract.EXPIRY_MARGIN_S
    directory = tmp_path / "identity"
    directory.mkdir()
    soon = time.time() + token_contract.EXPIRY_MARGIN_S / 2
    _token(directory, 1, {"identity": "i12", "token": "t", "expires_at": soon, "session_id": NEW})
    assert lookup.lookup(NEW, tmp_path) == ("i12", "")
    assert board.binding_for_session(NEW, tmp_path) == ("i12", "")
    _token(directory, 1, {"identity": "i12", "token": "t", "expires_at": time.time() + 600, "session_id": NEW})
    assert lookup.lookup(NEW, tmp_path) == ("i12", "t")
    assert board.binding_for_session(NEW, tmp_path) == ("i12", "t")
