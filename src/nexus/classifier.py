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
    # Data files (not prose — waste API calls and produce poor embeddings)
    ".txt", ".csv", ".tsv", ".dat", ".log",
})

# Binary asset extensions. The step-7 fall-through default is PROSE, so any
# binary file without a code/prose extension would otherwise be embedded as
# prose — producing zero search signal and wasting Voyage budget. Game/media
# repos (WoW addons, Unity, etc.) track these in git: textures, audio, fonts,
# compiled binaries. WeakAuras2 (2026-05-31) registered 366 such files
# (.tga ×246, .ogg ×104, .blp, .ttf, .mp3) as "prose"; the resulting docs
# collection held 0 usable vectors. Classify binary assets as SKIP.
# Operators with a genuine need opt back in via ``prose_extensions``.
_BINARY_EXTENSIONS: frozenset[str] = frozenset({
    # Images / textures
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".tif",
    ".webp", ".tga", ".blp", ".dds", ".psd", ".ai", ".eps",
    # Audio
    ".mp3", ".ogg", ".wav", ".flac", ".aac", ".m4a", ".opus", ".aiff",
    # Video
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".wmv", ".flv",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Archives
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".jar",
    # Compiled / binary objects
    ".so", ".dll", ".dylib", ".exe", ".o", ".a", ".obj", ".lib",
    ".class", ".pyc", ".pyo", ".wasm", ".bin", ".dat.gz",
    # Documents / misc binary
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".db", ".sqlite", ".sqlite3",
})

# nexus-haet (GH issue surfaced 2026-05-08): minified bundle filenames
# (``htmx.min.js``, ``react.min.css``, ``vendor.min.mjs``) are
# extension-wise indexable code, but the bytes are unreadable for
# semantic search: mangled identifiers, no whitespace, no comments.
# Embedding them wastes Voyage budget AND historically produced
# oversized chunks (the 2026-05-08 audit found 84 chunks > Voyage
# MAX_DOCUMENT_BYTES, all from a single ``htmx.min.js`` at 50,917
# bytes each, blocking re-embed). The T3.put MAX_DOCUMENT_BYTES guard
# (``db/t3.py:460``) drops oversized chunks defensively, but skipping
# the file at classification time avoids the chunker churn entirely.
#
# Operators with a legitimate need to index minified files (e.g.
# auditing a vendored bundle for known patterns) opt back in via
# ``indexing_config["index_minified"] = True``.
_MINIFIED_BASENAME_PATTERNS: tuple[str, ...] = (
    ".min.js", ".min.mjs", ".min.cjs",
    ".min.css",
    ".bundle.js", ".bundle.mjs",  # Webpack / Rollup output convention.
)


def _is_minified_basename(basename: str) -> bool:
    """Return True when ``basename`` matches a known minified-bundle
    naming convention. Case-insensitive; handles double extensions
    (e.g. ``htmx.min.js``) which a bare ``Path.suffix`` check would
    miss (it returns ``.js``).
    """
    lower = basename.lower()
    return any(lower.endswith(suffix) for suffix in _MINIFIED_BASENAME_PATTERNS)


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
    2. nexus-haet: minified bundles (basename matches
       ``_MINIFIED_BASENAME_PATTERNS``) -> SKIP, unless
       ``indexing_config["index_minified"] = True``
    3. prose_extensions config override (wins over all)
    4. Effective code set (defaults + code_extensions config)
    5. _SKIP_EXTENSIONS (known-noise file types)
    6. _BINARY_EXTENSIONS (binary assets — textures, audio, fonts, objects)
    7. Extensionless files: shebang → CODE, else → SKIP
    8. Everything else → PROSE
    """
    ext = path.suffix.lower()

    if ext == ".pdf":
        return ContentClass.PDF

    cfg = indexing_config or {}

    # nexus-haet: minified-bundle skip. Runs BEFORE the code-extension
    # check so ``htmx.min.js`` (extension ``.js``) doesn't reach the
    # CODE branch. Operators can opt back in via
    # ``indexing_config["index_minified"] = True``.
    if not cfg.get("index_minified", False):
        if _is_minified_basename(path.name):
            return ContentClass.SKIP

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

    # Binary assets (textures, audio, fonts, compiled objects). Checked after
    # prose_extensions so an operator override can still force one to PROSE.
    if ext in _BINARY_EXTENSIONS:
        return ContentClass.SKIP

    # Extensionless files: shebang → CODE, else → SKIP
    if ext == "":
        return ContentClass.CODE if _has_shebang(path) else ContentClass.SKIP

    return ContentClass.PROSE
