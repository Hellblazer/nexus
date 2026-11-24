# SPDX-License-Identifier: AGPL-3.0-or-later
"""The vector service's error type, in a leaf module.

It lives here, not in ``nexus.db.http_vector_client``, so that ``nexus.collection_errors`` (which that module imports) can
subclass it: ``RetiredNoSuccessorError`` is a ``VectorServiceError`` with ``code=409``, so every handler written for the
engine's 409 keeps working. ``nexus.db.http_vector_client.VectorServiceError`` is this class, re-exported.
"""
from __future__ import annotations


class VectorServiceError(RuntimeError):
    """Raised when the vector service returns an error.

    ``code`` carries the HTTP status when the failure was an HTTP error
    response (404 from an older service JAR, 422 model-unavailable, ...);
    ``None`` for transport-level failures. Callers use it for
    deployment-skew fallbacks (RDR-156 P3: /stats absent on a pre-catalog-005
    JAR → fall back to /collections + /count).
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        edge_refusal: bool = False,
        reason: str | None = None,
        engine_body: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        #: The engine's decoded error body when it sent a JSON object, else
        #: ``None``. A typed error carries fields beyond ``reason`` that a caller
        #: acts on (``nothing_moved`` / ``audit_ids`` on the restore verb's
        #: ``quarantine_restore_busy`` 503, nexus-wbfpw.55); a caller reads them
        #: with ``.get`` since an older engine sends none of them.
        self.engine_body = engine_body
        #: nexus-bgvnx: the engine's stable machine-readable ``reason`` from a
        #: typed error body (``unregistered_collection`` on the 422 for a
        #: collection with no catalog row); ``None`` when the body carried
        #: none, which includes every engine that predates the field.
        self.reason = reason
        #: nexus-1jtob: True when the EDGE (AWS ALB/WAF) generated this
        #: response and the application never saw the request. Deterministic
        #: in the request body — a caller must not retry it.
        self.edge_refusal = edge_refusal
