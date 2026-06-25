"""Service-mode guard for the catalog chunk_text_hash backfill (nexus-84gbt).

Regression coverage for the 6.0.0 local-validation defect: ``nx catalog setup``
ran a Chroma-specific chunk_text_hash backfill that reached into ``t3._client``.
In service mode ``t3`` is an ``HttpVectorClient`` (no ``_client`` attribute), so
the loop raised ``AttributeError`` and degraded to "Hash backfill partial",
leaving the catalog manifest empty. The backfill must be a clean no-op in
service mode (the service owns chash via its manifest/post-store path).
"""

from __future__ import annotations

import pytest

from nexus.commands import catalog as catmod


class _ExplodingT3:
    """A T3 stand-in that fails loudly if the Chroma-specific path is touched."""

    @property
    def _client(self):  # noqa: D401 — test double
        raise AssertionError("service mode must not access t3._client")

    def list_collections(self):
        raise AssertionError("service mode must not enumerate collections")


class _FakeCol:
    def __init__(self, name: str) -> None:
        self.name = name


class _LocalT3:
    """A local-mode T3 stand-in exposing the chromadb-style ``_client``."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

        class _Client:
            def get_collection(self_inner, name):  # noqa: N805 — test double
                return _FakeCol(name)

        self._client = _Client()

    def list_collections(self):
        return [{"name": n} for n in self._names]


def test_backfill_skips_in_service_mode(monkeypatch):
    """In service mode the helper returns 0 and never touches ``_client``."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True
    )
    # Must not raise AttributeError (the original bug) nor touch _client.
    assert catmod._backfill_all_chunk_text_hashes(_ExplodingT3()) == 0


def test_backfill_runs_in_local_mode(monkeypatch):
    """In local mode the helper iterates collections and sums updated counts."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: False
    )
    seen: list[str] = []

    def _fake_backfill(col, *args, **kwargs):
        seen.append(col.name)
        return (3, 0, 3)  # (updated, skipped, total)

    monkeypatch.setattr(
        "nexus.commands.collection._backfill_chunk_text_hash", _fake_backfill
    )

    total = catmod._backfill_all_chunk_text_hashes(_LocalT3(["code__a", "code__b"]))
    assert total == 6
    assert seen == ["code__a", "code__b"]
