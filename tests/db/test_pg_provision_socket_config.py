# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for _configure_cluster's postgresql.conf emission (nexus-6laob).

No PostgreSQL binaries required — _configure_cluster only writes a text file.
The regression guarded here: nx init --service failed on Debian/Ubuntu because
Postgres opened a Unix socket in the postgres-owned /var/run/postgresql even
though pg_ctl was started without -k. The fix writes
``unix_socket_directories = ''`` (TCP-only intent), disabling the socket.
"""
from __future__ import annotations

from pathlib import Path

from nexus.db.pg_provision import _configure_cluster


def _conf_lines(pgdata: Path) -> list[str]:
    return (pgdata / "postgresql.conf").read_text().splitlines()


def test_writes_empty_unix_socket_directories(tmp_path: Path) -> None:
    _configure_cluster(tmp_path, 55432)
    lines = _conf_lines(tmp_path)
    assert "unix_socket_directories = ''" in lines


def test_writes_port_and_tcp_listen(tmp_path: Path) -> None:
    _configure_cluster(tmp_path, 55432)
    lines = _conf_lines(tmp_path)
    assert "port = 55432" in lines
    assert "listen_addresses = '127.0.0.1'" in lines


def test_idempotent_no_duplicate_socket_directive(tmp_path: Path) -> None:
    _configure_cluster(tmp_path, 55432)
    _configure_cluster(tmp_path, 55433)  # re-run with a different port
    lines = _conf_lines(tmp_path)
    # Exactly one nexus-managed block survives the rewrite (BEGIN/END sentinels).
    assert lines.count("unix_socket_directories = ''") == 1
    assert lines.count("# nexus-managed: BEGIN") == 1
    assert lines.count("# nexus-managed: END") == 1
    # And the port reflects the latest run, not a stale append.
    assert "port = 55433" in lines
    assert "port = 55432" not in lines


def test_upgrade_from_old_format_conf(tmp_path: Path) -> None:
    """A conf written by the pre-sentinel nexus (bare ``# nexus-managed: <key>``
    comment + value lines, no BEGIN/END) must upgrade cleanly: the socket
    directive appears exactly once, the new port wins (PG last-wins), and the
    stale managed comment lines are removed. This is the cross-version path the
    existing-install repair (unconditional _configure_cluster) depends on."""
    conf = tmp_path / "postgresql.conf"
    conf.write_text(
        "# nexus-managed: port\n"
        "port = 55432\n"
        "# nexus-managed: listen_addresses\n"
        "listen_addresses = '127.0.0.1'\n"
    )
    _configure_cluster(tmp_path, 55433)
    lines = _conf_lines(tmp_path)
    # Socket directive written exactly once (only the new block has it).
    assert lines.count("unix_socket_directories = ''") == 1
    # Exactly one managed block, well-formed.
    assert lines.count("# nexus-managed: BEGIN") == 1
    assert lines.count("# nexus-managed: END") == 1
    # Stale old-format managed comment lines stripped.
    assert "# nexus-managed: port" not in lines
    assert "# nexus-managed: listen_addresses" not in lines
    # New port present; and it wins — its line comes after any stale old one.
    assert "port = 55433" in lines
    assert lines.index("port = 55433") > lines.index("# nexus-managed: BEGIN")


def test_unterminated_block_warns_not_swallows(tmp_path: Path, caplog) -> None:
    """A truncated prior write (BEGIN without END) must not silently drop every
    following line — it logs a warning (and the new block is still appended)."""
    conf = tmp_path / "postgresql.conf"
    conf.write_text("# nexus-managed: BEGIN\nport = 1\n")  # no END
    _configure_cluster(tmp_path, 55433)
    lines = _conf_lines(tmp_path)
    assert lines.count("unix_socket_directories = ''") == 1
    assert "port = 55433" in lines


def test_preserves_foreign_conf_lines(tmp_path: Path) -> None:
    conf = tmp_path / "postgresql.conf"
    conf.write_text("shared_buffers = 128MB\n# a comment\n")
    _configure_cluster(tmp_path, 55432)
    lines = _conf_lines(tmp_path)
    assert "shared_buffers = 128MB" in lines
    assert "# a comment" in lines
    assert "unix_socket_directories = ''" in lines


# ── max_locks_per_transaction (nexus-o26hs) ───────────────────────────────────
# The engine's RDR-225 walk creates models x tenants x 2 leaf partitions in one
# transaction and refuses to start (WalkLockPreflight) when their locks would not
# fit the lock table; at the PostgreSQL default of 64 that is about 39 tenants.
# The managed block raises it, unless the user's own conf already sets it.


def test_writes_max_locks_per_transaction_in_the_managed_block(tmp_path: Path) -> None:
    from nexus.db.pg_provision import MANAGED_MAX_LOCKS_PER_TRANSACTION

    _configure_cluster(tmp_path, 55432)
    lines = _conf_lines(tmp_path)
    directive = f"max_locks_per_transaction = {MANAGED_MAX_LOCKS_PER_TRANSACTION}"
    assert MANAGED_MAX_LOCKS_PER_TRANSACTION > 64
    assert lines.count(directive) == 1
    assert lines.index("# nexus-managed: BEGIN") < lines.index(directive) < lines.index("# nexus-managed: END")


def test_initdb_commented_default_does_not_count_as_a_user_setting(tmp_path: Path) -> None:
    # initdb's postgresql.conf carries the default commented out.
    (tmp_path / "postgresql.conf").write_text("#max_locks_per_transaction = 64\t\t# min 10\n")
    _configure_cluster(tmp_path, 55432)
    assert any(l.startswith("max_locks_per_transaction = ") for l in _conf_lines(tmp_path))


def test_a_users_own_max_locks_per_transaction_is_left_to_win(tmp_path: Path) -> None:
    # The managed block comes last and PostgreSQL takes the last value, so writing
    # ours would silently override (and could lower) the user's.
    for own in ("max_locks_per_transaction = 512", "  MAX_LOCKS_PER_TRANSACTION 512  # raised by hand"):
        (tmp_path / "postgresql.conf").write_text(own + "\n")
        _configure_cluster(tmp_path, 55432)
        lines = _conf_lines(tmp_path)
        assert own in lines
        assert [l for l in lines if "max_locks_per_transaction" in l.lower()] == [own]


def test_a_rerun_after_the_user_sets_it_drops_the_managed_value(tmp_path: Path) -> None:
    _configure_cluster(tmp_path, 55432)
    conf = tmp_path / "postgresql.conf"
    conf.write_text("max_locks_per_transaction = 512\n" + conf.read_text())
    _configure_cluster(tmp_path, 55432)
    lines = _conf_lines(tmp_path)
    assert [l for l in lines if "max_locks_per_transaction" in l] == ["max_locks_per_transaction = 512"]


def test_a_value_raised_on_the_managed_line_is_kept_and_a_lower_one_reset(tmp_path: Path) -> None:
    from nexus.db.pg_provision import MANAGED_MAX_LOCKS_PER_TRANSACTION

    conf = tmp_path / "postgresql.conf"
    _configure_cluster(tmp_path, 55432)
    managed = f"max_locks_per_transaction = {MANAGED_MAX_LOCKS_PER_TRANSACTION}"
    for edited, expected in (("max_locks_per_transaction = 512", 512), ("max_locks_per_transaction = 64", MANAGED_MAX_LOCKS_PER_TRANSACTION)):
        conf.write_text(conf.read_text().replace(managed, edited))
        _configure_cluster(tmp_path, 55432)
        lines = _conf_lines(tmp_path)
        managed = f"max_locks_per_transaction = {expected}"
        assert [l for l in lines if "max_locks_per_transaction" in l] == [managed], edited


def test_the_cold_start_rewrite_is_best_effort(tmp_path: Path, caplog) -> None:
    import os
    import sys

    import pytest

    from nexus.db.pg_provision import apply_managed_conf_before_start

    assert apply_managed_conf_before_start(tmp_path, 55432) is False, "no conf: left for the start to report"
    assert not (tmp_path / "postgresql.conf").exists()

    conf = tmp_path / "postgresql.conf"
    conf.write_text("shared_buffers = 128MB\n")
    assert apply_managed_conf_before_start(tmp_path, 55432) is True
    assert "# nexus-managed: BEGIN" in _conf_lines(tmp_path)

    if sys.platform == "win32" or os.geteuid() == 0:
        pytest.skip("POSIX mode bits; root ignores them")
    conf.write_text("shared_buffers = 128MB\n")
    conf.chmod(0o400)
    try:
        assert apply_managed_conf_before_start(tmp_path, 55432) is False
    finally:
        conf.chmod(0o600)
    assert conf.read_text() == "shared_buffers = 128MB\n"


# ── atomic rewrite (nexus-bt46b) ──────────────────────────────────────────────


def test_a_rewrite_that_fails_part_way_leaves_the_previous_conf_whole(tmp_path: Path, monkeypatch) -> None:
    """The bead's case: the new text is half written (here: the flush to disk
    fails after the write). The live conf is unchanged and nothing is left."""
    import os

    import pytest

    conf = tmp_path / "postgresql.conf"
    original = "shared_buffers = 128MB\n# a comment\n"
    conf.write_text(original)

    def _disk_full(fd: int) -> None:
        raise OSError(28, "No space left on device")

    from tests._module_seam import setattr_in

    setattr_in(monkeypatch, "nexus.db.pg_provision", "os.fsync", _disk_full)
    with pytest.raises(OSError):
        _configure_cluster(tmp_path, 55432)
    monkeypatch.undo()
    assert conf.read_text() == original, "a failed rewrite truncated or changed the live conf"
    assert [p.name for p in tmp_path.iterdir()] == ["postgresql.conf"], "the staged file was left behind"


def test_two_rewrites_stage_to_different_files(tmp_path: Path, monkeypatch) -> None:
    """Concurrent writers must not share a staging file (one would truncate the
    other's): each rewrite stages to its own name."""
    import os

    staged: list[str] = []
    real_replace = os.replace

    def _record(src, dst):
        staged.append(os.path.basename(src))
        return real_replace(src, dst)

    from tests._module_seam import setattr_in

    setattr_in(monkeypatch, "nexus.db.pg_provision", "os.replace", _record)
    _configure_cluster(tmp_path, 55432)
    _configure_cluster(tmp_path, 55433)
    assert len(staged) == 2 and staged[0] != staged[1]


def test_a_symlinked_conf_is_written_through_to_its_target(tmp_path: Path) -> None:
    import sys

    import pytest

    if sys.platform == "win32":
        pytest.skip("symlinks need privileges on Windows")
    real = tmp_path / "real.conf"
    real.write_text("shared_buffers = 128MB\n")
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "postgresql.conf").symlink_to(real)
    _configure_cluster(pgdata, 55432)
    assert (pgdata / "postgresql.conf").is_symlink()
    assert "# nexus-managed: BEGIN" in real.read_text().splitlines()


def test_the_rewrite_keeps_the_confs_mode_and_leaves_no_staging_file(tmp_path: Path) -> None:
    import os
    import stat
    import sys

    conf = tmp_path / "postgresql.conf"
    conf.write_text("shared_buffers = 128MB\n")
    if sys.platform != "win32":
        conf.chmod(0o640)
    _configure_cluster(tmp_path, 55432)
    assert "# nexus-managed: BEGIN" in _conf_lines(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["postgresql.conf"]
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(conf).st_mode) == 0o640


def test_a_killed_writers_staged_file_is_swept_and_a_fresh_one_kept(tmp_path: Path) -> None:
    import os
    import time

    stale = tmp_path / ".postgresql.conf.deadbeef"
    stale.write_text("half")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    fresh = tmp_path / ".postgresql.conf.inflight"
    fresh.write_text("another writer's")
    _configure_cluster(tmp_path, 55432)
    assert not stale.exists()
    assert fresh.exists(), "a staged file young enough to be a live writer's was removed"
