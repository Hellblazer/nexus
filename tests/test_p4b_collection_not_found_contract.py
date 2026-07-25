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
    from nexus.db.http_vector_client import HttpVectorClient

    client = HttpVectorClient()
    monkeypatch.setattr(client, "list_collections", lambda: [{"name": "other"}])
    with pytest.raises(CollectionNotFoundError):
        client.get_collection("missing__coll__stub-1024__v1")


def test_service_unavailable_maps_to_collection_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frecency loop's skip semantics depend on unreachable-service
    reading as not-found (pre-existing behavior, type now neutral)."""
    from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

    client = HttpVectorClient()

    def _boom() -> list:
        raise VectorServiceError("down")

    monkeypatch.setattr(client, "list_collections", _boom)
    with pytest.raises(CollectionNotFoundError):
        client.get_collection("any")


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
