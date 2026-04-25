# SPDX-License-Identifier: AGPL-3.0-or-later
import logging
import logging.handlers

from nexus.logging_setup import configure_logging


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
