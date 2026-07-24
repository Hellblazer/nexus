# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-j9ey: catalog doctor --name-vs-embed-dim check.

Detects the 4.28-era write-side bug where local-mode installs produced
collections named ``code__<owner>__voyage-code-3__v1`` while writing
384-dim MiniLM vectors inside. The forward fix shipped in 4.32.0
(commit fb0c34fc, PR #682) — fresh indexes now produce correctly-named
collections — but pre-4.32 data remains mislabeled.

The check samples one chunk's embedding per conformant collection,
compares the actual dim to the dim implied by the ``__<model>__``
segment, and FAILs on any mismatch. Read-only against T3; recommends
``nx collection rename`` for remediation.
"""
from __future__ import annotations

import json

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.commands.catalog_cmds.doctor import doctor_cmd
from tests.conftest import make_vector_test_client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    """Fresh EphemeralClient with collections cleared. The DefaultEmbeddingFunction
    is ONNX MiniLM L6 v2 → 384-dim, which gives us the realistic 4.28-era
    bug shape for free (collection is named for voyage but actually holds
    MiniLM vectors)."""
    client = make_vector_test_client()
    for col in list(client.list_collections()):
        try:
            client.delete_collection(col.name)
        except Exception:
            pass
    return client


def _seed(client, name: str, n_chunks: int = 1) -> None:
    col = client.get_or_create_collection(
        name=name, embedding_function=DefaultEmbeddingFunction(),
    )
    col.add(
        ids=[f"c{i}" for i in range(n_chunks)],
        documents=[f"sample content {i}" for i in range(n_chunks)],
        metadatas=[{"_": "_"} for _ in range(n_chunks)],
    )


def _fake_t3(client, names: list[str]):
    class _FakeT3:
        _client = client

        def list_collections(self):
            return [{"name": n} for n in names]

        def get_collection(self, name):
            return client.get_collection(name)

        def get_embeddings(self, collection_name, ids):
            import numpy as np
            col = client.get_collection(collection_name)
            result = col.get(ids=ids, include=["embeddings"])
            by_id = dict(zip(result["ids"], result["embeddings"]))
            return np.array(
                [by_id[i] for i in ids if i in by_id], dtype=np.float32,
            )
    return _FakeT3()


def _fake_t3_service_like(client, names: list[str]):
    """Simulates HttpVectorClient's public surface only — no ``_client``
    attribute. Regression guard for nexus-pyv0e: the doctor check must not
    reach into Chroma internals, only the dual-mode-safe public methods
    (``get_collection`` / ``get_embeddings``) both T3Database and
    HttpVectorClient expose."""
    class _FakeServiceT3:
        def list_collections(self):
            return [{"name": n} for n in names]

        def get_collection(self, name):
            return client.get_collection(name)

        def get_embeddings(self, collection_name, ids):
            import numpy as np
            col = client.get_collection(collection_name)
            result = col.get(ids=ids, include=["embeddings"])
            by_id = dict(zip(result["ids"], result["embeddings"]))
            return np.array(
                [by_id[i] for i in ids if i in by_id], dtype=np.float32,
            )
    return _FakeServiceT3()


class TestNameVsEmbedDim:
    def test_pass_when_local_token_matches_dim(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Conformant name with ``minilm-l6-v2-384`` token + 384-dim chunks → PASS."""
        name = "code__myproj__minilm-l6-v2-384__v1"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: _fake_t3(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is True
        assert payload["mismatches"] == []
        assert payload["checked"] == 1

    def test_fail_when_voyage_name_holds_minilm_vectors(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """The 4.28-era bug: name claims voyage-code-3 (1024d expected)
        but content is 384-dim MiniLM. Must FAIL and identify the
        collection."""
        name = "code__myproj__voyage-code-3__v1"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: _fake_t3(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is False
        assert len(payload["mismatches"]) == 1
        m = payload["mismatches"][0]
        assert m["collection"] == name
        assert m["expected_dim"] == 1024
        assert m["actual_dim"] == 384
        assert m["claimed_model"] == "voyage-code-3"

    def test_fail_when_voyage_context_name_holds_minilm_vectors(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Same shape for the docs side: voyage-context-3 claim, MiniLM body."""
        name = "docs__myproj__voyage-context-3__v1"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: _fake_t3(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is False
        m = payload["mismatches"][0]
        assert m["expected_dim"] == 1024
        assert m["actual_dim"] == 384

    def test_legacy_name_skipped(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Legacy 2-segment names (``code__myproj-abc12345``) have no
        embedder claim to verify. Skip them; report 0 checked."""
        name = "code__myproj-cafef00d"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: _fake_t3(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is True
        assert payload["checked"] == 0
        assert payload["skipped_non_conformant"] == 1

    def test_taxonomy_collection_skipped(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """``taxonomy__*`` carries centroid embeddings, out of scope."""
        _seed(chroma_client, "taxonomy__centroids")
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda: _fake_t3(chroma_client, ["taxonomy__centroids"]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is True
        assert payload["checked"] == 0

    def test_empty_collection_skipped_with_note(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Empty conformant collection has no chunk to sample. Don't FAIL
        on it; record in ``empty`` list so operator knows it was skipped."""
        name = "code__myproj__voyage-code-3__v1"
        chroma_client.get_or_create_collection(
            name=name, embedding_function=DefaultEmbeddingFunction(),
        )
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: _fake_t3(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is True
        assert payload["checked"] == 0
        assert name in payload["empty"]

    def test_service_mode_no_client_attribute_does_not_crash(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression (nexus-pyv0e): HttpVectorClient has no ``_client``
        attribute — reaching into it raised an unhandled AttributeError in
        service/cloud mode. A T3 handle exposing only the public
        get_collection/get_embeddings surface must work identically."""
        name = "code__myproj__minilm-l6-v2-384__v1"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda: _fake_t3_service_like(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is True
        assert payload["mismatches"] == []
        assert payload["checked"] == 1

    def test_service_mode_mismatch_still_detected(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Same mismatch-detection guarantee holds on the service-like
        (no ``_client``) path, not just the local-Chroma path."""
        name = "code__myproj__voyage-code-3__v1"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3",
            lambda: _fake_t3_service_like(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim", "--json"],
        )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is False
        m = payload["mismatches"][0]
        assert m["collection"] == name
        assert m["expected_dim"] == 1024
        assert m["actual_dim"] == 384

    def test_text_output_includes_remediation_hint(
        self, runner, chroma_client, monkeypatch: pytest.MonkeyPatch,
    ):
        """Non-JSON output names the offending collection and suggests
        ``nx collection rename`` with the correct target token."""
        name = "code__myproj__voyage-code-3__v1"
        _seed(chroma_client, name)
        monkeypatch.setattr(
            "nexus.db.make_t3", lambda: _fake_t3(chroma_client, [name]),
        )
        result = runner.invoke(
            doctor_cmd, ["--name-vs-embed-dim"],
        )
        assert result.exit_code == 1
        assert "name-vs-embed-dim: FAIL" in result.output
        assert name in result.output
        assert "rename" in result.output.lower()


class TestNameVsEmbedDimRealHttpVectorClient:
    """nexus-umvh2 doctrine (Wave-review CRITICAL, critic finding on
    nexus-pyv0e): a hand-rolled duck-typed fake proves the fake's shape
    doesn't crash, not that the REAL HttpVectorClient's actual
    get_collection/get_embeddings work through the real doctor command in
    service mode. Construct the real client; fake only the HTTP transport
    (``_post``/``_get``) — a missing/renamed method on HttpVectorClient
    fails HARD here instead of being absorbed by a duck-typed double.
    """

    @pytest.fixture(autouse=True)
    def _reset_client(self):
        from nexus.db.http_vector_client import reset_http_vector_client_for_tests
        reset_http_vector_client_for_tests()
        yield
        reset_http_vector_client_for_tests()

    def test_real_client_pass_when_dim_matches(
        self, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        from nexus.db.http_vector_client import HttpVectorClient

        name = "code__nexus-1-1__voyage-code-3__v1"

        def fake_get(path, **kw):
            assert path == "/v1/vectors/stats"
            return [{"name": name, "dim": 1024, "count": 1}]

        def fake_post(path, body, **kw):
            if path == "/v1/vectors/get":
                assert body["collection"] == name
                return {"ids": ["c1"], "documents": ["x"], "metadatas": [{}]}
            if path == "/v1/vectors/get-embeddings":
                assert body == {"collection": name, "ids": ["c1"]}
                return {"embeddings": [[0.1] * 1024]}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        monkeypatch.setattr("nexus.db.make_t3", lambda: HttpVectorClient())

        result = runner.invoke(doctor_cmd, ["--name-vs-embed-dim", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is True
        assert payload["checked"] == 1
        assert payload["mismatches"] == []

    def test_real_client_fail_when_dim_mismatches(
        self, runner, monkeypatch: pytest.MonkeyPatch,
    ):
        """The 4.28-era bug shape, reproduced through the REAL
        HttpVectorClient: name claims voyage-code-3 (1024d) but the
        service actually holds 384-dim vectors."""
        from nexus.db.http_vector_client import HttpVectorClient

        name = "code__nexus-1-1__voyage-code-3__v1"

        def fake_get(path, **kw):
            return [{"name": name, "dim": 384, "count": 1}]

        def fake_post(path, body, **kw):
            if path == "/v1/vectors/get":
                return {"ids": ["c1"], "documents": ["x"], "metadatas": [{}]}
            if path == "/v1/vectors/get-embeddings":
                return {"embeddings": [[0.1] * 384]}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        monkeypatch.setattr("nexus.db.make_t3", lambda: HttpVectorClient())

        result = runner.invoke(doctor_cmd, ["--name-vs-embed-dim", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)["name_vs_embed_dim"]
        assert payload["pass"] is False
        m = payload["mismatches"][0]
        assert m["collection"] == name
        assert m["expected_dim"] == 1024
        assert m["actual_dim"] == 384
