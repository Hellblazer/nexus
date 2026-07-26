# SPDX-License-Identifier: AGPL-3.0-or-later
"""Substrate-agnostic catalog fixture access for the unit suite (nexus-aqbrk).

RDR-158/155 substrate port. A large family of catalog tests seeds state with
``Catalog.init(dir)`` and then drives a CLI verb or hook that reads the
catalog through ``make_catalog_reader`` / ``make_catalog_writer``. Under the
engine substrate those two disagree: the test writes the LOCAL ``.catalog.db``
while the code under test reads the SERVICE catalog, so every count comes back
zero and every field empty — the bucket-2 "opaque assert" profile
(``assert 0 == 2024``, ``assert '' == 'Vaswani et al.'``).

:class:`ActiveCatalog` closes that gap by routing the test's own seeding
through the SAME factories the code under test uses. The test then exercises
whichever catalog is real, which is strictly more coverage than the local-only
form it replaces.

WHEN NOT TO USE THIS. Verbs that are local-only BY DESIGN — ``nx catalog
synthesize-log``, the doctor replay/consistency verbs, the local factory's own
read-only/admin semantics — should use the ``local_catalog_backend`` fixture
instead. The distinguishing question is whether the code under test routes
through the factories (use this) or reaches for the local artifacts directly
(pin instead).
"""
from __future__ import annotations

from typing import Any

from nexus.daemon.catalog_write_shim import CATALOG_WRITE_OPS

__all__ = [
    "ActiveCatalog",
    "active_reader",
    "count_documents",
    "only_document",
    "unroutable_write_target",
]


def active_reader() -> Any:
    """The catalog reader the CLI itself would use, resolved fresh.

    Deliberately not memoised: a cached reader can be a snapshot taken before
    the test's writes (the SQLite reader opens ``mode=ro`` at construction),
    so every read resolves a current one.
    """
    from nexus.commands import catalog as _cat_cmd

    return _cat_cmd._get_catalog()


def count_documents(collection: str | None = None) -> int:
    """Document count via the active reader.

    Replaces ``cat._db.execute("SELECT count(*) FROM documents")``, which has
    no service-mode equivalent.
    """
    reader = active_reader()
    if collection is not None:
        return len(reader.list_by_collection(collection))
    return sum(1 for _ in reader.all_documents())


def only_document() -> Any:
    """The single document in the catalog, asserting there is exactly one.

    Replaces ``cat._db.execute("SELECT <col> FROM documents").fetchone()`` in
    tests that register one entry and read a column back. ``fetchone()``
    silently took the first of N; this states the "exactly one" the tests
    already meant.
    """
    docs = list(active_reader().all_documents())
    assert len(docs) == 1, f"expected exactly one document, got {len(docs)}"
    return docs[0]


def unroutable_write_target() -> Any:
    """A target for a catalog WRITE that is not on ``CATALOG_WRITE_OPS``.

    ``set_alias`` is the known case (nexus-iltyk): it mutates, but it is not
    whitelisted, so ``CatalogWriter.__getattr__`` will not forward it on the
    SQLite arm — while in service mode ``_SharedServiceCatalogHandle`` proxies
    every attribute and therefore performs the write through what the factory
    calls a READER.

    So there is no single object that can do it on both substrates:
      - service: the shared handle, which proxies it (and is what production
        would end up using today, whether or not that is intended)
      - sqlite:  a directly-constructed WRITABLE ``Catalog``, since the
                 factory reader is ``mode=ro``

    This exists so a test can perform such a write on either substrate without
    the facade pretending the typed factories support it. It is deliberately
    ugly and deliberately named: if nexus-iltyk is resolved by whitelisting
    the op, every caller of this should collapse back to ``ActiveCatalog``.
    """
    from nexus.db.storage_mode import StorageBackend, storage_backend_for

    if storage_backend_for("catalog") is not StorageBackend.SQLITE:
        return active_reader()

    from nexus.catalog.catalog import Catalog
    from nexus.config import catalog_path

    path = catalog_path()
    return Catalog(path, path / ".catalog.db")


class ActiveCatalog:
    """Read/write facade over whichever catalog backend is live.

    Writes go through ``make_catalog_writer()`` (``CatalogWriter`` on SQLite,
    ``_ServiceCatalogWriter`` on the engine); everything else resolves against
    ``make_catalog_reader()``. The split is keyed on
    :data:`CATALOG_WRITE_OPS`, the same whitelist the daemon write-shim and
    the service writer both enforce, so an op that is not routable as a write
    fails here the way it would in production rather than silently reading.

    A writer is opened and closed per call. That is heavier than holding one,
    and it is what the CLI actually does — every ``nx catalog`` write command
    opens a writer for the duration of the command — so it also keeps the
    test honest about writer lifetime.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(
                f"ActiveCatalog has no private attribute {name!r}. Raw handles "
                f"(_db, _conn) have no service-mode equivalent — read through "
                f"the public API (see tests._catalog_fixture_ops.count_documents)."
            )
        if name in CATALOG_WRITE_OPS:
            return _WriteOp(name)
        return getattr(active_reader(), name)


class _WriteOp:
    """One whitelisted write, applied through a freshly-opened writer."""

    def __init__(self, op: str) -> None:
        self._op = op

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        try:
            return getattr(writer, self._op)(*args, **kwargs)
        finally:
            writer.close()
