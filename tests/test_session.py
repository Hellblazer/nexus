"""Session-id generator and Claude-session flat-file behaviours.

The legacy getsid-keyed session-file scheme (``_stable_pid``,
``session_file_path``, ``write_session_file``, ``read_session_id``)
was deleted as the RDR-105 P4 follow-up tracked by ``nexus-9nbk``.
Current callers use ``read_claude_session_id`` /
``write_claude_session_id`` against the flat
``~/.config/nexus/current_session`` file.
"""
import os
import re
import time
from pathlib import Path

import pytest

from nexus.session import generate_session_id


def test_generate_session_id_is_uuid4() -> None:
    sid = generate_session_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", sid)


def test_generate_session_id_unique() -> None:
    assert generate_session_id() != generate_session_id()


# ── write_session_record ──────────────────────────────────────────────────────







# ── find_ancestor_session ─────────────────────────────────────────────────────













# ── sweep_stale_sessions ──────────────────────────────────────────────────────













# ── UUID-keyed session records (current scheme; PID-keyed above is legacy) ──






















# ── Migration: legacy numeric-stem files swept on first new-code SessionStart




# ── nexus-99jb Layer 3: aggressive liveness-based reap ───────────────────────



































# ── RDR-094 Phase 3: sweep_orphan_tmpdirs ───────────────────────────────────


class TestSweepOrphanTmpdirs:
    """RDR-094 Phase 3: reap nx_t1_* tmpdirs that no session record
    points at AND are older than max_age_hours. Closes Gap 3 (orphan
    tmpdirs from chroma crashes that the record-based sweep cannot
    see)."""

    def test_reaps_old_orphan_with_no_record(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        orphan = tmpdir_root / "nx_t1_orphan_xyz"
        orphan.mkdir()
        (orphan / "chroma.sqlite3").write_bytes(b"data")
        # Backdate 30 hours.
        old = time.time() - 30 * 3600
        os.utime(orphan, (old, old))

        reaped = sweep_orphan_tmpdirs(
            tmpdir_root=tmpdir_root,
        )
        assert reaped == 1
        assert not orphan.exists()

    def test_skips_recent_tmpdir(self, tmp_path: Path) -> None:
        """In-flight tmpdir (created moments ago, no record yet) must
        not be reaped. The 24h cutoff is the protection."""
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        recent = tmpdir_root / "nx_t1_recent"
        recent.mkdir()
        # mtime is now, well within the cutoff.

        reaped = sweep_orphan_tmpdirs(
            tmpdir_root=tmpdir_root,
        )
        assert reaped == 0
        assert recent.exists()





    def test_handles_missing_tmpdir_root(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        reaped = sweep_orphan_tmpdirs(
            tmpdir_root=tmp_path / "does-not-exist",
        )
        assert reaped == 0

    def test_ignores_non_nx_t1_directories(self, tmp_path: Path) -> None:
        """Only nx_t1_* prefixed dirs are candidates. Other tmpdirs
        from other tools are safe."""
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        unrelated = tmpdir_root / "tmpXYZ_other"
        unrelated.mkdir()
        old = time.time() - 30 * 3600
        os.utime(unrelated, (old, old))

        reaped = sweep_orphan_tmpdirs(
            tmpdir_root=tmpdir_root,
        )
        assert reaped == 0
        assert unrelated.exists()

    def test_reaps_multiple_old_orphans(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        tmpdir_root = tmp_path / "tmproot"
        tmpdir_root.mkdir()
        old = time.time() - 48 * 3600
        for n in range(3):
            d = tmpdir_root / f"nx_t1_o{n}"
            d.mkdir()
            os.utime(d, (old, old))

        reaped = sweep_orphan_tmpdirs(
            tmpdir_root=tmpdir_root,
        )
        assert reaped == 3

    def test_uses_system_tempdir_when_root_unspecified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When tmpdir_root is None, fall back to tempfile.gettempdir()."""
        import tempfile as _tempfile

        from nexus.session import sweep_orphan_tmpdirs

        monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(tmp_path))
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        orphan = tmp_path / "nx_t1_o"
        orphan.mkdir()
        old = time.time() - 30 * 3600
        os.utime(orphan, (old, old))

        reaped = sweep_orphan_tmpdirs()
        assert reaped == 1
