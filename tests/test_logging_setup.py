# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import logging.handlers
from pathlib import Path

import pytest
import structlog

from nexus.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _restore_structlog_after_test():
    """configure_logging swaps structlog's logger_factory and processor
    chain. Without restore, downstream tests that depend on structlog's
    default PrintLoggerFactory (captured via capsys) would see nothing
    because LoggerFactory(stdlib) routes through stdlib logging instead.

    Save-and-restore lets each test in this file freely call
    configure_logging without polluting the rest of the suite. The
    save uses ``structlog.get_config()`` (returns a dict copy) and the
    restore re-applies via ``structlog.configure(**saved)``.
    """
    saved = structlog.get_config()
    yield
    # structlog.get_config() returns a dict; pass back to configure as kwargs.
    structlog.configure(**saved)


def test_cli_mode_no_file_handler():
    """CLI mode configures stderr only — no RotatingFileHandler."""
    configure_logging("cli", verbose=False)
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers == []


def test_cli_mode_verbose_sets_debug():
    configure_logging("cli", verbose=True)
    level = logging.getLogger().level
    assert level == logging.DEBUG


def test_cli_mode_default_sets_warning():
    configure_logging("cli", verbose=False)
    level = logging.getLogger().level
    assert level == logging.WARNING


def test_console_mode_creates_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("console")
    logs_dir = tmp_path / "logs"
    assert logs_dir.exists()
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) >= 1
    assert file_handlers[-1].baseFilename.endswith("console.log")
    # Cleanup
    for h in file_handlers:
        root.removeHandler(h)
        h.close()


def test_mcp_mode_creates_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("mcp")
    logs_dir = tmp_path / "logs"
    assert logs_dir.exists()
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) >= 1
    assert file_handlers[-1].baseFilename.endswith("mcp.log")
    # Cleanup
    for h in file_handlers:
        root.removeHandler(h)
        h.close()


def test_noisy_loggers_suppressed():
    configure_logging("cli")
    for name in ("httpx", "httpcore", "chromadb.telemetry", "opentelemetry"):
        assert logging.getLogger(name).level >= logging.WARNING


def test_repeated_call_does_not_accumulate_handlers(tmp_path, monkeypatch):
    """Calling configure_logging twice for the same mode replaces the handler."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("console")
    configure_logging("console")
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename.endswith("console.log")
    # Cleanup
    root.removeHandler(file_handlers[0])
    file_handlers[0].close()


def test_mcp_mode_default_sets_info(tmp_path, monkeypatch):
    """Non-CLI modes default to INFO so subprocess lifecycle, tool
    dispatch, and structured warnings actually land in the file
    handler. Historically defaulted to WARNING which is why the
    long-lived mcp.log was a 0-byte file even on a busy server."""
    monkeypatch.delenv("NEXUS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("mcp")
    assert logging.getLogger().level == logging.INFO
    # Cleanup
    root = logging.getLogger()
    for h in [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]:
        root.removeHandler(h)
        h.close()


def test_nexus_log_level_env_overrides_mode_default(tmp_path, monkeypatch):
    """NEXUS_LOG_LEVEL env var overrides the mode-default level so
    operators can force DEBUG without code changes."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NEXUS_LOG_LEVEL", "DEBUG")
    configure_logging("mcp")
    assert logging.getLogger().level == logging.DEBUG
    # Cleanup
    root = logging.getLogger()
    for h in [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]:
        root.removeHandler(h)
        h.close()


def test_structlog_events_reach_file_handler(tmp_path, monkeypatch):
    """The visibility regression fix: structlog events must route
    through stdlib logging and land in the rotating file handler.
    Without the LoggerFactory + processor chain wiring, structlog's
    default PrintLoggerFactory writes to stderr only — bypassing the
    file handler entirely. This test asserts the bridge: a structlog
    event emitted after configure_logging("mcp") shows up in the
    file. If this fails, mcp.log will go silent again."""
    import structlog

    monkeypatch.delenv("NEXUS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("mcp")
    log_path = tmp_path / "logs" / "mcp.log"
    assert log_path.exists()

    # Emit a sentinel structlog event. The KeyValueRenderer at INFO
    # level should funnel it through stdlib logging into the file.
    structlog.get_logger("nexus.test").info(
        "logging_smoke_test_sentinel", probe="abc-xyz-123",
    )
    # Force the handler to flush so the bytes are visible to the
    # filesystem before we read.
    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_path.read_text()
    assert "logging_smoke_test_sentinel" in contents, (
        f"structlog event did not land in mcp.log; got:\n{contents!r}\n"
        f"This means the structlog -> stdlib bridge in "
        f"configure_logging is broken. mcp.log will go silent again."
    )
    assert "abc-xyz-123" in contents, (
        f"structlog event reached the file but lost its key=value "
        f"context (probe field missing). Renderer chain regression."
    )
    # Cleanup
    root = logging.getLogger()
    for h in [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]:
        root.removeHandler(h)
        h.close()


def test_structlog_warning_below_default_filtered(tmp_path, monkeypatch):
    """At INFO default, a structlog DEBUG event should NOT land in
    the file. Pins the level filter so verbose-by-default never
    happens silently."""
    import structlog

    monkeypatch.delenv("NEXUS_LOG_LEVEL", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("mcp")
    log_path = tmp_path / "logs" / "mcp.log"

    structlog.get_logger("nexus.test").debug(
        "debug_event_should_be_filtered", probe="never-emit-xyz",
    )
    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_path.read_text() if log_path.exists() else ""
    assert "never-emit-xyz" not in contents, (
        "DEBUG event leaked through the INFO default filter"
    )
    # Cleanup
    root = logging.getLogger()
    for h in [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]:
        root.removeHandler(h)
        h.close()


def test_flush_logging_flushes_root_handlers():
    """nexus-61539: flush_logging() must flush every handler on the root
    logger so a shutdown breadcrumb is durable before process exit."""
    from nexus.logging_setup import flush_logging

    flushed: list[bool] = []

    class _RecordingHandler(logging.Handler):
        def flush(self) -> None:  # noqa: D401
            flushed.append(True)

        def emit(self, record: logging.LogRecord) -> None:
            pass

    root = logging.getLogger()
    h = _RecordingHandler()
    root.addHandler(h)
    try:
        flush_logging()
    finally:
        root.removeHandler(h)

    assert flushed, "flush_logging() did not flush the root handler"


def test_flush_logging_skips_handler_that_raises():
    """A handler whose flush() raises must not propagate out of
    flush_logging() (best-effort durability on a shutdown path)."""
    from nexus.logging_setup import flush_logging

    other_flushed: list[bool] = []

    class _BadHandler(logging.Handler):
        def flush(self) -> None:
            raise RuntimeError("flush boom")

        def emit(self, record: logging.LogRecord) -> None:
            pass

    class _GoodHandler(logging.Handler):
        def flush(self) -> None:
            other_flushed.append(True)

        def emit(self, record: logging.LogRecord) -> None:
            pass

    root = logging.getLogger()
    bad, good = _BadHandler(), _GoodHandler()
    root.addHandler(bad)
    root.addHandler(good)
    try:
        flush_logging()  # must not raise
    finally:
        root.removeHandler(bad)
        root.removeHandler(good)

    assert other_flushed, "a raising handler blocked flushing the others"


# ---------------------------------------------------------------------------
# nexus-ovbr7: daemon-observability additions — new modes + open_child_log
# ---------------------------------------------------------------------------


def test_storage_service_mode_creates_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("storage_service")
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) >= 1
    assert file_handlers[-1].baseFilename.endswith("storage_service.log")
    for h in file_handlers:
        root.removeHandler(h)
        h.close()


def test_t3_daemon_mode_creates_file_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    configure_logging("t3_daemon")
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) >= 1
    assert file_handlers[-1].baseFilename.endswith("t3_daemon.log")
    for h in file_handlers:
        root.removeHandler(h)
        h.close()


class TestOpenChildLog:
    """open_child_log: an append-mode binary handle for redirecting a
    daemon child's stdout/stderr to ``<config>/logs/<name>.log`` — the
    anti-DEVNULL primitive (nexus-ovbr7)."""

    def test_creates_logs_dir_and_appends(self, tmp_path):
        from nexus.logging_setup import open_child_log

        with open_child_log("jar_test", config_dir=tmp_path) as fh:
            fh.write(b"first\n")
        with open_child_log("jar_test", config_dir=tmp_path) as fh:
            fh.write(b"second\n")
        path = tmp_path / "logs" / "jar_test.log"
        assert path.read_bytes() == b"first\nsecond\n", (
            "handle must append, not truncate — prior crash output is evidence"
        )

    def test_binary_append_mode(self, tmp_path):
        from nexus.logging_setup import open_child_log

        with open_child_log("jar_test", config_dir=tmp_path) as fh:
            assert "a" in fh.mode and "b" in fh.mode

    def test_rotates_at_spawn_when_over_max_bytes(self, tmp_path):
        from nexus.logging_setup import open_child_log

        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "jar_test.log").write_bytes(b"x" * 100)
        (logs / "jar_test.log.1").write_bytes(b"old-backup-1")

        with open_child_log("jar_test", config_dir=tmp_path, max_bytes=50) as fh:
            fh.write(b"fresh\n")

        assert (logs / "jar_test.log").read_bytes() == b"fresh\n"
        assert (logs / "jar_test.log.1").read_bytes() == b"x" * 100
        assert (logs / "jar_test.log.2").read_bytes() == b"old-backup-1"

    def test_rotation_drops_oldest_beyond_backup_count(self, tmp_path):
        from nexus.logging_setup import open_child_log

        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "jar_test.log").write_bytes(b"x" * 100)
        (logs / "jar_test.log.1").write_bytes(b"backup-1")
        (logs / "jar_test.log.2").write_bytes(b"backup-2")

        with open_child_log(
            "jar_test", config_dir=tmp_path, max_bytes=50, backup_count=2,
        ) as fh:
            fh.write(b"fresh\n")

        assert (logs / "jar_test.log.1").read_bytes() == b"x" * 100
        assert (logs / "jar_test.log.2").read_bytes() == b"backup-1"
        assert not (logs / "jar_test.log.3").exists(), "oldest must be dropped"

    def test_no_rotation_under_max_bytes(self, tmp_path):
        from nexus.logging_setup import open_child_log

        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "jar_test.log").write_bytes(b"small")

        with open_child_log("jar_test", config_dir=tmp_path) as fh:
            fh.write(b"+more")
        assert (logs / "jar_test.log").read_bytes() == b"small+more"
        assert not (logs / "jar_test.log.1").exists()

    def test_dotted_name_builds_crash_channel_path(self, tmp_path):
        """The detached-spawn crash channel uses a dotted name
        (``storage_service.crash``) — distinct from the structlog file so
        the daemon's RotatingFileHandler never co-owns it."""
        from nexus.logging_setup import open_child_log

        with open_child_log("storage_service.crash", config_dir=tmp_path) as fh:
            fh.write(b"traceback\n")
        assert (tmp_path / "logs" / "storage_service.crash.log").exists()


class TestOpenChildLogOrDevnull:
    """CRE HIGH-1 (nexus-ovbr7): a logging failure must never be the
    reason a daemon fails to spawn — the DEVNULL path it replaces could
    never fail."""

    def test_degrades_to_devnull_on_oserror(self, tmp_path, monkeypatch):
        import subprocess

        from nexus import logging_setup as ls

        def _boom(*a, **k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(ls, "open_child_log", _boom)
        result = ls.open_child_log_or_devnull("jar_test", config_dir=tmp_path)
        assert result is subprocess.DEVNULL

    def test_passes_through_handle_on_success(self, tmp_path):
        from nexus.logging_setup import open_child_log_or_devnull

        fh = open_child_log_or_devnull("jar_test", config_dir=tmp_path)
        try:
            assert not isinstance(fh, int)
            assert Path(fh.name) == tmp_path / "logs" / "jar_test.log"
        finally:
            if not isinstance(fh, int):
                fh.close()


class TestDaemonModeStderrPolicy:
    """nexus-ovbr7: daemon modes drop the stderr StreamHandler when stderr
    is not a tty, so the rotating file is the single copy of every event
    and the spawner's crash-channel file does not accumulate a duplicate
    of the whole stream. A tty stderr (--foreground in a terminal) keeps
    the handler for interactive debugging."""

    def _stderr_stream_handlers(self) -> list[logging.Handler]:
        import sys

        return [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, "stream", None) is sys.stderr
        ]

    def _cleanup_file_handlers(self) -> None:
        root = logging.getLogger()
        for h in [
            x for x in root.handlers
            if isinstance(x, logging.handlers.RotatingFileHandler)
        ]:
            root.removeHandler(h)
            h.close()

    def test_daemon_mode_non_tty_drops_stderr_handler(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
        configure_logging("storage_service")
        try:
            assert self._stderr_stream_handlers() == [], (
                "non-tty daemon stderr must not duplicate the event stream"
            )
        finally:
            self._cleanup_file_handlers()

    def test_daemon_mode_tty_keeps_stderr_handler(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
        configure_logging("t2_daemon")
        try:
            assert self._stderr_stream_handlers(), (
                "--foreground terminal debugging needs the stderr handler"
            )
        finally:
            self._cleanup_file_handlers()

    def test_non_daemon_mode_keeps_stderr_handler(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
        configure_logging("mcp")
        try:
            assert self._stderr_stream_handlers(), (
                "non-daemon modes keep legacy stderr behaviour"
            )
        finally:
            self._cleanup_file_handlers()

    def test_t2_daemon_mode_non_tty_drops_stderr_handler(self, tmp_path, monkeypatch):
        """critic SIG-1: t2_daemon is a PRE-EXISTING mode retroactively
        added to _DAEMON_MODES; pin its membership so a refactor cannot
        silently drop it (the T2 daemon runs all SQLite writes)."""
        import sys

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
        configure_logging("t2_daemon")
        try:
            assert self._stderr_stream_handlers() == []
        finally:
            self._cleanup_file_handlers()

    def test_daemon_mode_repeated_configure_single_file_handler(
        self, tmp_path, monkeypatch,
    ):
        """critic SIG-2 (part b): re-invoking configure_logging for a
        daemon mode must not stack file handlers — a double-wire would
        write every event twice, the exact corruption the single-copy
        invariant exists to prevent."""
        import sys

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
        configure_logging("storage_service")
        configure_logging("storage_service")
        try:
            file_handlers = [
                h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)
            ]
            assert len(file_handlers) == 1
        finally:
            self._cleanup_file_handlers()
