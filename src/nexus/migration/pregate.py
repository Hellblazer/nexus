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

**RDR-180 land-then-transform (nexus-jxizy.10.8).** The legacy-id width BLOCK
that used to fire here (GH #1390 / nexus-sot7v: a pre-RDR-108 non-32/64-char
chunk id) is RETIRED — land-then-transform rehashes ``chunk_text`` server-side
to derive the canonical chash, so a 16-char or 32-hex source id no longer
needs to survive verbatim; it MIGRATES (the GH #1408 population). The width
classification that block used is repurposed as a read-only LANDING-MANIFEST
input (:func:`landing_width_manifest`), not a gate. What still genuinely
blocks is a collection whose sampled chunks carry NO TEXT AT ALL — nothing to
rehash from — via the ``no_text`` check in :func:`assert_models_supported`.
The pre-gate also exposes a disk-headroom preflight
(:func:`assert_disk_headroom`, design R6): staging temporarily doubles the
largest store, so landing needs free space >= the estimated source size.

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

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

import structlog

from nexus.migration.detection import (
    CollectionClassification,
    DetectionReport,
    WidthEra,
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


#: RDR-180 Q4 residual (nexus-jxizy.10.8): the ONE thing land-then-transform
#: still cannot migrate is a collection with no chunk text to rehash from.
#: A static reason (unlike the legacy-id block's per-collection detection
#: reason) since no_text carries no per-collection detail beyond the name,
#: which ``ModelPreGateBlocked`` already prints.
_NO_TEXT_REASON = (
    "collection has no chunk text in the sampled rows — RDR-180 "
    "land-then-transform derives the canonical chash by rehashing "
    "chunk_text server-side and there is nothing to rehash from "
    "(un-derivable); re-index this collection from its original source "
    "before migrating"
)


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
    no_text: frozenset[str] | set[str] = frozenset(),
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

    ``no_text`` (RDR-180 Q4 residual, nexus-jxizy.10.8) is the set of
    data-bearing collection names a caller has determined (via
    :func:`nexus.migration.detection.probe_has_text`) carry NO chunk text at
    all in the sampled rows. Land-then-transform rehashes ``chunk_text``
    server-side, so such a collection is genuinely un-derivable — it blocks
    REGARDLESS of ``exempt`` or model support, the same precedence the
    now-retired legacy-id width block held. The legacy-id width itself no
    longer gates (see the module docstring): a 16-char or 32-hex source id
    migrates by rehash, not verbatim copy.
    """
    src = source if source is not None else LiveServiceWiredModels()
    wired = resolve_wired_models(src, voyage_key_present=voyage_key_present)

    blocked: list[tuple[str, str]] = []
    for c in classifications:
        if not c.has_data:
            continue
        # RDR-180 Q4 residual: a no-text collection blocks BEFORE the
        # model-support check and REGARDLESS of exempt — same precedence the
        # retired legacy-id block held, because no_text is a data property
        # (nothing to rehash from) the model-support recomputation below
        # cannot see.
        if c.collection in no_text:
            blocked.append((c.collection, _NO_TEXT_REASON))
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


def landing_width_manifest(
    classifications: Iterable[CollectionClassification],
    probe: Callable[[CollectionClassification], WidthEra | None],
) -> dict[str, WidthEra]:
    """Per-data-bearing-collection width era, as LANDING-MANIFEST input.

    RDR-180 design Q4 item 3: the legacy-id width check used to be a
    pre-gate BLOCK (the removed check documented in
    :func:`assert_models_supported`'s module docstring); under land-then-
    transform it is repurposed into a read-only manifest the sequencer's
    landing phase and the rehearsal's non-vacuity asserts consume (e.g. "the
    rehearsal MUST include >=1 sixteen-char legacy id resolving
    post-promote").

    ``probe`` is injected (mirrors :class:`WiredModelSource`) — the real
    caller closes over a live-opened collection and
    :func:`nexus.migration.detection.probe_width_era`; tests pin the
    manifest-building logic with a pure per-classification lookup, no Chroma
    required. Empty collections are skipped (nothing to land); a probe
    returning ``None`` (unknown — probe failure or empty sample) is omitted
    from the manifest rather than guessed.
    """
    manifest: dict[str, WidthEra] = {}
    for c in classifications:
        if not c.has_data:
            continue
        era = probe(c)
        if era is not None:
            manifest[c.collection] = era
    return manifest


#: Escape hatch for :func:`assert_disk_headroom` — an operator who has
#: verified real headroom is sufficient (e.g. thin-provisioned storage) can
#: bypass the block. Logged loud (never a silent pass).
NX_STAGING_DISK_OVERRIDE_ENV = "NX_STAGING_DISK_OVERRIDE"


class StagingDiskPreflightBlocked(RuntimeError):
    """Raised before landing when the PG staging volume lacks headroom.

    RDR-180 design R6: staging temporarily DOUBLES the largest store (the
    source lands verbatim before promote); landing onto a volume with less
    free space than the estimated source size risks an out-of-disk failure
    mid-land. See :data:`NX_STAGING_DISK_OVERRIDE_ENV` for the escape hatch.
    """


def estimate_staging_source_bytes(
    sqlite_paths: Iterable[Path], chroma_dir: Path | None = None
) -> int:
    """Rough size estimate for the land-then-transform disk preflight.

    Sum of the source SQLite file sizes (the T2 stores: catalog, memory,
    ...) plus the local Chroma directory (when present — a cloud-only leg
    has no local footprint to size). Best-effort: a path that does not
    exist contributes zero rather than raising — a leg the user never
    populated should not block the estimate.
    """
    total = 0
    for p in sqlite_paths:
        if p.exists() and p.is_file():
            total += p.stat().st_size
    if chroma_dir is not None and chroma_dir.exists():
        total += sum(
            f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file()
        )
    return total


def assert_disk_headroom(
    *,
    estimated_bytes: int,
    pg_path: Path,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    override_env: str = NX_STAGING_DISK_OVERRIDE_ENV,
) -> None:
    """Pre-gate: BLOCK landing when free disk on *pg_path* < *estimated_bytes*.

    RDR-180 design R6 — staging temporarily doubles the largest store; this
    is the disk-headroom half of the mitigation (the other half,
    TRUNCATE-after-verify, is owned by the promote/clear phases, not this
    check). ``disk_usage`` is injected (default :func:`shutil.disk_usage`)
    so tests pin the block/pass decision without touching a real
    filesystem. Reads ``override_env`` directly from the process
    environment — set to ``"1"`` to bypass, logged loud, never silent.
    """
    if os.environ.get(override_env, "").strip() == "1":
        _log.warning(
            "disk_preflight_overridden",
            estimated_bytes=estimated_bytes,
            pg_path=str(pg_path),
            override_env=override_env,
        )
        return
    free_bytes = disk_usage(pg_path).free
    if free_bytes < estimated_bytes:
        _log.warning(
            "disk_preflight_blocked",
            estimated_bytes=estimated_bytes,
            free_bytes=free_bytes,
            pg_path=str(pg_path),
        )
        raise StagingDiskPreflightBlocked(
            f"migration blocked: staging landing needs an estimated "
            f"{estimated_bytes:,} byte(s) free on {pg_path} (staging "
            "temporarily doubles the largest store) but only "
            f"{free_bytes:,} byte(s) are free (no data has been touched). "
            f"Free up disk space, or set {override_env}=1 to proceed anyway "
            "— only if you have verified the real headroom is sufficient "
            "(e.g. thin-provisioned storage)."
        )
    _log.info(
        "disk_preflight_clear",
        estimated_bytes=estimated_bytes,
        free_bytes=free_bytes,
        pg_path=str(pg_path),
    )


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
