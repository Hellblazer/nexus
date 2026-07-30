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
leg (cloud mode) are retired, and the migration-ETL read legs died at
P4b P2 — no production code constructs a Chroma client any more.

Tests keep the same ``_client`` / ``_ef_override`` injection points: an
injected client short-circuits service dispatch and returns a
:class:`~nexus.db.t3.T3Database` facade over it, exactly as before.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.config import get_credential

if TYPE_CHECKING:
    from pathlib import Path

    # Type-only import. T3Database transitively pulls voyageai ->
    # transformers -> torch at module load. With this import at
    # runtime, every `from nexus.db.<sub> import ...` triggers
    # nexus.db.__init__ which fires the torch import (multi-second).
    # Any CLI subcommand importing under nexus.db hits this path.
    # Lazy-loading the T3Database import inside make_t3() removes
    # torch from the cold-start cost of `nx <subcommand>` invocations.
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.db.t3 import T3Database


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
      :class:`~nexus.db.t3.T3Database` facade over it (test-only since
      P4b P2 — the InMemoryVectorClient is the canonical substitute).

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


def read_legacy_catalog_counts(path: "Path") -> tuple[int | str, int | str]:
    """Count document / link rows in a FROZEN legacy catalog SQLite file.

    Read-only probe of a pre-PG migration SOURCE, for the ``nx doctor`` row
    that labels ``<config_dir>/catalog/.catalog.db`` as a frozen snapshot
    rather than a live mirror. The authoritative catalog is Postgres; these
    counts exist only so the row can say how much is in the snapshot.

    REHOMED from ``nexus.db.migrations`` in RDR-158 P4 Stage 4
    (nexus-i711w) — the migration registry is deleted; this doctor probe is
    the surviving consumer. It stays in the ``db/`` substrate for the same
    reason the original docstring gave: the storage-boundary lint counts raw
    ``sqlite3.connect`` sites outside ``db/`` and its epsilon-allow baseline
    is a DOWNWARD-only ratchet, so a caller-side connect would have to bump
    a count that is never permitted to rise.

    Returns ``("unknown", "unknown")`` for anything unreadable — a legacy
    file that cannot be opened must still be NAMED by the doctor row, never
    suppressed.

    Args:
        path: Path to the legacy ``.catalog.db``.

    Returns:
        ``(documents, links)`` — row counts, or ``"unknown"`` per table when
        the table is absent or the file cannot be read.
    """
    import sqlite3  # noqa: PLC0415 — read-only legacy-source probe; keep module import cheap

    docs: int | str = "unknown"
    links: int | str = "unknown"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for tbl, key in (("documents", "docs"), ("links", "links")):
                try:
                    n = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
                except sqlite3.Error:
                    continue
                if key == "docs":
                    docs = n
                else:
                    links = n
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — an unreadable legacy file must still be NAMED
        pass
    return docs, links


