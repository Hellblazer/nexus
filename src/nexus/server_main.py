# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point for the Nexus server subprocess."""
import sys

from nexus.server import start_server


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7474
    start_server(port=port)


if __name__ == "__main__":
    main()
