# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P1d (nexus-ue6g7.12): the per-collection-model pre-gate, idempotent
service-stack provisioning, and the fresh-user no-op.

Before any ETL call, the guided migration confirms every data-bearing
collection's embedding model is actually servable. The authority is the LIVE
service ``EmbedderRouter`` registry — the ``/version`` handshake's
``embedding_models`` list (``EmbedderRouter.availableModels()``) — resolved by
the service, NOT a static onnx-vs-voyage assumption on the client. This is a
belt-and-suspenders confirmation over the P0 pure :func:`detection.wired_models`
function (which is the source of truth pre-provisioning): when the service is
unreachable the gate falls back to the pure deployment-mode floor, never to
something weaker.

Gate decisions (per data-bearing collection, RF-2 / gates S1 + C3):

* model wired by the live service → PROCEED (onnx is wired in every mode);
* voyage model not wired (service has no voyage embedder) → BLOCK with the
  credential diagnostic (add ``NX_VOYAGE_API_KEY`` to the service) — gate C3;
* a model wired by no service embedder (e.g. a legacy minilm-384 collection,
  retired from the service by RDR-160) → BLOCK with the re-index
  diagnostic — gate S1.

A mixed store fires the gate only on its unsupported subset. Empty collections
are not gated (nothing to migrate). A fresh user with no data-bearing leg is a
clean no-op success.

**Intended P2 call order (the seam this phase exposes).** The P2 orchestrator
must sequence the P1 primitives as: (1) ``state.begin_migration`` so every
separate process observes ``migrating`` and suspends FIRST; (2)
``quiesce.assert_quiescent_for_migration`` so no foreign write-lock survives the
now-visible sentinel; (3) ``assert_models_supported`` so an unservable model
fails before any ETL; only then drive the ETL. Setting the sentinel before the
write-lock audit is load-bearing — auditing first leaves a window where a worker
starts a cycle between the audit and the sentinel write. The ordering is a
P1→P2 contract; P1 ships the primitives, P2 owns the sequencing.
"""
from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

import structlog

from nexus.migration.detection import (
    CollectionClassification,
    DetectionReport,
    classify_model_support,
    wired_models,
)

_log = structlog.get_logger(__name__)


@runtime_checkable
class WiredModelSource(Protocol):
    """Source of the set of embedding-model tokens the service has wired.

    Returns ``None`` when the live set cannot be obtained (service down / older
    service without the ``/version`` handshake), signalling the caller to fall
    back to the pure deployment-mode floor.
    """

    def wired_models(self) -> frozenset[str] | None: ...


class LiveServiceWiredModels:
    """Default :class:`WiredModelSource`: the live ``EmbedderRouter`` registry.

    Reads the running service's ``/version`` handshake (``embedding_models``)
    via the same discovery every HTTP storage client uses. Any failure —
    no lease, unreachable service, malformed payload — degrades to ``None`` so
    the pre-gate falls back to the pure floor rather than failing the migration
    on a transient probe error.
    """

    def wired_models(self) -> frozenset[str] | None:
        try:
            from urllib.parse import urlsplit  # noqa: PLC0415 — branch-local; only on wired-model probe

            from nexus.daemon.binary_lifecycle import fetch_service_version  # noqa: PLC0415 — circular-dep avoidance; daemon pulls heavy deps
            from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — branch-local probe helper

            # Scheme-aware: a managed TLS endpoint (NX_SERVICE_URL=https://…:443)
            # must reach the service over https; the old (host, port) path
            # hard-coded http and broke the handshake before the data path ran
            # (nexus-n3bwh). resolve_service_endpoint preserves the scheme.
            base_url, _token = resolve_service_endpoint()
            parsed = urlsplit(base_url)
            handshake = fetch_service_version(
                parsed.hostname, parsed.port, scheme=parsed.scheme
            )
        except Exception as exc:  # noqa: BLE001 - probe must never raise upward
            _log.debug("pregate_live_wired_unreachable", error=str(exc))
            return None
        if not isinstance(handshake, dict):
            return None
        models = handshake.get("embedding_models")
        if not isinstance(models, list) or not models:
            return None
        return frozenset(str(m) for m in models)


def resolve_wired_models(
    source: WiredModelSource, *, voyage_key_present: bool
) -> frozenset[str]:
    """Return the live wired set, or the pure deployment-mode floor if absent.

    Belt-and-suspenders: when the live registry is reachable it is AUTHORITATIVE
    and used verbatim — it reflects what the service actually wired and may be
    STRICTER or MORE PERMISSIVE than the client-side deployment-mode floor (e.g.
    a service started without a Voyage key returns ``{onnx}`` even when the
    caller's ``voyage_key_present`` is True; the live set then correctly blocks
    voyage collections the floor would have passed). The pure :func:`wired_models`
    floor is the fallback used ONLY when the live set is unreachable. The live
    set always includes ONNX (wired in every service mode), so onnx collections
    are never blocked by this resolution; voyage support tracks the live service,
    not the client assumption.
    """
    live = source.wired_models()
    if live is not None:
        return live
    return wired_models(voyage_key_present=voyage_key_present)


class ModelPreGateBlocked(RuntimeError):
    """Raised before any ETL when one or more collections are unservable.

    ``.blocked`` is the ``(collection, diagnostic)`` list; ``.collections`` is
    the affected-collection names for a terse operator summary.
    """

    def __init__(self, blocked: list[tuple[str, str]]) -> None:
        self.blocked = blocked
        self.collections = [c for c, _ in blocked]
        lines = "\n".join(f"  - {c}: {reason}" for c, reason in blocked)
        super().__init__(
            f"migration blocked: {len(blocked)} collection(s) use an embedding "
            "model the service cannot serve. Resolve each before migrating "
            "(no data has been touched):\n" + lines
        )


def assert_models_supported(
    classifications: list[CollectionClassification] | tuple[CollectionClassification, ...],
    *,
    voyage_key_present: bool,
    source: WiredModelSource | None = None,
    exempt: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Pre-gate: BLOCK before any ETL if a data-bearing collection is unservable.

    Each data-bearing collection's model is re-resolved against the live wired
    set (falling back to the pure floor). Empty collections are skipped. Raises
    :class:`ModelPreGateBlocked` listing every affected collection; returns
    ``None`` when every data-bearing collection is servable.

    ``exempt`` (RDR-162 P2) is the set of collection names the orchestrator will
    cross-model migrate (stored-text re-embed into a model-remapped target).
    They are unsupported under their CURRENT name but are NOT blocked — the ETL
    re-embeds their text into a servable target. The gate still blocks every
    other unsupported collection (voyage-no-key, non-conformant names).
    """
    src = source if source is not None else LiveServiceWiredModels()
    wired = resolve_wired_models(src, voyage_key_present=voyage_key_present)

    blocked: list[tuple[str, str]] = []
    for c in classifications:
        if not c.has_data:
            continue
        # GH #1390 / nexus-sot7v: legacy non-32-char chunk ids block BEFORE
        # the model-support check and REGARDLESS of exempt — the ids are the
        # identity the pgvector side keys on, no migration path (verbatim copy
        # OR cross-model re-embed) rewrites them, and the failure is a data
        # property that the model-support recomputation below cannot see (a
        # legacy-id collection with a supported-model NAME — e.g. the
        # canon-chat bge chunks_768 — would otherwise pass here and only fail
        # mid-ETL). Uses the detection's actionable re-index / do-NOT-drop-
        # constraints reason, not the model diagnostic.
        if c.legacy_ids:
            blocked.append((c.collection, c.reason))
            continue
        if c.collection in exempt:
            continue
        support, reason = classify_model_support(
            c.model, voyage_key_present=voyage_key_present, wired=wired
        )
        if support == "unsupported":
            blocked.append((c.collection, reason))

    if blocked:
        _log.warning(
            "pregate_blocked",
            collections=[c for c, _ in blocked],
            wired=sorted(wired),
        )
        raise ModelPreGateBlocked(blocked)
    _log.info("pregate_clear", wired=sorted(wired))


def ensure_service_stack(
    *, is_up: Callable[[], bool], start: Callable[[], None]
) -> bool:
    """Idempotently ensure the service stack is up. Returns ``True`` iff it was
    started here, ``False`` when it was already up (a no-op).

    Callables are injected so the orchestrator wires the real probes
    (``nx daemon service start`` / a health check) and tests stay hermetic.
    """
    if is_up():
        _log.info("ensure_service_stack_already_up")
        return False
    _log.info("ensure_service_stack_starting")
    start()
    return True


def is_fresh_user(report: DetectionReport) -> bool:
    """True iff the detected footprint has no data-bearing leg.

    A fresh user (no Chroma at all, or only empty collections) is a clean
    no-op: there is nothing to migrate and the whole flow succeeds without
    touching the service.
    """
    return not report.legs_with_data
