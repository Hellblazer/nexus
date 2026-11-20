# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.5 (nexus-p2m1x.10): ``collection_for`` refuses a quarantine ``for_tuple`` row.

Before P1.4 (nexus-p2m1x.4) the client refused a quarantine sibling by accident: it parsed the
returned name and ``quarantine-code`` is not a content type (nexus-n9xjy). P1.4 reads the row's
``model_version`` instead, so that accident no longer refuses anything, and only the engine's
``LIFECYCLE_STATE <> 'quarantine'`` filter stood between a quarantine row and a write. The row
now carries ``lifecycle_state``, and the client refuses a quarantine row (and a state it does not
know) loudly. ``dormant`` and ``disputed`` resolve as before: the engine's filter is
``ne('quarantine')`` on purpose, and both are admin states orthogonal to the pairing.

An engine older than the field sends no ``lifecycle_state``; its filter still guards it, and the
client accepts the row as before.

The transport is a fake; the client, its row reader and its error are the real ones.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from nexus.catalog.collection_name import RESOLVABLE_LIFECYCLE_STATES
from nexus.catalog.http_catalog_client import HttpCatalogClient

_OWNER = "1.1"
_MODEL = "bge-base-en-v15-768"
_SIBLING = "quarantine-code__1-1__bge-base-en-v15-768__v1"


def _client(monkeypatch: pytest.MonkeyPatch, row: dict) -> HttpCatalogClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/catalog/collections/for_tuple":
            return httpx.Response(200, json=row)
        return httpx.Response(200, json={})

    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    client = HttpCatalogClient(base_url="http://mock.test")
    client._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return client


def _row(**over: object) -> dict:
    return {
        "name": "code__1-1__bge-base-en-v15-768__v2", "content_type": "code", "owner_id": "1-1",
        "embedding_model": _MODEL, "model_version": "v2", "lifecycle_state": "live", **over,
    }


def test_a_quarantine_row_is_refused_naming_the_row_and_its_state(monkeypatch) -> None:
    """The n9xjy shape: the sibling's attributes match the tuple and its version parses."""
    cat = _client(monkeypatch, _row(name=_SIBLING, model_version="v1", lifecycle_state="quarantine"))
    with pytest.raises(ValueError, match=r"quarantine-code__1-1.*lifecycle_state 'quarantine'"):
        cat.collection_for("code", _OWNER, _MODEL)
    with pytest.raises(ValueError, match="lifecycle_state 'quarantine'"):
        cat.collection_for("code", _OWNER, _MODEL, bump=True)


def test_a_quarantine_row_under_a_prefix_free_name_is_refused(monkeypatch) -> None:
    """The refusal reads the row, not the name."""
    cat = _client(monkeypatch, _row(lifecycle_state="quarantine"))
    with pytest.raises(ValueError, match="lifecycle_state 'quarantine'"):
        cat.collection_for("code", _OWNER, _MODEL)


def test_an_unknown_state_is_refused(monkeypatch) -> None:
    cat = _client(monkeypatch, _row(lifecycle_state="retired"))
    with pytest.raises(ValueError, match="lifecycle_state 'retired'"):
        cat.collection_for("code", _OWNER, _MODEL)


@pytest.mark.parametrize("state", ["live", "dormant", "disputed"])
def test_a_live_dormant_or_disputed_row_resolves(monkeypatch, state) -> None:
    cat = _client(monkeypatch, _row(lifecycle_state=state))
    assert cat.collection_for("code", _OWNER, _MODEL).model_version == 2
    assert cat.collection_for("code", _OWNER, _MODEL, bump=True).model_version == 3


def test_a_row_without_the_field_resolves_as_before(monkeypatch) -> None:
    """An engine older than P2.5 sends no lifecycle_state; its own filter guards it."""
    row = _row()
    del row["lifecycle_state"]
    cat = _client(monkeypatch, row)
    assert cat.collection_for("code", _OWNER, _MODEL).model_version == 2


_CATALOG_REPOSITORY = (
    Path(__file__).resolve().parent.parent
    / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "db" / "CatalogRepository.java"
)


def _engine_lifecycle_states() -> set[str]:
    text = _CATALOG_REPOSITORY.read_text(encoding="utf-8")
    match = re.search(r"LIFECYCLE_STATES\s*=\s*java\.util\.Set\.of\(([^)]*)\)", text)
    assert match, "CatalogRepository.LIFECYCLE_STATES was not found: the constant moved or changed shape"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_the_resolvable_states_are_the_engines_states_less_quarantine() -> None:
    """RESOLVABLE_LIFECYCLE_STATES is a client copy of the engine's vocabulary (hygiene-002's CHECK, mirrored by
    CatalogRepository.LIFECYCLE_STATES). A state the engine adds is refused by every client that does not know it
    (an unknown state is a ValueError, by design), so adding one must fail here until the client decides on it."""
    engine = _engine_lifecycle_states()
    assert "quarantine" in engine, "guard: the engine vocabulary still names quarantine"
    assert RESOLVABLE_LIFECYCLE_STATES == engine - {"quarantine"}
