# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point for the Nexus server subprocess."""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nexus.server import start_server


def _configure_logging() -> None:
    """Configure a rotating log file for the server process."""
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


def main() -> None:
    _configure_logging()
    from nexus.config import _DEFAULTS
    default_port: int = _DEFAULTS["server"]["port"]
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else default_port
    except ValueError:
        sys.stderr.write(f"Invalid port: {sys.argv[1]!r}\n")
        sys.exit(1)
    start_server(port=port)


if __name__ == "__main__":
    main()
