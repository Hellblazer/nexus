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
    """Return a :class:`T3Database` built from the current credentials.

    Dispatch (RDR-120 P6 — direct mode decommissioned):

    - **Local mode + no injected** ``_client``: route via
      ``nexus.daemon.t3_client.make_t3_client`` to the running T3
      daemon (``chromadb.HttpClient`` against
      ``~/.config/nexus/t3_addr.<uid>`` or ``NX_T3_ADDR``). The daemon
      MUST be running; the client raises ``T3DaemonError`` naming
      ``nx daemon t3 start`` on connection failure.
    - **Cloud mode**: backed by ``chromadb.CloudClient`` with Voyage AI
      embeddings. The daemon model does not apply in cloud mode;
      CloudClient is already HTTP-served.
    - **Injected** ``_client``: short-circuits daemon dispatch.
      ``EphemeralClient`` is the canonical test substitute.

    Keyword-only injection points (for tests):

    * ``_client`` — substitute an ``EphemeralClient`` or ``MagicMock``
      to avoid real CloudClient / HttpClient connections. Passing
      ``_client`` short-circuits daemon dispatch (used by every unit
      test that does not spin up its own daemon).
    * ``_ef_override`` — override the embedding function (e.g.
      ``DefaultEmbeddingFunction()``) to avoid Voyage AI API calls.
    """
    from nexus.config import is_local_mode, load_config
    # Runtime import of T3Database (was moved out of module-scope to
    # break the eager torch-import chain during CLI startup).
    from nexus.db.t3 import T3Database

    if is_local_mode() and _client is None:
        # RDR-120 P6 (nexus-qg86h): direct mode decommissioned. Local
        # mode always routes through the T3 daemon; the legacy
        # ``PersistentClient`` + ``LocalEmbeddingFunction`` direct-open
        # path is deleted. Tests that need a non-daemon local backend
        # inject ``_client`` (typically ``EphemeralClient``).
        from nexus.daemon.t3_client import make_t3_client
        return make_t3_client()

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
