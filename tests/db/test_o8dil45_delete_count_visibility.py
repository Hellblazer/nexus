# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o8dil.45 (RDR-191): ``HttpVectorClient``'s delete surfaces discarded
the server's actual ``{"deleted": N}`` response, so no Python caller could
observe how many chunks a delete actually removed -- only how many it
requested.

``nexus-o8dil.5`` (RDR-191 P2, F10c) made this a live correctness gap:
``PgVectorRepository.delete`` is now server-side anti-join-scoped, so it can
legitimately delete FEWER chunks than requested (a chash another LIVE
document's manifest still references is correctly retained instead of
destroyed). Pre-fix, "requested == deleted" was always true, so discarding
the response was harmless; post-fix, a discarded response means the caller
silently OVER-REPORTS success on a request the server partially refused --
violating the project's "no silent fallback for data-correctness problems --
FAIL LOUD" directive.

This file pins ``_ServiceCollectionStub.delete()``'s contract: it must
return the server's ACTUAL ``deleted`` count as an ``int``, mirroring
``delete_by_id``'s pre-existing ``result.get("deleted", 0)`` capture (see
``TestDeleteByIdReturnsActualCount`` below for the contrast pin).
"""
from __future__ import annotations

from nexus.db.http_vector_client import HttpVectorClient, _ServiceCollectionStub


class TestServiceCollectionStubDeleteReturnsActualCount:
    def test_delete_returns_actual_server_deleted_count(self, monkeypatch) -> None:
        """Core non-vacuity case: the server's anti-join refuses one of the
        three requested chashes (still live-referenced elsewhere). Pre-fix,
        ``delete()`` returned ``None`` unconditionally -- ``result == 2``
        would fail on ``None == 2``."""
        posted = []

        def fake_post(path, body, **kw):
            posted.append((path, body))
            return {"deleted": len(body["ids"]) - 1}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        stub = _ServiceCollectionStub("col__test__m__v1")

        result = stub.delete(["a" * 64, "b" * 64, "c" * 64])

        assert result == 2, (
            f"expected the server's ACTUAL deleted count (2 of 3 requested), "
            f"got {result!r} -- delete() must not discard the response body"
        )
        assert isinstance(result, int)
        assert posted == [
            ("/v1/vectors/store-delete",
             {"collection": "col__test__m__v1", "ids": ["a" * 64, "b" * 64, "c" * 64]}),
        ]

    def test_delete_full_success_returns_len_ids(self, monkeypatch) -> None:
        """Control: when the server deletes everything requested (the common
        case, and the ONLY case pre-nexus-o8dil.5), the actual count equals
        the requested count."""
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: {"deleted": len(body["ids"])},
        )
        stub = _ServiceCollectionStub("col__test__m__v1")

        assert stub.delete(["a" * 64, "b" * 64]) == 2

    def test_delete_empty_ids_returns_zero_without_a_request(self, monkeypatch) -> None:
        posted = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: posted.append(body) or {"deleted": 0},
        )
        stub = _ServiceCollectionStub("col__test__m__v1")

        assert stub.delete([]) == 0
        assert posted == [], "an empty ids list must not fire a request"


class TestDeleteByIdReturnsActualCount:
    """Contrast pin: ``delete_by_id`` already had this contract (bool, but
    built on the same ``result.get("deleted", 0)`` capture) before this bead
    -- ``delete()``'s fix generalizes this existing pattern, per the bead's
    explicit instruction not to invent a new one."""

    def test_delete_by_id_already_reads_the_deleted_field(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: {"deleted": 0},
        )
        client = HttpVectorClient()
        assert client.delete_by_id("col__test__m__v1", "a" * 64) is False
