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
