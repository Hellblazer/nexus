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
    path.write_text(json.dumps({"generation": 1, "pid": pid, "started_at": 1, **record}))
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
    (directory / "4.1.token").write_text("{broken")
    for sid in (NEW, OLD, OLD.upper(), OTHER, "", "dddddddd-0000-4000-8000-000000000004"):
        assert lookup.lookup(sid, tmp_path)[0] == board.identity_for_session(sid, tmp_path), sid
    assert lookup.lookup(OLD, tmp_path) == ("i12", "t")
