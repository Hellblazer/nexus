# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re

import structlog

# ChromaDB collection name constraints:
# - 3–63 characters
# - Must start and end with an alphanumeric character
# - May contain alphanumeric characters, hyphens, or underscores in the middle
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$")


def validate_collection_name(name: str) -> None:
    """Raise ValueError if *name* violates ChromaDB collection name constraints.

    Enforces two sets of rules:
    1. Structural (open-source ChromaDB): 3–63 characters, alphanumeric + hyphens/underscores,
       must start and end with alphanumeric.
    2. Cloud byte-length limit: name must not exceed 128 bytes when UTF-8 encoded.
       Relevant if names ever contain multi-byte characters; all current ASCII names
       are well within this limit since they cap at 63 chars = 63 bytes.
    """
    # Length check fires first for <3 chars; regex rejects other invalid patterns.
    # Both gates are needed: length for clear error messages, regex for charset/boundary validation.
    if not (3 <= len(name) <= 63):
        raise ValueError(
            f"Collection name {name!r} must be 3–63 characters (got {len(name)})"
        )
    if not _COLLECTION_NAME_RE.match(name):
        raise ValueError(
            f"Collection name {name!r} must start and end with an alphanumeric character "
            "and contain only alphanumeric characters, hyphens, or underscores"
        )
    # ChromaDB Cloud additional constraint: 128-byte limit (byte length, not char length).
    name_bytes = len(name.encode())
    if name_bytes > 128:
        raise ValueError(
            f"Collection name {name!r} exceeds ChromaDB Cloud 128-byte limit "
            f"(encoded as {name_bytes} bytes)"
        )


def _local_model_name() -> str:
    """Return the active local embedding model name (cached)."""
    from nexus.db.local_ef import LocalEmbeddingFunction
    return LocalEmbeddingFunction().model_name


def embedding_model_for_collection(collection_name: str) -> str:
    """Return the model used at QUERY time for a T3 collection.

    In local mode, all collections use the same local embedding model.

    In cloud mode:
    - CCE collections (docs__, knowledge__, rdr__) use voyage-context-3
    - All other collections use voyage-4
    """
    from nexus.config import is_local_mode
    if is_local_mode():
        return _local_model_name()
    if collection_name.startswith(("docs__", "knowledge__", "rdr__")):
        return "voyage-context-3"
    return "voyage-4"


def index_model_for_collection(collection_name: str) -> str:
    """Return the model used at INDEX time for a T3 collection.

    In local mode, all collections use the same local embedding model.

    In cloud mode:
    - code__      → voyage-code-3
    - docs__, knowledge__, rdr__ → voyage-context-3 (CCE)
    - all others  → voyage-4
    """
    from nexus.config import is_local_mode
    if is_local_mode():
        return _local_model_name()
    if collection_name.startswith("code__"):
        return "voyage-code-3"
    if collection_name.startswith(("docs__", "knowledge__", "rdr__")):
        return "voyage-context-3"
    return "voyage-4"


def t3_collection_name(user_arg: str) -> str:
    """Resolve a --collection argument to a T3 collection name.

    If the argument already contains ``__``, it is used as-is (fully-qualified).
    Otherwise the content is stored under ``knowledge__{user_arg}``.
    """
    if "__" in user_arg:
        return user_arg
    return f"knowledge__{user_arg}"


def resolve_corpus(corpus: str, all_collections: list[str]) -> list[str]:
    """Resolve a --corpus argument to a list of matching collection names.

    If *corpus* contains ``__`` it is treated as an exact collection name.
    Otherwise it is treated as a prefix — all collections starting with
    ``{corpus}__`` are returned.
    """
    if "__" in corpus:
        matches = [c for c in all_collections if c == corpus]
    else:
        prefix = f"{corpus}__"
        matches = [c for c in all_collections if c.startswith(prefix)]
    if not matches:
        structlog.get_logger().debug("resolve_corpus: no collections matched", corpus=corpus)
    return matches
