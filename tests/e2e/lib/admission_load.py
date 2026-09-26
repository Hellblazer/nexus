# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Driver for ``tests/e2e/admission-load-gate.sh`` (nexus-u2mlh.9).

Proves that the engine's CCE admission control
(``admission_refusals_total``) or its request-deadline abort
(``deadline_aborts_total``) actually fires, in the cloud, through the
public edge, under concurrent embed load — the close check for
nexus-u2mlh.2 and its epic nexus-u2mlh.

Two design choices worth reading before changing this file:

1. **Raw HTTP, never ``HttpVectorClient.upsert_chunks()``.** That client's
   own layered retry (``nexus.retry._vector_with_retry`` on top of
   ``http_vector_client._request``'s inner gateway retry) transparently
   retries a plain 503 refusal up to three times *inside* one call before
   any of it reaches caller code, and even the one 503 shape that DOES
   propagate immediately — a deadline abort, ``X-Nexus-Deadline-Outcome:
   aborted`` — loses its headers by the time ``VectorServiceError`` reaches
   the caller (it carries only ``code``/``edge_refusal``). The evidence
   this gate exists to produce — the raw status code, ``Retry-After``, and
   ``X-Nexus-Deadline-Outcome`` per attempt — needs one un-retried POST per
   request, so :func:`post_upsert` talks to ``/v1/vectors/upsert-chunks``
   directly. The shared process-wide rate-limit brake
   (``nexus.rate_brake``) is deliberately never engaged by this path
   either — it exists to pace concurrent indexer workers, and this driver
   wants every ramp step to land at once.

2. **Collection lifecycle reuses the real write path's own functions**
   (``nexus.corpus.t3_collection_name`` /
   ``ensure_collection_registered``, ``nexus.db.collection_purge
   .purge_collection_cascade``) rather than re-deriving names or hand-
   rolling a delete — the exact functions a normal indexing write and the
   interactive collection-delete verb call, so this throwaway collection
   is minted and torn down identically to production usage. Both of those
   calls go through the engine's own write guard
   (``nexus.db.service_endpoint.guard_production_write``) internally; only
   the raw upsert POST above bypasses it, so :func:`run_gate` calls the
   guard explicitly once before firing any load.

Pure functions (no network, unit-tested in ``tests/test_admission_load
_gate.py``): counter extraction/comparison, ramp-stop decision,
non-vacuity check, nonce/subject/document generation, response
summarization. Everything that resolves a live endpoint or sends a
request is a thin, deliberately un-pure wrapper around those.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

#: The one embedder every CCE write in this gate targets (knowledge
#: collections embed with voyage-context-3 — see AGENTS.md's collection
#: table).
DEFAULT_EMBEDDER = "voyage-context-3"

#: Concurrency steps tried, in order, stopping at the first that moves a
#: counter. bead nexus-u2mlh.9's own sizing note: thread_width=12 today and
#: the edge clamps the request deadline to ~50s on this route, so admission
#: refusal needs on the order of 70-200 concurrent document-sized upserts —
#: comfortably inside the 256 ceiling below.
DEFAULT_RAMP_STEPS: tuple[int, ...] = (32, 64, 128, 256)

#: Document-size bounds (bytes) — "document-sized", per the bead: large
#: enough that a single request is a real CCE batch, small enough that 256
#: of them in flight is a load test, not a memory test.
MIN_DOCUMENT_BYTES = 6 * 1024
MAX_DOCUMENT_BYTES = 24 * 1024
DEFAULT_TARGET_BYTES = 12 * 1024

#: Advisory client-declared embed budget (milliseconds), matching
#: ``HttpVectorClient``'s own ``_UPSERT_CHUNKS_DEADLINE_MS`` for this route
#: — the public edge clamps it to ~50s regardless of what is sent
#: (conexus-26b5), so this value only matters for a box where the clamp is
#: ever lifted.
DEFAULT_REQUEST_DEADLINE_MS = 540_000

#: Per-request socket timeout: comfortably above the edge's ~50s deadline
#: clamp so a genuine 503 (refused fast, or aborted at the deadline) is
#: observed rather than a client-side read timeout racing it.
DEFAULT_REQUEST_TIMEOUT_S = 70.0

#: The one tenant every install uses today (see
#: ``http_vector_client._process_default_tenant``'s own docstring: a named
#: constant, not a real per-install lookup).
TENANT = "default"

#: Subject-naming grammar, docs/collections.md Rule 3: lowercase,
#: hyphen-separated, ASCII, two-to-three words.
_SUBJECT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){1,2}$")


class AdmissionLoadVacuousError(RuntimeError):
    """No admission/deadline counter moved at any ramp step tried."""


# ── Pure: counters ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbedderCounters:
    admission_refusals_total: int = 0
    deadline_aborts_total: int = 0
    queue_depth: int = 0
    chunks_done_total: int = 0
    thread_width: int = 0


def snapshot_counters(status: dict[str, Any] | None, embedder: str = DEFAULT_EMBEDDER) -> EmbedderCounters:
    """Extract *embedder*'s counters from a ``GET /v1/status`` body.

    Tolerant of a missing/malformed body — returns all-zero counters
    rather than raising, matching ``fetch_engine_status``'s own
    fail-closed contract (this module never treats an unprobeable status
    read as a crash; the caller's ramp loop treats it as "nothing moved").
    """
    if not isinstance(status, dict):
        return EmbedderCounters()
    activity = status.get("embedder_activity")
    if not isinstance(activity, dict):
        return EmbedderCounters()
    entry = activity.get(embedder)
    if not isinstance(entry, dict):
        return EmbedderCounters()

    def _int(key: str) -> int:
        value = entry.get(key, 0)
        return value if isinstance(value, int) else 0

    return EmbedderCounters(
        admission_refusals_total=_int("admission_refusals_total"),
        deadline_aborts_total=_int("deadline_aborts_total"),
        queue_depth=_int("queue_depth"),
        chunks_done_total=_int("chunks_done_total"),
        thread_width=_int("thread_width"),
    )


def counters_moved(before: EmbedderCounters, after: EmbedderCounters) -> bool:
    """True when either the admission-refusal or deadline-abort counter
    advanced between two snapshots — the one predicate this whole gate
    exists to make true at least once."""
    return (
        after.admission_refusals_total > before.admission_refusals_total
        or after.deadline_aborts_total > before.deadline_aborts_total
    )


def counters_delta(before: EmbedderCounters, after: EmbedderCounters) -> dict[str, int]:
    """Human-readable before/after evidence for one ramp step."""
    return {
        "admission_refusals_total": after.admission_refusals_total - before.admission_refusals_total,
        "deadline_aborts_total": after.deadline_aborts_total - before.deadline_aborts_total,
        "chunks_done_total": after.chunks_done_total - before.chunks_done_total,
    }


# ── Pure: ramp planning + non-vacuity ───────────────────────────────────


def parse_ramp_steps(raw: str) -> list[int]:
    """Parse a comma-separated concurrency list, e.g. ``"32,64,128,256"``.

    Raises ``ValueError`` on an empty list or one that is not strictly
    increasing (a non-increasing ramp cannot be read as "escalating load"
    and would make the stop-at-first-movement logic ambiguous).
    """
    steps = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not steps:
        raise ValueError("ramp-steps must name at least one concurrency value")
    if steps != sorted(steps) or len(set(steps)) != len(steps):
        raise ValueError(f"ramp-steps must be strictly increasing, got {steps}")
    return steps


@dataclass(frozen=True)
class RampOutcome:
    tried: tuple[int, ...]
    stopped_at_step: int | None
    stopped_at_index: int | None


def decide_ramp_outcome(steps: Sequence[int], moved: Sequence[bool]) -> RampOutcome:
    """Pure ramp-stop decision: the first step where *moved* is True wins;
    an all-``False`` *moved* is the non-vacuity failure this gate must
    name explicitly rather than pass silently."""
    if len(steps) != len(moved):
        raise ValueError(f"steps/moved length mismatch: {len(steps)} vs {len(moved)}")
    for index, did_move in enumerate(moved):
        if did_move:
            return RampOutcome(tuple(steps), steps[index], index)
    return RampOutcome(tuple(steps), None, None)


def require_non_vacuous(outcome: RampOutcome) -> None:
    """Raise :class:`AdmissionLoadVacuousError` when no ramp step moved
    either counter — the vacuous-gate doctrine (nexus-moht0): a sweep that
    found nothing to check is a failure, not a pass."""
    if outcome.stopped_at_step is None:
        raise AdmissionLoadVacuousError(
            "no admission_refusals_total or deadline_aborts_total movement "
            f"observed at any concurrency step tried: {list(outcome.tried)}"
        )


# ── Pure: nonce, subject naming, document generation ────────────────────


def make_run_nonce(source: Callable[[], str] | None = None) -> str:
    """A lowercase-alnum run identifier, unique per invocation by default
    (``uuid4().hex[:10]``). *source* is an injection seam for deterministic
    tests — it must itself already return a lowercase-alnum string."""
    nonce = (source() if source is not None else uuid.uuid4().hex[:10]).lower()
    if not re.fullmatch(r"[a-z0-9]+", nonce):
        raise ValueError(f"run nonce must be lowercase alnum, got {nonce!r}")
    return nonce


def load_collection_subject(nonce: str) -> str:
    """The throwaway knowledge-collection SUBJECT for this run
    (``u2mlh-load-<nonce>``) — docs/collections.md Rule 3 shape (lowercase,
    hyphen-separated, ASCII, two-to-three words). Never the rendered
    four-segment name; that resolution needs the real embed-profile
    resolver and lives in :func:`resolve_collection_name`, exercised only
    at gate runtime."""
    subject = f"u2mlh-load-{nonce}"
    if not _SUBJECT_RE.match(subject):
        raise ValueError(f"generated subject {subject!r} does not fit docs/collections.md Rule 3's grammar")
    return subject


def generate_document(nonce: str, tag: str, target_bytes: int = DEFAULT_TARGET_BYTES) -> str:
    """A unique, document-sized text body for one load-step request.

    Content is derived from *nonce* + *tag* so no two calls — even
    concurrent workers within the same ramp step — can collide on chunk
    identity: the server's existing-chash embed-skip (RDR-181) must never
    elide this call's embed, or the ramp would silently stop generating
    load without any client-visible symptom.
    """
    if not (MIN_DOCUMENT_BYTES <= target_bytes <= MAX_DOCUMENT_BYTES):
        raise ValueError(
            f"target_bytes {target_bytes} outside the documented "
            f"[{MIN_DOCUMENT_BYTES}, {MAX_DOCUMENT_BYTES}] range"
        )
    header = f"admission-load-gate probe run={nonce} tag={tag}\n\n"
    parts: list[str] = [header]
    size = len(header.encode("utf-8"))
    n = 0
    while size < target_bytes:
        line = f"line {n} nonce={nonce} tag={tag} filler text sized for a real CCE embed batch\n"
        parts.append(line)
        size += len(line.encode("utf-8"))
        n += 1
    return "".join(parts)


def load_document_id(nonce: str, tag: str, target_bytes: int = DEFAULT_TARGET_BYTES) -> str:
    """Content-addressed chunk id for this call's own document — computed
    the same way a real indexer would (sha256 hex of the exact chunk
    text), so the id sent on the wire matches production's own identity
    convention (AGENTS.md: chunk natural id is the full 64-hex
    ``chunk_text_hash``)."""
    text = generate_document(nonce, tag, target_bytes)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Pure: raw-503 evidence rollup ───────────────────────────────────────


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    retry_after: float | None
    deadline_outcome: str | None
    elapsed_s: float
    error: str | None = None


def summarize_responses(responses: Sequence[RawResponse]) -> dict[str, Any]:
    """Pure evidence rollup over one ramp step's raw responses: counts by
    status code and by ``X-Nexus-Deadline-Outcome`` — the raw-503 evidence
    the client library's own transparent retry would otherwise hide (see
    this module's docstring, point 1)."""
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    errors = 0
    for r in responses:
        key = str(r.status_code)
        by_status[key] = by_status.get(key, 0) + 1
        if r.deadline_outcome:
            by_outcome[r.deadline_outcome] = by_outcome.get(r.deadline_outcome, 0) + 1
        if r.error:
            errors += 1
    return {
        "count": len(responses),
        "by_status": by_status,
        "by_deadline_outcome": by_outcome,
        "transport_errors": errors,
    }


# ── Network: endpoint, collection lifecycle, raw upsert ─────────────────


def resolve_cloud_endpoint() -> tuple[str, str]:
    """``(base_url, token)`` for this box's configured service, via the
    same evidence-gated resolver ``GET /v1/status`` reads through
    (``nexus.db.http_engine_status.fetch_engine_status``)."""
    from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred: keeps this module importable with zero network deps for the pure-function tests
        resolve_service_endpoint_with_evidence_gate,
    )

    return resolve_service_endpoint_with_evidence_gate()


def fetch_status(base_url: str, token: str, *, timeout: float = 20.0) -> dict[str, Any] | None:
    """``GET /v1/status``, fail-closed to ``None`` on any error — mirrors
    ``nexus.db.http_engine_status.fetch_engine_status``'s own contract."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/v1/status", headers=headers, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception:  # noqa: BLE001 — fail-closed: a transport blip is "unprobeable", never a crash
        return None
    return body if isinstance(body, dict) else None


def resolve_collection_name(subject: str) -> str:
    """The rendered four-segment collection name this box would mint for
    *subject* — the SAME resolver every real write path uses
    (``nexus.corpus.t3_collection_name``), so the name this gate writes
    under is exactly what a normal write targeting *subject* would use.
    Pure/network-free with no live probe (``t3=None``): no grandfathering
    onto a pre-existing collection is possible or desired for a brand-new
    throwaway subject."""
    from nexus.corpus import t3_collection_name  # noqa: PLC0415 — deferred: keeps this module importable with zero nexus.* deps for the pure-function tests

    return t3_collection_name(subject, for_write=True)


def register_load_collection(name: str) -> None:
    """Register *name* the same way every write path does before its
    first chunk lands (RDR-204 Phase 1: the engine no longer
    auto-registers on first write)."""
    from nexus.corpus import ensure_collection_registered  # noqa: PLC0415 — deferred, see resolve_collection_name

    ensure_collection_registered(name)


def delete_load_collection(name: str) -> dict[str, Any]:
    """The engine's own delete-and-cascade-purge for *name* — functionally
    identical to the interactive collection-delete verb's own call,
    invoked directly so this driver never shells out to a CLI subprocess."""
    from nexus.db import make_t3  # noqa: PLC0415 — deferred, see resolve_collection_name
    from nexus.db.collection_purge import purge_collection_cascade  # noqa: PLC0415 — deferred, see resolve_collection_name

    cascade = purge_collection_cascade(make_t3(), name)
    return {"t3_absent": cascade.t3_absent, "failures": list(cascade.failures)}


def post_upsert(
    client: httpx.Client,
    collection: str,
    nonce: str,
    tag: str,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    deadline_ms: int = DEFAULT_REQUEST_DEADLINE_MS,
) -> RawResponse:
    """One raw, un-retried POST to ``/v1/vectors/upsert-chunks``.

    Deliberately bypasses ``HttpVectorClient.upsert_chunks`` — see this
    module's docstring, point 1, for why: that client's layered retry
    silently absorbs the exact evidence (raw status + ``Retry-After`` +
    ``X-Nexus-Deadline-Outcome`` per attempt) this gate exists to observe.
    """
    text = generate_document(nonce, tag, target_bytes)
    doc_id = load_document_id(nonce, tag, target_bytes)
    body = {"collection": collection, "ids": [doc_id], "documents": [text], "metadatas": [{}]}
    headers = {"X-Nexus-Request-Deadline-Ms": str(deadline_ms)}
    t0 = time.monotonic()
    try:
        resp = client.post("/v1/vectors/upsert-chunks", content=json.dumps(body).encode("utf-8"), headers=headers)
    except Exception as exc:  # noqa: BLE001 — a transport failure is evidence for this gate, never a crash
        return RawResponse(status_code=0, retry_after=None, deadline_outcome=None, elapsed_s=time.monotonic() - t0, error=str(exc)[:200])
    elapsed = time.monotonic() - t0
    retry_after_raw = resp.headers.get("Retry-After")
    try:
        retry_after = float(retry_after_raw) if retry_after_raw is not None else None
    except ValueError:
        retry_after = None
    return RawResponse(
        status_code=resp.status_code,
        retry_after=retry_after,
        deadline_outcome=resp.headers.get("X-Nexus-Deadline-Outcome"),
        elapsed_s=elapsed,
    )


def fire_step(
    client: httpx.Client,
    collection: str,
    nonce: str,
    step_index: int,
    concurrency: int,
    *,
    target_bytes: int = DEFAULT_TARGET_BYTES,
) -> list[RawResponse]:
    """Fire *concurrency* concurrent raw upserts (one document each) and
    collect every response — never fail-fast: a single worker's transport
    error must not hide the other 255 workers' evidence."""
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(post_upsert, client, collection, nonce, f"{step_index}-{i}", target_bytes=target_bytes)
            for i in range(concurrency)
        ]
        return [f.result() for f in futures]


# ── Orchestration ────────────────────────────────────────────────────────


def run_gate(
    *,
    dry_run: bool,
    ramp_steps: Sequence[int] = DEFAULT_RAMP_STEPS,
    embedder: str = DEFAULT_EMBEDDER,
    target_bytes: int = DEFAULT_TARGET_BYTES,
    nonce_source: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Run (or, with ``dry_run=True``, only plan) the whole ramp.

    Never raises: every failure mode — endpoint resolution, the write
    guard, registration, the ramp itself, cleanup — is caught and folded
    into the returned ``{"passed": bool, "reason": str, ...}`` result, so
    the caller always gets a clean verdict instead of a bare traceback.
    Cleanup (the collection delete) runs in a ``finally`` whenever a
    collection name was ever resolved, regardless of how the run failed.
    """
    nonce = make_run_nonce(nonce_source)
    subject = load_collection_subject(nonce)
    plan: dict[str, Any] = {
        "nonce": nonce,
        "subject": subject,
        "ramp_steps": list(ramp_steps),
        "embedder": embedder,
        "target_bytes": target_bytes,
    }

    if dry_run:
        plan["collection_name"] = resolve_collection_name(subject)
        return {
            "passed": True,
            "dry_run": True,
            "reason": "dry-run: plan only, no network touched",
            "plan": plan,
        }

    name: str | None = None
    steps_tried: list[int] = []
    moved_flags: list[bool] = []
    evidence: list[dict[str, Any]] = []
    error: str | None = None
    cleanup: dict[str, Any] = {"attempted": False}

    try:
        base_url, token = resolve_cloud_endpoint()

        from nexus.db.service_endpoint import guard_production_write  # noqa: PLC0415 — deferred, see resolve_collection_name

        guard_production_write(base_url)

        name = resolve_collection_name(subject)
        plan["collection_name"] = name
        print(f"[admission-load] plan: {json.dumps(plan)}")

        register_load_collection(name)

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": TENANT,
            "Content-Type": "application/json",
        }
        with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=DEFAULT_REQUEST_TIMEOUT_S) as client:
            for step_index, concurrency in enumerate(ramp_steps):
                before = snapshot_counters(fetch_status(base_url, token), embedder)
                t0 = time.monotonic()
                responses = fire_step(client, name, nonce, step_index, concurrency, target_bytes=target_bytes)
                elapsed = time.monotonic() - t0
                after = snapshot_counters(fetch_status(base_url, token), embedder)
                moved = counters_moved(before, after)
                steps_tried.append(concurrency)
                moved_flags.append(moved)
                step_evidence = {
                    "concurrency": concurrency,
                    "before": dataclasses.asdict(before),
                    "after": dataclasses.asdict(after),
                    "delta": counters_delta(before, after),
                    "moved": moved,
                    "responses": summarize_responses(responses),
                    "elapsed_s": round(elapsed, 2),
                }
                evidence.append(step_evidence)
                print(f"[admission-load] step {json.dumps(step_evidence)}")
                if moved:
                    break
    except Exception as exc:  # noqa: BLE001 — surfaced as a FAILED verdict, never a bare traceback
        error = str(exc)
    finally:
        if name is not None:
            cleanup["attempted"] = True
            try:
                cleanup.update(delete_load_collection(name))
                cleanup["ok"] = not cleanup.get("failures")
            except Exception as exc:  # noqa: BLE001 — cleanup failure is reported, never masked
                cleanup["ok"] = False
                cleanup["error"] = str(exc)
        else:
            cleanup["ok"] = True
            cleanup["skipped_reason"] = "no collection name was ever resolved; nothing to clean up"

    result: dict[str, Any] = {"plan": plan, "steps": evidence, "cleanup": cleanup}

    if error is not None:
        result["passed"] = False
        result["reason"] = f"error during load: {error}"
        return result

    outcome = decide_ramp_outcome(steps_tried, moved_flags)
    try:
        require_non_vacuous(outcome)
    except AdmissionLoadVacuousError as exc:
        result["passed"] = False
        result["reason"] = str(exc)
        return result

    if not cleanup.get("ok", False):
        result["passed"] = False
        result["reason"] = (
            f"load succeeded (stopped at concurrency={outcome.stopped_at_step}) "
            f"but cleanup did not confirm success: {cleanup}"
        )
        return result

    result["passed"] = True
    result["stopped_at_step"] = outcome.stopped_at_step
    result["reason"] = f"admission/deadline counter moved at concurrency={outcome.stopped_at_step}"
    return result


# ── CLI ────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run the admission-load ramp against the resolved cloud endpoint.")
    run.add_argument("--dry-run", action="store_true", help="Print the plan; touch no network.")
    run.add_argument(
        "--ramp-steps",
        default=",".join(str(s) for s in DEFAULT_RAMP_STEPS),
        help="Comma-separated concurrency steps, strictly increasing.",
    )
    run.add_argument("--embedder", default=DEFAULT_EMBEDDER)
    run.add_argument("--target-bytes", type=int, default=DEFAULT_TARGET_BYTES)
    run.add_argument(
        "--result-file",
        default=None,
        help="Write the JSON result here (the wrapper script reads it for the FAILED reason).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        ramp_steps = parse_ramp_steps(args.ramp_steps)
        result = run_gate(
            dry_run=args.dry_run,
            ramp_steps=ramp_steps,
            embedder=args.embedder,
            target_bytes=args.target_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — belt-and-suspenders: run_gate already catches its own failures; this covers a bug in the CLI plumbing around it
        result = {"passed": False, "reason": f"driver error: {exc}"}

    if args.result_file:
        with open(args.result_file, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, default=str)

    verdict = "PASSED" if result.get("passed") else "FAILED"
    print(f"[admission-load] {verdict}: {result.get('reason')}")
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
