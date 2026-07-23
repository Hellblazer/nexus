# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database tier package.

Public factory
--------------
``make_t3(**kwargs)`` — return the T3 vector-store handle.

RDR-155 P4a.2 (bead nexus-1k8s1, serving-path Chroma retire): with no
injected ``_client``, T3 serving routes through the pgvector-backed
nexus-service HTTP API in BOTH modes — the
:class:`~nexus.db.http_vector_client.HttpVectorClient` singleton. The
Chroma daemon leg (local mode) and the direct ``chromadb.CloudClient``
leg (cloud mode) are retired; the ONLY surviving Chroma constructors are
the Phase-5 ETL read legs in ``nexus.migration.chroma_read``.

Tests keep the same ``_client`` / ``_ef_override`` injection points: an
injected client short-circuits service dispatch and returns a
:class:`~nexus.db.t3.T3Database` facade over it, exactly as before.
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
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.db.t3 import T3Database


def make_ephemeral_chroma_client():
    """In-process, in-memory chroma client for session-scoped caches
    (nexus-373jo: the plan-match cache's substrate when T1 is
    service-backed and carries no chroma client).

    Lives HERE — ``src/nexus/db/`` is the storage-boundary lint's allowed
    home for substrate-client construction — precisely so callers need no
    ``# epsilon-allow`` token (the no-new-SQLite census freezes exemption
    growth; a structural home beats an exemption). NOT storage: nothing
    persists.

    SHARED-STATE GOTCHA (review-verified, the documented
    project_chromadb_ephemeral_shared_state class): EphemeralClient
    instances in one process share backing state (SharedSystemClient
    caches by settings hash). Callers must scope rows themselves (e.g. a
    session_id filter); instance boundaries are NOT isolation.

    P4b (Chroma retirement) rehoming target: engine-side embed. Tracked
    on nexus-373jo and the g37fr rehoming inventory.
    """
    import chromadb  # noqa: PLC0415 — deferred; heavy import on a rare init path

    return chromadb.EphemeralClient()


def make_t3(*, _client=None, _ef_override=None) -> "T3Database | HttpVectorClient":
    """Return the T3 vector-store handle for the current configuration.

    Dispatch (RDR-155 P4a.2 — serving-path Chroma retired):

    - **No injected** ``_client`` (production, both local and cloud
      mode): return the process-local
      :class:`~nexus.db.http_vector_client.HttpVectorClient`, which
      routes every vector op through the nexus-service ``/v1/vectors``
      HTTP API (pgvector storage, server-side embedding). Connection
      details come from ``NX_SERVICE_URL`` / ``NX_SERVICE_TOKEN`` at
      request time — constructing the handle performs no I/O.
    - **Injected** ``_client``: return a
      :class:`~nexus.db.t3.T3Database` facade over it.
      ``EphemeralClient`` is the canonical test substitute; the Phase-5
      ETL wraps the ``nexus.migration.chroma_read`` read legs the same
      way.

    Keyword-only injection points (for tests):

    * ``_client`` — substitute an ``EphemeralClient`` or ``MagicMock``
      to avoid real network connections. Passing ``_client``
      short-circuits service dispatch (used by every unit test that
      exercises the T3Database facade).
    * ``_ef_override`` — override the embedding function (e.g.
      ``DefaultEmbeddingFunction()``) to avoid Voyage AI API calls.
      Only meaningful together with ``_client``.
    """
    if _client is None:
        # RDR-155 P4a.2 (nexus-1k8s1): pgvector service serves T3 in both
        # modes. The local chroma-daemon leg (RDR-120) and the cloud
        # CloudClient leg are retired.
        from nexus.db.http_vector_client import get_http_vector_client  # noqa: PLC0415 — deferred to avoid circular import (http_vector_client)

        return get_http_vector_client()

    from nexus.config import load_config  # noqa: PLC0415 — deferred to avoid circular import (config)
    # Runtime import of T3Database (was moved out of module-scope to
    # break the eager torch-import chain during CLI startup).
    from nexus.db.t3 import T3Database  # noqa: PLC0415 — deferred to avoid circular import (db.t3)

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


