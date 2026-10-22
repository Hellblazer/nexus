# SPDX-License-Identifier: AGPL-3.0-or-later
"""The real-config-dir mutation guard and a live process's mint-lock touch.

``nexus.db.data_token._open_mint_lock`` writes a lock file's bytes once, when
it creates it (the attribution line, nexus-28bzt), and on every later mint only
refreshes its mtime so the reaper measures time since last use (nexus-ndfc1).
On an armed laptop every live ``nx-mcp`` host re-mints about hourly, so a long
run's (mtime_ns, size) diff showed ``MODIFIED data_token_mint_lock.<digest>``
with no test involved: measured 2026-10-22, a 2174-passed ``-m lint -n 8`` run
ended rc=1 on exactly that, and the selector floor test's child session failed
the same way (nexus-2edpf).

Unchanged bytes do not prove no test touched a lock (a test's mint touches only
the mtime too), so the rule is creator-based: a lock that existed at session
start, kept its bytes, and whose attribution names no pytest test is benign. A
lock created under pytest names its test and is never exempt, so a leak fails
every run, not only the first. The lock stays OFF the allowlist
(``test_election_flock_allowance_stays_narrow``).
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import (  # type: ignore[attr-defined]
    _snapshot_mint_lock_contents,
    _split_appends_from_state,
)

REL = "data_token_mint_lock." + "a" * 64
BEFORE = {REL: (1, 300)}
AFTER = {REL: (2, 300)}
CHANGED = [("MODIFIED", REL)]
LIVE = b'{"argv0": "nx-mcp", "pid": 1, "pytest_current_test": null}\n'
UNDER_TEST = b'{"argv0": "pytest", "pid": 2, "pytest_current_test": "tests/test_x.py::test_y (call)"}\n'


def _split(content: bytes | None, *, baseline: bytes | None = None, changed=CHANGED, before=BEFORE):
    base = None if baseline is None and content is None else {REL: content if baseline is None else baseline}
    after = None if content is None else {REL: content}
    return _split_appends_from_state(
        changed, before, AFTER, mint_lock_baseline_content=base, mint_lock_content=after,
    )


def test_a_live_hosts_touch_is_benign() -> None:
    assert _split(LIVE) == ([], CHANGED)


def test_a_lock_from_a_release_without_attribution_is_benign() -> None:
    assert _split(b"") == ([], CHANGED)


def test_a_lock_a_test_created_is_never_exempt() -> None:
    """The leak the guard exists for: once created under pytest, every later
    run that touches the lock still fails."""
    assert _split(UNDER_TEST) == (CHANGED, [])


def test_an_unparseable_attribution_is_not_trusted() -> None:
    assert _split(b"not json\n") == (CHANGED, [])


def test_changed_bytes_are_a_state_mutation() -> None:
    assert _split(UNDER_TEST, baseline=LIVE) == (CHANGED, [])


def test_an_added_lock_is_a_state_mutation() -> None:
    added = [("ADDED", REL)]
    state, appends = _split_appends_from_state(
        added, {}, AFTER, mint_lock_baseline_content={}, mint_lock_content={REL: LIVE},
    )
    assert (state, appends) == (added, [])


def test_a_removed_lock_is_a_state_mutation() -> None:
    removed = [("REMOVED", REL)]
    state, appends = _split_appends_from_state(
        removed, BEFORE, {}, mint_lock_baseline_content={REL: LIVE}, mint_lock_content={},
    )
    assert (state, appends) == (removed, [])


def test_unreadable_content_on_either_side_is_a_state_mutation() -> None:
    for before, after in (({}, {REL: LIVE}), ({REL: LIVE}, {}), ({}, {}), (None, {REL: LIVE}), ({REL: LIVE}, None)):
        state, _ = _split_appends_from_state(
            CHANGED, BEFORE, AFTER, mint_lock_baseline_content=before, mint_lock_content=after,
        )
        assert state == CHANGED, (before, after)


def test_a_file_without_the_prefix_is_not_covered() -> None:
    for rel in ("sub/" + REL, "data_token_lease." + "a" * 64):
        state, _ = _split_appends_from_state(
            [("MODIFIED", rel)], {rel: (1, 300)}, {rel: (2, 300)},
            mint_lock_baseline_content={rel: LIVE}, mint_lock_content={rel: LIVE},
        )
        assert state == [("MODIFIED", rel)], rel


def test_no_content_given_keeps_the_strict_verdict() -> None:
    assert _split_appends_from_state(CHANGED, BEFORE, AFTER) == (CHANGED, [])


def test_the_snapshot_reads_root_mint_locks_only(tmp_path: Path) -> None:
    (tmp_path / REL).write_bytes(LIVE)
    (tmp_path / "data_token_lease.b").write_bytes(b"not a lock")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / REL).write_bytes(LIVE)
    (tmp_path / ("data_token_mint_lock." + "c" * 64)).write_bytes(b"x" * 70_000)
    assert _snapshot_mint_lock_contents(tmp_path) == {REL: LIVE}


def test_the_snapshot_of_a_missing_dir_is_empty(tmp_path: Path) -> None:
    assert _snapshot_mint_lock_contents(tmp_path / "absent") == {}


def test_the_guard_prefix_is_the_product_prefix() -> None:
    from nexus.db.data_token import _MINT_LOCK_PREFIX
    from tests.conftest import _MINT_LOCK_FILE_PREFIX  # type: ignore[attr-defined]

    assert _MINT_LOCK_FILE_PREFIX == _MINT_LOCK_PREFIX
