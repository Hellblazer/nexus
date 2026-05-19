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
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.commands.catalog import doctor_cmd


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def chroma_client():
    """Fresh EphemeralClient with collections cleared. The DefaultEmbeddingFunction
    is ONNX MiniLM L6 v2 → 384-dim, which gives us the realistic 4.28-era
    bug shape for free (collection is named for voyage but actually holds
    MiniLM vectors)."""
    client = chromadb.EphemeralClient()
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
    return _FakeT3()


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
