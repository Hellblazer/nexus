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

    Keyword-only injection points (for tests):

    * ``_client`` — substitute an ``EphemeralClient`` or ``MagicMock`` to
      avoid real CloudClient connections.
    * ``_ef_override`` — override the embedding function (e.g.
      ``DefaultEmbeddingFunction()``) to avoid Voyage AI API calls.
    """
    return T3Database(
        tenant=get_credential("chroma_tenant"),
        database=get_credential("chroma_database"),
        api_key=get_credential("chroma_api_key"),
        voyage_api_key=get_credential("voyage_api_key"),
        _client=_client,
        _ef_override=_ef_override,
    )
