# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client's version, as the engine reads it (RDR-223 Phase 3 Step 2, nexus-z0o2p.24).

Every request sent through the vector client, the T2 and catalog clients and the T1 scratch store
carries ``X-Nexus-Client-Version: <conexus version>``. Three senders do NOT: the hook-side calls
(``hooks/mailbox_drain.py``, ``hooks/tuple_ledger_project.py``) and the ``/v1/status`` probe
(``db/http_engine_status.py``). Importing this module costs ~690 ms cold, because ``nexus.db``'s
package init runs first, and the hooks fire on every prompt and tool call; none of the three writes a
chunk, so the soak below is unaffected. The engine logs it on
the ``ownerless_chunk_write_refused`` / ``ownerless_chunk_write_would_refuse`` lines and records
``absent`` when it is missing, so the log-only soak can name which clients still write a chunk before
its owner: a request WITHOUT the header comes from a client older than the release that added it.
The ``User-Agent`` cannot do this job: it is ``Python-urllib/3.x`` or ``python-httpx/0.x``, the
transport and not the product.

A leaf module on purpose: ``db.http_vector_client`` (urllib), ``db.t2._refreshable_client`` (httpx; the
T2 stores and the catalog client) and ``db.http_scratch_store`` (httpx) all import it, and none
may import another for it.
"""
from __future__ import annotations

from functools import lru_cache

#: The request header, spelled as the engine (``VectorHandler.CLIENT_VERSION_HEADER``) reads it.
CLIENT_VERSION_HEADER: str = "X-Nexus-Client-Version"


@lru_cache(maxsize=1)
def client_version() -> str:
    """The installed conexus version, or ``unknown`` when the distribution metadata is absent
    (a bare source tree). Cached: it cannot change inside a process."""
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415 — deferred: CLI cold-start cost

    try:
        return version("conexus")
    except PackageNotFoundError:
        return "unknown"


def client_identity_headers() -> dict[str, str]:
    """The headers that name this client to the engine."""
    return {CLIENT_VERSION_HEADER: client_version()}
