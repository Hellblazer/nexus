# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for CLI command modules."""
from pathlib import Path


def default_db_path() -> Path:
    """Return the default path to the T2 SQLite database.

    Respects ``NEXUS_CONFIG_DIR`` via ``config.nexus_config_dir()`` so
    sandbox / test / multi-profile runs can redirect T2 writes away
    from the user's production memory.db. Previously hard-coded
    ``~/.config/nexus/memory.db`` with no override, which made
    sandbox-mode isolation impossible without redefining ``$HOME``.
    """
    from nexus.config import nexus_config_dir

    return nexus_config_dir() / "memory.db"
