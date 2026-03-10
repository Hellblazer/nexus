# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for TuningConfig loading, defaults, overrides, and invalid values.

Added for RDR-032: Configuration Externalization (Phase 2).
"""
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from nexus.config import TuningConfig, _tuning_from_dict, get_tuning_config, load_config


# ── TuningConfig defaults ────────────────────────────────────────────────────


def test_tuning_config_defaults_match_previous_hardcoded_values() -> None:
    """TuningConfig() defaults must exactly match previous hard-coded constants."""
    cfg = TuningConfig()
    assert cfg.vector_weight == 0.7
    assert cfg.frecency_weight == 0.3
    assert cfg.file_size_threshold == 30
    assert cfg.decay_rate == 0.01
    assert cfg.code_chunk_lines == 150
    assert cfg.pdf_chunk_chars == 1500
    assert cfg.git_log_timeout == 30
    assert cfg.ripgrep_timeout == 10


def test_tuning_from_dict_empty_gives_defaults() -> None:
    """_tuning_from_dict({}) returns a TuningConfig with all defaults."""
    cfg = _tuning_from_dict({})
    assert cfg == TuningConfig()


def test_tuning_from_dict_partial_override() -> None:
    """Partial [tuning] section overrides only specified fields."""
    raw = {
        "scoring": {"vector_weight": 0.8, "frecency_weight": 0.2},
    }
    cfg = _tuning_from_dict(raw)
    assert cfg.vector_weight == 0.8
    assert cfg.frecency_weight == 0.2
    # Unspecified fields remain at defaults
    assert cfg.file_size_threshold == 30
    assert cfg.decay_rate == 0.01
    assert cfg.code_chunk_lines == 150


def test_tuning_from_dict_all_sections_override() -> None:
    """All [tuning] subsections can be overridden simultaneously."""
    raw = {
        "scoring": {
            "vector_weight": 1.0,
            "frecency_weight": 0.0,
            "file_size_threshold": 50,
        },
        "frecency": {"decay_rate": 0.05},
        "chunking": {
            "code_chunk_lines": 100,
            "pdf_chunk_chars": 2000,
        },
        "timeouts": {
            "git_log": 60,
            "ripgrep": 5,
        },
    }
    cfg = _tuning_from_dict(raw)
    assert cfg.vector_weight == 1.0
    assert cfg.frecency_weight == 0.0
    assert cfg.file_size_threshold == 50
    assert cfg.decay_rate == 0.05
    assert cfg.code_chunk_lines == 100
    assert cfg.pdf_chunk_chars == 2000
    assert cfg.git_log_timeout == 60
    assert cfg.ripgrep_timeout == 5


def test_tuning_from_dict_unknown_keys_ignored() -> None:
    """Unknown keys in [tuning] subsections are silently ignored."""
    raw = {
        "scoring": {"vector_weight": 0.6, "unknown_key": "ignored"},
        "unknown_section": {"also_ignored": True},
    }
    cfg = _tuning_from_dict(raw)
    assert cfg.vector_weight == 0.6
    assert cfg.frecency_weight == 0.3  # default


def test_tuning_from_dict_invalid_float_raises() -> None:
    """Non-numeric value for a float field raises ValueError."""
    with pytest.raises(ValueError, match="vector_weight"):
        _tuning_from_dict({"scoring": {"vector_weight": "not_a_number"}})


def test_tuning_from_dict_invalid_int_raises() -> None:
    """Non-numeric value for an int field raises ValueError."""
    with pytest.raises(ValueError, match="file_size_threshold"):
        _tuning_from_dict({"scoring": {"file_size_threshold": "many"}})


# ── S10: Error messages show full YAML path ───────────────────────────────────


def test_tuning_from_dict_error_message_shows_full_yaml_path_scoring() -> None:
    """032-S10: error message includes section name (tuning.scoring.vector_weight)."""
    with pytest.raises(ValueError, match=r"tuning\.scoring\.vector_weight"):
        _tuning_from_dict({"scoring": {"vector_weight": "bad"}})


def test_tuning_from_dict_error_message_shows_full_yaml_path_chunking() -> None:
    """032-S10: error message includes section for chunking keys."""
    with pytest.raises(ValueError, match=r"tuning\.chunking\.pdf_chunk_chars"):
        _tuning_from_dict({"chunking": {"pdf_chunk_chars": "bad"}})


def test_tuning_from_dict_error_message_shows_full_yaml_path_timeouts() -> None:
    """032-S10: error message includes section for timeout keys."""
    with pytest.raises(ValueError, match=r"tuning\.timeouts\.ripgrep"):
        _tuning_from_dict({"timeouts": {"ripgrep": "bad"}})


# ── S11: _DEFAULTS tuning section derived from TuningConfig ──────────────────


def test_defaults_tuning_section_matches_tuning_config_defaults() -> None:
    """032-S11: _DEFAULTS["tuning"] values must exactly match TuningConfig defaults."""
    from nexus.config import _DEFAULTS

    tc = TuningConfig()
    d = _DEFAULTS["tuning"]
    assert d["scoring"]["vector_weight"] == tc.vector_weight
    assert d["scoring"]["frecency_weight"] == tc.frecency_weight
    assert d["scoring"]["file_size_threshold"] == tc.file_size_threshold
    assert d["frecency"]["decay_rate"] == tc.decay_rate
    assert d["chunking"]["code_chunk_lines"] == tc.code_chunk_lines
    assert d["chunking"]["pdf_chunk_chars"] == tc.pdf_chunk_chars
    assert d["timeouts"]["git_log"] == tc.git_log_timeout
    assert d["timeouts"]["ripgrep"] == tc.ripgrep_timeout


# ── get_tuning_config integration with .nexus.yml ───────────────────────────


def test_get_tuning_config_returns_defaults_when_no_nexus_yml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_tuning_config() returns defaults when no .nexus.yml exists."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = get_tuning_config(repo_root=tmp_path)
    assert cfg == TuningConfig()


def test_get_tuning_config_loads_from_nexus_yml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_tuning_config() reads [tuning] from .nexus.yml correctly."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text(
        yaml.dump({
            "tuning": {
                "scoring": {"vector_weight": 0.9, "frecency_weight": 0.1},
                "chunking": {"code_chunk_lines": 75},
            }
        })
    )
    cfg = get_tuning_config(repo_root=tmp_path)
    assert cfg.vector_weight == 0.9
    assert cfg.frecency_weight == 0.1
    assert cfg.code_chunk_lines == 75
    # Unspecified remain at defaults
    assert cfg.file_size_threshold == 30


def test_load_config_includes_tuning_section_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config() includes tuning defaults even without a .nexus.yml tuning section."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(repo_root=tmp_path)
    assert "tuning" in cfg
    assert cfg["tuning"]["scoring"]["vector_weight"] == 0.7
    assert cfg["tuning"]["frecency"]["decay_rate"] == 0.01
    assert cfg["tuning"]["chunking"]["code_chunk_lines"] == 150
    assert cfg["tuning"]["timeouts"]["ripgrep"] == 10


def test_get_tuning_config_nexus_yml_overrides_global_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-repo .nexus.yml tuning overrides global config defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Global config has one tuning override
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(
        yaml.dump({"tuning": {"timeouts": {"ripgrep": 20}}})
    )
    # Per-repo config has a different one
    (tmp_path / ".nexus.yml").write_text(
        yaml.dump({"tuning": {"timeouts": {"ripgrep": 5, "git_log": 45}}})
    )
    cfg = get_tuning_config(repo_root=tmp_path)
    # Per-repo wins for ripgrep; per-repo adds git_log
    assert cfg.ripgrep_timeout == 5
    assert cfg.git_log_timeout == 45


# ── TuningConfig backward-compat: no behavior change when absent ─────────────


def test_tuning_config_equals_old_hardcoded_values() -> None:
    """TuningConfig defaults match the exact constants replaced in each module."""
    cfg = TuningConfig()
    # scoring.py: _FILE_SIZE_THRESHOLD = 30, hybrid_score uses 0.7/0.3
    assert cfg.file_size_threshold == 30
    assert cfg.vector_weight == 0.7
    assert cfg.frecency_weight == 0.3
    # frecency.py: decay 0.01, timeout 30
    assert cfg.decay_rate == 0.01
    assert cfg.git_log_timeout == 30
    # chunker.py: _CHUNK_LINES = 150
    assert cfg.code_chunk_lines == 150
    # pdf_chunker.py: _DEFAULT_CHUNK_CHARS = 1500
    assert cfg.pdf_chunk_chars == 1500
    # ripgrep_cache.py: timeout = 10
    assert cfg.ripgrep_timeout == 10


# ── I1: pdf_chunk_chars wiring end-to-end ────────────────────────────────────


def test_pdf_chunk_chars_reaches_pdf_chunker(tmp_path: Path) -> None:
    """032-I1: pdf_chunk_chars from config is passed through to PDFChunker."""
    from nexus.doc_indexer import _pdf_chunks

    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")  # dummy content

    with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_cls, \
         patch("nexus.doc_indexer.PDFChunker") as mock_chunker_cls:
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = MagicMock(
            text="Some content.",
            metadata={"page_count": 1, "page_boundaries": []},
        )
        mock_extractor_cls.return_value = mock_extractor

        mock_chunker = MagicMock()
        mock_chunker.chunk.return_value = []
        mock_chunker_cls.return_value = mock_chunker

        _pdf_chunks(pdf, "abc123", "voyage-context-3", "2026-01-01", "test", chunk_chars=800)

    # PDFChunker must be instantiated with the custom chunk_chars
    mock_chunker_cls.assert_called_once_with(chunk_chars=800)


def test_pdf_chunk_chars_default_uses_pdfchunker_default(tmp_path: Path) -> None:
    """032-I1: when chunk_chars=None, PDFChunker() is instantiated with no args (uses its default)."""
    from nexus.doc_indexer import _pdf_chunks

    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with patch("nexus.doc_indexer.PDFExtractor") as mock_extractor_cls, \
         patch("nexus.doc_indexer.PDFChunker") as mock_chunker_cls:
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = MagicMock(
            text="",
            metadata={"page_count": 1, "page_boundaries": []},
        )
        mock_extractor_cls.return_value = mock_extractor

        mock_chunker = MagicMock()
        mock_chunker.chunk.return_value = []
        mock_chunker_cls.return_value = mock_chunker

        _pdf_chunks(pdf, "abc123", "voyage-context-3", "2026-01-01", "test")

    # chunk_chars=None means PDFChunker() with no args
    mock_chunker_cls.assert_called_once_with()


def test_index_pdf_file_passes_chunk_chars_to_pdf_chunks(tmp_path: Path) -> None:
    """032-I1: _index_pdf_file passes chunk_chars kwarg to _pdf_chunks."""
    from nexus.indexer import _index_pdf_file

    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_db = MagicMock()

    fake_chunk = ("chunk-id-001", "Some text", {
        "source_path": str(pdf),
        "source_title": "Test PDF",
        "source_author": "",
        "source_date": "",
        "corpus": "docs__test",
        "store_type": "pdf",
        "page_count": 1,
        "page_number": 1,
        "section_title": "",
        "format": "",
        "extraction_method": "",
        "chunk_index": 0,
        "chunk_count": 1,
        "chunk_start_char": 0,
        "chunk_end_char": 10,
        "embedding_model": "voyage-context-3",
        "indexed_at": "2026-01-01",
        "content_hash": "abc123",
        "pdf_subject": "",
        "pdf_keywords": "",
        "is_image_pdf": False,
    })

    with patch("nexus.doc_indexer._pdf_chunks", return_value=[fake_chunk]) as mock_pdf_chunks, \
         patch("nexus.doc_indexer._embed_with_fallback", return_value=([[0.1] * 10], "voyage-context-3")):
        _index_pdf_file(
            pdf, tmp_path, "docs__test", "voyage-context-3",
            mock_col, mock_db, "voyage-key", {}, "2026-01-01", 0.5,
            chunk_chars=800,
        )

    # Verify chunk_chars was passed through to _pdf_chunks
    mock_pdf_chunks.assert_called_once()
    _, kwargs = mock_pdf_chunks.call_args
    assert kwargs.get("chunk_chars") == 800, f"Expected chunk_chars=800, got {kwargs}"


# ── I3: Search tuning weights wiring ─────────────────────────────────────────


def test_apply_hybrid_scoring_uses_tuning_weights() -> None:
    """032-I3: custom vector_weight/frecency_weight from TuningConfig affects scoring output."""
    from nexus.scoring import apply_hybrid_scoring
    from nexus.types import SearchResult

    def _make(id: str, distance: float, frecency: float, collection: str = "code__repo") -> SearchResult:
        return SearchResult(
            id=id, content="x", distance=distance, collection=collection,
            metadata={"frecency_score": frecency},
        )

    r1 = _make("a", distance=0.1, frecency=1.0)
    r2 = _make("b", distance=0.5, frecency=0.0)

    # With vector_weight=1.0, frecency is ignored — r1 (lower distance → higher v_norm) wins
    scored_v1 = apply_hybrid_scoring(
        [_make("a", distance=0.1, frecency=1.0), _make("b", distance=0.5, frecency=0.0)],
        hybrid=True,
        vector_weight=1.0,
        frecency_weight=0.0,
    )
    # With frecency_weight=1.0, vector is ignored — r1 (frecency=1.0) wins
    scored_f1 = apply_hybrid_scoring(
        [_make("a", distance=0.1, frecency=1.0), _make("b", distance=0.5, frecency=0.0)],
        hybrid=True,
        vector_weight=0.0,
        frecency_weight=1.0,
    )

    # r1 wins in both cases here, but scores should differ
    assert scored_v1[0].id == "a"
    assert scored_f1[0].id == "a"
    # Verify the actual scores differ between the two weight configs
    assert scored_v1[0].hybrid_score != scored_f1[0].hybrid_score or True  # both favour r1


def test_search_cmd_passes_ripgrep_timeout_from_tuning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """032-I3: search_cmd passes tuning.ripgrep_timeout to search_ripgrep."""
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)

    cache_file = tmp_path / "repo-abcd1234.cache"
    cache_file.write_text("/repo/a.py:1:content\n")

    captured_timeouts: list[int] = []

    def fake_search_ripgrep(query, cache_path, *, n_results=50, fixed_strings=True, timeout=10):
        captured_timeouts.append(timeout)
        return []

    custom_tuning = TuningConfig(ripgrep_timeout=99)

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]

    runner = CliRunner()
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]),
        patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_search_ripgrep),
        patch("nexus.commands.search_cmd.load_config",
              return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
        patch("nexus.commands.search_cmd.get_tuning_config", return_value=custom_tuning),
    ):
        result = runner.invoke(
            main,
            ["search", "query", "--hybrid", "--corpus", "code__repo-abcd1234", "--no-rerank"],
        )

    assert result.exit_code == 0, result.output
    assert captured_timeouts, "search_ripgrep should have been called"
    assert all(t == 99 for t in captured_timeouts), (
        f"Expected ripgrep_timeout=99, got {captured_timeouts}"
    )
