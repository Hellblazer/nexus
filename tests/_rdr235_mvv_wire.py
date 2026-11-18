# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The RDR-235 MVV journey's observers (``tests/test_rdr235_mvv_aspect_queue_tuples.py``): the wire ledger,
the pause watch and the plain-traceback switch. They observe and report; none of them changes what the
worker or the engine does."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import pytest

#: The queue's tuple-space subspace (the journey's ``_SUB``).
SUBSPACE = "aspects"


def _ts(value: str | None) -> datetime:
    assert value, "the engine must render this timestamp"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def plain_tracebacks() -> None:
    """Render logged exceptions as plain text for the rest of this test (conftest's autouse
    ``_restore_structlog_after_test`` puts the suite's config back).

    A mitigation, not a cure. The suite's default structlog renderer draws every ``exc_info`` as a rich
    traceback with each frame's locals, the heaviest allocation burst inside any of the journey's claim
    windows (measured for nexus-bpikw, 2026-11-17: 30-50 ms idle, up to 0.5 s on a loaded chas lab-m grant; plain text, a few ms). CI run 44056253456
    lapsed document 7's claim during such a render: for about 3 s neither the worker nor the renewer
    logged or sent anything, a pause of the process inferred from the log, and the renew and then the nack answered ClaimNotFound. What paused the process (a
    GC pass, a cgroup or host stall) is not identified; ``PauseWatch`` reports it next time. Production
    renders exceptions as plain text too (``logging_setup.configure_logging``: ``format_exc_info``).
    """
    import structlog  # noqa: PLC0415 — test-local

    procs = structlog.get_config()["processors"]
    swapped = [
        structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback)
        if isinstance(p, structlog.dev.ConsoleRenderer) else p
        for p in procs
    ]
    replaced = sum(isinstance(p, structlog.dev.ConsoleRenderer) for p in procs)
    assert replaced >= 1, f"no ConsoleRenderer to make plain in the suite's structlog processors: {procs}"
    structlog.configure(processors=swapped)


class PauseWatch:
    """Evidence for a lapse that tells its causes apart, cheap enough not to change the journey's timing.

    A heartbeat thread ticks every ``TICK_S`` and keeps its longest gaps (when each ended, on this
    process's wall clock); ``gc.callbacks`` time every collection and keep those of ``GC_REPORT_S`` or
    more (generation, start, duration). Read with the ledger's renewals: a renewal gap with a heartbeat gap
    over the same time is a process-wide pause, and a long collection there names GC; a renewal gap with
    no heartbeat gap is a block on the renewal path itself.
    """

    TICK_S = 0.05
    GC_REPORT_S = 0.1
    KEEP = 5

    def __init__(self) -> None:
        # Reentrant: a gc callback can fire on a thread already inside this lock.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.gaps: list[tuple[float, datetime]] = []
        self.gcs: list[dict[str, Any]] = []
        self._gc_t0 = 0.0
        self._gc_at: datetime | None = None

    def start(self) -> None:
        import gc  # noqa: PLC0415 — test-local

        gc.callbacks.append(self._on_gc)
        self._thread = threading.Thread(target=self._run, name="mvv-pause-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        import gc  # noqa: PLC0415 — test-local

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self.TICK_S):
            now = time.monotonic()
            gap = now - last
            last = now
            with self._lock:
                self.gaps.append((gap, datetime.now(timezone.utc)))
                self.gaps.sort(key=lambda g: -g[0])
                del self.gaps[self.KEEP:]

    def _on_gc(self, phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            self._gc_t0 = time.perf_counter()
            self._gc_at = datetime.now(timezone.utc)
            return
        took = time.perf_counter() - self._gc_t0
        if took >= self.GC_REPORT_S and self._gc_at is not None:
            with self._lock:
                self.gcs.append({
                    "generation": info.get("generation"), "started_at": self._gc_at.isoformat(),
                    "took_s": round(took, 3),
                })

    def report(self) -> str:
        with self._lock:
            gaps = [(round(g, 3), at.isoformat()) for g, at in self.gaps]
            gcs = list(self.gcs)
        return (
            f"pause watch: heartbeat every {self.TICK_S}s, longest gaps (s, ended at) {gaps};"
            f" collections of {self.GC_REPORT_S}s or more {gcs}"
        )


class Ledger:
    """Observes the wire the worker uses, without changing it.

    Wraps ``HttpTupleStore`` claim, ack, nack and release and
    ``HttpDocumentAspectsStore.upsert``: each wrapper calls the real method and
    records what came back (a claim row with the engine's ``claimed_at`` and
    ``lease_until``, or the typed error). Nothing is faked or short-circuited;
    the engine answers every call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.claims: list[dict[str, Any]] = []
        self.ends: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        #: Every renew and what the engine answered (its new ``lease_until``, or the
        #: typed error), for failure messages only: no bullet asserts on renewals.
        self.renews: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self._lock:
            self.claims.clear()
            self.ends.clear()
            self.upserts.clear()
            self.renews.clear()

    def install(self, mp: pytest.MonkeyPatch) -> None:
        from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
        from nexus.db.t2.http_tuple_store import HttpTupleStore

        led = self
        orig_inp, orig_in = HttpTupleStore.inp, HttpTupleStore.in_
        orig_ack, orig_nack, orig_release = (
            HttpTupleStore.ack, HttpTupleStore.nack, HttpTupleStore.release,
        )
        orig_renew = HttpTupleStore.renew
        orig_upsert = HttpDocumentAspectsStore.upsert

        def _claim(subspace: str, claimant: str, got: Any) -> None:
            if subspace != SUBSPACE or got is None:
                return
            row, claim_id = got
            with led._lock:
                led.claims.append({
                    "task_id": row.id, "claim_id": claim_id, "claimant": claimant,
                    "attempts": row.attempts, "claimed_at": _ts(row.claimed_at),
                    "lease_until": _ts(row.lease_until), "created_at": _ts(row.created_at),
                })

        def inp(self_, subspace, keys, *, claimant, lease_s):
            got = orig_inp(self_, subspace, keys, claimant=claimant, lease_s=lease_s)
            _claim(subspace, claimant, got)
            return got

        def in_(self_, subspace, keys, *, claimant, lease_s, timeout_s=0):
            got = orig_in(self_, subspace, keys, claimant=claimant, lease_s=lease_s, timeout_s=timeout_s)
            _claim(subspace, claimant, got)
            return got

        def _end(kind: str, fn, claim_id, claimant, **kw):
            # ``sent_at`` is this process's wall clock, for failure messages only; every
            # timing assertion reads the engine's stamps.
            rec = {
                "kind": kind, "claim_id": claim_id, "claimant": claimant, "error": None,
                "sent_at": datetime.now(timezone.utc), **kw,
            }
            try:
                return fn()
            except BaseException as exc:
                rec["error"] = type(exc).__name__
                raise
            finally:
                rec["done_at"] = datetime.now(timezone.utc)
                with led._lock:
                    led.ends.append(rec)

        def renew(self_, claim_id, claimant, lease_s):
            rec = {
                "claim_id": claim_id, "sent_at": datetime.now(timezone.utc),
                "lease_until": None, "error": None,
            }
            try:
                rec["lease_until"] = got = orig_renew(self_, claim_id, claimant, lease_s)
                return got
            except BaseException as exc:
                rec["error"] = type(exc).__name__
                raise
            finally:
                rec["done_at"] = datetime.now(timezone.utc)
                with led._lock:
                    led.renews.append(rec)

        def ack(self_, claim_id, claimant, reply=None):
            return _end("ack", lambda: orig_ack(self_, claim_id, claimant, reply), claim_id, claimant)

        def nack(self_, claim_id, claimant, *, retry_after_s=None, dead=False):
            return _end(
                "nack",
                lambda: orig_nack(self_, claim_id, claimant, retry_after_s=retry_after_s, dead=dead),
                claim_id, claimant, retry_after_s=retry_after_s, dead=dead,
            )

        def release(self_, claim_id, claimant):
            return _end("release", lambda: orig_release(self_, claim_id, claimant), claim_id, claimant)

        def upsert(self_, record, *, claim_id=None, claimant=None):
            rec = {
                "doc_id": record.doc_id, "claim_id": claim_id, "claimant": claimant, "error": None,
            }
            try:
                return orig_upsert(self_, record, claim_id=claim_id, claimant=claimant)
            except BaseException as exc:
                rec["error"] = type(exc).__name__
                raise
            finally:
                with led._lock:
                    led.upserts.append(rec)

        mp.setattr(HttpTupleStore, "inp", inp)
        mp.setattr(HttpTupleStore, "in_", in_)
        mp.setattr(HttpTupleStore, "ack", ack)
        mp.setattr(HttpTupleStore, "nack", nack)
        mp.setattr(HttpTupleStore, "release", release)
        mp.setattr(HttpTupleStore, "renew", renew)
        mp.setattr(HttpDocumentAspectsStore, "upsert", upsert)

    def claims_of(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (c for c in self.claims if c["task_id"] == task_id), key=lambda c: c["claimed_at"],
            )

    def ends_of(self, claim_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.ends if e["claim_id"] == claim_id]

    def story(self, task_id: str) -> str:
        """Every claim of *task_id*, each with every renewal and ending call (sent and answered, on this
        process's wall clock, with what the engine answered), for a failure message, in claim order."""
        lines = [f"task {task_id}:"]
        for c in self.claims_of(task_id):
            lines.append(
                f"  claim {c['claim_id']} attempts={c['attempts']} claimant={c['claimant']}"
                f" claimed_at={c['claimed_at'].isoformat()} lease_until={c['lease_until'].isoformat()}"
            )
            with self._lock:
                renews = [r for r in self.renews if r["claim_id"] == c["claim_id"]]
                ends = [e for e in self.ends if e["claim_id"] == c["claim_id"]]
            for r in renews:
                answer = r["lease_until"].isoformat() if r["error"] is None else r["error"]
                lines.append(
                    f"    renew sent_at={r['sent_at'].isoformat()} done_at={r['done_at'].isoformat()} -> {answer}"
                )
            for e in ends:
                extra = {k: e[k] for k in ("retry_after_s", "dead") if k in e}
                lines.append(
                    f"    {e['kind']} sent_at={e['sent_at'].isoformat()} done_at={e['done_at'].isoformat()}"
                    f" error={e['error']} {extra}"
                )
        return "\n".join(lines)
