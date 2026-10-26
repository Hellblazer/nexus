"""RDR-155 P4b Phase 0c: the substrate-neutral missing-collection contract.

The raiser (``HttpVectorClient.get_collection``) and every catcher
(indexer x3, collection_purge, t3_reidentify, manifest_backfill) speak
``nexus.errors.CollectionNotFoundError`` instead of
``chromadb.errors.NotFoundError``.

P3 (2026-07-25): the deletion window is CLOSED and the transition worked as
designed — the chroma member dropped out of ``collection_not_found_errors()``
when the dependency left, and not one catcher needed an edit. The tests that
pinned the two-member window are replaced below by their end-state
counterparts; the census tripwire is TIGHTENED, because the sanctioned
deferred import in ``errors.py`` is gone too, so there is now no allow-list at
all.
"""
from __future__ import annotations

import pytest

from nexus.errors import CollectionNotFoundError, collection_not_found_errors


def test_raiser_uses_nexus_native_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A collection the service lists without the name is truly absent: the
    native type, and no ``__cause__`` (nexus-v9xwj: the manifest heal marks on
    exactly this)."""
    from nexus.db.http_vector_client import HttpVectorClient

    client = HttpVectorClient()
    monkeypatch.setattr(client, "collection_stats", lambda *a, **k: [{"name": "other", "count": 1}])
    # The listing primes the process-wide collection-row cache; keep that out of this test.
    monkeypatch.setattr("nexus.mcp_infra.prime_collections_cache", lambda rows: None)
    with pytest.raises(CollectionNotFoundError) as caught:
        client.get_collection("missing__coll__stub-1024__v1")
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("code", [503, 500, None])
def test_service_unavailable_maps_to_collection_not_found(
    monkeypatch: pytest.MonkeyPatch, code: int | None,
) -> None:
    """The frecency loop's skip semantics depend on unreachable-service
    reading as not-found (pre-existing behavior, type now neutral).

    The failure is injected where the real client fails, in the stats call under
    ``list_collections`` (a non-404 ``VectorServiceError``), not by faking
    ``list_collections`` itself. ``get_collection`` must list strictly: the
    default listing swallows that error and returns ``[]``, which reads as an
    absent collection and raises with no ``__cause__`` (nexus-v9xwj), so an
    outage would be indistinguishable from absence.
    """
    from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

    client = HttpVectorClient()

    def _boom(*args, **kwargs) -> list:
        raise VectorServiceError("down", code=code)

    monkeypatch.setattr(client, "collection_stats", _boom)
    with pytest.raises(CollectionNotFoundError) as caught:
        client.get_collection("any")
    assert isinstance(caught.value.__cause__, VectorServiceError)


def test_a_missing_stats_route_still_lists_through_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 from the stats route is the pre-catalog-005 skew, not an outage:
    strict listing leaves that fallback alone, so ``get_collection`` still
    answers from ``/collections``."""
    from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

    client = HttpVectorClient()

    def _no_stats(*args, **kwargs) -> list:
        raise VectorServiceError("no stats route", code=404)

    monkeypatch.setattr(client, "collection_stats", _no_stats)
    monkeypatch.setattr(client, "_list_collections_via_count", lambda **_kw: [{"name": "present__c__m-1__v1"}])
    assert client.get_collection("present__c__m-1__v1").name == "present__c__m-1__v1"


def test_an_outage_is_logged_when_get_collection_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default listing logged ``http_vector_list_collections_failed``; the
    strict one raises instead, so ``get_collection`` carries the signal for the
    callers that skip a ``CollectionNotFoundError`` without logging (frecency,
    the orphan GC, the manifest backfill)."""
    from structlog.testing import capture_logs

    from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

    client = HttpVectorClient()

    def _boom(*args, **kwargs) -> list:
        raise VectorServiceError("down", code=503)

    monkeypatch.setattr(client, "collection_stats", _boom)
    with capture_logs() as logs, pytest.raises(CollectionNotFoundError):
        client.get_collection("any")
    events = [e for e in logs if e["event"] == "http_vector_get_collection_service_unavailable"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["collection"] == "any"


def test_an_outage_on_a_pre_catalog_005_engine_is_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stats route is missing (404) AND ``/collections`` fails: the fallback
    used to return ``[]``, which reads as an absent collection. Under a strict
    listing it raises; the default listing keeps its degrade-to-empty contract."""
    from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

    client = HttpVectorClient()

    def _no_stats(*args, **kwargs) -> list:
        raise VectorServiceError("no stats route", code=404)

    def _collections_down(path: str, **kwargs):
        raise VectorServiceError("collections down", code=503)

    monkeypatch.setattr(client, "collection_stats", _no_stats)
    monkeypatch.setattr("nexus.db.http_vector_client._get", _collections_down)
    monkeypatch.setattr("nexus.mcp_infra.prime_collections_cache", lambda rows: None)
    assert client.list_collections() == []
    with pytest.raises(VectorServiceError):
        client.list_collections(strict=True)
    with pytest.raises(CollectionNotFoundError) as caught:
        client.get_collection("any")
    assert isinstance(caught.value.__cause__, VectorServiceError)


def test_tuple_is_the_nexus_native_type_alone() -> None:
    """The P3 end state, no longer simulated.

    Was two tests: one pinning that chroma's NotFoundError was IN the tuple
    during the window, one simulating its absence by patching __import__.
    Both premises are gone — the dependency is actually absent — so this is
    the direct assertion.
    """
    assert collection_not_found_errors() == (CollectionNotFoundError,)


def test_chromadb_is_not_importable() -> None:
    """Non-vacuity guard for the test above.

    ``collection_not_found_errors()`` returning a 1-tuple proves nothing on
    its own if chromadb merely happens to be installed and the function were
    quietly reverted to the two-member form. Pin the actual precondition: the
    dependency is gone from the environment.
    """
    with pytest.raises(ImportError):
        import chromadb  # noqa: F401, PLC0415 — the import failing IS the assertion


def test_no_raw_chroma_notfound_contract_sites_remain() -> None:
    """Census tripwire: no direct chromadb.errors.NotFoundError coupling
    anywhere in the package.

    P3 TIGHTENED this. It used to allow two files — errors.py (the sanctioned
    deferred import) and db/t3.py (slated to die whole). errors.py's shim is
    collapsed and t3.py is chroma-free, so the allow-list is now EMPTY and any
    reappearance anywhere is a failure.
    """
    import pathlib

    import nexus

    root = pathlib.Path(nexus.__file__).parent
    offenders = []
    for py in root.rglob("*.py"):
        text = py.read_text()
        if "from chromadb.errors import NotFoundError" in text:
            offenders.append(str(py.relative_to(root)))
    assert not offenders, (
        f"raw chromadb NotFoundError couplings re-grew at {offenders}; use "
        "nexus.errors.CollectionNotFoundError / collection_not_found_errors()"
    )


def test_catchers_catch_the_native_raiser() -> None:
    """A catcher written as `except collection_not_found_errors():` handles
    the nexus-native raiser.

    Was `test_catchers_tolerate_both_members`, which also raised chroma's
    type. That arm died with the dependency; the surviving arm is the one
    every production catcher actually depends on.
    """
    try:
        raise CollectionNotFoundError("x")
    except collection_not_found_errors():
        pass
