# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-spujb: every chunk must fit its embedding model's token window.

bge-base-en-v1.5 truncates at 512 tokens and MiniLM at 256, and the rest of
the chunk silently drops out of the vector. The Voyage models take 32,000,
which the 12 KB chunk cap keeps out of reach. The window belongs to the
model, so it is looked up by the model token, never by deployment mode.

These tests count with a synthetic WordLevel tokenizer (one token per word
or punctuation run, no special tokens), so they need no provisioned model.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs
from tokenizers import Tokenizer, models, pre_tokenizers

from nexus import embed_window
from nexus.chunker import chunk_file, split_line_chunks_to_window, split_text_to_token_window
from nexus.corpus import CANONICAL_EMBEDDING_MODELS
from nexus.db.local_ef import LOCAL_EMBEDDING_TOKENS
from nexus.embed_window import MODEL_MAX_TOKENS, TokenWindow, window_for_collection, window_for_model
from nexus.md_chunker import SemanticMarkdownChunker
from nexus.pdf_chunker import PDFChunker

REPO_ROOT = Path(__file__).resolve().parents[1]


def _synthetic_tokenizer(path: Path) -> Path:
    tok = Tokenizer(models.WordLevel(vocab={"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(path))
    return path


@pytest.fixture
def window(tmp_path) -> TokenWindow:
    return TokenWindow(20, _synthetic_tokenizer(tmp_path / "tokenizer.json"))


@pytest.fixture(autouse=True)
def _fresh_window_cache():
    embed_window.window_for_model.cache_clear()
    yield
    embed_window.window_for_model.cache_clear()


# ── the table ────────────────────────────────────────────────────────────────

def test_every_writable_model_has_a_window() -> None:
    """A model with no entry would be unguarded, so a new model token has to
    land in the table in the same change that makes it writable."""
    for token in LOCAL_EMBEDDING_TOKENS | CANONICAL_EMBEDDING_MODELS:
        assert token in MODEL_MAX_TOKENS, token


def test_the_local_windows_are_the_engine_truncation_lengths() -> None:
    """Bge768Embedder.MAX_SEQ_LEN and OnnxEmbedder's maxLength."""
    assert MODEL_MAX_TOKENS["bge-base-en-v15-768"] == 512
    assert MODEL_MAX_TOKENS["minilm-l6-v2-384"] == 256


def test_the_canonical_write_models_have_no_window_to_enforce() -> None:
    """The 12 KB chunk cap keeps every chunk inside their window, whatever
    mode writes them, so no tokenizer is loaded for them."""
    assert CANONICAL_EMBEDDING_MODELS
    for model in CANONICAL_EMBEDDING_MODELS:
        assert window_for_model(model) is None, model


def test_bge_counts_with_the_provisioned_tokenizer(tmp_path, monkeypatch) -> None:
    _synthetic_tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    w = window_for_model("bge-base-en-v15-768")
    assert w is not None and w.max_tokens == 512
    assert w.count("three short words") == 3


def test_the_fastembed_name_resolves_to_the_same_window(tmp_path, monkeypatch) -> None:
    """doc_indexer's local embed path reports local_ef.model_name, the
    fastembed name, where collection names carry the model token."""
    _synthetic_tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    w = window_for_model("BAAI/bge-base-en-v1.5")
    assert w is not None and w.max_tokens == 512


# ── RDR-224 guest walk: the window follows the collection, not the Python EF ─

def test_the_window_follows_the_collection_name_token_not_the_python_ef(
    tmp_path, monkeypatch
) -> None:
    """On a service-backed local install `nx init` provisions bge-768 for the
    engine, while the Python EF (no fastembed extra) reports MiniLM. Indexing
    passed that MiniLM name to the window lookup, so every index run warned
    about a missing MiniLM tokenizer at a Chroma-era path and checked chunks
    against a window the engine does not embed with. The collection name
    carries the model the engine really uses."""
    _synthetic_tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINILM_CACHE_DIR", str(tmp_path / "no-chroma-cache"))
    with capture_logs() as logs:
        w = window_for_collection("code__repo-1-1__bge-base-en-v15-768__v1", "all-MiniLM-L6-v2")
    assert w is not None and w.max_tokens == 512
    assert not [e for e in logs if e.get("event") == "embed_window_tokenizer_missing"], logs


def test_a_voyage_collection_name_needs_no_window_whatever_the_ef_says(
    tmp_path, monkeypatch, cloud_mode
) -> None:
    """The MiniLM tokenizer is PRESENT here, so a lookup keyed on the EF's
    model would return a 256-token window; only the collection name's
    voyage token keeps it None. Without the file the assertion would pass
    on a clean host even if the feature regressed."""
    monkeypatch.setenv("NX_MINILM_CACHE_DIR", str(tmp_path / "chroma"))
    from nexus.db.minilm_direct import artifact_dir

    (artifact_dir()).mkdir(parents=True)
    _synthetic_tokenizer(artifact_dir() / "tokenizer.json")
    # Positive control: the EF's own model does get a window from that file.
    control = window_for_model("all-MiniLM-L6-v2")
    assert control is not None and control.max_tokens == 256
    assert window_for_collection(
        "docs__repo-1-1__voyage-context-3__v1", "all-MiniLM-L6-v2"
    ) is None


def test_a_non_conformant_collection_falls_back_to_the_given_model(tmp_path, monkeypatch, cloud_mode) -> None:
    """A legacy two-segment name carries no token to read."""
    _synthetic_tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    w = window_for_collection("code__legacy-abcd1234", "BAAI/bge-base-en-v1.5")
    assert w is not None and w.max_tokens == 512
    assert window_for_collection("", "voyage-code-3") is None


def test_a_missing_tokenizer_warns_naming_the_path_and_gets_no_window(tmp_path, monkeypatch) -> None:
    """The client can miss the file the engine embeds from (a Python-only
    NX_SERVICE_BGE_DIR, the suite's fenced $HOME); refusing to index there
    would be worse than an unchecked window, so it is loud, not fatal."""
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path / "absent"))
    with capture_logs() as logs:
        assert window_for_model("bge-base-en-v15-768") is None
    hits = [e for e in logs if e.get("event") == "embed_window_tokenizer_missing"]
    assert hits and "tokenizer.json" in str(hits[0].get("path")), logs


def test_an_unknown_model_token_warns_and_gets_no_window() -> None:
    """Unguarded but visible: refusing to index is worse than today."""
    with capture_logs() as logs:
        assert window_for_model("no-such-model") is None
    assert [e for e in logs if e.get("event") == "embed_window_unknown_model"
            and e.get("model") == "no-such-model"], logs


# ── splitting ────────────────────────────────────────────────────────────────

def test_split_text_to_token_window_keeps_every_character(window) -> None:
    text = " ".join(f"w{i}" for i in range(200))
    pieces = split_text_to_token_window(text, window)
    assert "".join(pieces) == text
    assert all(window.count(p) <= window.max_tokens for p in pieces)
    assert len(pieces) >= 10


def test_markdown_chunks_fit_the_window(window) -> None:
    body = "# T\n\n" + " ".join(f"word{i}" for i in range(300)) + "\n"
    chunks = SemanticMarkdownChunker(token_window=window).chunk(body, {})
    assert all(window.count(c.text) <= window.max_tokens for c in chunks)
    joined = " ".join(c.text for c in chunks)
    assert [i for i in range(300) if f"word{i}" not in joined] == []
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_pdf_chunks_fit_the_window(window) -> None:
    text = " ".join(f"tok{i}." for i in range(300))
    chunks = PDFChunker(token_window=window).chunk(text, {})
    assert all(window.count(c.text) <= window.max_tokens for c in chunks)
    joined = " ".join(c.text for c in chunks)
    assert [i for i in range(300) if f"tok{i}." not in joined] == []
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_code_chunks_fit_the_window_and_keep_their_lines(window) -> None:
    lines = [f"x_{i} = {i}" for i in range(200)]
    chunks = chunk_file(Path("m.py"), "\n".join(lines) + "\n", token_window=window)
    assert all(window.count(c["text"]) <= window.max_tokens for c in chunks)
    joined = "\n".join(c["text"] for c in chunks)
    assert [ln for ln in lines if ln not in joined] == []
    for c in chunks:
        assert c["text"].count("\n") + 1 == c["line_end"] - c["line_start"] + 1
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_line_chunks_fit_the_window(window) -> None:
    """The prose indexer's line-chunk path (non-markdown text files)."""
    raw = [(1, 60, "\n".join(f"line {i} of text" for i in range(60)))]
    out = split_line_chunks_to_window(raw, window)
    assert all(window.count(t) <= window.max_tokens for _, _, t in out)
    assert "\n".join(t for _, _, t in out) == raw[0][2]
    assert out[0][0] == 1 and out[-1][1] == 60


# ── wiring ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("path", "needle"), [
    ("src/nexus/code_indexer.py", "token_window=window_for_collection(ctx.corpus, ctx.embedding_model)"),
    ("src/nexus/prose_indexer.py", "window_for_collection(ctx.corpus, ctx.embedding_model)"),
    ("src/nexus/doc_indexer.py", "window_for_collection(collection_name, target_model)"),
    ("src/nexus/pipeline_stages.py", "token_window=window_for_collection(corpus, target_model)"),
])
def test_every_indexer_chunks_to_its_models_window(path, needle) -> None:
    """Wiring pin: a chunk site that stops passing the window goes back to
    silent truncation on bge collections, and no unit test of a chunker
    would notice."""
    assert needle in (REPO_ROOT / path).read_text(encoding="utf-8")


def test_doc_indexer_pins_the_window_to_the_collection_not_the_ef() -> None:
    """`nx index md`, `nx index pdf` and `nx collection reindex` chunk through
    doc_indexer, where target_model is the Python EF's model on a local
    install. Both chunk functions must ask for the window by collection, and
    both partials must hand them the collection (nexus-25wlq)."""
    src = (REPO_ROOT / "src/nexus/doc_indexer.py").read_text(encoding="utf-8")
    assert "window_for_model" not in src
    assert src.count("window_for_collection(collection_name, target_model)") == 2
    assert src.count("collection_name=col_name") == 3  # pdf partial, markdown partial x2


def test_markdown_chunks_use_the_collections_window_not_the_efs(tmp_path, monkeypatch) -> None:
    """Behavioural pin: a bge collection indexed while the EF says MiniLM is
    chunked to bge's 512 tokens, and no MiniLM tokenizer is looked up."""
    from nexus.doc_indexer import _markdown_chunks

    _synthetic_tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINILM_CACHE_DIR", str(tmp_path / "no-chroma-cache"))
    md = tmp_path / "doc.md"
    md.write_text("# T\n\n" + " ".join(f"word{i}" for i in range(1500)) + "\n", encoding="utf-8")
    with capture_logs() as logs:
        out = _markdown_chunks(
            md, "h", "all-MiniLM-L6-v2", "2026-01-01", "corp",
            collection_name="docs__repo-1-1__bge-base-en-v15-768__v1",
        )
    assert not [e for e in logs if e.get("event") == "embed_window_tokenizer_missing"], logs
    w = window_for_model("bge-base-en-v15-768")
    assert w is not None and len(out) >= 3
    assert all(w.count(text) <= 512 for _id, text, _meta in out)


def test_pdf_chunks_use_the_collections_window_not_the_efs(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from nexus import doc_indexer

    _synthetic_tokenizer(tmp_path / "tokenizer.json")
    monkeypatch.setenv("NX_SERVICE_BGE_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINILM_CACHE_DIR", str(tmp_path / "no-chroma-cache"))
    seen: list = []
    real = doc_indexer.PDFChunker

    def spy(*a, **kw):
        seen.append(kw.get("token_window"))
        return real(*a, **kw)

    monkeypatch.setattr(doc_indexer, "PDFChunker", spy)
    monkeypatch.setattr(
        doc_indexer.PDFExtractor, "extract",
        lambda self, *a, **kw: SimpleNamespace(
            text=" ".join(f"tok{i}." for i in range(1500)),
            metadata={"page_count": 1, "pages_with_text": [1]},
        ),
    )
    with capture_logs() as logs:
        try:
            doc_indexer._pdf_chunks(
                tmp_path / "x.pdf", "h", "all-MiniLM-L6-v2", "2026-01-01", "corp",
                git_meta={}, collection_name="docs__repo-1-1__bge-base-en-v15-768__v1",
            )
        except Exception:  # noqa: BLE001 — only the chunker's window argument is under test
            pass
    assert not [e for e in logs if e.get("event") == "embed_window_tokenizer_missing"], logs
    assert seen and seen[0] is not None and seen[0].max_tokens == 512
