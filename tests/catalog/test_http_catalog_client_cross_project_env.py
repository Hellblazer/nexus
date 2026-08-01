# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-e7cys: NEXUS_CATALOG_ALLOW_CROSS_PROJECT env-var-to-wire-field plumbing.

The nexus-3e4s cross-project source_uri containment guard is enforced
engine-side (CatalogRepository.deriveSourceUri's 5-arg overload). The LOCAL
arm's escape hatch was ``NEXUS_CATALOG_ALLOW_CROSS_PROJECT=1``, read directly
by the process doing the registering — but the engine has no access to the
CLIENT's environment. The honest wire shape is a request field
(``allow_cross_project``) the client populates from its own environment.

These tests never make a real HTTP call: ``client._post`` is monkeypatched to
capture the outgoing payload, matching the pattern used throughout
tests/catalog/test_http_catalog_client.py.
"""
from __future__ import annotations

from typing import Any

from nexus.catalog.http_catalog_client import HttpCatalogClient

_ENV = "NEXUS_CATALOG_ALLOW_CROSS_PROJECT"


def _client() -> HttpCatalogClient:
    return HttpCatalogClient(base_url="http://fake-nexus-engine", tenant="t", _token="tok")


class TestRegisterEnvPlumbing:
    def test_forwards_allow_cross_project_when_env_set(self, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        captured: dict[str, Any] = {}

        def _fake_post(path: str, body: dict | None = None) -> Any:
            captured.update(body or {})
            return {"tumbler": "1.1.1"}

        client = _client()
        client._post = _fake_post  # type: ignore[method-assign]
        client.register("1.1", "Doc", source_uri="file:///repo/root/a.py")

        assert captured.get("allow_cross_project") is True

    def test_omits_allow_cross_project_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        captured: dict[str, Any] = {}

        def _fake_post(path: str, body: dict | None = None) -> Any:
            captured.update(body or {})
            return {"tumbler": "1.1.1"}

        client = _client()
        client._post = _fake_post  # type: ignore[method-assign]
        client.register("1.1", "Doc", source_uri="file:///repo/root/a.py")

        assert "allow_cross_project" not in captured

    def test_explicit_kwarg_wins_over_env_default(self, monkeypatch) -> None:
        # Env says "bypass"; the caller explicitly says "don't" — the explicit
        # value must win, matching every other kwargs.update()-last default in
        # this client (payload.update(kwargs) runs AFTER the env-var default).
        monkeypatch.setenv(_ENV, "1")
        captured: dict[str, Any] = {}

        def _fake_post(path: str, body: dict | None = None) -> Any:
            captured.update(body or {})
            return {"tumbler": "1.1.1"}

        client = _client()
        client._post = _fake_post  # type: ignore[method-assign]
        client.register(
            "1.1", "Doc", source_uri="file:///repo/root/a.py", allow_cross_project=False
        )

        assert captured.get("allow_cross_project") is False


class TestRegisterManyEnvPlumbing:
    def test_forwards_allow_cross_project_to_every_doc_when_env_set(self, monkeypatch) -> None:
        monkeypatch.setenv(_ENV, "1")
        pages: list[list[dict]] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            assert path == "/doc/register_many"
            page = body["docs"]
            pages.append(page)
            return {"tumblers": [f"1.1.{i + 1}" for i in range(len(page))]}

        client = _client()
        client._post = _fake_post  # type: ignore[method-assign]
        docs = [{"title": "a", "file_path": "a.py"}, {"title": "b", "file_path": "b.py"}]
        client.register_many("1.1", docs)

        assert len(pages) == 1
        assert all(d.get("allow_cross_project") is True for d in pages[0])

    def test_omits_allow_cross_project_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(_ENV, raising=False)
        pages: list[list[dict]] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            page = body["docs"]
            pages.append(page)
            return {"tumblers": [f"1.1.{i + 1}" for i in range(len(page))]}

        client = _client()
        client._post = _fake_post  # type: ignore[method-assign]
        docs = [{"title": "a", "file_path": "a.py"}]
        client.register_many("1.1", docs)

        assert "allow_cross_project" not in pages[0][0]

    def test_explicit_per_doc_value_wins_over_env_default(self, monkeypatch) -> None:
        # Dict-merge precedence ({"allow_cross_project": True, **d}): a doc
        # that already names the field keeps its own value; a doc that
        # doesn't gets the env-var default.
        monkeypatch.setenv(_ENV, "1")
        pages: list[list[dict]] = []

        def _fake_post(path: str, body: dict | None = None) -> Any:
            page = body["docs"]
            pages.append(page)
            return {"tumblers": [f"1.1.{i + 1}" for i in range(len(page))]}

        client = _client()
        client._post = _fake_post  # type: ignore[method-assign]
        docs = [
            {"title": "a", "file_path": "a.py", "allow_cross_project": False},
            {"title": "b", "file_path": "b.py"},
        ]
        client.register_many("1.1", docs)

        page = pages[0]
        by_title = {d["title"]: d for d in page}
        assert by_title["a"]["allow_cross_project"] is False
        assert by_title["b"]["allow_cross_project"] is True
