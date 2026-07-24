# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-te885.8.1 — pg-source read leg for verify-fill (Phase 0 prereq, RDR-155 P4b).

A Chroma-shaped, READ-ONLY HTTP adapter over a LOCAL nexus-service pgvector
store. ``verify_fill_local`` / ``verify_fill_cloud`` (``vector_etl.py``) both
assume the source is a Chroma store; neither covers rows written directly to
local pgvector post-cutover that exist in no Chroma store at all — the exact
substrate behind the 2026-07-01 nexus-te885.1 incident (27,283 chunks,
previously reconciled once by an ad hoc manual script). This module presents
the SAME duck-typed interface ``nexus.db.reconcile.iter_collection_chunks``
already consumes, so ``verify_fill_collections`` needs zero changes to gain a
pg-source leg — only a new caller (``verify_fill_pg_source``, nexus-te885.8.2)
that opens THIS adapter instead of a Chroma client.

Locked design: T2 ``nexus/design-te885.8-pg-source-verify-fill.md``.

Constructed from an EXPLICIT ``(base_url, token)`` pair — deliberately NOT
via ``HttpVectorClient``'s process-wide singleton
(``nexus.db.http_vector_client.get_http_vector_client``), which resolves
exactly one endpoint per process via env/config.yml/lease and cannot be
pointed at a SECOND, different local service while ``HttpVectorClient``
itself is in use as the migration TARGET (cloud). This module makes its own
HTTP calls to the explicit URL; it does not import or depend on
``http_vector_client.py`` at all.

Embeddings CRITICAL (plan-audit finding, T1 scratch a40b2afd): the Java
engine's ``POST /v1/vectors/get`` (``VectorHandler.handleGet``) accepts and
SILENTLY IGNORES the ``include`` request field and NEVER returns embeddings,
regardless of what is asked for. The same-model passthrough migration path
(``_is_same_model_passthrough`` in ``vector_etl.py`` — exactly the
local-pgvector -> cloud te885.1 scenario this module exists for) calls
``iter_collection_chunks(..., include_embeddings=True)`` expecting real
vectors back. A naive single-endpoint mirror would silently yield
``embedding=None`` for every chunk, tripping the migration's fallback to a
BILLED Voyage re-embed — precisely the cost te885.1's ad hoc script existed
to avoid. This adapter's ``.get()`` therefore STITCHES two calls when
embeddings are requested: ``/v1/vectors/get`` for ids/documents/metadatas,
then ``/v1/vectors/get-embeddings`` for the embeddings of those same ids.

Alignment hazard: ``POST /v1/vectors/get-embeddings``
(``VectorHandler.handleGetEmbeddings``) DROPS ids it cannot find, returning
fewer rows than requested, in request order, with no id-correlation beyond
that ordering (Chroma parity per the Java docstring — "the Python caller
detects the count mismatch"). The stitch below keys returned embeddings back
to their id by building an ``id -> embedding`` map from the (possibly
shorter) response, then looks each REQUESTED id up in that map — any id the
embeddings call dropped yields ``embedding=None`` for that specific chunk,
never a positional/shifted misassignment onto a different chunk's id.

This module lives in ``src/nexus/migration/`` — the ENTIRE package is slated
for deletion at RDR-155 P4b. Self-contained by design; no dependents outside
``migration/``.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import structlog

_log = structlog.get_logger(__name__)

#: Default per-call timeouts, mirroring http_vector_client's _post/_get.
_POST_TIMEOUT = 120
_GET_TIMEOUT = 30


class PgReadError(RuntimeError):
    """Raised when the local pg-source nexus-service returns an error.

    ``code`` carries the HTTP status when available (``None`` for a
    transport-level failure) — mirrors
    ``nexus.db.http_vector_client.VectorServiceError`` in shape without
    importing that module (this adapter has no dependency on it).
    """

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _post(
    base_url: str,
    token: str,
    path: str,
    body: dict[str, Any],
    *,
    tenant: str = "default",
    timeout: int = _POST_TIMEOUT,
) -> Any:
    """POST JSON to *base_url + path*, return the parsed response body.

    Module-level (not a method) so tests can monkeypatch it directly, per
    the ``tests/db/test_http_vector_client.py`` convention.
    """
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    import urllib.request  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Nexus-Tenant": tenant,
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — explicit local-service URL, not user-controlled
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:  # noqa: BLE001 — error-body decode is best-effort; fall back to raw bytes
            err = {"error": body_bytes.decode(errors="replace")}
        raise PgReadError(
            f"POST {path} -> HTTP {e.code}: {err.get('error', err)}", code=e.code
        ) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise PgReadError(f"POST {path} failed: {e}") from e


def _get(
    base_url: str,
    token: str,
    path: str,
    *,
    tenant: str = "default",
    timeout: int = _GET_TIMEOUT,
) -> Any:
    """GET from *base_url + path*, return the parsed response body."""
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    import urllib.request  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    headers = {"Authorization": f"Bearer {token}", "X-Nexus-Tenant": tenant}
    req = urllib.request.Request(base_url + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — explicit local-service URL, not user-controlled
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err = json.loads(body_bytes)
        except Exception:  # noqa: BLE001 — error-body decode is best-effort; fall back to raw bytes
            err = {"error": body_bytes.decode(errors="replace")}
        raise PgReadError(
            f"GET {path} -> HTTP {e.code}: {err.get('error', err)}", code=e.code
        ) from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise PgReadError(f"GET {path} failed: {e}") from e


class _CollectionRef:
    """Mirrors Chroma's ``client.list_collections()`` element: a ``.name``-bearing handle."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _PgCollectionHandle:
    """Chroma ``Collection``-shaped handle backed by explicit-URL HTTP calls."""

    def __init__(self, name: str, base_url: str, token: str, *, tenant: str = "default") -> None:
        self.name = name
        self._base_url = base_url
        self._token = token
        self._tenant = tenant

    def count(self) -> int:
        """Chunk count — Chroma ``Collection.count()`` parity."""
        result = _get(
            self._base_url,
            self._token,
            "/v1/vectors/count?collection=" + quote(self.name),
            tenant=self._tenant,
        )
        return int(result.get("count", 0))

    def get(
        self,
        include: list[str] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Chroma ``Collection.get()`` parity: ``{ids, documents, metadatas, embeddings}``.

        ``POST /v1/vectors/get`` always returns ids/documents/metadatas
        regardless of ``include`` (the server ignores that field — see
        module docstring), so those three keys are always populated here.
        ``embeddings`` is ``None`` unless ``"embeddings"`` is in *include*,
        in which case the two-endpoint stitch fires (see
        :meth:`_fetch_embeddings`).
        """
        include = include or []
        body = {"collection": self.name, "limit": limit, "offset": offset}
        result = _post(self._base_url, self._token, "/v1/vectors/get", body, tenant=self._tenant)
        ids = list(result.get("ids") or [])
        out: dict[str, Any] = {
            "ids": ids,
            "documents": list(result.get("documents") or []),
            "metadatas": list(result.get("metadatas") or []),
        }
        if "embeddings" in include:
            out["embeddings"] = self._fetch_embeddings(ids) if ids else []
        else:
            out["embeddings"] = None
        return out

    def _fetch_embeddings(self, ids: list[str]) -> list[list[float] | None]:
        """Stitch call 2: fetch embeddings for *ids*, aligned back by id.

        ``/v1/vectors/get-embeddings`` may return FEWER rows than requested
        (dropped ids), in request order, with no id-correlation guarantee
        beyond that ordering. Build an ``id -> embedding`` map from
        whatever came back, then look up each REQUESTED id — a dropped id
        yields ``None`` for that specific position, never a positional
        shift that would misalign a different chunk's vector onto it.
        """
        result = _post(
            self._base_url,
            self._token,
            "/v1/vectors/get-embeddings",
            {"collection": self.name, "ids": ids},
            tenant=self._tenant,
        )
        returned_ids = result.get("ids") or []
        returned_embeddings = result.get("embeddings") or []
        if len(returned_ids) != len(ids):
            _log.info(
                "pg_read_embeddings_dropped_ids",
                collection=self.name,
                requested=len(ids),
                returned=len(returned_ids),
            )
        by_id = dict(zip(returned_ids, returned_embeddings))
        return [by_id.get(chunk_id) for chunk_id in ids]


class PgReadClient:
    """Chroma-``client``-shaped read-only handle over a local nexus-service.

    Presents ``list_collections()`` / ``get_collection(name)`` exactly as
    ``nexus.db.reconcile.iter_collection_chunks`` /
    ``list_collection_names`` already consume, so those functions work
    against this adapter unmodified.
    """

    def __init__(self, base_url: str, token: str, *, tenant: str = "default") -> None:
        if not base_url:
            raise ValueError("base_url is required (explicit local-service URL)")
        if not token:
            raise ValueError("token is required (explicit local-service token)")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._tenant = tenant
        _log.info("pg_read_client_opened", base_url=self._base_url, tenant=tenant)

    def list_collections(self) -> list[_CollectionRef]:
        """All collections visible to this tenant — Chroma ``client.list_collections()`` parity."""
        result = _get(self._base_url, self._token, "/v1/vectors/collections", tenant=self._tenant)
        return [
            _CollectionRef(entry["name"])
            for entry in (result or [])
            if isinstance(entry, dict) and entry.get("name")
        ]

    def get_collection(self, name: str) -> _PgCollectionHandle:
        """Chroma ``client.get_collection(name)`` parity."""
        return _PgCollectionHandle(name, self._base_url, self._token, tenant=self._tenant)
