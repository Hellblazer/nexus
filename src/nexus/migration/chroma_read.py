# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-155 P4a.2 (bead nexus-1k8s1) — the surviving Chroma READ client.

The Phase-4a serving retire removed every Chroma client construction from
the live serving/query/storage paths; this module is the ONE place in
``src/nexus`` still allowed to construct them (locked by
``tests/test_rdr155_p4a_serving_retire.py``). It exists for exactly one
consumer: the Phase-5 migration ETL (beads nexus-unp61 / nexus-9n4pn),
which reads every chunk out of the legacy Chroma stores and loads it into
the pgvector-backed nexus-service.

Both read legs are REQUIRED (RDR-155 §Migrate — an ETL with only one leg
is a silent half-migration):

* **Local leg** — ``chromadb.PersistentClient`` over the on-disk store the
  retired local daemon served (default ``~/.config/nexus/chroma``).
* **Cloud leg** — ``chromadb.CloudClient`` against the configured
  ChromaCloud tenant/database.

READ-ONLY BY CONVENTION: this module exposes open + iterate helpers only.
No upsert/delete wrappers — writes go to pgvector through the service.
Reads page at ``QUOTAS`` limits (``chroma_quotas`` survives Phase 4a to
govern exactly this leg; its deletion is Phase 4b, gated on P5.G).

Full deletion of this module (and the quotas it leans on) is Phase 4b
(nexus-19svb / nexus-g37fr), gated on P5.G migration completion.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from nexus.db.chroma_quotas import QUOTAS  # noqa: F401  — dying-set pin (test_rdr155_p4b_quotas_rehome); pagers rehomed to db.reconcile

_log = structlog.get_logger(__name__)


def open_local_read_client(local_path: str | Path | None = None) -> Any:
    """Open the LOCAL read leg: a ``chromadb.PersistentClient`` on *local_path*.

    The on-disk store the retired T3 daemon used to serve. The caller owns
    single-process discipline: chromadb's WAL races when two
    PersistentClients open the same store concurrently, so the ETL must be
    the only opener (the serving path no longer opens it — that is the
    point of Phase 4a).

    A ``None`` *local_path* resolves via
    :func:`nexus.migration.detection.resolve_default_local_leg` — the
    product's own env-aware local-Chroma default (nexus-id750, GH #1381).
    This is the DEEP chokepoint of the default: pre-fix, a bare
    ``nx guided-upgrade`` threaded an un-defaulted ``None`` from the CLI all
    the way down to this constructor, crashing on ``Path(None)`` at the ETL
    copy step — AFTER provisioning a service (critique CRITICAL).
    """
    if local_path is None:
        from nexus.migration.detection import resolve_default_local_leg  # noqa: PLC0415 — circular-dep avoidance (detection imports this module deferred)

        local_path = resolve_default_local_leg()
    p = Path(local_path)
    if not p.is_dir():
        raise FileNotFoundError(
            f"local Chroma store not found at {p} — nothing to migrate, or the "
            "path is wrong (default: $NX_LOCAL_CHROMA_PATH -> "
            "$XDG_DATA_HOME/nexus/chroma -> ~/.local/share/nexus/chroma)"
        )
    import chromadb  # noqa: PLC0415  — optional/heavy dependency deferred (chromadb)

    client = chromadb.PersistentClient(path=str(p))  # epsilon-allow: RDR-155 P5 ETL local read leg — the ONE surviving local Chroma constructor (P4a.1 contract)
    _log.info("chroma_read_local_opened", path=str(p))
    return client


def open_cloud_read_client(
    tenant: str = "",
    database: str = "",
    api_key: str = "",
) -> Any:
    """Open the CLOUD read leg: a ``chromadb.CloudClient``.

    Empty arguments fall back to the configured credentials
    (``nx config set chroma_*``), mirroring the retired serving
    constructor's behaviour so existing deployments migrate without
    re-plumbing credentials.
    """
    from nexus.config import get_credential  # noqa: PLC0415  — command-local import (nexus.config)

    tenant = tenant or get_credential("chroma_tenant")
    database = database or get_credential("chroma_database")
    api_key = api_key or get_credential("chroma_api_key")
    if not (database and api_key):
        raise RuntimeError(
            "ChromaCloud read leg needs chroma_database + chroma_api_key "
            "(nx config set chroma_database/chroma_api_key) — refusing a "
            "half-configured cloud read"
        )
    import chromadb  # noqa: PLC0415  — optional/heavy dependency deferred (chromadb)

    client = chromadb.CloudClient(  # epsilon-allow: RDR-155 P5 ETL cloud read leg — the ONE surviving CloudClient constructor (P4a.1 contract)
        tenant=tenant or None, database=database, api_key=api_key
    )
    # The serving path's stalled-read hazard applies to the ETL too:
    # chromadb hardcodes httpx.Client(timeout=None).
    from nexus.db.t3 import _apply_chroma_http_timeout  # noqa: PLC0415  — command-local import (nexus.db.t3)

    _apply_chroma_http_timeout(client)
    _log.info("chroma_read_cloud_opened", tenant=tenant, database=database)
    return client


# Substrate-neutral pagers REHOMED to nexus.db.reconcile (nexus-jg74b):
# they operate on any Chroma-SHAPED client (PgReadClient included) and
# survive the Chroma wave. Re-exported here as a shim so this module's
# dying consumers (vector_etl, etl_ports, collision_audit + their tests)
# stay untouched and delete whole-file with it at P2.
from nexus.db.reconcile import (  # noqa: E402, F401  — re-export shim (dies with this module)
    iter_collection_chunks,
    list_collection_names,
)
