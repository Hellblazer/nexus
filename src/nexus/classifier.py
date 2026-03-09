# SPDX-License-Identifier: AGPL-3.0-or-later
"""File classification for repository indexing.

Extension-based classification determines which embedding model and chunking
strategy each file receives during repository indexing.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()


class ContentClass(Enum):
    """Content classification for a repository file."""
    CODE = "code"
    PROSE = "prose"
    PDF = "pdf"
    SKIP = "skip"


# Derived from LANGUAGE_REGISTRY to prevent drift (RDR-028).
from nexus.languages import LANGUAGE_REGISTRY, GPU_SHADER_EXTENSIONS

_CODE_EXTENSIONS: frozenset[str] = frozenset(LANGUAGE_REGISTRY.keys()) | GPU_SHADER_EXTENSIONS

# Extensions for known-noise files that should never be indexed.
_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    # Build / config
    ".xml", ".json", ".yml", ".yaml", ".toml", ".properties",
    ".ini", ".cfg", ".conf", ".gradle",
    # Web / markup
    ".html", ".htm", ".css", ".svg",
    # Shell / batch (Windows)
    ".cmd", ".bat", ".ps1",
    # Lock files
    ".lock",
})


def _has_shebang(path: Path) -> bool:
    """Return True if *path* starts with a shebang (#!)."""
    try:
        with path.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError as exc:
        _log.debug("has_shebang_read_failed", path=str(path), error=str(exc))
        return False


def classify_file(
    path: Path,
    *,
    indexing_config: dict[str, Any] | None = None,
) -> ContentClass:
    """Classify *path* as CODE, PROSE, PDF, or SKIP based on extension.

    Priority order:
    1. PDF (always PDF)
    2. prose_extensions config override (wins over all)
    3. Effective code set (defaults + code_extensions config)
    4. _SKIP_EXTENSIONS (known-noise file types)
    5. Extensionless files: shebang → CODE, else → SKIP
    6. Everything else → PROSE
    """
    ext = path.suffix.lower()

    if ext == ".pdf":
        return ContentClass.PDF

    cfg = indexing_config or {}
    prose_overrides = set(cfg.get("prose_extensions", []))
    code_additions = set(cfg.get("code_extensions", []))

    # prose_extensions wins over everything
    if ext in prose_overrides:
        return ContentClass.PROSE

    # code_extensions adds to defaults
    effective_code = _CODE_EXTENSIONS | code_additions
    if ext in effective_code:
        return ContentClass.CODE

    # Known-noise extensions
    if ext in _SKIP_EXTENSIONS:
        return ContentClass.SKIP

    # Extensionless files: shebang → CODE, else → SKIP
    if ext == "":
        return ContentClass.CODE if _has_shebang(path) else ContentClass.SKIP

    return ContentClass.PROSE
