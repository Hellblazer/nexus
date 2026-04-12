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
