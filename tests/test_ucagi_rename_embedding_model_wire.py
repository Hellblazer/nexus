# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ucagi: the rename request carries the new name's model token.

The engine rename keeps the SOURCE row's embedding_model (nexus-7f4o4),
so a conformant new name whose model token disagrees with that row would
register a name claiming a model the vectors are not in. The engine does not
parse names (RDR-204), so the client sends the token as ``embedding_model``
(the way it already sends content_type and owner_id) and the engine answers a
disagreement with 409 ``collection_model_mismatch``. The engine half is pinned
by CatalogHandlerRenameTest (real PG); these are the wire pins.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.collection_rename import rename_collection_data_plane

pytestmark = pytest.mark.usefixtures("cloud_mode")

_VOYAGE = "voyage-context-3"
_NEW = f"docs__1-1__{_VOYAGE}__v1"


def _capture(monkeypatch):
    sent: list[dict] = []
    client = HttpCatalogClient.__new__(HttpCatalogClient)
    monkeypatch.setattr(
        client, "_post",
        lambda path, body, **kw: sent.append(body) or {"renamed": {}},
        raising=False,
    )
    return client, sent


class TestWire:
    def test_a_conformant_new_name_sends_its_model_token(self, monkeypatch):
        client, sent = _capture(monkeypatch)
        client.rename_collection_cascade("docs__old", _NEW)
        assert sent[0]["embedding_model"] == _VOYAGE
        # sent beside the two attributes it travels with
        assert sent[0]["content_type"] == "docs"
        assert sent[0]["owner_id"] == "1-1"

    def test_the_int_returning_verb_sends_it_too(self, monkeypatch):
        client, sent = _capture(monkeypatch)
        client.rename_collection("docs__old", _NEW)
        assert sent[0]["embedding_model"] == _VOYAGE

    def test_a_legacy_new_name_carries_no_token_so_none_is_sent(self, monkeypatch):
        client, sent = _capture(monkeypatch)
        client.rename_collection_cascade("docs__old", "docs__new")
        assert "embedding_model" not in sent[0]

    def test_the_explicit_opt_out_omits_it(self, monkeypatch):
        client, sent = _capture(monkeypatch)
        client.rename_collection_cascade("docs__old", _NEW, check_model_token=False)
        assert "embedding_model" not in sent[0]
        assert sent[0]["content_type"] == "docs"


def _mismatch_exc() -> httpx.HTTPStatusError:
    body = {
        "error": "refusing to rename collection 'docs__old' (embedding model "
                 "'bge-base-en-v15-768') to 'x' (names 'voyage-context-3')",
        "reason": "collection_model_mismatch",
        "source_model": "bge-base-en-v15-768",
        "target_model": _VOYAGE,
    }
    req = httpx.Request("POST", "http://e/v1/catalog/collections/rename")
    resp = httpx.Response(409, content=json.dumps(body).encode(), request=req)
    return httpx.HTTPStatusError("409 Conflict", request=req, response=resp)


class TestDataPlane:
    def _t3(self) -> MagicMock:
        t3 = MagicMock()
        t3.collection_exists = MagicMock(side_effect=lambda n: n == "docs__old")
        return t3

    def test_default_call_to_the_client_is_unchanged(self):
        client = MagicMock()
        client.rename_collection_cascade = MagicMock(return_value={})
        rename_collection_data_plane("docs__old", _NEW, t3_db=self._t3(), catalog=client)
        client.rename_collection_cascade.assert_called_once_with("docs__old", _NEW)

    def test_the_opt_out_is_forwarded_only_when_set(self):
        client = MagicMock()
        client.rename_collection_cascade = MagicMock(return_value={})
        rename_collection_data_plane(
            "docs__old", _NEW, t3_db=self._t3(), catalog=client,
            check_model_token=False,
        )
        client.rename_collection_cascade.assert_called_once_with(
            "docs__old", _NEW, check_model_token=False,
        )

    def test_an_engine_model_mismatch_surfaces_the_engines_text(self):
        client = MagicMock()
        client.rename_collection_cascade = MagicMock(side_effect=_mismatch_exc())
        with pytest.raises(click.ClickException) as ei:
            rename_collection_data_plane(
                "docs__old", _NEW, t3_db=self._t3(), catalog=client,
            )
        msg = str(ei.value.message)
        assert "unchanged" in msg
        assert "bge-base-en-v15-768" in msg, msg
        assert "409 Conflict" not in msg, "the bare status line hides the models"

    def test_another_409_keeps_its_status_text(self):
        req = httpx.Request("POST", "http://e/x")
        resp = httpx.Response(409, content=b'{"error":"target collection already exists"}', request=req)
        client = MagicMock()
        client.rename_collection_cascade = MagicMock(
            side_effect=httpx.HTTPStatusError("409 Conflict", request=req, response=resp),
        )
        with pytest.raises(click.ClickException) as ei:
            rename_collection_data_plane(
                "docs__old", _NEW, t3_db=self._t3(), catalog=client,
            )
        assert "409 Conflict" in str(ei.value.message)


_COUNTS = {
    "tax_topics": 0, "tax_assignments": 0, "tax_meta": 0,
    "chash": 0, "catalog_docs": 0,
}


class TestForcePrefixChangeOptsOut:
    """nx collection rename --force-prefix-change is the documented override
    of the model-token guard; the engine check must not take it away."""

    def _invoke(self, *extra: str):
        from nexus.commands.collection import rename_cmd

        row = {"embedding_model": "bge-base-en-v15-768", "lifecycle_state": "live",
               "content_type": "docs", "owner_id": "legacy"}
        with patch(
            "nexus.commands.collection.rename_collection_data_plane",
            return_value=_COUNTS,
        ) as plane, patch(
            "nexus.mcp_infra.get_collection_row",
            side_effect=lambda name, **_k: row if name == "docs__foo" else None,
        ):
            result = CliRunner().invoke(rename_cmd, ["docs__foo", _NEW, *extra])
        return result, plane

    def test_forced_rename_sets_the_opt_out(self):
        result, plane = self._invoke("--force-prefix-change")
        assert result.exit_code == 0, result.output
        plane.assert_called_once_with("docs__foo", _NEW, check_model_token=False)

    def test_an_agreeing_rename_does_not(self):
        from nexus.commands.collection import rename_cmd

        row = {"embedding_model": _VOYAGE, "lifecycle_state": "live",
               "content_type": "docs", "owner_id": "legacy"}
        with patch(
            "nexus.commands.collection.rename_collection_data_plane",
            return_value=_COUNTS,
        ) as plane, patch(
            "nexus.mcp_infra.get_collection_row",
            side_effect=lambda name, **_k: row if name == "docs__foo" else None,
        ):
            result = CliRunner().invoke(rename_cmd, ["docs__foo", _NEW])
        assert result.exit_code == 0, result.output
        plane.assert_called_once_with("docs__foo", _NEW)
