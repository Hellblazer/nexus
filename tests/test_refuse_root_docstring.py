# SPDX-License-Identifier: AGPL-3.0-or-later
"""``refuse_root`` says what it does on Windows (RDR-224, nexus-f9bgu.33, review m12).

Its docstring called a native-Windows client out of scope (WSL2 only) after native
Windows became a supported target. The refusal itself is correct: Windows has no
euid, so nothing is refused there, and an elevated Administrator is covered by the
ACL grant instead. The text and the behaviour are pinned together.
"""
from __future__ import annotations

import os

import pytest

from nexus.db import pg_provision
from tests._module_seam import setattr_in


def test_the_docstring_no_longer_calls_native_windows_out_of_scope() -> None:
    doc = pg_provision.refuse_root.__doc__ or ""
    assert "out of scope" not in doc and "WSL2" not in doc
    assert "Administrator" in doc and "grant_user_tree_access" in doc


def test_without_a_geteuid_nothing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "geteuid", raising=False)
    pg_provision.refuse_root()  # returns: Windows has no euid


def test_root_is_still_refused_where_there_is_an_euid(monkeypatch: pytest.MonkeyPatch) -> None:
    setattr_in(monkeypatch, "nexus.db.pg_provision", "os.geteuid", lambda: 0, raising=False)
    with pytest.raises(pg_provision.PgRootUserError):
        pg_provision.refuse_root()
