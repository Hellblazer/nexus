"""The garbage sweep (nexus.garbage): every litter class this repo has
produced is found and removed, and nothing live is touched."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from nexus.db.t1 import (
    _t1_session_mint_lock_path,
    clear_t1_session_lease,
    publish_t1_session_lease,
)
from nexus.garbage import (
    MINT_LOCK_MAX_AGE_DAYS,
    OPERATOR_LOG_MAX_AGE_DAYS,
    ROTATED_LOG_MAX_AGE_DAYS,
    TRASH_MAX_AGE_DAYS,
    catalog_garbage,
    reclaim_catalog_garbage,
    sweep_local_garbage,
)

DAY = 86_400
NOW = 1_800_000_000.0


def _touch(path: Path, *, age_days: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


class TestLocalSweep:
    def test_missing_config_dir_is_a_noop(self, tmp_path: Path) -> None:
        report = sweep_local_garbage(tmp_path / "absent", now=NOW)
        assert report.removed_count == 0 and report.failed_count == 0

    def test_rotated_logs_past_the_window_go_and_live_logs_stay(self, tmp_path: Path) -> None:
        old = _touch(tmp_path / "logs" / "aspect_worker_daemon.crash.log.1", age_days=ROTATED_LOG_MAX_AGE_DAYS + 1)
        fresh = _touch(tmp_path / "logs" / "aspect_worker_daemon.crash.log.2", age_days=ROTATED_LOG_MAX_AGE_DAYS - 1)
        live = _touch(tmp_path / "logs" / "aspect_worker_daemon.crash.log", age_days=400)

        report = sweep_local_garbage(tmp_path, now=NOW)

        assert not old.exists()
        assert fresh.exists(), "inside the window is kept"
        assert live.exists(), "the live (unrotated) log is never a sweep target"
        assert report.removed == {"rotated_log": [old.name]}

    def test_operator_dumps_past_the_window_go(self, tmp_path: Path) -> None:
        old_t = _touch(tmp_path / "logs" / "operator-timeout-20260506T010203.log", age_days=OPERATOR_LOG_MAX_AGE_DAYS + 1)
        old_b = _touch(tmp_path / "logs" / "operator-budget-20260506T010203.log", age_days=OPERATOR_LOG_MAX_AGE_DAYS + 1)
        fresh = _touch(tmp_path / "logs" / "operator-timeout-20260905T010203.log", age_days=0.5)
        other = _touch(tmp_path / "logs" / "index-nexus.log", age_days=400)

        report = sweep_local_garbage(tmp_path, now=NOW)

        assert not old_t.exists() and not old_b.exists()
        assert fresh.exists() and other.exists()
        assert sorted(report.removed["operator_log"]) == sorted([old_t.name, old_b.name])

    def test_stale_mint_locks_go_unless_their_session_has_a_lease(self, tmp_path: Path) -> None:
        stale = _touch(tmp_path / "t1_mint_dead-session.lock", age_days=MINT_LOCK_MAX_AGE_DAYS + 1)
        leased = _touch(tmp_path / "t1_mint_live-session.lock", age_days=30)
        _touch(tmp_path / "t1_session_lease.live-session", age_days=0)
        young = _touch(tmp_path / "t1_mint_young-session.lock", age_days=0.2)

        report = sweep_local_garbage(tmp_path, now=NOW)

        assert not stale.exists()
        assert leased.exists(), "a lock whose session holds a lease is never reaped, whatever its age"
        assert young.exists(), "inside the one-day window is kept"
        assert report.removed == {"mint_lock": [stale.name]}

    def test_a_failed_unlink_is_reported_not_raised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stale = _touch(tmp_path / "t1_mint_dead.lock", age_days=5)
        real_unlink = Path.unlink

        def boom(self: Path, *a, **k):
            if self == stale:
                raise PermissionError("nope")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", boom)
        report = sweep_local_garbage(tmp_path, now=NOW)
        assert report.failed == {"mint_lock": [stale.name]}
        assert report.removed_count == 0

    def test_default_clock_is_wall_time(self, tmp_path: Path) -> None:
        # No `now`: a file stamped a year ago by the real clock is reaped.
        old = tmp_path / "t1_mint_ancient.lock"
        old.write_bytes(b"")
        stamp = time.time() - 365 * DAY
        os.utime(old, (stamp, stamp))
        assert sweep_local_garbage(tmp_path).removed == {"mint_lock": [old.name]}


class _FakeCatalog:
    def __init__(self, links: list[dict], trash: dict, *, raise_on: str | None = None) -> None:
        self._links = list(links)
        self._trash = trash
        self.unlinked: list[tuple] = []
        self.purge_calls: list[dict] = []
        self._raise_on = raise_on

    def orphaned_links(self) -> list[dict]:
        if self._raise_on == "links":
            raise RuntimeError("engine down")
        return list(self._links)

    def unlink(self, from_t, to_t, link_type) -> int:
        self.unlinked.append((from_t, to_t, link_type))
        self._links = [l for l in self._links if (l["from_tumbler"], l["to_tumbler"], l["link_type"]) != (from_t, to_t, link_type)]
        return 1

    def purge_trash(self, older_than_days=1, *, dry_run=True) -> dict:
        self.purge_calls.append({"older_than_days": older_than_days, "dry_run": dry_run})
        return dict(self._trash)


_LINKS = [
    {"from_tumbler": "1.1.4092", "to_tumbler": "1.1.43", "link_type": "implements"},
    {"from_tumbler": "1.1.4092", "to_tumbler": "1.1.4", "link_type": "implements"},
]
_TRASH = {"documents_purged": 880, "chunks_384_stranded": 0, "chunks_768_stranded": 0, "chunks_1024_stranded": 1503}


class TestCatalogGarbage:
    def test_counts_are_read_only_and_window_is_one_day(self) -> None:
        client = _FakeCatalog(_LINKS, _TRASH)
        g = catalog_garbage(client)
        assert (g.orphaned_links, g.trash_documents, g.stranded_chunks) == (2, 880, 1503)
        assert g.total == 2385 and g.error is None
        assert client.purge_calls == [{"older_than_days": TRASH_MAX_AGE_DAYS, "dry_run": True}]
        assert client.unlinked == []

    def test_engine_error_is_reported_never_read_as_clean(self) -> None:
        g = catalog_garbage(_FakeCatalog(_LINKS, _TRASH, raise_on="links"))
        assert g.total == 0 and g.error == "engine down"

    def test_reclaim_deletes_links_then_purges(self) -> None:
        client = _FakeCatalog(_LINKS, _TRASH)
        r = reclaim_catalog_garbage(client)
        assert r.links_deleted == 2 and r.trash_documents == 880 and r.stranded_chunks == 1503
        assert len(client.unlinked) == 2
        assert client.purge_calls == [{"older_than_days": TRASH_MAX_AGE_DAYS, "dry_run": False}]
        assert catalog_garbage(client).orphaned_links == 0

    def test_reclaim_raises_on_engine_error(self) -> None:
        with pytest.raises(RuntimeError):
            reclaim_catalog_garbage(_FakeCatalog(_LINKS, _TRASH, raise_on="links"))


def test_lease_clear_also_removes_the_mint_lock(tmp_path: Path) -> None:
    """Item 2 of the plan: the mint lock is unlinked with the lease at
    session end, so one session leaves zero files behind."""
    publish_t1_session_lease("sess-1", "tok", tmp_path)
    lock = _t1_session_mint_lock_path("sess-1", tmp_path)
    lock.write_bytes(b"")
    clear_t1_session_lease("sess-1", tmp_path)
    assert not lock.exists()
    assert not list(tmp_path.glob("t1_*")), "a finished session leaves no T1 files"
    clear_t1_session_lease("sess-1", tmp_path)  # idempotent
