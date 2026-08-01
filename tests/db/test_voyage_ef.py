# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nexus-owned Voyage embedding function (RDR-155 P4b P3).

Replaces ``chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction`` at
its two surviving call sites (``db/t3.py::_build_embedding_fn`` and the
``tests/db/test_embed_parity.py`` cloud-standard oracle) so the chromadb
dependency can leave pyproject.

The chroma implementation is a thin wrapper over ``voyageai.Client`` --
``voyageai>=0.2`` is already a first-class nexus dependency and nexus already
constructs ``voyageai.Client`` directly in the indexer paths. These tests pin
the wrapper semantics that must be preserved byte-for-byte, because the Java
engine's ``VoyageEmbedder`` is gated against this Python path in the embed
parity suite: the exact ``embed()`` kwargs and the float32 dtype are the
contract, not incidental detail.
"""
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from nexus.db.voyage_ef import VoyageEmbeddingFunction


def _fake_client(vectors):
    client = MagicMock()
    client.embed.return_value = MagicMock(embeddings=vectors)
    return client


def test_name_matches_the_chroma_oracle():
    """Collection metadata records the EF name; changing it would silently
    re-key existing collections."""
    assert VoyageEmbeddingFunction.name() == "voyageai"


def test_call_forwards_the_exact_embed_kwargs():
    """The four kwargs chroma passed are the wire contract with Voyage.

    input_type in particular is load-bearing: CCE collections embed with
    query/document subtypes and mixing them is the documented
    random-noise-similarity bug class (t3.py::_cce_embed).
    """
    client = _fake_client([[0.1, 0.2]])
    with patch("voyageai.Client", return_value=client):
        ef = VoyageEmbeddingFunction(model_name="voyage-code-3", api_key="k")
        ef(input=["hello"])

    client.embed.assert_called_once_with(
        texts=["hello"],
        model="voyage-code-3",
        input_type=None,
        truncation=True,
    )


def test_returns_float32_numpy_arrays():
    """Parity oracle asserts float32; returning float64 would make the Java
    comparison fail on dtype rather than on a real embedding divergence."""
    client = _fake_client([[0.1, 0.2], [0.3, 0.4]])
    with patch("voyageai.Client", return_value=client):
        ef = VoyageEmbeddingFunction(model_name="voyage-code-3", api_key="k")
        out = ef(input=["a", "b"])

    assert len(out) == 2
    for vec in out:
        assert isinstance(vec, np.ndarray)
        assert vec.dtype == np.float32
    np.testing.assert_allclose(out[0], np.array([0.1, 0.2], dtype=np.float32))


def test_input_type_and_truncation_are_forwarded_when_set():
    client = _fake_client([[0.5]])
    with patch("voyageai.Client", return_value=client):
        ef = VoyageEmbeddingFunction(
            model_name="voyage-context-3",
            api_key="k",
            input_type="query",
            truncation=False,
        )
        ef(input=["q"])

    client.embed.assert_called_once_with(
        texts=["q"], model="voyage-context-3", input_type="query", truncation=False,
    )


def test_accepts_input_positionally_and_by_keyword():
    """InMemoryVectorClient calls ``self._ef(input=documents)`` by keyword
    (inmemory_vector_store.py:160); other call sites pass positionally."""
    client = _fake_client([[1.0]])
    with patch("voyageai.Client", return_value=client):
        ef = VoyageEmbeddingFunction(model_name="voyage-3", api_key="k")
        assert len(ef(["x"])) == 1
        assert len(ef(input=["x"])) == 1


def test_embed_query_delegates_to_call():
    """Chroma EF protocol surface used by the in-memory store's query path
    (inmemory_vector_store.py:168 probes for embed_query first)."""
    client = _fake_client([[2.0]])
    with patch("voyageai.Client", return_value=client):
        ef = VoyageEmbeddingFunction(model_name="voyage-3", api_key="k")
        out = ef.embed_query(["q"])
    assert len(out) == 1


def test_missing_api_key_fails_loud():
    """No silent fallback for a credential problem -- an unauthenticated EF
    would otherwise surface as an opaque API error at first embed."""
    with patch.dict("os.environ", {}, clear=True), pytest.raises(ValueError, match="VOYAGE_API_KEY"):
        VoyageEmbeddingFunction(model_name="voyage-3", api_key=None)


def test_api_key_falls_back_to_environment():
    client = _fake_client([[3.0]])
    with patch.dict("os.environ", {"VOYAGE_API_KEY": "from-env"}, clear=True), \
            patch("voyageai.Client", return_value=client) as ctor:
        VoyageEmbeddingFunction(model_name="voyage-3")
    ctor.assert_called_once_with(api_key="from-env")


def test_no_chromadb_import():
    """The whole point: this module must not IMPORT chromadb.

    Asserted over the parsed AST, not the source text -- the module docstring
    legitimately names the chroma class it replaces, and a substring ban would
    force that provenance out of the file to stay green.
    """
    import ast
    import inspect

    from nexus.db import voyage_ef

    tree = ast.parse(inspect.getsource(voyage_ef))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert not [m for m in imported if m.split(".")[0] == "chromadb"], imported
