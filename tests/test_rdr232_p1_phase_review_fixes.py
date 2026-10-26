# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 phase-review fixes (nexus-jypmj.1.9 code review F2, F3, F4 minors).

Named-source resolution, the owner-name lookup and the primary read each run off the event loop and
inside ``nx_answer``'s own deadline with a fixed ceiling, the same pattern as the lifecycle read
(``tests/test_rdr232_lifecycle_stream.py``): a hit is a disclosed cap and the answer proceeds without the
stream. Tenant-free: slow fakes stand in for the catalog and T3. Each test names the claim it pins.
"""
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from nexus.plans.evidence import OwnerResolution, SourceRef, build_evidence_request
from nexus.plans.primary_evidence import (
    DEFAULT_CAPS,
    NamedSourceResolution,
    PrimaryEvidence,
    ResolvedSource,
    resolve_named_sources,
    select_primary_evidence,
)

DOC = "1.1.9"


class SlowCatalog:
    """A catalog whose every call sleeps; records the thread each call ran on."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls: list[tuple[str, int]] = []

    def _go(self, name: str) -> None:
        self.calls.append((name, threading.get_ident()))
        time.sleep(self.delay)

    def find(self, query, *, content_type=None, limit=100):  # noqa: ARG002
        self._go("find")
        return []

    def get_owner_by_prefix(self, prefix):  # noqa: ARG002
        self._go("owner")
        return {"owner_type": "repo"}

    def resolve(self, tumbler, *, follow_alias=True):  # noqa: ARG002
        self._go("resolve")
        return None

    def get_manifests(self, doc_ids):  # noqa: ARG002
        self._go("manifests")
        return {}


def _request(*sources: str, **extra):
    return build_evidence_request(
        {"source": list(sources) if len(sources) > 1 else sources[0], **extra}, scope="", name_lookup=lambda n: n,
    )


def _resolved_one() -> NamedSourceResolution:
    return NamedSourceResolution(sources=(ResolvedSource(
        ref=SourceRef.parse("RDR-084"), status="resolved", tumbler=DOC, title="RDR-084", qualified=True, rdr_number=84,
    ),))


# ── the resolution is cooperative: it starts no catalog call past its deadline ────────────────────────────


def test_resolve_named_sources_starts_no_catalog_call_past_the_deadline() -> None:
    now = [100.0]

    class Tick(SlowCatalog):
        def _go(self, name: str) -> None:
            super()._go(name)
            now[0] += 5.0  # each call costs five (fake) seconds

    cat = Tick(0.0)
    res = resolve_named_sources(
        [SourceRef.parse(r) for r in ("RDR-084", "RDR-085", "RDR-086")], OwnerResolution("none"),
        cat=cat, deadline=108.0, clock=lambda: now[0],
    )
    assert len(cat.calls) == 2, "the clock reads 100, 105, then 110 past the 108 deadline: the third call is not made"
    assert res.catalog_calls == 2
    assert all(s.status == "unresolved" for s in res.sources)
    late = [s for s in res.sources if "deadline" in s.detail]
    assert len(late) >= 2, [s.detail for s in res.sources]
    lines = "\n".join(res.disclosures())
    assert "stopped at the deadline" in lines


def test_resolve_named_sources_without_a_deadline_is_unchanged() -> None:
    cat = SlowCatalog(0.0)
    res = resolve_named_sources([SourceRef.parse("RDR-084")], OwnerResolution("none"), cat=cat)
    assert res.sources[0].status == "unresolved" and "deadline" not in res.sources[0].detail
    assert [c[0] for c in cat.calls] == ["find", "find"]


def test_the_default_ceiling_is_a_cap_field_and_on_the_wire() -> None:
    assert DEFAULT_CAPS.max_seconds == 20.0 and DEFAULT_CAPS.to_wire()["max_seconds"] == 20.0


# ── nx_answer's named-source step: off the event loop, bounded by the deadline ────────────────────────────


@pytest.mark.asyncio
async def test_named_source_resolution_runs_off_the_event_loop() -> None:
    from nexus.mcp import core as mcp_core

    cat = SlowCatalog(0.08)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    loop_thread = threading.get_ident()
    with patch.object(mcp_core, "_get_catalog", return_value=cat):
        res = await mcp_core._nx_answer_named_sources(_request("RDR-084"), deadline=None)
    task.cancel()
    assert res is not None and len(cat.calls) >= 2
    assert all(tid != loop_thread for _, tid in cat.calls), "catalog calls ran on the event-loop thread"
    assert ticks >= 8, f"the event loop was blocked: {ticks} ticks during {len(cat.calls)} sleeping calls"


@pytest.mark.asyncio
async def test_a_hung_catalog_call_cannot_hold_the_answer_past_the_deadline() -> None:
    from nexus.mcp import core as mcp_core

    cat = SlowCatalog(1.5)
    t0 = time.monotonic()
    with patch.object(mcp_core, "_get_catalog", return_value=cat):
        res = await mcp_core._nx_answer_named_sources(_request("RDR-084", "RDR-085"), deadline=t0 + 0.3)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"the answer waited {elapsed:.2f}s on a 1.5 s catalog call under a 0.3 s deadline"
    assert res is not None and [s.status for s in res.sources] == ["unresolved", "unresolved"]
    assert all("deadline" in s.detail for s in res.sources)
    assert res.resolved() == ()
    assert "stopped at the deadline" in "\n".join(res.disclosures())


@pytest.mark.asyncio
async def test_no_source_named_makes_no_catalog_call_and_no_thread() -> None:
    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "_get_catalog", side_effect=AssertionError("no catalog call")):
        assert await mcp_core._nx_answer_named_sources(build_evidence_request(None, scope="", name_lookup=str)) is None


# ── the owner-name lookup: off the loop, bounded, disclosed ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_owner_name_lookup_runs_off_the_event_loop() -> None:
    from nexus.mcp import core as mcp_core

    seen: list[int] = []

    def slow_lookup_factory(_cat):
        def lookup(name: str) -> str:
            seen.append(threading.get_ident())
            time.sleep(0.05)
            return "1.61"
        return lookup

    with patch.object(mcp_core, "_get_catalog", return_value=MagicMock()), \
         patch("nexus.plans.evidence.catalog_name_lookup", slow_lookup_factory):
        req = await mcp_core._nx_answer_evidence_request(bindings={"owner": "ART", "source": "RDR-084"}, scope="")
    assert req.owner.status == "resolved" and req.owner.tumbler == "1.61"
    assert seen and all(t != threading.get_ident() for t in seen)


@pytest.mark.asyncio
async def test_a_hung_owner_lookup_is_disclosed_as_the_deadline_never_a_guess() -> None:
    from nexus.mcp import core as mcp_core

    def hung_factory(_cat):
        def lookup(name: str) -> str:
            time.sleep(1.5)
            return "1.61"
        return lookup

    t0 = time.monotonic()
    with patch.object(mcp_core, "_get_catalog", return_value=MagicMock()), \
         patch("nexus.plans.evidence.catalog_name_lookup", hung_factory):
        req = await mcp_core._nx_answer_evidence_request(
            bindings={"owner": "ART", "source": "RDR-084"}, scope="", deadline=t0 + 0.3)
    assert time.monotonic() - t0 < 1.0
    assert req.owner.status == "unresolved" and req.owner.tumbler is None
    assert "deadline" in req.owner.detail and "Nothing was guessed" in req.owner.detail


@pytest.mark.asyncio
async def test_an_abandoned_owner_lookup_thread_starts_no_further_lookup() -> None:
    """After the deadline the loop has moved on; the thread it left must not go on to look up the project."""
    from nexus.mcp import core as mcp_core

    looked_up: list[str] = []

    def factory(_cat):
        def lookup(name: str) -> str:
            looked_up.append(name)
            time.sleep(0.4)
            return "1.61"
        return lookup

    t0 = time.monotonic()
    with patch.object(mcp_core, "_get_catalog", return_value=MagicMock()), \
         patch("nexus.plans.evidence.catalog_name_lookup", factory):
        req = await mcp_core._nx_answer_evidence_request(
            bindings={"owner": "ART", "project": "NX", "source": "RDR-084"}, scope="", deadline=t0 + 0.2)
        await asyncio.sleep(0.6)  # the abandoned thread finishes its first lookup and reaches the second
    assert req.owner.status == "unresolved" and "deadline" in req.owner.detail
    assert looked_up == ["ART"], f"the thread went on to look up {looked_up[1:]} past the deadline"


@pytest.mark.asyncio
async def test_a_call_with_no_reserved_binding_touches_no_catalog() -> None:
    # asyncio is imported inside the function, so a module-local seam cannot see to_thread; the
    # catalog guard is the observable claim (no reserved binding returns before any lookup or thread).
    from nexus.mcp import core as mcp_core

    with patch.object(mcp_core, "_get_catalog", side_effect=AssertionError("no catalog call")), \
         patch("nexus.plans.evidence.catalog_name_lookup", side_effect=AssertionError("no lookup")):
        req = await mcp_core._nx_answer_evidence_request(bindings={"topic": "x"}, scope="rdr")
    assert not req.requested


# ── the primary read: bounded, disclosed, and it discloses a failure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_hung_primary_read_is_bounded_by_the_deadline_and_disclosed() -> None:
    from nexus.mcp import core as mcp_core

    def hung(resolution, **_kw):  # noqa: ARG001
        time.sleep(1.5)
        return PrimaryEvidence(items=())

    t0 = time.monotonic()
    with patch("nexus.plans.primary_evidence.select_primary_evidence", hung), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch.object(mcp_core, "_get_t3", return_value=MagicMock()), \
         patch.object(mcp_core, "_get_catalog", return_value=MagicMock()):
        ev, stream = await mcp_core._nx_answer_primary_stream(_resolved_one(), None, deadline=t0 + 0.3)
    assert time.monotonic() - t0 < 1.0
    assert ev is not None and ev.caps_hit == ("deadline",) and ev.items == ()
    assert stream is None, "nothing was read and no lifecycle record: no reserved stream"
    assert any("stopped at the deadline" in line for line in ev.disclosures())


@pytest.mark.asyncio
async def test_a_failed_primary_read_is_disclosed_not_silent() -> None:
    """Code review minor: the docstring promised a disclosure; the exception path returned (None, ...)."""
    from nexus.mcp import core as mcp_core

    with patch("nexus.plans.primary_evidence.select_primary_evidence", side_effect=RuntimeError("t3 down")), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=lambda c, h: {}), \
         patch.object(mcp_core, "_get_t3", return_value=MagicMock()), \
         patch.object(mcp_core, "_get_catalog", return_value=MagicMock()):
        ev, stream = await mcp_core._nx_answer_primary_stream(_resolved_one(), None)
    assert ev is not None and ev.error == "RuntimeError" and stream is None
    (line,) = ev.disclosures()
    assert "could not be read" in line and "RuntimeError" in line and "t3 down" not in line
    assert ev.to_wire()["error"] == "RuntimeError"


def test_select_primary_evidence_starts_no_fetch_past_the_deadline() -> None:
    now = [100.0]

    class Cat:
        def get_manifests(self, ids):
            now[0] += 5.0
            from nexus.catalog.types import ManifestRow  # noqa: PLC0415

            return {DOC: [ManifestRow(chash=f"{i:064x}", position=i, collection="rdr__1-1__x__v1") for i in range(250)]}

    fetched: list[int] = []

    def fetch(collection, chashes):  # noqa: ARG001
        fetched.append(len(chashes))
        now[0] += 5.0
        return {c: f"text {c[-4:]}" for c in chashes}

    out = select_primary_evidence(_resolved_one(), cat=Cat(), fetch_chunks=fetch, deadline=108.0, clock=lambda: now[0])
    assert len(fetched) <= 1, "the clock passed the deadline after the manifest read and the first batch"
    assert "deadline" in out.caps_hit
    assert any("stopped at the deadline" in line for line in out.disclosures())


def test_select_primary_evidence_without_a_deadline_is_unchanged() -> None:
    class Cat:
        def get_manifests(self, ids):
            return {}

    out = select_primary_evidence(_resolved_one(), cat=Cat(), fetch_chunks=lambda c, h: {})
    assert "deadline" not in out.caps_hit and out.error == ""

