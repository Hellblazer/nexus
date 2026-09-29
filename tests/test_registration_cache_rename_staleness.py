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
re-stamped, and nothing on that path calls register or upsert. A name this
process has never cached is read once before its first implicit registration
and refused if retired, and every read failure on that path or the retry path
fails closed. Only an explicit-kwargs registration (``nx collection reindex``,
backfill), which skips the read, revives.
"""
from __future__ import annotations

import itertools
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
    """A catalog client. Its row reads LIVE until :func:`_prime` has cached the
    name in this process (a cold implicit write reads first and would refuse an
    already-retired name); ``_prime`` then switches it to *row*, the state some
    OTHER process left behind (default: still live)."""
    w = MagicMock()
    w.get_collection.return_value = {"superseded_by": ""}
    w._later = row if row is not None else {"superseded_by": ""}
    return w


def _prime(
    w: MagicMock, registrar: Callable[[], MagicMock], name: str = _OLD,
) -> None:
    """Cache *name* through *registrar* while the row is live, then let the
    catalog move on to ``w._later`` and forget the cold read's call count."""
    ensure_collection_registered(name, registrar=registrar)
    w.get_collection.reset_mock()
    w.get_collection.return_value = w._later


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
    _prime(w, reg, _OLD)
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
    _prime(w, reg, _OLD)
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
    _prime(w, reg, _OLD)
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
    _prime(w, reg, _LEGACY)
    _age_out(clock)

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_LEGACY, registrar=reg)

    assert w.register_collection.call_count == 1


def test_a_refusal_is_not_one_shot_every_write_is_refused(clock: list[float]) -> None:
    """The refusal must not evict the entry: an evicted name is cold, and a
    cold write upserts, which would revive the tombstone on the SECOND write."""
    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    _prime(w, reg, _OLD)
    _age_out(clock)

    for _ in range(3):
        with pytest.raises(SupersededCollectionWriteError, match=_NEW):
            ensure_collection_registered(_OLD, registrar=reg)

    assert w.register_collection.call_count == 1  # never re-upserted
    assert _OLD in corpus._REGISTERED_COLLECTIONS  # kept, and still stale


def test_stale_write_to_a_renamed_away_name_raises_naming_the_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    _prime(w, reg, _OLD)

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
    _prime(w, reg, _OLD)

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
    _prime(w, reg, _OLD)
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
    _prime(w, reg, _OLD)

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
    _prime(w, reg, _OLD)
    _age_out(clock)
    w.get_collection.side_effect = httpx.ConnectError("blip")

    ensure_collection_registered(_OLD, registrar=reg)  # does not raise

    assert w.register_collection.call_count == 1
    w.get_collection.side_effect = None
    _age_out(clock)  # past the backoff: the entry is still stale, so it re-reads
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 2


def test_row_gone_on_expiry_falls_back_to_registration(clock: list[float]) -> None:
    """A row the engine no longer has (ghost sweep) is not a tombstone: the
    ordinary registration path repairs it, as it always did."""
    w = _writer()
    reg = _registrar(w)
    _prime(w, reg, _OLD)
    _age_out(clock)
    w.get_collection.return_value = None

    ensure_collection_registered(_OLD, registrar=reg)
    assert w.register_collection.call_count == 2


# ── the "not registered" retry cannot revive a retired name ─────────────────


def _not_registered_422() -> Exception:
    resp = httpx.Response(422, text="collection is not registered")
    return httpx.HTTPStatusError("422", request=httpx.Request("POST", "http://x"), response=resp)


def _live_then_superseded() -> MagicMock:
    """Read live for the cold registration, then superseded for every later read."""
    w = MagicMock()
    w.get_collection.side_effect = itertools.chain(
        [{"superseded_by": ""}], itertools.repeat({"superseded_by": _NEW}),
    )
    return w


def test_not_registered_retry_refuses_a_superseded_name() -> None:
    w = _live_then_superseded()
    reg = _registrar(w)
    writes = MagicMock(side_effect=_not_registered_422())

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        write_with_registration_retry(_OLD, writes, registrar=reg)

    assert writes.call_count == 1  # never retried
    assert w.register_collection.call_count == 1  # only the initial cold registration


def test_retry_path_refusal_is_not_one_shot() -> None:
    w = _live_then_superseded()
    reg = _registrar(w)
    writes = MagicMock(side_effect=_not_registered_422())

    for _ in range(3):
        with pytest.raises(SupersededCollectionWriteError, match=_NEW):
            write_with_registration_retry(_OLD, writes, registrar=reg)

    assert w.register_collection.call_count == 1  # only the very first cold registration
    assert writes.call_count == 1  # later calls were refused before writing


def test_retry_path_refusal_keeps_the_entry_stale_not_evicted() -> None:
    """Pins keep-stale on the retry path: swapping ``expire`` for ``discard``
    must fail this. With the entry kept, the next call revalidates (a failing
    read is fail-open there) and reaches the write; with it evicted, the next
    call is cold and its read fails CLOSED before any write."""
    w = _live_then_superseded()
    reg = _registrar(w)
    writes = MagicMock(side_effect=_not_registered_422())
    with pytest.raises(SupersededCollectionWriteError):
        write_with_registration_retry(_OLD, writes, registrar=reg)

    assert _OLD in corpus._REGISTERED_COLLECTIONS
    assert corpus._REGISTERED_COLLECTIONS.is_stale(_OLD)

    w.get_collection.side_effect = httpx.ConnectError("catalog down")
    with pytest.raises(httpx.ConnectError):  # the retry-path read, fail closed
        write_with_registration_retry(_OLD, writes, registrar=reg)
    assert writes.call_count == 2  # revalidation failed open and reached the write
    assert w.register_collection.call_count == 1  # and still never re-registered


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
    _prime(w, reg, _OLD)  # stamped at t=1000
    corpus._REGISTERED_COLLECTIONS.clear()
    _age_out(clock)

    corpus._REGISTERED_COLLECTIONS.add(_OLD)  # seeded now

    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 0
    assert w.register_collection.call_count == 1


def test_expire_marks_every_partition_but_leaves_other_names(clock: list[float]) -> None:
    w = _writer({"superseded_by": _NEW})
    scoped_w = _writer({"superseded_by": _NEW})
    other_w = _writer()
    reg, scoped, other = _registrar(w), _scoped_registrar(scoped_w), _registrar(other_w)
    _prime(w, reg)
    _prime(scoped_w, scoped)
    _prime(other_w, other, _NEW)

    corpus.expire_cached_registration(_OLD)

    for r in (reg, scoped):
        with pytest.raises(SupersededCollectionWriteError):
            ensure_collection_registered(_OLD, registrar=r)
    ensure_collection_registered(_NEW, registrar=other)  # untouched: no read
    assert other_w.get_collection.call_count == 0


def test_explicit_discard_then_register_still_revives() -> None:
    """``nx collection reindex`` discards the cache entry and re-registers with
    explicit kwargs on purpose: that is a deliberate registration and upserts."""
    w = _writer({"superseded_by": _NEW})
    reg = _registrar(w)
    _prime(w, reg)
    corpus.discard_cached_registration(_OLD)
    kwargs = {
        "content_type": "docs", "owner_id": "wwuzp-old-1-1",
        "embedding_model": "voyage-context-3", "model_version": "v1",
    }
    ensure_collection_registered(_OLD, registrar=reg, kwargs=kwargs)
    assert w.register_collection.call_count == 2



# -- a cold process's first implicit write reads before it upserts ----------


def test_cold_implicit_write_to_a_superseded_name_is_refused_every_time() -> None:
    w = MagicMock()  # already retired before this process ever touches it
    w.get_collection.return_value = {"name": _OLD, "superseded_by": _NEW}
    reg = _registrar(w)

    for _ in range(2):
        with pytest.raises(SupersededCollectionWriteError, match=_NEW):
            ensure_collection_registered(_OLD, registrar=reg)

    assert w.register_collection.call_count == 0
    assert _OLD not in corpus._REGISTERED_COLLECTIONS


def test_cold_implicit_write_registers_when_the_row_is_live_or_absent() -> None:
    for row in ({"superseded_by": ""}, None):
        corpus._REGISTERED_COLLECTIONS.clear()
        w = MagicMock()
        w.get_collection.return_value = row
        ensure_collection_registered(_OLD, registrar=_registrar(w))
        assert w.register_collection.call_count == 1


def test_cold_read_failure_fails_closed_and_never_registers() -> None:
    """One failed GET followed by a good POST would revive a tombstone, so a
    cold implicit registration that cannot read the row does not register."""
    w = MagicMock()
    w.get_collection.side_effect = httpx.ConnectError("blip")
    with pytest.raises(httpx.ConnectError):
        ensure_collection_registered(_OLD, registrar=_registrar(w))
    assert w.register_collection.call_count == 0
    assert _OLD not in corpus._REGISTERED_COLLECTIONS
    w.close.assert_called_once()  # the writer is not leaked on the refusal


def test_explicit_kwargs_registration_still_revives_without_a_read() -> None:
    """``nx collection reindex`` deletes the row then registers with explicit
    kwargs; that is a deliberate registration and upserts as always."""
    w = _writer({"superseded_by": _NEW})
    kwargs = {
        "content_type": "docs", "owner_id": "wwuzp-old-1-1",
        "embedding_model": "voyage-context-3", "model_version": "v1",
    }
    ensure_collection_registered(_OLD, registrar=_registrar(w), kwargs=kwargs)
    assert w.register_collection.call_count == 1
    assert w.get_collection.call_count == 0


# -- a failing revalidation read backs off instead of re-reading every write -


def test_failed_read_backs_off_then_retries(clock: list[float]) -> None:
    w = _writer()
    reg = _registrar(w)
    _prime(w, reg, _OLD)
    _age_out(clock)
    w.get_collection.side_effect = httpx.ConnectError("catalog down")

    for _ in range(5):  # a burst of writes inside the backoff window
        ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 1

    clock[0] += corpus._REVALIDATION_BACKOFF_SECONDS + 0.5
    ensure_collection_registered(_OLD, registrar=reg)
    assert w.get_collection.call_count == 2  # backoff elapsed: tried again
    assert w.register_collection.call_count == 1


# -- the CLI renders the refusal as a clean error ---------------------------


def test_cli_renders_a_superseded_write_as_a_clean_error() -> None:
    from click.testing import CliRunner

    from nexus.cli import main

    @main.command("wwuzp-probe")
    def _probe() -> None:
        raise SupersededCollectionWriteError(_OLD, _NEW)

    try:
        result = CliRunner().invoke(main, ["wwuzp-probe"])
    finally:
        main.commands.pop("wwuzp-probe", None)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # a ClickException, not a traceback
    assert _NEW in result.output and _OLD in result.output
    assert "register it explicitly" not in result.output
    assert "Traceback" not in result.output


# -- nx index repo refuses a superseded target instead of un-retiring it ----


def test_index_repo_refuses_a_superseded_target_after_a_failed_migration(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase-4 migration failed (``phase4_migration_failed``) and the run
    carries on with the name the registry gives it. The indexer registers
    with explicit kwargs, which upserts and would un-retire a tombstone, so
    it must read first and stop loudly."""
    from nexus.indexer import _run_index
    from tests.test_indexer_seam_b_cutover import _mock_db, _reg, _service_mode_patches

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("x = 1\n")
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_LOCAL", "0")
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setenv("CHROMA_API_KEY", "fake")

    db, _ = _mock_db()
    extra = {
        "nexus.indexer._migrate_legacy_collections": {"side_effect": RuntimeError("boom")},
        "nexus.corpus._read_collection_row": {"return_value": {"superseded_by": _NEW}},
    }
    with _service_mode_patches(db, extra=extra) as mocks:
        with pytest.raises(SupersededCollectionWriteError, match=_NEW) as ei:
            _run_index(repo, _reg())
        # The indexer cannot "write to <successor>": the message names the
        # real remedy, with commands that exist.
        msg = str(ei.value)
        assert "nx index repo" in msg
        assert f"nx collection info {_NEW}" in msg
        assert "nx catalog doctor --collections-drift" in msg
        assert "Write to" not in msg
        assert mocks["ensure_collection_registered"].call_count == 0
        assert mocks["_index_code_file"].call_count == 0


# -- a rename that lands DURING the revalidation read is not lost ------------


def test_expire_during_the_read_is_not_overwritten_by_the_restamp(
    clock: list[float],
) -> None:
    """The read says live, but a rename lands (and expires the entry) before
    the revalidator re-stamps. The re-stamp must not paper over the expire."""
    w = _writer()
    reg = _registrar(w)
    _prime(w, reg, _OLD)
    _age_out(clock)

    def slow_read_during_which_a_rename_lands(name: str) -> dict:
        corpus.expire_cached_registration(name)  # the rename, mid-read
        return {"superseded_by": ""}  # what the read saw BEFORE the rename

    w.get_collection.side_effect = slow_read_during_which_a_rename_lands
    ensure_collection_registered(_OLD, registrar=reg)  # returns: the read said live
    assert w.get_collection.call_count == 1

    w.get_collection.side_effect = None
    w.get_collection.return_value = {"superseded_by": _NEW}  # the rename's result
    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_OLD, registrar=reg)  # still stale: re-read
    assert w.register_collection.call_count == 1


# -- the ambient read path (no registrar) ------------------------------------


def _write_only_proxy() -> MagicMock:
    """Like ``_ServiceCatalogWriter``: exposes writes, raises AttributeError
    for anything else, ``get_collection`` included."""
    return MagicMock(spec=["register_collection", "close"])


def test_ambient_read_goes_through_the_catalog_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _write_only_proxy()
    reader = MagicMock()
    reader.get_collection.return_value = {"superseded_by": _NEW}
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda: proxy)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_OLD)  # registrar=None: the ambient path

    reader.get_collection.assert_called_once_with(_OLD)
    assert proxy.register_collection.call_count == 0


def test_ambient_live_row_registers_and_later_revalidates_by_reader(
    monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    proxy = _write_only_proxy()
    reader = MagicMock()
    reader.get_collection.return_value = {"superseded_by": ""}
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda: proxy)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)

    ensure_collection_registered(_OLD)
    assert proxy.register_collection.call_count == 1
    _age_out(clock)
    ensure_collection_registered(_OLD)
    assert reader.get_collection.call_count == 2  # cold read + revalidation read
    assert proxy.register_collection.call_count == 1


# -- the retry path fails CLOSED when it cannot read ---------------------------


def test_retry_path_read_failure_propagates_and_never_re_registers() -> None:
    """The cold read succeeds (live), the write 422s "not registered", and the
    retry's read then fails: that propagates and nothing re-registers."""
    w = MagicMock()
    w.get_collection.side_effect = itertools.chain(
        [{"superseded_by": ""}], itertools.repeat(httpx.ConnectError("catalog down")),
    )
    writes = MagicMock(side_effect=_not_registered_422())

    with pytest.raises(httpx.ConnectError):
        write_with_registration_retry(_OLD, writes, registrar=_registrar(w))

    assert w.register_collection.call_count == 1  # the cold registration only
    assert writes.call_count == 1


# -- a discard that overlaps a revalidation read is not undone by it ---------


def test_discard_during_the_read_is_not_undone_by_the_restamp(
    clock: list[float],
) -> None:
    w = _writer()
    reg = _registrar(w)
    _prime(w, reg)
    _age_out(clock)

    def read_during_which_the_entry_is_discarded(name: str) -> dict:
        corpus.discard_cached_registration(name)  # e.g. `nx collection reindex`
        return {"superseded_by": ""}

    w.get_collection.side_effect = read_during_which_the_entry_is_discarded
    ensure_collection_registered(_OLD, registrar=reg)

    assert _OLD not in corpus._REGISTERED_COLLECTIONS  # not re-added fresh


@pytest.mark.parametrize("removal", ["discard", "remove", "pop", "clear"])
def test_every_removal_bumps_the_generation(removal: str) -> None:
    cache = corpus._REGISTERED_COLLECTIONS
    cache.add(_OLD)
    before = cache.generation(_OLD)
    if removal == "discard":
        cache.discard(_OLD)
    elif removal == "remove":
        cache.remove(_OLD)
    elif removal == "pop":
        cache.pop()
    else:
        cache.clear()
    assert _OLD not in cache
    assert cache.generation(_OLD) > before


# -- a retired name reports the refusal, not an unrelated failure ------------


def test_cold_refusal_is_reported_before_kwargs_derivation_and_profile_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_run(*_a, **_k):
        raise AssertionError("derived kwargs for a name that is already retired")

    monkeypatch.setattr(corpus, "collection_registration_kwargs", must_not_run)
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", must_not_run)
    w = MagicMock()
    w.get_collection.return_value = {"superseded_by": _NEW}

    with pytest.raises(SupersededCollectionWriteError, match=_NEW):
        ensure_collection_registered(_OLD, registrar=_registrar(w))
    assert w.register_collection.call_count == 0


def test_cold_profile_mismatch_still_reports_for_a_live_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(corpus, "_profile_model_for_content_type", lambda _ct: "some-other-model")
    w = MagicMock()
    w.get_collection.return_value = {"superseded_by": ""}
    with pytest.raises(corpus.EmbeddingProfileMismatchError):
        ensure_collection_registered(_OLD, registrar=_registrar(w))
    assert w.register_collection.call_count == 0
