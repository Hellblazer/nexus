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


def configure_logging(
    mode: Literal["cli", "console", "mcp", "hook"],
    verbose: bool = False,
) -> None:
    """Configure logging for the given nexus entry point.

    - **cli**: stderr only, WARNING default (matches prior behavior exactly).
    - **console/mcp/hook**: stderr + RotatingFileHandler under ``<config_dir>/logs/<mode>.log``.
    """
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr, force=True)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )

    # Suppress noisy HTTP wire-trace loggers even in verbose mode
    for noisy in ("httpx", "httpcore", "chromadb.telemetry", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if mode == "cli":
        return  # stderr only — zero behavior change

    # Non-CLI modes get a rotating file handler
    logs_dir = _config_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{mode}.log"

    # Remove stale handler for the same file if re-called (e.g. server restart)
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
