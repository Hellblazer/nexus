"""Service-mode guard for the catalog chunk_text_hash backfill (nexus-84gbt).

Regression coverage for the 6.0.0 local-validation defect: ``nx catalog setup``
ran a Chroma-specific chunk_text_hash backfill that reached into ``t3._client``.
In service mode ``t3`` is an ``HttpVectorClient`` (no ``_client`` attribute), so
the loop raised ``AttributeError`` and degraded to "Hash backfill partial". The
backfill must be a clean no-op in service mode (the service owns chash via its
manifest/post-store path).

The guard keys on the HANDLE (``is_service_backed`` / ``isinstance``), NOT the
env (``is_vector_service_mode``): these tests use a real ``HttpVectorClient``
subclass and a plain object so they exercise the actual predicate, not a
monkeypatch.
"""

from __future__ import annotations

from nexus.commands import catalog as catmod
from nexus.db.http_vector_client import HttpVectorClient


class _ServiceT3(HttpVectorClient):
    """A real service-backed handle (isinstance HttpVectorClient → True).

    __init__ is bypassed (no network); the only members the helper may touch
    raise loudly to prove the service branch never reaches the Chroma path.
    """

    def __init__(self) -> None:  # noqa: D401 — test double; bypass network init
        pass

    @property
    def _client(self):
        raise AssertionError("service mode must not access t3._client")

    def list_collections(self):
        raise AssertionError("service mode must not enumerate collections")


class _FakeCol:
    def __init__(self, name: str) -> None:
        self.name = name


class _LocalT3:
    """A local-mode handle (NOT an HttpVectorClient) exposing chromadb _client."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

        class _Client:
            def get_collection(self_inner, name):  # noqa: N805 — test double
                return _FakeCol(name)

        self._client = _Client()

    def list_collections(self):
        return [{"name": n} for n in self._names]


def test_backfill_skips_in_service_mode():
    """A real HttpVectorClient handle no-ops without touching ``_client``."""
    # No env monkeypatch: the guard must key on the instance type.
    assert catmod._backfill_all_chunk_text_hashes(_ServiceT3()) == 0


def test_backfill_runs_in_local_mode(monkeypatch):
    """A non-service handle iterates collections and sums updated counts."""
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
