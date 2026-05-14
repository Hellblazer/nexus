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

import os
from typing import TYPE_CHECKING

from nexus.config import get_credential


class DaemonModeDiagnosticError(RuntimeError):
    """Raised when an admin/diagnostic path attempts a direct
    ``sqlite3.connect`` or ``chromadb.PersistentClient`` open while
    ``NX_STORAGE_MODE=daemon`` is set.

    Phase 1 (RDR-112 ``nexus-61x6``) hands T2 ownership to a daemon
    process that holds the SQLite WAL writer. A direct connect from a
    client process would race the daemon. The diagnostic / admin
    callers must route through the daemon's RPC surface in daemon
    mode; the legacy direct-open path is only valid in direct mode.
    """


def reject_under_daemon_mode(op_name: str) -> None:
    """Raise :class:`DaemonModeDiagnosticError` when daemon mode is set.

    Used at every direct ``sqlite3.connect`` callsite outside
    ``src/nexus/db/`` that targets the T2 ``memory.db`` (or its T3
    chroma equivalent). The function is a no-op when the env var is
    unset or non-daemon — direct-mode behaviour is unchanged.

    RDR-112 P1 prereq (Phase-0-gate foundation review, 2026-05-14).
    """
    if os.environ.get("NX_STORAGE_MODE", "").lower() == "daemon":
        raise DaemonModeDiagnosticError(
            f"{op_name} cannot open T2/T3 storage directly while "
            f"NX_STORAGE_MODE=daemon is set. Route through the daemon "
            f"RPC surface or run the command in direct mode."
        )

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
    # Runtime import of T3Database (was moved out of module-scope to
    # break the eager torch-import chain during CLI startup).
    from nexus.db.t3 import T3Database

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


def make_ephemeral_t3(*, ef=None) -> "T3Database":
    """Return a T3Database backed by an in-process ``EphemeralClient``.

    Used by ``nx index --dry-run`` and similar paths that need a
    throwaway T3 surface without touching cloud credentials or the
    persistent local store. Keeps the ``chromadb.EphemeralClient``
    construction inside ``src/nexus/db/`` per RDR-112 D3 (no client-
    process chroma construction outside this module).

    When ``ef`` is omitted, the bundled ONNX MiniLM
    ``DefaultEmbeddingFunction`` is used so callers get a working
    embedder without API keys.
    """
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    from nexus.db.t3 import T3Database

    embedder = ef or DefaultEmbeddingFunction()
    return T3Database(_client=chromadb.EphemeralClient(), _ef_override=embedder)
