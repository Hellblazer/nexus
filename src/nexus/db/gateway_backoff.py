# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared gateway-transient-retry backoff shape (nexus-1ytp6 / nexus-r46u9).

T3 (``http_vector_client.py``'s ``_request``) and T2/catalog
(``db/t2/_refreshable_client.py``'s ``RefreshableHttpStoreMixin``)
round-trip through two different HTTP transports (urllib vs httpx)
against the SAME managed edge, and both need the SAME 502/503/504
backoff shape. Originally ``http_vector_client.py`` owned the schedule
and the mixin imported it from there (nexus-1ytp6, a one-directional
dependency); nexus-r46u9 moved it to THIS leaf module (imports nothing
from ``nexus.db``, so neither of the two callers imports the other)
once the embed-write 504 floor below turned out to be needed in BOTH
places — a Voyage slowdown produces the identical premature-re-embed
hazard whether the write reaches the engine via
``/v1/vectors/upsert-chunks``/``/store-put`` (T3) or via the catalog's
combined-write ``/v1/catalog/manifest/write_many`` (T2/catalog).
``http_vector_client.py`` keeps thin re-exports of the old
underscore-prefixed names for the tests that reference them that way;
``db/t2/_refreshable_client.py`` imports straight from here.
"""
from __future__ import annotations

#: Backoff schedule for gateway-transient HTTP codes (502/503/504). Found by
#: the nexus-duoak.4 scaling sweep: concurrent CCE upsert batches slow
#: server-side embedding past the gateway timeout, and a single unretried 504
#: killed an entire ``nx index repo`` run. Upserts are idempotent
#: (content-addressed), so bounded retry is safe for every /v1 call family.
_GATEWAY_RETRY_SLEEPS: tuple[float, ...] = (2.0, 5.0, 10.0)
_GATEWAY_RETRY_CODES = frozenset({502, 503, 504})

#: The edge's OWN upstream request-timeout bound for the three embed-write
#: routes below, as measured at the time this floor was added (conexus,
#: 2026-09-14). A 504 on one of those routes means the edge cut the
#: request off at this bound while the engine's SYNCHRONOUS server-side
#: embed was still running, not that the engine gave up.
#:
#: Sizing evidence (bead nexus-r46u9, a Voyage-slowdown incident): a
#: 64-chunk CCE ``/v1/vectors/upsert-chunks`` batch (matching
#: ``http_vector_client._CCE_UPSERT_CHUNK_CAP``) took ~35s server-side
#: under that slowdown and finished successfully every one of 44 times
#: measured — so the RESIDUAL work remaining after the edge's cutoff was
#: only ~5s in that particular storm (35s - 30s). That 5s is NOT what
#: :data:`_EMBED_WRITE_504_BACKOFF_FLOOR_S` below is sized to: the
#: residual scales with how bad the slowdown is, not a fixed 5s. The
#: floor is instead sized so a batch taking up to TWICE this bound
#: (60s total server-side) finishes before the first resend arrives —
#: cutoff at 30s, plus a 30s floored sleep, = 60s elapsed.
#:
#: conexus PR #358 (merged, not yet deployed as of this comment) raises
#: the edge's upstream bound for these same three routes to 55s. That
#: lengthens the REQUEST before a 504 can fire at all — it does not
#: change the RESIDUAL after a 504, which is what the floor below exists
#: to cover — so the floor does NOT move when that deploys, and stays
#: derived from THIS constant (the bound this floor was actually sized
#: against), not the pending 55s one. Per conexus's explicit ask: this is
#: also not a reason to raise ``http_vector_client._CCE_UPSERT_CHUNK_CAP``
#: — a longer edge bound is headroom for an existing slowdown, not a
#: license for bigger batches.
_EDGE_UPSTREAM_BOUND_S = 30.0

#: nexus-r46u9: floor (seconds) for a gateway-retry sleep that follows a 504
#: on a server-side-embedding write route (see
#: :data:`_EMBED_SERVER_SIDE_WRITE_PATH_SUFFIXES` below). DERIVED from
#: :data:`_EDGE_UPSTREAM_BOUND_S` (see that constant's docstring for the
#: sizing evidence and why PR #358's pending edge-bound bump does not move
#: this value) rather than a bare literal, so "how long until the edge
#: could 504" and "how long to wait before resending" stay visibly tied
#: at the definition site. 77 of the incident's 101 edge timeouts were on
#: the combined write (``/v1/catalog/manifest/write_many`` with inline
#: chunks) specifically — the headline number, and why this floor is
#: shared rather than T3-only. Upserts/writes here are content-addressed
#: (idempotent), so a duplicate resend is not data corruption -- just
#: wasted Voyage cost/time during the exact slowdown that caused the 504
#: in the first place, which this floor gives the in-flight attempt real
#: headroom to finish before a duplicate is fired. Applies to 504 only --
#: 502/503 (and 504 on every other route) keep the schedule above
#: unchanged; see :func:`is_embed_server_side_write_path`.
_EMBED_WRITE_504_BACKOFF_FLOOR_S = _EDGE_UPSTREAM_BOUND_S

#: Path suffixes (matched by ``path.endswith``, the same convention
#: ``http_vector_client._T3_WRITE_PATH_SUFFIXES`` uses) for routes where the
#: engine performs a SYNCHRONOUS server-side embed as part of the write
#: itself -- exactly the routes :data:`_EMBED_WRITE_504_BACKOFF_FLOOR_S`
#: exists for. THREE such routes, spanning both HTTP clients:
#:
#:   * ``/v1/vectors/upsert-chunks``   -- ``http_vector_client.py``,
#:     ``HttpVectorClient.upsert_chunks``. Always embeds.
#:   * ``/v1/vectors/store-put``       -- ``http_vector_client.py``,
#:     ``HttpVectorClient.put``. Always embeds.
#:   * ``/v1/catalog/manifest/write_many`` -- ``catalog/http_catalog_client.py``,
#:     ``HttpCatalogClient.write_manifest_many`` (the "combined write",
#:     RDR-195/nexus-kl2z6/nexus-wxjr6). CONDITIONAL: embeds only when the
#:     POST body carries a non-empty top-level ``chunks`` list --
#:     ``write_manifest_many`` sets ``body["chunks"]`` on the first page
#:     ONLY, and only when the caller supplied chunks. A manifest-only
#:     write_many page never triggers a server-side embed and finishes
#:     well under the edge's ~30s cutoff, so it structurally cannot 504
#:     for this reason and must not pay the floor. See
#:     :func:`is_embed_server_side_write_path` for how that conditionality
#:     is applied.
_EMBED_SERVER_SIDE_WRITE_PATH_SUFFIXES: tuple[str, ...] = (
    "/upsert-chunks",
    "/store-put",
    "/manifest/write_many",
)

_WRITE_MANY_SUFFIX = "/manifest/write_many"


def _is_embed_server_side_write_path(path: str, body: dict | None = None) -> bool:
    """True when a 504 on *path* means the engine may still be finishing a
    synchronous server-side embed for THIS request, and so should be given
    :data:`_EMBED_WRITE_504_BACKOFF_FLOOR_S` before a retry re-sends it.

    *body* is the outgoing POST body/payload dict. Only the T2/catalog
    client's ``_once_with_gateway_retry`` (``db/t2/_refreshable_client.py``,
    which forwards ``kwargs.get("json")``) ever passes it, and only
    ``/manifest/write_many`` reads it — to tell a chunk-carrying
    combined-write page (embeds) from a manifest-only page (does not); see
    :data:`_EMBED_SERVER_SIDE_WRITE_PATH_SUFFIXES`'s docstring.
    ``http_vector_client.py``'s ``_request`` never reaches
    ``/manifest/write_many`` at all (that route is catalog-only) and never
    passes *body*, matching the default: the classifier never consults it
    for ``/upsert-chunks``/``/store-put`` either, since both always embed
    regardless of body content. The T2 client's own ``_post`` always
    populates ``kwargs["json"]`` before this function is ever called for a
    write_many page, so no LIVE caller reaches ``/manifest/write_many``
    with *body* unset — there is deliberately no special case for that
    combination here.
    """
    if not any(path.endswith(suffix) for suffix in _EMBED_SERVER_SIDE_WRITE_PATH_SUFFIXES):
        return False
    if path.endswith(_WRITE_MANY_SUFFIX):
        return bool(body.get("chunks"))
    return True
