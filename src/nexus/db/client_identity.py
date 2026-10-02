# SPDX-License-Identifier: AGPL-3.0-or-later
"""The client's version, as the engine reads it (RDR-223 Phase 3 Step 2, nexus-z0o2p.24).

``X-Nexus-Client-Version: <conexus version>`` is sent by the shared client classes, not by every engine
request: ``HttpVectorClient`` (urllib), ``RefreshableHttpStoreMixin`` (httpx; the T2 stores and the catalog client),
``HttpTokenStore`` (httpx) and ``HttpScratchStore`` (httpx). A request that does not go through one of
them does not send it: the hook calls, the status, version, whoami and health probes, the token mint
and the daemon's admin calls. None of those writes a chunk, so the soak below is unaffected, and
``tests/test_z0o2p24_client_version_header.py`` classifies the HTTP call sites in ``src/nexus`` as a
sender or a named non-sender, so a new one fails until it is placed. A call site there is a call that
resolves, through the module's own imports, to an entry point of httpx, requests, aiohttp, urllib3,
``urllib.request`` or ``http.client``, a ``curl`` or ``wget`` command run through ``subprocess`` or ``os``,
or an ``importlib.import_module`` of one of those libraries; ``getattr(httpx, name)`` and a command
assembled from fragments are not seen. Hooks stay non-senders on cost:
importing this module from a hook added about 55 ms warm (median 122 ms against 64-68 ms for the hook's
own imports) and about 200-320 ms with no bytecode cache, because ``nexus.db``'s package init runs
first, and hooks fire on every prompt and tool call. Method (2026-10-01, macOS, Python 3.12.11): the wall
time of ``python -c "import ..."`` in a fresh interpreter, median of 15; "no bytecode cache" points
``PYTHONPYCACHEPREFIX`` at a new empty directory for every sample. The engine logs it on
the ``ownerless_chunk_write_refused`` / ``ownerless_chunk_write_would_refuse`` lines and records
``absent`` when it is missing, so the log-only soak can name which clients still write a chunk before
its owner: a request WITHOUT the header comes from a client older than the release that added it.
The ``User-Agent`` cannot do this job: it is ``Python-urllib/3.x`` or ``python-httpx/0.x``, the
transport and not the product.

A leaf module on purpose: ``db.http_vector_client`` (urllib), ``db.t2._refreshable_client`` (httpx; the
T2 stores and the catalog client), ``db.t2.http_token_store`` and ``db.http_scratch_store`` (httpx) all
import it, and none may import another for it.
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
