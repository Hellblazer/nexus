# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Literal

import structlog


def _config_dir() -> Path:
    """Return the nexus config directory, respecting NEXUS_CONFIG_DIR."""
    override = os.environ.get("NEXUS_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


def _resolve_level(mode: str, verbose: bool) -> int:
    """Resolve the effective log level.

    Precedence (high to low):
      1. ``NEXUS_LOG_LEVEL`` env var (DEBUG / INFO / WARNING / ERROR / CRITICAL)
      2. ``verbose=True`` flag, DEBUG
      3. mode default: ``cli`` is WARNING (zero behaviour change for the CLI),
         non-cli (``console`` / ``mcp`` / ``hook``) is INFO so subprocess
         lifecycle, tool dispatch, and structured warnings actually land in
         the file handler.
    """
    override = os.environ.get("NEXUS_LOG_LEVEL", "").strip().upper()
    if override:
        resolved = getattr(logging, override, None)
        if isinstance(resolved, int):
            return resolved
    if verbose:
        return logging.DEBUG
    if mode == "cli":
        return logging.WARNING
    return logging.INFO


def configure_logging(
    mode: Literal["cli", "console", "mcp", "hook"],
    verbose: bool = False,
) -> None:
    """Configure logging for the given nexus entry point.

    Routes structlog events through the stdlib ``logging`` module so the
    rotating file handler installed below catches them. Without this
    bridge, ``structlog.get_logger().info(...)`` writes via structlog's
    default ``PrintLoggerFactory`` which goes to stderr, bypassing the
    file handler entirely (the historical reason ``mcp.log`` was a 0-byte
    file even on a long-running server).

    Modes:
      * ``cli``: stderr only, WARNING default. Kept legacy-compatible so
        the human-facing CLI does not gain noise from this change.
      * ``console`` / ``mcp`` / ``hook``: stderr + RotatingFileHandler at
        ``<config_dir>/logs/<mode>.log``, INFO default. Lifecycle events,
        tool dispatches, and structured warnings now land in the log file.

    The level is overridable via the ``NEXUS_LOG_LEVEL`` env var; useful
    for one-off DEBUG runs without code changes.
    """
    level = _resolve_level(mode, verbose)

    # Configure stdlib root logger first. ``force=True`` clears any prior
    # handlers so re-invocation (e.g. server hot-restart) does not stack
    # duplicates.
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr, force=True)

    # Suppress noisy HTTP / telemetry wire-trace loggers even in verbose
    # mode. These produce so much output at DEBUG that the signal in
    # mcp.log would drown.
    for noisy in ("httpx", "httpcore", "chromadb.telemetry", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Bridge structlog -> stdlib logging. Two things wired here:
    #   1. ``LoggerFactory`` makes ``structlog.get_logger()`` return a
    #      logger that writes via the stdlib logging module instead of
    #      stderr-print.
    #   2. The processor chain serialises the structured event to a
    #      single-line key=value rendering before stdlib formats it.
    #      ``add_log_level`` keeps the level visible in the rendered
    #      message; ``TimeStamper`` adds an ISO timestamp; the final
    #      ``KeyValueRenderer`` turns the event dict into a string the
    #      file formatter can prepend with its own ``asctime / name /
    #      levelname`` columns.
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.set_exc_info,
            structlog.processors.KeyValueRenderer(
                key_order=["event", "timestamp", "level"],
                drop_missing=True,
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if mode == "cli":
        return  # stderr only — zero behaviour change for the CLI entry point

    # Non-CLI modes get a rotating file handler at <config>/logs/<mode>.log.
    logs_dir = _config_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{mode}.log"

    # Remove stale handler for the same file if re-called (e.g. server
    # restart in tests). Without this, repeated configure_logging calls
    # would stack handlers and write each event N times.
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(log_path):
            root.removeHandler(h)
            h.close()

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(handler)
