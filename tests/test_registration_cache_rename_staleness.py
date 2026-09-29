# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wwuzp: the per-process registration cache must not outlive a rename.

``ensure_collection_registered`` skips the ``/collections/upsert`` once a
name is cached. A rename retires the old name as a superseded tombstone; a
long-lived process that wrote to the old name beforehand kept writing chunks
under it with no upsert to clear ``superseded_by`` (a cold process would
revive it). Two closures are pinned here: the process that ran the rename
evicts the old name, and every process re-validates a cache entry after a
bounded age, so the process that did NOT run the rename converges too.
"""
from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

import nexus.corpus as corpus
from nexus.corpus import ensure_collection_registered

_OLD = "docs__wwuzp-old-1-1__voyage-context-3__v1"
_NEW = "docs__wwuzp-new-1-1__voyage-context-3__v1"


@pytest.fixture(autouse=True)
def _clean_cache():
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


def _registrar() -> tuple[MagicMock, Callable[[], MagicMock]]:
    writer = MagicMock()
    return writer, (lambda: writer)


def _scoped_registrar(writer: MagicMock) -> Callable[[], MagicMock]:
    def registrar() -> MagicMock:
        return writer

    registrar.scope = ("http://engine", "tenant", "digest")  # type: ignore[attr-defined]
    return registrar


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda _ct: None)


def test_cache_entry_expires_and_re_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(corpus, "_registration_clock", lambda: now[0])
    writer, registrar = _registrar()

    ensure_collection_registered(_OLD, registrar=registrar)
    now[0] += corpus._REGISTRATION_TTL_SECONDS - 1
    ensure_collection_registered(_OLD, registrar=registrar)
    assert writer.register_collection.call_count == 1  # still fresh

    now[0] += 2  # past the TTL: another process may have renamed it away
    ensure_collection_registered(_OLD, registrar=registrar)
    assert writer.register_collection.call_count == 2


def test_scoped_cache_entry_expires_too(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [50.0]
    monkeypatch.setattr(corpus, "_registration_clock", lambda: now[0])
    writer = MagicMock()
    registrar = _scoped_registrar(writer)

    ensure_collection_registered(_OLD, registrar=registrar)
    now[0] += corpus._REGISTRATION_TTL_SECONDS + 1
    ensure_collection_registered(_OLD, registrar=registrar)
    assert writer.register_collection.call_count == 2


def test_evict_registration_clears_every_partition() -> None:
    writer, registrar = _registrar()
    scoped_writer = MagicMock()
    scoped = _scoped_registrar(scoped_writer)

    ensure_collection_registered(_OLD, registrar=registrar)
    ensure_collection_registered(_OLD, registrar=scoped)
    ensure_collection_registered(_NEW, registrar=registrar)

    corpus.evict_registration_everywhere(_OLD)

    ensure_collection_registered(_OLD, registrar=registrar)
    ensure_collection_registered(_OLD, registrar=scoped)
    ensure_collection_registered(_NEW, registrar=registrar)
    assert writer.register_collection.call_count == 3  # _OLD twice, _NEW once
    assert scoped_writer.register_collection.call_count == 2


def test_rename_cascade_evicts_the_old_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    writer, registrar = _registrar()
    ensure_collection_registered(_OLD, registrar=registrar)
    assert writer.register_collection.call_count == 1

    client = object.__new__(HttpCatalogClient)
    monkeypatch.setattr(
        HttpCatalogClient, "_post",
        lambda self, path, body, **kw: {"renamed": {"chunks": 3}},
    )
    client.rename_collection_cascade(_OLD, _NEW)

    # A write to the renamed-away name in this process now re-upserts it
    # (reviving it, as a cold process would) instead of striding past.
    ensure_collection_registered(_OLD, registrar=registrar)
    assert writer.register_collection.call_count == 2


def test_failed_rename_leaves_the_cache_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    writer, registrar = _registrar()
    ensure_collection_registered(_OLD, registrar=registrar)

    def boom(self, path, body, **kw):
        raise RuntimeError("engine refused")

    monkeypatch.setattr(HttpCatalogClient, "_post", boom)
    with pytest.raises(RuntimeError):
        object.__new__(HttpCatalogClient).rename_collection_cascade(_OLD, _NEW)

    ensure_collection_registered(_OLD, registrar=registrar)
    assert writer.register_collection.call_count == 1
