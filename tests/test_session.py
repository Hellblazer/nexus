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


# ── Fix #1: T1 chroma store relocation (nexus-ycwec) ─────────────────────────


class TestT1StoreDirHelper:
    """_make_t1_store_dir creates the store under config_dir/t1/ and preserves
    the nx_t1_ prefix so the safe-kill gate in sweep_orphan_t1_chromadbs
    still matches."""

    def test_creates_under_config_t1_not_tempdir(self, tmp_path: Path) -> None:
        from nexus.session import _make_t1_store_dir

        config_dir = tmp_path / "config"
        store = _make_t1_store_dir(config_dir)
        assert store.is_dir()
        assert store.is_relative_to(config_dir / "t1")

    def test_prefix_preserved_for_safe_kill_gate(self, tmp_path: Path) -> None:
        """The safe-kill gate in _parse_orphan_t1_chromadb_candidates checks
        'nx_t1_' in the chroma command — the store dirname must contain it."""
        from nexus.session import _make_t1_store_dir

        config_dir = tmp_path / "config"
        store = _make_t1_store_dir(config_dir)
        assert "nx_t1_" in store.name

    def test_creates_t1_parent_dir_if_absent(self, tmp_path: Path) -> None:
        from nexus.session import _make_t1_store_dir

        config_dir = tmp_path / "config"
        assert not (config_dir / "t1").exists()
        _make_t1_store_dir(config_dir)
        assert (config_dir / "t1").exists()

    def test_mode_is_700(self, tmp_path: Path) -> None:
        """Store dir should be readable only by the owner."""
        from nexus.session import _make_t1_store_dir

        config_dir = tmp_path / "config"
        store = _make_t1_store_dir(config_dir)
        mode = store.stat().st_mode & 0o777
        assert mode == 0o700


class TestSweepOrphanTmpdirsNewRoot:
    """sweep_orphan_tmpdirs with a config_dir argument sweeps <config>/t1/
    AND the legacy OS-temp root for migration cleanup (nexus-ycwec)."""

    @pytest.fixture(autouse=True)
    def _no_live_t1_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """nexus-oj1hn: sweep_orphan_tmpdirs now consults
        _live_t1_chromadb_paths() before reaping. Default to no live
        servers so these mtime-only regression tests are unaffected
        and deterministic (no real ps subprocess call)."""
        import nexus.session as session

        monkeypatch.setattr(session, "_live_t1_chromadb_paths", lambda: set())

    def test_sweeps_config_t1_root(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        config_dir = tmp_path / "config"
        t1_root = config_dir / "t1"
        t1_root.mkdir(parents=True)
        orphan = t1_root / "nx_t1_abcd"
        orphan.mkdir()
        old = time.time() - 30 * 3600
        os.utime(orphan, (old, old))

        reaped = sweep_orphan_tmpdirs(config_dir=config_dir)
        assert reaped == 1
        assert not orphan.exists()

    def test_also_sweeps_legacy_tmpdir_root(self, tmp_path: Path) -> None:
        """Migration: a pre-fix orphan under OS-temp must also be reaped."""
        from nexus.session import sweep_orphan_tmpdirs

        config_dir = tmp_path / "config"
        (config_dir / "t1").mkdir(parents=True)
        legacy_root = tmp_path / "legacy_tmp"
        legacy_root.mkdir()
        legacy_orphan = legacy_root / "nx_t1_old"
        legacy_orphan.mkdir()
        old = time.time() - 30 * 3600
        os.utime(legacy_orphan, (old, old))

        reaped = sweep_orphan_tmpdirs(
            config_dir=config_dir, tmpdir_root=legacy_root
        )
        assert reaped == 1
        assert not legacy_orphan.exists()

    def test_skips_recent_in_config_t1(self, tmp_path: Path) -> None:
        from nexus.session import sweep_orphan_tmpdirs

        config_dir = tmp_path / "config"
        t1_root = config_dir / "t1"
        t1_root.mkdir(parents=True)
        recent = t1_root / "nx_t1_new"
        recent.mkdir()
        # mtime is now

        reaped = sweep_orphan_tmpdirs(config_dir=config_dir)
        assert reaped == 0
        assert recent.exists()


class TestSweepOneTmpdirRootLiveness:
    """nexus-oj1hn / GH #1151: _sweep_one_tmpdir_root must skip any
    candidate whose path is in live_paths, regardless of mtime."""

    def test_live_path_skipped_non_live_reaped(self, tmp_path: Path) -> None:
        """Non-vacuity: in ONE call, a live-marked candidate survives
        despite being past the mtime cutoff, WHILE a second, non-live
        stale candidate IS reaped -- proves the skip is real, not
        that the sweep simply reaped nothing."""
        from nexus.session import _sweep_one_tmpdir_root

        root = tmp_path / "t1"
        root.mkdir()
        live = root / "nx_t1_live"
        live.mkdir()
        stale = root / "nx_t1_stale"
        stale.mkdir()
        old = time.time() - 30 * 3600
        os.utime(live, (old, old))
        os.utime(stale, (old, old))
        cutoff = time.time() - 24 * 3600

        reaped = _sweep_one_tmpdir_root(root, cutoff, live_paths={str(live)})

        assert reaped == 1
        assert live.exists()
        assert not stale.exists()

    def test_default_live_paths_none_preserves_prior_behavior(
        self, tmp_path: Path
    ) -> None:
        """No live_paths argument (the pre-fix call signature) must
        still reap a stale candidate -- backward compatible default."""
        from nexus.session import _sweep_one_tmpdir_root

        root = tmp_path / "t1"
        root.mkdir()
        stale = root / "nx_t1_stale"
        stale.mkdir()
        old = time.time() - 30 * 3600
        os.utime(stale, (old, old))
        cutoff = time.time() - 24 * 3600

        reaped = _sweep_one_tmpdir_root(root, cutoff)

        assert reaped == 1
        assert not stale.exists()

    def test_live_path_survives_when_root_is_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SUBSTANTIVE-CRITIC SIGNIFICANT FINDING (review round 1):
        tempfile.mkdtemp internally returns os.path.abspath(file), not a
        naive path join -- so a live path reported via ps is ALWAYS
        absolute, even if the directory it was created under was
        reached via a relative `root`. Without normalizing str(d)
        through os.path.abspath() before the live_paths membership
        check, a relative root would build relative candidate strings
        that could never match an always-absolute live_paths entry,
        silently defeating the whole liveness check and reproducing
        the reaped-live-dir bug under this precondition."""
        from nexus.session import _sweep_one_tmpdir_root

        root = tmp_path / "t1"
        root.mkdir()
        live = root / "nx_t1_live"
        live.mkdir()
        old = time.time() - 30 * 3600
        os.utime(live, (old, old))
        cutoff = time.time() - 24 * 3600

        # What a real chroma process's --path argument (and thus a real
        # _live_t1_chromadb_paths() entry) would contain, regardless of
        # how _make_t1_store_dir's config_dir was originally specified.
        live_path_as_ps_would_report_it = os.path.abspath(str(live))

        monkeypatch.chdir(tmp_path)
        relative_root = Path("t1")

        reaped = _sweep_one_tmpdir_root(
            relative_root, cutoff, live_paths={live_path_as_ps_would_report_it}
        )

        assert reaped == 0
        assert live.exists()


class TestSweepOrphanTmpdirsLiveness:
    """nexus-oj1hn / GH #1151: sweep_orphan_tmpdirs wires
    _live_t1_chromadb_paths() into both root scans so an idle-but-live
    session's backing store is never reaped, reproducing the exact
    reported scenario end-to-end (with the ps subprocess mocked)."""

    def test_live_session_tmpdir_survives_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import nexus.session as session
        from nexus.session import sweep_orphan_tmpdirs

        config_dir = tmp_path / "config"
        t1_root = config_dir / "t1"
        t1_root.mkdir(parents=True)
        live = t1_root / "nx_t1_live"
        live.mkdir()
        old = time.time() - 30 * 3600
        os.utime(live, (old, old))

        monkeypatch.setattr(session, "_live_t1_chromadb_paths", lambda: {str(live)})

        reaped = sweep_orphan_tmpdirs(config_dir=config_dir)

        assert reaped == 0
        assert live.exists()

    def test_non_live_sibling_still_reaped_alongside_live(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity at the sweep_orphan_tmpdirs level too: the live
        dir survives while a stale, non-live sibling in the same root
        is reaped in the same call."""
        import nexus.session as session
        from nexus.session import sweep_orphan_tmpdirs

        config_dir = tmp_path / "config"
        t1_root = config_dir / "t1"
        t1_root.mkdir(parents=True)
        live = t1_root / "nx_t1_live"
        live.mkdir()
        stale = t1_root / "nx_t1_stale"
        stale.mkdir()
        old = time.time() - 30 * 3600
        os.utime(live, (old, old))
        os.utime(stale, (old, old))

        monkeypatch.setattr(session, "_live_t1_chromadb_paths", lambda: {str(live)})

        reaped = sweep_orphan_tmpdirs(config_dir=config_dir)

        assert reaped == 1
        assert live.exists()
        assert not stale.exists()


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

    @pytest.fixture(autouse=True)
    def _no_live_t1_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """nexus-oj1hn: sweep_orphan_tmpdirs now consults
        _live_t1_chromadb_paths() before reaping. Default to no live
        servers so these mtime-only regression tests are unaffected
        and deterministic (no real ps subprocess call)."""
        import nexus.session as session

        monkeypatch.setattr(session, "_live_t1_chromadb_paths", lambda: set())

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


# ── Fix #4: clearer T1ServerNotFoundError guidance (nexus-ycwec) ─────────────


class TestT1ServerNotFoundErrorGuidance:
    """_reconnect raises T1ServerNotFoundError with actionable guidance:
    tells the user to /clear or restart the MCP server.  The in-place
    reconnect path is INTENTIONALLY unsupported per RDR-105 P4 (nexus-jnx7);
    this only improves the message, never adds reconnect logic."""

    def test_reconnect_message_mentions_clear_or_restart(self) -> None:
        """The message raised by _reconnect is actionable: it names /clear
        or MCP server restart as the recovery step (nexus-ycwec Fix #4)."""
        import unittest.mock as mock
        from nexus.db.t1 import T1Database, T1ServerNotFoundError

        db = T1Database.__new__(T1Database)
        db._dead = False
        db._session_id = "test-session"

        with pytest.raises(T1ServerNotFoundError) as exc_info:
            db._reconnect()

        text = str(exc_info.value).lower()
        # Must name the actionable recovery step
        assert "/clear" in text or "restart" in text, (
            f"_reconnect error lacks /clear or restart guidance: {text!r}"
        )

    def test_reconnect_is_idempotent_when_already_dead(self) -> None:
        """_reconnect with _dead=True is a no-op (does not double-raise)."""
        from nexus.db.t1 import T1Database

        db = T1Database.__new__(T1Database)
        db._dead = True
        db._session_id = "test-session"
        # Should return without raising
        db._reconnect()
