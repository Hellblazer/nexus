"""Local mode never embeds client-side when the engine embeds (nexus-b7s8t).

In local mode T3 is the service-backed ``HttpVectorClient`` and the engine
embeds server-side; ``upsert_chunks_with_embeddings`` discards whatever the
client sends. ``index_repository`` and ``nx index rdr`` nevertheless ran the
bge ONNX model on every chunk in this process — 466% CPU and ~2 GB RSS on a
16 GB laptop, competing with the engine JVM that then embedded the same
chunks again. The client embeds only when the vector backend is opted OUT
of service mode (the in-memory / chroma-injected test substrate).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.indexer import select_local_mode_embed_fn


class _NeverCalledEF:
    """Stands in for LocalEmbeddingFunction; raises if anything embeds."""

    model_name = "stand-in"

    def __call__(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError(f"client embedded {len(texts)} texts in service mode")


def test_service_mode_returns_the_server_embed_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    fn = select_local_mode_embed_fn(_NeverCalledEF())
    assert fn(["a", "b", "c"]) == [[], [], []]


def test_opted_out_backend_keeps_the_client_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
    ef = _NeverCalledEF()
    assert select_local_mode_embed_fn(ef) is ef


def test_index_rdr_in_local_service_mode_passes_no_embed_fn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx index rdr`` hands ``embed_fn=None`` down, so doc_indexer's own
    gate installs the server-embed stub; the local EF is never built."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "001-x.md").write_text("# RDR-001\n\nbody\n")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    def _boom(*a: object, **k: object) -> None:
        raise AssertionError("LocalEmbeddingFunction constructed in service mode")

    with (
        patch("nexus.db.local_ef.LocalEmbeddingFunction", _boom),
        patch("nexus.doc_indexer.batch_index_markdowns", return_value={}) as mock_batch,
    ):
        result = CliRunner().invoke(main, ["index", "rdr", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert mock_batch.call_args.kwargs["embed_fn"] is None
