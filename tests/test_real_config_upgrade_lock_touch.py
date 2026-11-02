# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real-config-dir mutation guard and a live process's ``upgrade-auto.lock`` write.

``nexus.util.upgrade_auto_lock.acquire_auto_lock`` truncates the lock file and writes the
taker's pid every time it takes the lock, and the SessionStart hook's probe takes it for an
instant in every session that starts. So while a suite runs, any session starting on the box
rewrites the real ``~/.config/nexus/upgrade-auto.lock``: measured 2026-11-01, a 2296-test
``-m lint`` run ended rc=1 on ``MODIFIED upgrade-auto.lock`` with no test involved, and the
selector floor test's child session failed the same way (nexus-iox43).

The lock's bytes change on every take, so the mint lock's unchanged-bytes rule cannot apply.
The rule is writer-based instead: a taker under pytest (``PYTEST_CURRENT_TEST`` set, which a
test's subprocesses inherit) writes ``<pid> pytest``, a live host writes the pid alone. A lock
that existed at session start and ends holding a bare pid is benign; one ending with the pytest
mark, created, removed or holding anything else stays a state mutation. The last writer decides:
a live take after a test's take hides the test's, as a later live mint would.
"""

from __future__ import annotations

import os
import types

import pytest

import tests.conftest as conftest_module
from nexus.util import upgrade_auto_lock as lock_mod
from tests.conftest import _UPGRADE_LOCK_NAME, _split_appends_from_state  # type: ignore[attr-defined]

REL = lock_mod.LOCK_NAME
BEFORE = {REL: (1, 5)}
AFTER = {REL: (2, 5)}
CHANGED = [("MODIFIED", REL)]


def _split(content: bytes | None, changed=CHANGED, before=BEFORE, after=AFTER):
    return _split_appends_from_state(changed, before, after, upgrade_lock_content=content)


def test_a_live_takers_pid_is_benign() -> None:
    assert _split(b"48213") == ([], CHANGED)


@pytest.mark.parametrize("content", [b"48213 pytest", b"", b"not a pid", b"48213\n{}"],
                         ids=["pytest-mark", "empty", "text", "extra-line"])
def test_anything_but_a_bare_pid_is_a_state_mutation(content: bytes) -> None:
    assert _split(content) == (CHANGED, [])


def test_an_unread_lock_is_a_state_mutation() -> None:
    assert _split(None) == (CHANGED, [])


def test_a_created_lock_is_a_state_mutation() -> None:
    added = [("ADDED", REL)]
    assert _split(b"48213", changed=added, before={}) == (added, [])


def test_a_removed_lock_is_a_state_mutation() -> None:
    removed = [("REMOVED", REL)]
    assert _split(None, changed=removed, after={}) == (removed, [])


def test_a_lock_of_that_name_below_the_root_is_not_exempt() -> None:
    rel = "sub/" + REL
    changed = [("MODIFIED", rel)]
    assert _split(b"48213", changed=changed, before={rel: (1, 5)}, after={rel: (2, 5)}) == (changed, [])


def test_a_taker_under_pytest_marks_the_lock(tmp_path, monkeypatch) -> None:
    """The writer half of the rule: the mark is what keeps a test's take from passing."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/x.py::y (call)")
    lock = lock_mod.acquire_auto_lock(tmp_path)
    try:
        assert (tmp_path / REL).read_bytes() == f"{os.getpid()} pytest".encode()
    finally:
        lock.release()


def test_a_live_taker_writes_the_bare_pid(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    lock = lock_mod.acquire_auto_lock(tmp_path)
    try:
        assert (tmp_path / REL).read_bytes() == str(os.getpid()).encode()
    finally:
        lock.release()


@pytest.mark.parametrize("text", ["48213", "48213 pytest"])
def test_holder_pid_reads_either_form(tmp_path, text: str) -> None:
    (tmp_path / REL).write_text(text, encoding="utf-8")
    assert lock_mod.AutoLock(fd=None, held_by_other=True, path=tmp_path / REL).holder_pid() == 48213


def test_the_guard_names_the_same_lock() -> None:
    assert _UPGRADE_LOCK_NAME == lock_mod.LOCK_NAME


# ── the guard's own wiring: the lock reader and the call site that hands its bytes over ──


def test_the_reader_returns_a_small_lock_s_bytes(tmp_path) -> None:
    (tmp_path / REL).write_bytes(b"48213")
    assert conftest_module._read_upgrade_lock(tmp_path) == b"48213"


def test_the_reader_refuses_an_absent_a_large_or_a_linked_lock(tmp_path) -> None:
    assert conftest_module._read_upgrade_lock(tmp_path) is None
    (tmp_path / REL).write_bytes(b"1" * 65)
    assert conftest_module._read_upgrade_lock(tmp_path) is None
    (tmp_path / REL).unlink()
    (tmp_path / "elsewhere").write_bytes(b"48213")
    (tmp_path / REL).symlink_to(tmp_path / "elsewhere")
    assert conftest_module._read_upgrade_lock(tmp_path) is None


def _run_guard(monkeypatch, config_dir, rewrite: bytes) -> int:
    """Drive _check_real_config_dir_mutations end to end against *config_dir*: snapshot a lock
    holding a bare pid, rewrite it, and return the exit status the guard leaves on the session."""
    (config_dir / REL).write_bytes(b"1")
    monkeypatch.setattr(conftest_module, "_real_config_dir_for_guard", lambda: config_dir)
    monkeypatch.setattr(conftest_module, "_is_controller_or_serial", True)
    monkeypatch.setattr(conftest_module, "_real_config_dir_baseline_dir", config_dir)
    monkeypatch.setattr(conftest_module, "_mint_lock_baseline_content", {})
    monkeypatch.setattr(conftest_module, "_real_config_dir_baseline", conftest_module._snapshot_real_config_dir())
    os.utime(config_dir / REL, ns=(1, 1))
    (config_dir / REL).write_bytes(rewrite)
    session = types.SimpleNamespace(exitstatus=0)
    conftest_module._check_real_config_dir_mutations(session)
    return session.exitstatus


def test_the_guard_passes_a_live_host_s_take(tmp_path, monkeypatch) -> None:
    assert _run_guard(monkeypatch, tmp_path, b"48213") == 0


def test_the_guard_fails_a_test_s_take(tmp_path, monkeypatch, capsys) -> None:
    assert _run_guard(monkeypatch, tmp_path, b"48213 pytest") == 1
    assert "upgrade-auto.lock" in capsys.readouterr().out
