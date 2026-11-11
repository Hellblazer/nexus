# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpCatalogClient.chash_constraints_validated`` (nexus-f3yqd.6, F30): whether the chash width CHECKs on
``nexus.chunks`` and ``nexus.catalog_document_chunks`` are VALIDATED (``GET /v1/schema/chash-conformance``, engine
0.1.189, capability ``chash_conformance_constraints_supported``; any authenticated token).

``True`` only when the engine answers, lists both poison tables, and every listed constraint is present and validated with ``all_validated`` true:
then no row of those tables can be non-conformant and the doctor skips its scans. ``False`` is an answer that is not
validated (every store until the 0.1.190 VALIDATE job runs). ``None`` is no answer. Both mean scan as before.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient

_VALID = {"all_validated": True, "constraints": [
    {"table": "nexus.chunks", "name": "chunks_chash_octet_check", "present": True, "validated": True, "definition_matches": True},
    {"table": "nexus.catalog_document_chunks", "name": "catalog_document_chunks_chash_octet_check", "present": True, "validated": True, "definition_matches": True},
]}


def _client(monkeypatch: pytest.MonkeyPatch, *, flag: bool | None = True, get=None) -> tuple[HttpCatalogClient, list]:
    c = object.__new__(HttpCatalogClient)
    calls: list = []
    flags = {} if flag is None else {"chash_conformance_constraints_supported": flag}
    monkeypatch.setattr(c, "_version_flags", lambda: flags, raising=False)

    def record(path, **kw):
        calls.append((path, kw))
        return get(path, **kw) if callable(get) else get

    monkeypatch.setattr(c, "_get_unprefixed", record, raising=False)
    return c, calls


def test_a_validated_store_reads_true(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_VALID)
    assert c.chash_constraints_validated() is True
    ((path, kw),) = calls
    assert path == "/v1/schema/chash-conformance" and kw["idempotent"] is False and 0 < kw["timeout"] < 30


@pytest.mark.parametrize("body", [
    {**_VALID, "all_validated": False},
    {"all_validated": True, "constraints": [{**_VALID["constraints"][0], "validated": False}, _VALID["constraints"][1]]},
    {"all_validated": True, "constraints": [{**_VALID["constraints"][0], "present": False}, _VALID["constraints"][1]]},
    {"all_validated": True, "constraints": []},
    {"all_validated": True, "constraints": [{**_VALID["constraints"][0], "definition_matches": False}, _VALID["constraints"][1]]},
    {"all_validated": True, "constraints": [{k: v for k, v in _VALID["constraints"][0].items() if k != "definition_matches"},
                                            _VALID["constraints"][1]]},
    {"all_validated": True, "constraints": [_VALID["constraints"][0]]},
    {"all_validated": True, "constraints": [_VALID["constraints"][0],
                                            {**_VALID["constraints"][1], "table": "nexus.some_other_table"}]},
])
def test_anything_short_of_every_constraint_validated_reads_false(monkeypatch: pytest.MonkeyPatch, body: dict) -> None:
    c, _calls = _client(monkeypatch, get=body)
    assert c.chash_constraints_validated() is False


@pytest.mark.parametrize("flag", [False, None])
def test_an_engine_without_the_flag_is_not_asked(monkeypatch: pytest.MonkeyPatch, flag: bool | None) -> None:
    c, calls = _client(monkeypatch, flag=flag, get=_VALID)
    assert c.chash_constraints_validated() is None and calls == []


@pytest.mark.parametrize("status", [403, 404, 503])
def test_a_refusal_missing_route_or_timeout_is_no_answer(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def fail(path, **kw):
        request = httpx.Request("GET", "http://engine" + path)
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))

    c, _calls = _client(monkeypatch, get=fail)
    assert c.chash_constraints_validated() is None


@pytest.mark.parametrize("body", [None, [], {"all_validated": "yes", "constraints": []}, {"all_validated": True}])
def test_a_malformed_body_is_no_answer(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    c, _calls = _client(monkeypatch, get=body)
    assert c.chash_constraints_validated() is None


@pytest.mark.parametrize("table", [["nexus.chunks"], {"t": 1}, None])
def test_a_constraint_without_a_table_name_is_malformed(monkeypatch: pytest.MonkeyPatch, table: object) -> None:
    body = {**_VALID, "constraints": [{**_VALID["constraints"][0], "table": table}, _VALID["constraints"][1]]}
    c, _calls = _client(monkeypatch, get=body)
    assert c.chash_constraints_validated() is None
