# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConsoleConfig:
    """Configuration for the nx console server."""

    port: int = 8765
    host: str = "127.0.0.1"
    project: str = ""
    cwd: str = ""
    watch_paths: list[Path] = field(default_factory=list)
