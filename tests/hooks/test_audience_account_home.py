# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audience helper's account-home lookup, in the set CI runs on native Windows (nexus-sby49).

``conexus/hooks/scripts/_audience.py`` reads the OS account's home from the account
database (``pwd`` on POSIX) or the known-folder profile API (Windows), never from
``HOME`` or ``USERPROFILE``. ``tests/test_audience.py`` pins the contract, but it sits
outside the Windows set; this file carries the lookup itself into it: the profile API
must answer an existing directory and must ignore a changed ``USERPROFILE``.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import _audience  # noqa: E402


@pytest.fixture(autouse=True)
def _no_seam(monkeypatch):
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)


def test_the_account_home_is_an_existing_directory():
    home = _audience.account_home()
    assert home and os.path.isdir(home), home


def test_a_changed_home_variable_does_not_move_the_account_home(tmp_path, monkeypatch):
    before = _audience.account_home()
    for name in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "NEXUS_CONFIG_DIR"):
        monkeypatch.setenv(name, str(tmp_path))
    after = _audience.account_home()
    assert os.path.normcase(after) == os.path.normcase(before)
    assert os.path.normcase(after) != os.path.normcase(str(tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="the known-folder profile API is Windows only")
def test_the_windows_profile_api_answers_a_directory_and_ignores_userprofile(tmp_path, monkeypatch):
    first = _audience._windows_profile_dir()
    assert first and os.path.isdir(first), first
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    second = _audience._windows_profile_dir()
    assert os.path.normcase(second) == os.path.normcase(first)


def test_a_flag_in_a_decoy_home_does_not_make_a_maintainer(tmp_path, monkeypatch):
    flag = tmp_path / ".config" / "nexus" / "maintainer-profile"
    flag.parent.mkdir(parents=True)
    flag.write_text("")
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(tmp_path))
    real = _audience.account_home()
    assert os.path.normcase(real) != os.path.normcase(str(tmp_path))
    assert _audience.account_flag() is os.path.isfile(os.path.join(real, ".config", "nexus", "maintainer-profile"))
