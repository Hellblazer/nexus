# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database tier package.

Public factory
--------------
``make_t3(**kwargs)`` — construct a :class:`~nexus.db.t3.T3Database` from
the configured credentials.  Accepts the same ``_client`` and
``_ef_override`` keyword injection arguments as ``T3Database.__init__`` so
tests can pass a fake client without hitting ChromaDB Cloud.
"""
from __future__ import annotations

from nexus.config import get_credential
from nexus.db.t3 import T3Database


def make_t3(*, _client=None, _ef_override=None) -> T3Database:
    """Return a :class:`T3Database` built from the current credentials.

    In local mode (``is_local_mode()`` returns True), returns a T3Database
    backed by ``chromadb.PersistentClient`` with a ``LocalEmbeddingFunction``.
    No API keys required.

    In cloud mode, returns a T3Database backed by ``chromadb.CloudClient``
    with Voyage AI embeddings.

    Keyword-only injection points (for tests):

    * ``_client`` — substitute an ``EphemeralClient`` or ``MagicMock`` to
      avoid real CloudClient connections.
    * ``_ef_override`` — override the embedding function (e.g.
      ``DefaultEmbeddingFunction()``) to avoid Voyage AI API calls.
    """
    from nexus.config import is_local_mode, load_config, _default_local_path

    if is_local_mode() and _client is None:
        from nexus.db.local_ef import LocalEmbeddingFunction
        import os

        model_override = os.environ.get("NX_LOCAL_EMBED_MODEL", "")
        ef = _ef_override or LocalEmbeddingFunction(
            model_name=model_override if model_override else None,
        )
        return T3Database(
            local_mode=True,
            local_path=str(_default_local_path()),
            _ef_override=ef,
        )

    cfg = load_config()
    read_timeout_seconds: float = cfg.get("voyageai", {}).get("read_timeout_seconds", 120.0)
    return T3Database(
        tenant=get_credential("chroma_tenant"),
        database=get_credential("chroma_database"),
        api_key=get_credential("chroma_api_key"),
        voyage_api_key=get_credential("voyage_api_key"),
        read_timeout_seconds=read_timeout_seconds,
        _client=_client,
        _ef_override=_ef_override,
    )
