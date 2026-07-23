"""RDR-155 P4b Phase 0c: the substrate-neutral missing-collection contract.

The raiser (``HttpVectorClient.get_collection``) and every catcher
(indexer x3, collection_purge, t3_reidentify, manifest_backfill) speak
``nexus.errors.CollectionNotFoundError`` instead of
``chromadb.errors.NotFoundError``. During the deletion window the
chroma-backed TEST substrate still raises chroma's type natively, so the
catchers tolerate both via ``collection_not_found_errors()`` — whose chroma
member drops out AUTOMATICALLY at P3 when the dependency leaves (the
deferred import fails closed to the nexus-native type alone).
"""
from __future__ import annotations

import builtins

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


def test_transition_tuple_contains_both_types() -> None:
    import chromadb.errors

    types_ = collection_not_found_errors()
    assert CollectionNotFoundError in types_
    assert chromadb.errors.NotFoundError in types_


def test_tuple_collapses_when_chromadb_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The P3 end state, simulated: chromadb unimportable -> the tuple is
    the nexus-native type alone, catchers need zero edits at removal."""
    real_import = builtins.__import__

    def _no_chroma(name, *args, **kwargs):
        if name.startswith("chromadb"):
            raise ImportError("chromadb removed (P4b P3 end state)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_chroma)
    assert collection_not_found_errors() == (CollectionNotFoundError,)


def test_no_raw_chroma_notfound_contract_sites_remain() -> None:
    """Census tripwire: the raiser/catcher contract must not re-grow direct
    chromadb.errors.NotFoundError couplings outside the dying modules
    (t3.py dies whole at P4b; errors.py holds the sanctioned deferred
    import)."""
    import pathlib

    import nexus

    root = pathlib.Path(nexus.__file__).parent
    allowed = {root / "errors.py", root / "db" / "t3.py"}
    offenders = []
    for py in root.rglob("*.py"):
        if py in allowed:
            continue
        text = py.read_text()
        if "from chromadb.errors import NotFoundError" in text:
            offenders.append(str(py.relative_to(root)))
    assert not offenders, (
        f"raw chromadb NotFoundError couplings re-grew at {offenders}; use "
        "nexus.errors.CollectionNotFoundError / collection_not_found_errors()"
    )


def test_catchers_tolerate_both_members() -> None:
    """A catcher written as `except collection_not_found_errors():` handles
    the nexus-native raiser AND the chroma-native test substrate."""
    import chromadb.errors

    for exc in (CollectionNotFoundError("x"),
                chromadb.errors.NotFoundError("y")):
        try:
            raise exc
        except collection_not_found_errors():
            pass
