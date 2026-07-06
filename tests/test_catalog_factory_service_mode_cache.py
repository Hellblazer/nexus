# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5en9j: SERVICE-mode make_catalog_reader()/make_catalog_writer()
must share ONE process-lifetime HttpCatalogClient instead of constructing
(and immediately closing) one per call.

This was the LARGEST single reconstruction count in the nexus-53x7s
shakeout evidence (394x http_catalog_client.init in one run) -- larger
than any of the 8 T2Database substores that bead's first fix addressed.
Same design as mcp_infra._service_t2_write_locked: a process-lifetime
singleton, one lock held for the full call (not just checkout), with
reactive eviction on any call failure.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_around(monkeypatch):
    import nexus.catalog.factory as factory

    monkeypatch.setattr(factory, "_is_catalog_service_mode", lambda: True)
    factory.reset_shared_service_catalog_client_for_tests()
    yield
    factory.reset_shared_service_catalog_client_for_tests()


def _install_fake_client(monkeypatch, constructed: list) -> None:
    class _FakeHttpCatalogClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            return f"doc:{doc_id}"

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _FakeHttpCatalogClient)


def test_reader_reuses_singleton_across_calls(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    for _ in range(5):
        reader = make_catalog_reader()
        assert reader.by_doc_id("abc") == "doc:abc"

    assert len(constructed) == 1, "must share one HttpCatalogClient for the process lifetime"
    assert constructed[0].closed is False


def test_reader_close_is_noop_and_does_not_tear_down_shared_client(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    reader1 = make_catalog_reader()
    reader1.by_doc_id("x")
    reader1.close()  # historical call-site pattern: close on every exit

    reader2 = make_catalog_reader()
    assert reader2.by_doc_id("y") == "doc:y"

    assert len(constructed) == 1, "close() on the shared handle must not close the underlying client"
    assert constructed[0].closed is False


def test_writer_and_reader_share_the_same_underlying_client(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    make_catalog_reader().by_doc_id("a")
    writer = make_catalog_writer()
    writer.close()

    assert len(constructed) == 1, "reader and writer must share one underlying client"


def test_call_failure_evicts_and_next_call_rebuilds(monkeypatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    constructed: list = []

    class _FlakyClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False
            self.calls = 0

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            self.calls += 1
            raise ConnectionError("stale lease")

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _FlakyClient)

    reader = make_catalog_reader()
    with pytest.raises(ConnectionError):
        reader.by_doc_id("x")

    assert len(constructed) == 1
    assert constructed[0].closed is True, "the failed client must be evicted (closed) immediately"

    class _WorkingClient:
        def __init__(self, *_a, **_kw) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def by_doc_id(self, doc_id: str) -> str:
            return f"doc:{doc_id}"

    monkeypatch.setattr("nexus.catalog.http_catalog_client.HttpCatalogClient", _WorkingClient)

    reader2 = make_catalog_reader()
    assert reader2.by_doc_id("y") == "doc:y"
    assert len(constructed) == 2, "the next call must rebuild against a fresh instance"


def test_reset_shared_service_catalog_client_for_tests_clears_singleton(monkeypatch) -> None:
    import nexus.catalog.factory as factory

    constructed: list = []
    _install_fake_client(monkeypatch, constructed)

    factory.make_catalog_reader().by_doc_id("a")
    assert factory._service_catalog_client is not None

    factory.reset_shared_service_catalog_client_for_tests()
    assert factory._service_catalog_client is None
