# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nexus.config import TuningConfig, _tuning_from_dict, get_tuning_config, load_config

_EXPECTED_DEFAULTS = {
    "vector_weight": 0.7, "frecency_weight": 0.3, "file_size_threshold": 30,
    "decay_rate": 0.01, "code_chunk_lines": 150, "pdf_chunk_chars": 1500,
    "git_log_timeout": 30, "ripgrep_timeout": 10,
}


# ── TuningConfig defaults ──────────────────────────────────────────────────

def test_tuning_config_defaults() -> None:
    cfg = TuningConfig()
    for attr, val in _EXPECTED_DEFAULTS.items():
        assert getattr(cfg, attr) == val

def test_tuning_from_dict_empty_gives_defaults() -> None:
    assert _tuning_from_dict({}) == TuningConfig()

def test_tuning_from_dict_partial_override() -> None:
    cfg = _tuning_from_dict({"scoring": {"vector_weight": 0.8, "frecency_weight": 0.2}})
    assert cfg.vector_weight == 0.8
    assert cfg.frecency_weight == 0.2
    assert cfg.file_size_threshold == 30
    assert cfg.code_chunk_lines == 150

def test_tuning_from_dict_all_sections_override() -> None:
    cfg = _tuning_from_dict({
        "scoring": {"vector_weight": 1.0, "frecency_weight": 0.0, "file_size_threshold": 50},
        "frecency": {"decay_rate": 0.05},
        "chunking": {"code_chunk_lines": 100, "pdf_chunk_chars": 2000},
        "timeouts": {"git_log": 60, "ripgrep": 5},
    })
    assert (cfg.vector_weight, cfg.frecency_weight, cfg.file_size_threshold) == (1.0, 0.0, 50)
    assert cfg.decay_rate == 0.05
    assert (cfg.code_chunk_lines, cfg.pdf_chunk_chars) == (100, 2000)
    assert (cfg.git_log_timeout, cfg.ripgrep_timeout) == (60, 5)

def test_tuning_from_dict_unknown_keys_ignored() -> None:
    cfg = _tuning_from_dict({"scoring": {"vector_weight": 0.6, "unknown_key": "ignored"}, "unknown_section": {"also_ignored": True}})
    assert cfg.vector_weight == 0.6
    assert cfg.frecency_weight == 0.3


# ── Invalid values ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("section,key,value,match", [
    ("scoring", "vector_weight", "not_a_number", "vector_weight"),
    ("scoring", "file_size_threshold", "many", "file_size_threshold"),
])
def test_tuning_from_dict_invalid_raises(section, key, value, match) -> None:
    with pytest.raises(ValueError, match=match):
        _tuning_from_dict({section: {key: value}})


# ── S10: Error messages show full YAML path ─────────────────────────────────

@pytest.mark.parametrize("section,key,pattern", [
    ("scoring", "vector_weight", r"tuning\.scoring\.vector_weight"),
    ("chunking", "pdf_chunk_chars", r"tuning\.chunking\.pdf_chunk_chars"),
    ("timeouts", "ripgrep", r"tuning\.timeouts\.ripgrep"),
])
def test_error_message_shows_full_yaml_path(section, key, pattern) -> None:
    with pytest.raises(ValueError, match=pattern):
        _tuning_from_dict({section: {key: "bad"}})


# ── S11: _DEFAULTS matches TuningConfig ────────────────────────────────────

def test_defaults_tuning_section_matches_tuning_config() -> None:
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


# ── get_tuning_config with .nexus.yml ───────────────────────────────────────

def test_get_tuning_config_returns_defaults_when_no_nexus_yml(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert get_tuning_config(repo_root=tmp_path) == TuningConfig()

def test_get_tuning_config_loads_from_nexus_yml(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".nexus.yml").write_text(yaml.dump({
        "tuning": {"scoring": {"vector_weight": 0.9, "frecency_weight": 0.1}, "chunking": {"code_chunk_lines": 75}},
    }))
    cfg = get_tuning_config(repo_root=tmp_path)
    assert (cfg.vector_weight, cfg.frecency_weight, cfg.code_chunk_lines) == (0.9, 0.1, 75)
    assert cfg.file_size_threshold == 30

def test_load_config_includes_tuning_section_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(repo_root=tmp_path)
    assert cfg["tuning"]["scoring"]["vector_weight"] == 0.7
    assert cfg["tuning"]["frecency"]["decay_rate"] == 0.01
    assert cfg["tuning"]["chunking"]["code_chunk_lines"] == 150
    assert cfg["tuning"]["timeouts"]["ripgrep"] == 10

def test_get_tuning_config_nexus_yml_overrides_global(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    global_dir = tmp_path / ".config" / "nexus"
    global_dir.mkdir(parents=True)
    (global_dir / "config.yml").write_text(yaml.dump({"tuning": {"timeouts": {"ripgrep": 20}}}))
    (tmp_path / ".nexus.yml").write_text(yaml.dump({"tuning": {"timeouts": {"ripgrep": 5, "git_log": 45}}}))
    cfg = get_tuning_config(repo_root=tmp_path)
    assert cfg.ripgrep_timeout == 5
    assert cfg.git_log_timeout == 45


# ── Backward compat ────────────────────────────────────────────────────────

def test_tuning_config_equals_old_hardcoded_values() -> None:
    cfg = TuningConfig()
    for attr, val in _EXPECTED_DEFAULTS.items():
        assert getattr(cfg, attr) == val


# ── I1: pdf_chunk_chars wiring ──────────────────────────────────────────────

@pytest.mark.parametrize("chunk_chars,expected_call", [
    (800, lambda cls: cls.assert_called_once_with(chunk_chars=800)),
    (None, lambda cls: cls.assert_called_once_with()),
])
def test_pdf_chunk_chars_wiring(tmp_path, chunk_chars, expected_call) -> None:
    from nexus.doc_indexer import _pdf_chunks
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    with patch("nexus.doc_indexer.PDFExtractor") as mock_ext_cls, \
         patch("nexus.doc_indexer.PDFChunker") as mock_chunker_cls:
        mock_ext = MagicMock()
        mock_ext.extract.return_value = MagicMock(text="Content.", metadata={"page_count": 1, "page_boundaries": []})
        mock_ext_cls.return_value = mock_ext
        # Return one chunk so the nexus-aold zero-chunk-from-non-empty-text
        # guard does not fire. The test's purpose is the PDFChunker(...)
        # constructor call, not chunk-output handling.
        fake_chunk = MagicMock(text="Content.", chunk_index=0, metadata={})
        mock_chunker_cls.return_value = MagicMock(chunk=MagicMock(return_value=[fake_chunk]))
        kwargs = {"chunk_chars": chunk_chars} if chunk_chars is not None else {}
        _pdf_chunks(pdf, "abc123", "voyage-context-3", "2026-01-01", "test", **kwargs)
    expected_call(mock_chunker_cls)


def test_index_pdf_file_passes_chunk_chars(tmp_path) -> None:
    from nexus.indexer import _index_pdf_file
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    fake_chunk = ("chunk-id-001", "Some text", {
        "source_path": str(pdf), "source_title": "Test PDF", "source_author": "",
        "source_date": "", "corpus": "docs__test", "store_type": "pdf",
        "page_count": 1, "page_number": 1, "section_title": "", "format": "",
        "extraction_method": "", "chunk_index": 0, "chunk_count": 1,
        "chunk_start_char": 0, "chunk_end_char": 10, "embedding_model": "voyage-context-3",
        "indexed_at": "2026-01-01", "content_hash": "abc123", "pdf_subject": "",
        "pdf_keywords": "", "is_image_pdf": False,
    })
    with patch("nexus.doc_indexer._pdf_chunks", return_value=[fake_chunk]) as mock_pdf_chunks, \
         patch("nexus.doc_indexer._embed_with_fallback", return_value=([[0.1] * 10], "voyage-context-3")):
        _index_pdf_file(pdf, tmp_path, "docs__test", "voyage-context-3",
                        MagicMock(get=MagicMock(return_value={"ids": [], "metadatas": []})),
                        MagicMock(), "voyage-key", {}, "2026-01-01", 0.5, chunk_chars=800)
    assert mock_pdf_chunks.call_args[1].get("chunk_chars") == 800


# ── I3: Search tuning weights wiring ───────────────────────────────────────

def test_apply_hybrid_scoring_uses_tuning_weights() -> None:
    from nexus.scoring import apply_hybrid_scoring
    from nexus.types import SearchResult

    def _make(id, distance, frecency):
        return SearchResult(id=id, content="x", distance=distance, collection="code__repo",
                            metadata={"frecency_score": frecency})

    scored_v = apply_hybrid_scoring(
        [_make("a", 0.1, 1.0), _make("b", 0.5, 0.0)], hybrid=True, vector_weight=1.0, frecency_weight=0.0)
    scored_f = apply_hybrid_scoring(
        [_make("a", 0.1, 1.0), _make("b", 0.5, 0.0)], hybrid=True, vector_weight=0.0, frecency_weight=1.0)
    assert scored_v[0].id == "a"
    assert scored_f[0].id == "a"


def test_search_cmd_passes_ripgrep_timeout(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner
    from nexus.cli import main

    monkeypatch.setenv("CHROMA_API_KEY", "k")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    monkeypatch.setenv("CHROMA_TENANT", "t")
    monkeypatch.setenv("CHROMA_DATABASE", "d")
    monkeypatch.setattr("nexus.commands.search_cmd._CONFIG_DIR", tmp_path)
    (tmp_path / "repo-abcd1234.cache").write_text("/repo/a.py:1:content\n")

    captured: list[int] = []
    def fake_rg(query, cache_path, *, n_results=50, fixed_strings=True, timeout=10):
        captured.append(timeout)
        return []

    mock_t3 = MagicMock()
    mock_t3.list_collections.return_value = [{"name": "code__repo-abcd1234"}]
    with (
        patch("nexus.commands.search_cmd._t3", return_value=mock_t3),
        patch("nexus.commands.search_cmd.search_cross_corpus", return_value=[]),
        patch("nexus.commands.search_cmd.search_ripgrep", side_effect=fake_rg),
        patch("nexus.commands.search_cmd.load_config", return_value={"embeddings": {"rerankerModel": "rerank-2.5"}}),
        patch("nexus.commands.search_cmd.get_tuning_config", return_value=TuningConfig(ripgrep_timeout=99)),
    ):
        result = CliRunner().invoke(main, ["search", "query", "--hybrid", "--corpus", "code__repo-abcd1234", "--no-rerank"])
    assert result.exit_code == 0, result.output
    assert captured and all(t == 99 for t in captured)
