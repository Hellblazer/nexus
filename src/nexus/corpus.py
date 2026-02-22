# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re

# ChromaDB collection name constraints:
# - 3–63 characters
# - Must start and end with an alphanumeric character
# - May contain alphanumeric characters, hyphens, or underscores in the middle
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$")


def validate_collection_name(name: str) -> None:
    """Raise ValueError if *name* violates ChromaDB collection name constraints."""
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
    """Return the Voyage AI model name appropriate for a T3 collection."""
    if collection_name.startswith("code__"):
        return "voyage-code-3"
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
        return [c for c in all_collections if c == corpus]
    prefix = f"{corpus}__"
    return [c for c in all_collections if c.startswith(prefix)]
