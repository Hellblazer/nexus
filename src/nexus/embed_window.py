# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedding-model token windows (nexus-spujb).

An embedder reads at most its model's window of tokens and drops the rest of
a chunk from the vector with no signal: bge-base-en-v1.5 at 512 and MiniLM at
256 (the engine's ``Bge768Embedder`` and ``OnnxEmbedder`` tokenize with
``truncation=true``). The window belongs to the MODEL, so it is looked up by
the model token a collection name carries, never by deployment mode.

The Voyage models take 32,000 tokens. A chunk is capped at
``SAFE_CHUNK_BYTES`` and a token covers at least one byte, so no chunk can
reach that window; :func:`window_for_model` returns ``None`` for them and no
tokenizer is loaded.
"""
from __future__ import annotations

import functools
from pathlib import Path

import structlog
from tokenizers import Tokenizer

from nexus.db.limits import SAFE_CHUNK_BYTES

_log = structlog.get_logger(__name__)

#: Token window per embedding-model token. Every model a collection can be
#: written with must have an entry (``tests/test_embed_window.py`` pins it).
MODEL_MAX_TOKENS: dict[str, int] = {
    "bge-base-en-v15-768": 512,
    "minilm-l6-v2-384": 256,
    "voyage-code-3": 32_000,
    "voyage-context-3": 32_000,
    "voyage-3": 32_000,
}

class UnknownEmbeddingModelError(ValueError):
    """The model token has no entry in :data:`MODEL_MAX_TOKENS`."""


class TokenWindow:
    """A model's token limit plus the tokenizer that counts against it."""

    def __init__(self, max_tokens: int, tokenizer_path: Path) -> None:
        self.max_tokens = max_tokens
        self.tokenizer_path = tokenizer_path
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.no_truncation()

    def count(self, text: str) -> int:
        """Tokens the model sees for *text*, special tokens included."""
        return len(self._tokenizer.encode(text).ids)

    def fits(self, text: str) -> bool:
        return self.count(text) <= self.max_tokens


def _tokenizer_path(model: str) -> Path:
    if model == "bge-base-en-v15-768":
        from nexus.db.service_bge_model import (  # noqa: PLC0415 — deferred; only small-window models need it
            TOKENIZER_FILENAME,
            service_bge_model_dir,
        )
        return service_bge_model_dir() / TOKENIZER_FILENAME
    if model == "minilm-l6-v2-384":
        from nexus.db.minilm_direct import artifact_dir  # noqa: PLC0415 — deferred; only small-window models need it

        # The same artifact the Python MiniLM embedder and the engine's
        # OnnxEmbedder load; not downloaded here, see window_for_model.
        return artifact_dir() / "tokenizer.json"
    raise UnknownEmbeddingModelError(f"no tokenizer location recorded for {model!r}")


def has_small_window(model: str) -> bool:
    """True when *model* reads fewer tokens than a chunk can hold, from the
    table alone: no tokenizer is loaded, so a read path can ask safely."""
    from nexus.db.local_ef import _MODEL_TOKENS  # noqa: PLC0415 — deferred; local_ef is only needed for the alias

    max_tokens = MODEL_MAX_TOKENS.get(_MODEL_TOKENS.get(model, model))
    return max_tokens is not None and max_tokens < SAFE_CHUNK_BYTES


@functools.cache
def window_for_model(model: str) -> TokenWindow | None:
    """The token window chunks for *model* must fit, or ``None`` when the
    chunk byte cap already keeps every chunk inside it.

    An unrecorded *model* gets no window and a warning, not an error: a
    collection name can carry any model token, and refusing to index an
    install that works today is worse than leaving an unknown model as
    unguarded as it was. The completeness test in
    ``tests/test_embed_window.py`` is where a missing entry for a writable
    model fails. A small-window model whose tokenizer file the client cannot
    see also gets no window and a warning naming the path, for the same
    reason: the engine may still embed from a file the client does not see.
    """
    from nexus.db.local_ef import _MODEL_TOKENS  # noqa: PLC0415 — deferred; local_ef is only needed for the alias

    # The local embed path reports the fastembed name ("BAAI/bge-base-en-v1.5")
    # where collection names carry its token ("bge-base-en-v15-768").
    model = _MODEL_TOKENS.get(model, model)
    max_tokens = MODEL_MAX_TOKENS.get(model)
    if max_tokens is None:
        _log.warning(
            "embed_window_unknown_model",
            model=model,
            remedy="add it to nexus.embed_window.MODEL_MAX_TOKENS",
        )
        return None
    if max_tokens >= SAFE_CHUNK_BYTES:
        return None
    path = _tokenizer_path(model)
    if not path.is_file():
        # Loud, not fatal: the client can miss a file the engine still embeds
        # from (a Python-only NX_SERVICE_BGE_DIR, the suite's fenced $HOME),
        # and refusing to index there is worse than an unchecked window.
        _log.warning(
            "embed_window_tokenizer_missing",
            model=model,
            max_tokens=max_tokens,
            path=str(path),
            consequence="chunks for this model are not checked against its token window",
        )
        return None
    return TokenWindow(max_tokens, path)
