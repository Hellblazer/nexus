# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point for the Nexus server subprocess."""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from nexus.server import start_server


def _configure_logging() -> None:
    """Configure a rotating log file and structlog to write to it."""
    log_path = Path.home() / ".config" / "nexus" / "serve.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)

    # Configure structlog to route through the stdlib logging bridge so all
    # modules using structlog write to the same rotating file handler above.
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def main() -> None:
    _configure_logging()
    from nexus.config import default_port
    port_default: int = default_port()
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else port_default
    except ValueError:
        sys.stderr.write(f"Invalid port: {sys.argv[1]!r}\n")
        sys.exit(1)
    if not 1 <= port <= 65535:
        sys.stderr.write(f"Port must be 1-65535, got {port}\n")
        sys.exit(1)
    start_server(port=port)


if __name__ == "__main__":
    main()
