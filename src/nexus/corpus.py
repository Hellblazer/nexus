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
    """Raise ValueError if *name* violates ChromaDB collection name constraints."""
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


def embedding_model_for_collection(collection_name: str) -> str:
    """Return the Voyage AI model used at QUERY time for a T3 collection.

    voyage-4 is the universal query model for all collection types.
    Code collections are indexed with voyage-code-3 but queried with voyage-4 —
    the semantic spaces are compatible enough for effective retrieval, and a single
    query model simplifies cross-corpus search.
    """
    return "voyage-4"


def index_model_for_collection(collection_name: str) -> str:
    """Return the Voyage AI model used at INDEX time for a T3 collection.

    code__      → voyage-code-3    (code-optimised index; voyage-4 at query time)
    docs__      → voyage-context-3 (CCE for richer cross-chunk context)
    knowledge__ → voyage-context-3 (CCE for richer cross-chunk context)
    rdr__       → voyage-context-3 (CCE for RDR decision documents)
    all others  → voyage-4         (standard embedding)
    """
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
