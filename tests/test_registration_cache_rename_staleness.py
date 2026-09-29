# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wwuzp: the per-process registration cache must not outlive a rename,
and a stale implicit write must never revive a retired name.

``ensure_collection_registered`` skips the ``/collections/upsert`` once a
name is cached. A rename (or a deliberate ``supersede_collection``) retires
the old name as a superseded tombstone; a long-lived process that wrote to
the old name beforehand kept writing chunks under it. The upsert clears
``superseded_by`` unconditionally, so re-running it to "refresh" the entry
would un-retire ANY tombstone (deliberate supersedes, Phase-4 legacy names)
and erase the evidence ``collection_shape`` reports. So an aged or retired
cache entry is re-validated with a READ instead: superseded means the write
is refused naming the successor, not superseded means the entry is
re-stamped, and nothing on that path calls register or upsert. Only an
explicit registration (a name this process never cached, or one it
deliberately discarded) revives.
"""
from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import httpx
import pytest

import nexus.corpus as corpus
from nexus.corpus import (
    SupersededCollectionWriteError,
    ensure_collection_registered,
    write_with_registration_retry,
)

_OLD = "docs__wwuzp-old-1-1__voyage-context-3__v1"
_NEW = "docs__wwuzp-new-1-1__voyage-context-3__v1"
_LEGACY = "docs__wwuzp-legacy"  # a pre-RDR-103 name, Phase-4 renames it to a conformant one


@pytest.fixture(autouse=True)
def _clean_cache():
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.fixture(autouse=True)
def _no_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda _ct: None)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(corpus, "_registration_clock", lambda: now[0])
    return now


def _writer(row: dict | None = None) -> MagicMock:
    """A catalog client whose ``get_collection`` returns *row* (default: a
    live, not-superseded row)."""
    w = MagicMock()
    w.get_collection.return_value = row if row is not None else {"superseded_by": ""}
    return w


def _registrar(w: MagicMock) -> Callable[[], MagicMock]:
    return lambda: w


def _scoped_registrar(w: MagicMock) -> Callable[[], MagicMock]:
    def registrar() -> MagicMock:
        return w

    registrar.scope = ("http://engine", "tenant", "digest")  # type: ignore[attr-defined]
    return registrar


def _age_out(clock: list[float]) -> None:
    clock[0] += corpus._REGISTRATION_TTL_SECONDS + 1


# ── a not-superseded entry re-stamps without an upsert ──────────────────────


def test_aged_entry_that_is_not_superseded_restamps_with_a_read_and_no_upsert(
    clock: list[float],
) -> None:
    w = _writer()
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.register_collection.call_count == 1

    clock[0] += corpus._REGISTRATION_TTL_SECONDS - 1
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 0  # still fresh: no read

    clock[0] += 2
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 1
    assert w.register_collection.call_count == 1  # the read never registers

    ensure_collection_registered(_OLD, registrar=reg)  # re-stamped: fresh again
    assert w.get_collection.call_count == 1


def test_scoped_entry_revalidates_through_its_own_writer(clock: list[float]) -> None:
    w = _writer()
    reg = _scoped_registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    _age_out(clock)
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 1
    assert w.register_collection.call_count == 1


# ── a stale implicit write to a retired name is refused, never revived ──────


def test_stale_writer_does_not_revive_a_deliberate_supersede_tombstone(
    clock: list[float],
) -> None:
    w = _writer({"name": _OLD, "superseded_by": _NEW, "superseded_at": "2026-09-29"})
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    _age_out(clock)

    with pytest.raises(SupersededCollectionWriteError) as ei:
        ensure_collection_registered(_OLD, registrar=reg)

    assert ei.value.name == _OLD and ei.value.successor == _NEW
    assert _NEW in str(ei.value)
    assert w.register_collection.call_count == 1  # the tombstone was NOT re-upserted


def test_stale_writer_does_not_revive_a_phase4_legacy_name(clock: list[float]) -> None:
    w = _writer({
        "name": _LEGACY, "superseded_by": _NEW, "legacy_grandfathered": True,
    })
    reg = _registrar(w)
    ensure_collection_registered(_LEGACY, registrar=reg)
    _age_out(clock)

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_LEGACY, registrar=reg)

    assert w.register_collection.call_count == 1


def test_refused_name_is_evicted_so_the_refusal_is_not_cached_as_registered(
    clock: list[float],
) -> None:
    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    _age_out(clock)
    with pytest.raises(SupersededCollectionWriteError):
        ensure_collection_registered(_OLD, registrar=reg)
    assert _OLD not in corpus._REGISTERED_COLLECTIONS


def test_stale_write_to_a_renamed_away_name_raises_naming_the_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)

    monkeypatch.setattr(
        HttpCatalogClient, "_post",
        lambda self, path, body, **kw: {"renamed": {"chunks": 3}},
    )
    object.__new__(HttpCatalogClient).rename_collection_cascade(_OLD, _NEW)

    # Same process, no TTL wait: the rename itself marked the entry for a read.
    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_OLD, registrar=reg)
    assert w.register_collection.call_count == 1


def test_supersede_collection_marks_the_entry_for_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three non-rename ``supersede_collection`` callers (indexer,
    ``catalog migrate-fallback``, ``catalog collections``) must not leave a
    cached 'registered' entry behind either."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)

    monkeypatch.setattr(
        HttpCatalogClient, "_post", lambda self, path, body, **kw: {"updated": 1},
    )
    object.__new__(HttpCatalogClient).supersede_collection(_OLD, _NEW)

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_OLD, registrar=reg)


def test_rename_that_leaves_the_old_row_live_just_restamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-model copy rename leaves the source live: the read says not
    superseded, so the entry is re-stamped and nothing is upserted."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    w = _writer()
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    monkeypatch.setattr(
        HttpCatalogClient, "_post", lambda self, path, body, **kw: {"renamed": {}},
    )
    object.__new__(HttpCatalogClient).rename_collection_cascade(_OLD, _NEW, cross_model=True)

    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 1
    assert w.register_collection.call_count == 1


def test_failed_rename_leaves_the_cache_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    w = _writer()
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)

    def boom(self, path, body, **kw):
        raise RuntimeError("engine refused")

    monkeypatch.setattr(HttpCatalogClient, "_post", boom)
    with pytest.raises(RuntimeError):
        object.__new__(HttpCatalogClient).rename_collection_cascade(_OLD, _NEW)

    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 0  # still fresh: not marked
    assert w.register_collection.call_count == 1


# ── the expiry read must not turn a blip into a failed write ────────────────


def test_read_failure_on_expiry_proceeds_on_the_cached_entry(clock: list[float]) -> None:
    w = _writer()
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    _age_out(clock)
    w.get_collection.side_effect = httpx.ConnectError("blip")

    ensure_collection_registered(_OLD, registrar=reg)  # does not raise

    assert w.register_collection.call_count == 1
    w.get_collection.side_effect = None
    ensure_collection_registered(_OLD, registrar=reg)  # still stale: retries the read
    assert w.get_collection.call_count == 2


def test_row_gone_on_expiry_falls_back_to_registration(clock: list[float]) -> None:
    """A row the engine no longer has (ghost sweep) is not a tombstone: the
    ordinary registration path repairs it, as it always did."""
    w = _writer()
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    _age_out(clock)
    w.get_collection.return_value = None

    ensure_collection_registered(_OLD, registrar=reg)
    assert w.register_collection.call_count == 2


# ── the "not registered" retry cannot revive a retired name ─────────────────


def _not_registered_422() -> Exception:
    resp = httpx.Response(422, text="collection is not registered")
    return httpx.HTTPStatusError("422", request=httpx.Request("POST", "http://x"), response=resp)


def test_not_registered_retry_refuses_a_superseded_name() -> None:
    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    writes = MagicMock(side_effect=_not_registered_422())

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        write_with_registration_retry(_OLD, writes, registrar=reg)

    assert writes.call_count == 1  # never retried
    assert w.register_collection.call_count == 1  # only the initial cold registration


def test_not_registered_retry_still_repairs_a_swept_collection() -> None:
    w = _writer()
    w.get_collection.return_value = None  # swept: no row at all
    reg = _registrar(w)
    writes = MagicMock(side_effect=[_not_registered_422(), "ok"])

    assert write_with_registration_retry(_OLD, writes, registrar=reg) == "ok"
    assert w.register_collection.call_count == 2


# ── cache mechanics ─────────────────────────────────────────────────────────


def test_a_bare_set_add_gets_a_fresh_stamp_never_a_leftover_one(clock: list[float]) -> None:
    """Tests seed ``_REGISTERED_COLLECTIONS`` directly. A stamp left by an
    earlier occupant of the same name must not make that seed look aged."""
    w = _writer()
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)  # stamped at t=1000
    corpus._REGISTERED_COLLECTIONS.clear()
    _age_out(clock)

    corpus._REGISTERED_COLLECTIONS.add(_OLD)  # seeded now

    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 0
    assert w.register_collection.call_count == 1


def test_expire_marks_every_partition_but_leaves_other_names(clock: list[float]) -> None:
    w = _writer({"superseded_by": _NEW})
    scoped_w = _writer({"superseded_by": _NEW})
    reg, scoped = _registrar(w), _scoped_registrar(scoped_w)
    other_w = _writer()
    other = _registrar(other_w)
    ensure_collection_registered(_OLD, registrar=reg)
    ensure_collection_registered(_OLD, registrar=scoped)
    ensure_collection_registered(_NEW, registrar=other)

    corpus.expire_cached_registration(_OLD)

    for r in (reg, scoped):
        with pytest.raises(SupersededCollectionWriteError):
            ensure_collection_registered(_OLD, registrar=r)
    ensure_collection_registered(_NEW, registrar=other)  # untouched: no read
    assert other_w.get_collection.call_count == 0


def test_explicit_discard_then_register_still_revives() -> None:
    """``nx collection reindex`` discards and re-registers on purpose: that
    is an explicit registration and keeps upserting."""
    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    ensure_collection_registered(_OLD, registrar=reg)
    corpus.discard_cached_registration(_OLD)
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.register_collection.call_count == 2
