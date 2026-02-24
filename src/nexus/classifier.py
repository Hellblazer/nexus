# SPDX-License-Identifier: AGPL-3.0-or-later
"""File classification for repository indexing.

Extension-based classification determines which embedding model and chunking
strategy each file receives during repository indexing.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any


class ContentClass(Enum):
    """Content classification for a repository file."""
    CODE = "code"
    PROSE = "prose"
    PDF = "pdf"


# Canonical code extensions — the greppable, authoritative list.
# Everything NOT in this set (and not .pdf) is treated as prose.
_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".cpp", ".cc", ".c", ".h", ".hpp", ".rb", ".cs", ".sh", ".bash",
    ".kt", ".swift", ".scala", ".r", ".m", ".php",
})


def classify_file(
    path: Path,
    *,
    indexing_config: dict[str, Any] | None = None,
) -> ContentClass:
    """Classify *path* as code, prose, or PDF based on its extension.

    *indexing_config* is the ``indexing`` section from ``.nexus.yml``:
    - ``code_extensions``: list of extensions to add to the code set
    - ``prose_extensions``: list of extensions forced to prose (wins over all)
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

    return ContentClass.PROSE
