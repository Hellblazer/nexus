# SPDX-License-Identifier: AGPL-3.0-or-later
"""The seed cache's save side and its bounded size, offline: nothing is saved from an unclean or too-small
store, a save never raises and leaves nothing behind, one entry per start and template family is kept, and an
entry unused for 30 days expires. The restore rules are pinned in ``tests/test_upgrade_rehearsal_seed_cache.py``,
whose fixtures these tests share.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_upgrade_rehearsal_seed_cache import (
    FLOOR,
    Clock,
    ProbeError,
    _live,
    cache,
    key,
    make_run,
    new_run,
    sc,
    seeded_cache,
)

# --- a save never keeps a state it should not, and never raises ---------------------------------------------------


def test_a_store_that_was_not_cleanly_stopped_is_not_saved(tmp_path: Path) -> None:
    c = cache(tmp_path, clean_check=lambda _c: "postmaster.pid exists")
    res = c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "not cleanly shut down" in res.reason
    assert _live(tmp_path) == []


def test_a_store_under_the_floor_is_not_saved(tmp_path: Path) -> None:
    res = cache(tmp_path).save(
        key(), make_run(tmp_path, "seed-a", rows=10), floor=FLOOR
    )
    assert not res.saved and f"10 rows, below the floor of {FLOOR}" in res.reason


def test_a_prepare_that_did_not_prepare_is_not_saved(tmp_path: Path) -> None:
    run = make_run(tmp_path, "seed-a")
    (run / "prepare.json").write_text(
        json.dumps({"verdict": "error", "collections": {}})
    )
    res = cache(tmp_path).save(key(), run, floor=FLOOR)
    assert not res.saved and "prepare verdict" in res.reason


def test_a_store_outside_the_run_dir_is_not_saved(tmp_path: Path) -> None:
    run = make_run(tmp_path, "seed-a")
    elsewhere = tmp_path / "elsewhere-pg"
    elsewhere.mkdir()
    creds = run / "config" / "pg_credentials"
    creds.write_text(
        creds.read_text().replace(str(run / "config" / "postgres"), str(elsewhere))
    )
    res = cache(tmp_path).save(key(), run, floor=FLOOR)
    assert not res.saved and "outside" in res.reason


def test_a_failing_pg_read_at_save_time_never_raises_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    def boom(_config: Path, _olds) -> object:
        raise ProbeError("psql failed: timeout")

    c = cache(tmp_path, read_fn=boom)
    res = c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "ProbeError: psql failed" in res.reason
    root = tmp_path / "cache"
    assert (
        not root.exists()
        or [p.name for p in root.iterdir() if p.name != sc.LOCK_NAME] == []
    )


def test_a_save_that_fails_midway_leaves_no_entry_and_no_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sc._copy_tree
    calls = {"n": 0}

    def flaky(src: Path, dst: Path, deadline=None) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        real(src, dst, deadline)

    monkeypatch.setattr(sc, "_copy_tree", flaky)
    res = cache(tmp_path).save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "No space left" in res.reason
    assert [
        p.name for p in (tmp_path / "cache").iterdir() if p.name != sc.LOCK_NAME
    ] == []


def test_a_save_past_the_ceiling_stops_and_leaves_nothing(tmp_path: Path) -> None:
    res = cache(tmp_path, ceiling_s=0.0).save(
        key(), make_run(tmp_path, "seed-a"), floor=FLOOR
    )
    assert not res.saved and "ceiling" in res.reason
    root = tmp_path / "cache"
    assert (
        not root.exists()
        or [p.name for p in root.iterdir() if p.name != sc.LOCK_NAME] == []
    )


def test_a_second_save_of_one_key_keeps_the_first(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path)
    kept = c.save(k, make_run(tmp_path, "seed-b"), floor=FLOOR)
    assert not kept.saved and "kept its entry" in kept.reason


def test_a_save_waits_for_a_restore_in_progress_and_gives_up_when_it_does_not_end(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path, lock_timeout_s=0.5)
    (tmp_path / "cache").mkdir()
    with sc._locked(tmp_path / "cache", exclusive=False, timeout_s=1) as got:
        assert got
        res = c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR)
    assert not res.saved and "lock was not free" in res.reason
    assert [
        p.name for p in (tmp_path / "cache").iterdir() if p.name != sc.LOCK_NAME
    ] == []


# --- bounded size ----------------------------------------------------------------------------------------------


def test_saving_a_new_key_evicts_the_old_one_of_the_same_start_and_no_other(
    tmp_path: Path,
) -> None:
    logged: list[str] = []
    c = cache(tmp_path, log=logged.append)
    k_old, k_new = key(), key(seeder=(("upgrade_boot_writes.py", "new"),))
    k_other = key(from_version="7.99.0")
    for i, k in enumerate((k_old, k_other, k_new)):
        assert c.save(k, make_run(tmp_path, f"seed-{i}"), floor=FLOOR).saved
    assert _live(tmp_path) == sorted([k_new.name, k_other.name])
    assert any(k_old.name in m and "evicted" in m for m in logged)
    assert not [
        p for p in (tmp_path / "cache").iterdir() if p.name.startswith(".evict")
    ]


def test_a_standin_entry_never_evicts_the_real_one_and_the_reverse(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path)
    real, standin = key(), key(kind="standin", seeder=(("standin.py", "s"),))
    assert c.save(real, make_run(tmp_path, "a"), floor=FLOOR).saved
    assert c.save(standin, make_run(tmp_path, "b"), floor=FLOOR).saved
    assert _live(tmp_path) == sorted([real.name, standin.name])
    real2 = key(seeder=(("upgrade_boot_writes.py", "edited"),))
    assert c.save(real2, make_run(tmp_path, "c"), floor=FLOOR).saved
    assert _live(tmp_path) == sorted([real2.name, standin.name])


def test_a_start_whose_version_prefixes_another_is_not_evicted_by_it(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path)
    assert c.save(
        key(from_version="8.0.0.post1"), make_run(tmp_path, "a"), floor=FLOOR
    ).saved
    assert c.save(key(from_version="8.0.0"), make_run(tmp_path, "b"), floor=FLOOR).saved
    assert len(_live(tmp_path)) == 2


def test_an_entry_unused_for_30_days_is_evicted_by_the_next_save(
    tmp_path: Path,
) -> None:
    clock = Clock()
    c = cache(tmp_path, clock=clock)
    old = key(from_version="7.99.0")
    assert c.save(old, make_run(tmp_path, "a"), floor=FLOOR).saved
    clock.t += 29 * 86400
    assert c.save(key(), make_run(tmp_path, "b"), floor=FLOOR).saved
    assert old.name in _live(tmp_path), "29 days is within the bound"
    clock.t += 2 * 86400
    third = key(from_version="7.76.0")
    assert c.save(third, make_run(tmp_path, "c"), floor=FLOOR).saved
    assert old.name not in _live(tmp_path), "31 days unused: gone"


def test_a_hit_keeps_its_entry_young_and_sweeps_the_old_ones(tmp_path: Path) -> None:
    clock = Clock()
    c = cache(tmp_path, clock=clock)
    used, orphan = key(), key(from_version="7.99.0")
    assert c.save(orphan, make_run(tmp_path, "o"), floor=FLOOR).saved
    assert c.save(used, make_run(tmp_path, "u"), floor=FLOOR).saved
    clock.t += 20 * 86400
    assert c.restore(used, new_run(tmp_path), floor=FLOOR).ok  # last used day 20
    clock.t += 15 * 86400  # day 35: the orphan is 35 days idle, the used entry 15
    other = tmp_path / "run-c" / "from-8.0.0"
    for d in ("home/.local/bin", "config", "logs", "cwd"):
        (other / d).mkdir(parents=True)
    (other / "home" / ".local" / "bin" / "nx").write_text("fresh client")
    assert c.restore(used, other, floor=FLOOR).ok
    assert _live(tmp_path) == [used.name], (
        "a run that only restores still bounds the cache"
    )


def test_a_transient_rejection_never_evicts(tmp_path: Path) -> None:
    c, k, _ = seeded_cache(tmp_path, lock_timeout_s=0.2)
    dst = new_run(tmp_path)
    (dst / "prepare.json").write_text("{}")  # a dirty destination
    assert not c.restore(k, dst, floor=FLOOR).ok
    assert _live(tmp_path) == [k.name]


def test_a_stale_temp_dir_from_a_killed_save_is_removed_on_the_next_save(
    tmp_path: Path,
) -> None:
    c = cache(tmp_path)
    stale, fresh = tmp_path / "cache" / ".tmp-old-x", tmp_path / "cache" / ".tmp-new-y"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = stale.stat().st_mtime - 2 * sc.STALE_TMP_S
    os.utime(stale, (old, old))
    assert c.save(key(), make_run(tmp_path, "seed-a"), floor=FLOOR).saved
    assert not stale.exists() and fresh.exists(), (
        "another run's save in flight is left alone"
    )
