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

from typing import TYPE_CHECKING

from nexus.config import get_credential

if TYPE_CHECKING:
    # Type-only import. T3Database transitively pulls voyageai ->
    # transformers -> torch at module load. With this import at
    # runtime, every `from nexus.db.<sub> import ...` triggers
    # nexus.db.__init__ which fires the torch import (multi-second).
    # The CLI hits this path via nexus.cli -> nexus.commands.catalog
    # -> nexus.catalog.catalog -> nexus.catalog.catalog_db ->
    # `from nexus.db.t2 import _sanitize_fts5`. Lazy-loading the
    # T3Database import inside make_t3() removes torch from the cold-
    # start cost of `nx <subcommand>` invocations.
    from nexus.db.t3 import T3Database


def make_t3(*, _client=None, _ef_override=None) -> "T3Database":
    """Return a :class:`T3Database` built from the current credentials
    and the active ``NX_STORAGE_MODE``.

    Dispatch (RDR-120 P2):

    - ``NX_STORAGE_MODE=daemon`` + local mode + no injected ``_client``:
      route via ``nexus.daemon.t3_client.make_t3_client`` to the running
      T3 daemon (``chromadb.HttpClient`` against
      ``~/.config/nexus/t3_addr.<uid>`` or ``NX_T3_ADDR``).
    - ``NX_STORAGE_MODE=direct`` + local mode (or daemon mode with an
      injected ``_client`` for tests): backed by
      ``chromadb.PersistentClient`` with a ``LocalEmbeddingFunction``.
      No API keys required.
    - Cloud mode: backed by ``chromadb.CloudClient`` with Voyage AI
      embeddings, regardless of ``NX_STORAGE_MODE``. The daemon model
      does not apply in cloud mode; CloudClient is already HTTP-served.

    Keyword-only injection points (for tests):

    * ``_client`` — substitute an ``EphemeralClient`` or ``MagicMock``
      to avoid real CloudClient / HttpClient connections. Passing
      ``_client`` short-circuits the daemon-mode dispatch.
    * ``_ef_override`` — override the embedding function (e.g.
      ``DefaultEmbeddingFunction()``) to avoid Voyage AI API calls.
    """
    from nexus.config import (
        is_local_mode, load_config, _default_local_path, storage_mode,
    )
    # Runtime import of T3Database (was moved out of module-scope to
    # break the eager torch-import chain during CLI startup).
    from nexus.db.t3 import T3Database

    if (
        is_local_mode()
        and _client is None
        and storage_mode() == "daemon"
    ):
        # Daemon-mode dispatch (RDR-120 P2). T3Client construction
        # raises T3DaemonError when the daemon is unreachable; the
        # message names ``nx daemon t3 start`` as the operator fix.
        from nexus.daemon.t3_client import make_t3_client
        return make_t3_client()

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
