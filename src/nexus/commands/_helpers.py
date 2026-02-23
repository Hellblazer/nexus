# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helpers for CLI command modules."""
from pathlib import Path


def default_db_path() -> Path:
    """Return the default path to the T2 SQLite database."""
    return Path.home() / ".config" / "nexus" / "memory.db"
