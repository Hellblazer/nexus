# SPDX-License-Identifier: AGPL-3.0-or-later
"""The registrations that precede a catalog document go through ONE function (RDR-229 P3.1, nexus-p2m1x.17).

Since P3.1 the engine refuses a document under a collection with no registry row (422
``unregistered_collection``), so ``put_note``, the ``.nxexp`` import pre-pass, the ``doc_indexer`` pre-flight and
the 422 retry each register the collection first. They used to call ``ensure_collection_registered`` four times,
and each call is a site in the collection-name census (pin 217 -> 221, a ratchet the RDR says only falls).
``registration_retry.register_collection_for_write`` is the one call; the four go through it, so the census holds
one site for the seam and P3.8's ``collection_mint`` replaces one body.

Tenant-free: the engine-backed behaviour is ``tests/test_rdr229_preflight_registers_collection.py`` and the P3.1
Java suites. Here a fake writer and a recorder stand in for the registration.
"""
from __future__ import annotations

from typing import Any

import pytest
import structlog

import nexus.registration_retry as registration_retry


class _Writer:
    """A catalog writer that names the registrar its combined writes register with, as ``_ServiceCatalogWriter`` does."""

    def __init__(self) -> None:
        self.catalog_registrar = lambda: self
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _record(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        registration_retry, "register_collection_for_write",
        lambda name, registrar=None: calls.append((name, registrar)))
    return calls


def test_the_seam_calls_the_registration_primitive_with_the_registrar(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "nexus.corpus.ensure_collection_registered",
        lambda name, *, registrar=None, kwargs=None: seen.append((name, registrar)))
    registrar = object()

    registration_retry.register_collection_for_write("docs__seam__bge-base-en-v15-768__v1", registrar)  # type: ignore[arg-type]

    assert seen == [("docs__seam__bge-base-en-v15-768__v1", registrar)]


def test_put_note_registers_through_the_seam_with_the_writers_registrar(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog.note_write import _register_collection_first

    writer = _Writer()
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **kw: writer)
    calls = _record(monkeypatch)

    assert _register_collection_first("knowledge__seam__bge-base-en-v15-768__v1") is None

    assert calls == [("knowledge__seam__bge-base-en-v15-768__v1", writer.catalog_registrar)]
    assert writer.closed


def test_put_note_logs_a_registration_failure_at_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure is best effort (the catalog hook meets the same engine), but the engine's later 422 names
    only the collection, so the real cause is logged where an operator reads it, as doc_indexer's pre-flight does."""
    from nexus.catalog.note_write import _register_collection_first

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_writer", lambda *a, **kw: _Writer())

    def fail(name: str, registrar: Any = None) -> None:
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr(registration_retry, "register_collection_for_write", fail)

    with structlog.testing.capture_logs() as logs:
        assert _register_collection_first("knowledge__seam__bge-base-en-v15-768__v1") is None

    entries = [e for e in logs if e["event"] == "collection_registered_first_failed"]
    assert len(entries) == 1
    assert entries[0]["log_level"] == "warning"
    assert entries[0]["error_type"] == "RuntimeError"
    assert "engine unreachable" in entries[0]["error"]


def test_the_422_retry_registers_again_through_the_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.corpus as corpus

    monkeypatch.setattr(corpus, "_read_collection_row", lambda name, registrar=None: None)
    calls = _record(monkeypatch)
    registrar = object()
    name = "docs__retry__bge-base-en-v15-768__v1"

    registration_retry.reregister_after_unregistered(name, RuntimeError("422"), registrar)  # type: ignore[arg-type]

    assert calls == [(name, registrar)]
