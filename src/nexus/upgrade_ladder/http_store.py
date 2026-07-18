# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-186 .12: the engine-backed completion ledger — ladder.db's successor.

:class:`HttpLadderStore` implements the :class:`~nexus.upgrade_ladder
.protocol.CompletionLedger` Protocol over the engine's ``/v1/ladder``
endpoints (``nexus.ladder_completions``, engine >= v0.1.46). It replaces the
retired SQLite ``CompletionStore``/``ladder.db`` (RDR-186 D3
derive-first-record-late; the NO-SQLITE directive): completion facts are
position BOOKKEEPING, never truth, so the pre-engine window needs no durable
local substrate at all — the walk runs against
:class:`~nexus.upgrade_ladder.holder.InProcessCompletionHolder` fronting this
store, and a crash before the flush costs one idempotent re-derivation
(RF-186-2), never correctness.

VERIFIED_AT SEMANTICS (pinned, engine-half critic 2026-07-18 — do not
"fix" this later thinking it a bug): the engine upsert SERVER-stamps
``verified_at = now()`` at FLUSH time, discarding the holder's buffered
verify-moment timestamp by design. Audit metadata is observability-only
and accepted lossy (RF-186-2). Format asymmetry is also known and
accepted: this client stamps ``isoformat(timespec="seconds")``
(``+00:00``) while the engine returns Java ``OffsetDateTime.toString()``
(``Z``, sub-second precision) — both parse via
``datetime.fromisoformat`` and nothing compares them.

NO position surface: ladder position stays DERIVED through the single
``completion.derive_ladder_position`` algorithm (Gap-4 mechanism 1); this
store serves raw facts only.
"""
from __future__ import annotations

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.upgrade_ladder.completion import CompletionRecord


class HttpLadderStore(RefreshableHttpStoreMixin):
    """Thin HTTP client for ``/v1/ladder`` — a :class:`CompletionLedger`.

    Endpoint/token/tenant resolution and 401 self-healing come from
    :class:`RefreshableHttpStoreMixin` (the f2qvx house pattern; tenant
    default ``"default"`` matches the engine's
    ``TenantConstants.DEFAULT_TENANT``). Construct with no arguments in
    production. Raises on any HTTP failure — the holder's write-through
    catches, warns, and keeps the record owed in ``unflushed()``.
    """

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        """Record one rung's verified completion (engine upsert; the server
        stamps ``verified_at`` — see the module docstring)."""
        self._post("/v1/ladder/record", {
            "rung_name": rung_name,
            "package_version": package_version,
            "detail": detail,
        })

    def verified_rungs(self) -> frozenset[str]:
        return frozenset(row["rung_name"] for row in self._completion_rows())

    def completions(self) -> dict[str, CompletionRecord]:
        return {
            row["rung_name"]: CompletionRecord(
                rung_name=row["rung_name"],
                verified_at=row["verified_at"],
                package_version=row["package_version"],
                detail=row.get("detail", ""),
            )
            for row in self._completion_rows()
        }

    def _completion_rows(self) -> list[dict[str, str]]:
        return self._get("/v1/ladder/completions")["completions"]

    def __enter__(self) -> "HttpLadderStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class DeferredLadderLedger:
    """Constructs the HTTP store on FIRST USE — the engine-defer window's
    production ledger (RDR-186 .12).

    The ladder RUNS when the engine may be absent (its own rungs install
    it), but :class:`HttpLadderStore`'s mixin resolves the endpoint at
    construction and fails loud when nothing is resolvable. Resolution
    therefore defers to the first actual backend call: an unresolvable or
    down engine surfaces as that call raising, which the holder's
    degradation contract already treats as backend-down (warn, keep the
    record owed in ``unflushed()``) — the walk proceeds in-process, and
    the end-of-walk flush retries once the walk's own rungs have brought
    the engine up. A resolution failure is an OUTAGE here, never a
    programming error — the loud AttributeError/TypeError contract is
    about the Protocol surface, which this wrapper implements fully.
    """

    def __init__(self) -> None:
        self._store: HttpLadderStore | None = None

    def _real(self) -> HttpLadderStore:
        if self._store is None:
            self._store = HttpLadderStore()
        return self._store

    def record_verified(self, rung_name: str, *, package_version: str, detail: str = "") -> None:
        self._real().record_verified(
            rung_name, package_version=package_version, detail=detail
        )

    def verified_rungs(self) -> frozenset[str]:
        return self._real().verified_rungs()

    def completions(self) -> dict[str, CompletionRecord]:
        return self._real().completions()

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
