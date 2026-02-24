# Smart Repository Indexing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the single-model code indexer with a unified pipeline that classifies files by extension, embeds code with voyage-code-3 and prose/PDFs with voyage-context-3, and stores results in separate `code__` and `docs__` collections per repo.

**Architecture:** Extension-based file classification drives a three-stream pipeline (code, prose, PDF) within a single file walk. The existing `doc_indexer.py` embedding helpers (`_embed_with_fallback`, `_pdf_chunks`, `_markdown_chunks`) are reused for prose/PDF streams. Registry tracks both collection names. `.nexus.yml` overrides classification defaults. RDR directories are auto-discovered and indexed into `docs__rdr__` (T3 only).

**Tech Stack:** Python 3.12+, ChromaDB, Voyage AI (voyage-code-3, voyage-context-3, voyage-4), tree-sitter, PyMuPDF4LLM, pytest, click

**Design doc:** `docs/plans/2026-02-24-smart-repo-indexing-design.md`

---

### Task 1: File Classification — Tests

**Files:**
- Create: `tests/test_classifier.py`

**Step 1: Write the failing tests**

```python
# tests/test_classifier.py
"""Unit tests for file classification logic."""
from pathlib import Path

import pytest


def test_python_file_classified_as_code():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("main.py")) == ContentClass.CODE


def test_markdown_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("README.md")) == ContentClass.PROSE


def test_yaml_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("config.yaml")) == ContentClass.PROSE


def test_toml_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("pyproject.toml")) == ContentClass.PROSE


def test_json_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("package.json")) == ContentClass.PROSE


def test_pdf_file_classified_as_pdf():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("paper.pdf")) == ContentClass.PDF


def test_unknown_extension_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("notes.txt")) == ContentClass.PROSE


def test_no_extension_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("Makefile")) == ContentClass.PROSE


def test_code_extensions_set_matches_design():
    """The canonical set must match the design doc exactly."""
    from nexus.classifier import _CODE_EXTENSIONS
    expected = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
        ".cpp", ".cc", ".c", ".h", ".hpp", ".rb", ".cs", ".sh", ".bash",
        ".kt", ".swift", ".scala", ".r", ".m", ".php",
    }
    assert _CODE_EXTENSIONS == expected


def test_config_code_extensions_override():
    """code_extensions in config adds to the default set."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"code_extensions": [".sql", ".proto"]}
    assert classify_file(Path("schema.sql"), indexing_config=cfg) == ContentClass.CODE
    assert classify_file(Path("api.proto"), indexing_config=cfg) == ContentClass.CODE


def test_config_prose_extensions_override():
    """prose_extensions wins over both defaults and code_extensions."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"prose_extensions": [".sh"], "code_extensions": [".sql"]}
    # .sh is in defaults as code, but prose_extensions overrides
    assert classify_file(Path("deploy.sh"), indexing_config=cfg) == ContentClass.PROSE
    # .sql added by code_extensions still works
    assert classify_file(Path("query.sql"), indexing_config=cfg) == ContentClass.CODE


def test_case_insensitive_extension():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("Main.PY")) == ContentClass.CODE
    assert classify_file(Path("Doc.PDF")) == ContentClass.PDF
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_classifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.classifier'`

**Step 3: Commit test file**

```bash
git add tests/test_classifier.py
git commit -m "test: add file classification unit tests for smart repo indexing"
```

---

### Task 2: File Classification — Implementation

**Files:**
- Create: `src/nexus/classifier.py`

**Step 1: Implement the classifier module**

```python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""File classification for repository indexing.

Extension-based classification determines which embedding model and chunking
strategy each file receives during repository indexing.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any


class ContentClass(Enum):
    """Content classification for a repository file."""
    CODE = "code"
    PROSE = "prose"
    PDF = "pdf"


# Canonical code extensions — the greppable, authoritative list.
# Everything NOT in this set (and not .pdf) is treated as prose.
_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".cpp", ".cc", ".c", ".h", ".hpp", ".rb", ".cs", ".sh", ".bash",
    ".kt", ".swift", ".scala", ".r", ".m", ".php",
})


def classify_file(
    path: Path,
    *,
    indexing_config: dict[str, Any] | None = None,
) -> ContentClass:
    """Classify *path* as code, prose, or PDF based on its extension.

    *indexing_config* is the ``indexing`` section from ``.nexus.yml``:
    - ``code_extensions``: list of extensions to add to the code set
    - ``prose_extensions``: list of extensions forced to prose (wins over all)
    """
    ext = path.suffix.lower()

    if ext == ".pdf":
        return ContentClass.PDF

    cfg = indexing_config or {}
    prose_overrides = set(cfg.get("prose_extensions", []))
    code_additions = set(cfg.get("code_extensions", []))

    # prose_extensions wins over everything
    if ext in prose_overrides:
        return ContentClass.PROSE

    # code_extensions adds to defaults
    effective_code = _CODE_EXTENSIONS | code_additions
    if ext in effective_code:
        return ContentClass.CODE

    return ContentClass.PROSE
```

**Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_classifier.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add src/nexus/classifier.py
git commit -m "feat: add file classification module for smart repo indexing"
```

---

### Task 3: Config — Indexing Section Tests and Implementation

**Files:**
- Modify: `src/nexus/config.py:36-58` (add indexing to _DEFAULTS)
- Modify: `tests/test_config.py` (add tests for indexing section)

**Step 1: Write the failing tests**

Add to existing test file or create new section. Check if `tests/test_config.py` exists first; if not, create it.

```python
# Add to tests/test_config.py (or create)
"""Tests for indexing config section."""
from pathlib import Path
from unittest.mock import patch

import pytest


def test_defaults_include_indexing_section():
    from nexus.config import _DEFAULTS
    assert "indexing" in _DEFAULTS
    assert _DEFAULTS["indexing"]["code_extensions"] == []
    assert _DEFAULTS["indexing"]["prose_extensions"] == []
    assert _DEFAULTS["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_load_config_returns_indexing_defaults():
    from nexus.config import load_config
    with patch("nexus.config._global_config_path") as mock_path:
        mock_path.return_value = Path("/nonexistent/config.yml")
        cfg = load_config(repo_root=Path("/nonexistent/repo"))
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr"]


def test_nexus_yml_indexing_overrides(tmp_path: Path):
    from nexus.config import load_config
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nexus.yml").write_text(
        "indexing:\n"
        "  code_extensions: [.sql, .proto]\n"
        "  rdr_paths:\n"
        "    - docs/rdr\n"
        "    - design/decisions\n"
    )
    with patch("nexus.config._global_config_path") as mock_path:
        mock_path.return_value = Path("/nonexistent/config.yml")
        cfg = load_config(repo_root=repo)
    assert cfg["indexing"]["code_extensions"] == [".sql", ".proto"]
    assert cfg["indexing"]["rdr_paths"] == ["docs/rdr", "design/decisions"]
    # prose_extensions not set → stays default
    assert cfg["indexing"]["prose_extensions"] == []
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v -k "indexing"`
Expected: FAIL — `KeyError: 'indexing'`

**Step 3: Add indexing section to _DEFAULTS**

Modify `src/nexus/config.py:36-58`:

```python
_DEFAULTS: dict[str, Any] = {
    "server": {
        "port": 7890,
        "headPollInterval": 10,
        "ignorePatterns": [],
    },
    "embeddings": {
        "rerankerModel": "rerank-2.5",
    },
    "pm": {
        "archiveTtl": 90,
    },
    "mxbai": {
        "stores": [],
    },
    "chromadb": {
        "tenant": "",
        "database": "",
    },
    "client": {
        "host": "localhost",
    },
    "indexing": {
        "code_extensions": [],
        "prose_extensions": [],
        "rdr_paths": ["docs/rdr"],
    },
}
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v -k "indexing"`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/nexus/config.py tests/test_config.py
git commit -m "feat: add indexing config section with code_extensions, prose_extensions, rdr_paths"
```

---

### Task 4: Chunker Cleanup — Remove Config Extensions from AST

**Files:**
- Modify: `src/nexus/chunker.py:11-31`
- Modify: `tests/test_chunker.py` (add test for YAML fallback)

**Step 1: Write failing test**

```python
# Add to tests/test_chunker.py
def test_yaml_uses_line_chunking_not_ast():
    """YAML files should use line-based chunking, not AST (they're prose now)."""
    from nexus.chunker import chunk_file
    content = "key: value\nlist:\n  - item1\n  - item2\n"
    chunks = chunk_file(Path("config.yaml"), content)
    assert chunks
    assert not chunks[0].get("ast_chunked", False)


def test_toml_uses_line_chunking_not_ast():
    """TOML files should use line-based chunking, not AST (they're prose now)."""
    from nexus.chunker import chunk_file
    content = "[section]\nkey = \"value\"\n"
    chunks = chunk_file(Path("pyproject.toml"), content)
    assert chunks
    assert not chunks[0].get("ast_chunked", False)
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chunker.py -v -k "yaml_uses_line or toml_uses_line"`
Expected: FAIL — YAML gets AST-chunked

**Step 3: Remove .toml, .yaml, .yml from _AST_EXTENSIONS**

Modify `src/nexus/chunker.py:11-31`:

```python
_AST_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".cs": "c_sharp",
    ".sh": "bash",
    ".bash": "bash",
}
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chunker.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/nexus/chunker.py tests/test_chunker.py
git commit -m "fix: remove .toml/.yaml/.yml from AST extensions (they are prose)"
```

---

### Task 5: Registry — Dual Collection Names

**Files:**
- Modify: `src/nexus/registry.py:16-26,47-58`
- Create or modify: `tests/test_registry.py`

**Step 1: Write failing tests**

```python
# tests/test_registry.py (add to existing or create)
"""Tests for dual-collection registry."""
from pathlib import Path

import pytest

from nexus.registry import RepoRegistry


def test_add_stores_both_collection_names(tmp_path: Path):
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    reg.add(repo)
    info = reg.get(repo)
    assert "code_collection" in info
    assert "docs_collection" in info
    assert info["code_collection"].startswith("code__")
    assert info["docs_collection"].startswith("docs__")
    # Same hash suffix
    code_suffix = info["code_collection"].split("code__")[1]
    docs_suffix = info["docs_collection"].split("docs__")[1]
    assert code_suffix == docs_suffix


def test_backward_compat_collection_key(tmp_path: Path):
    """The 'collection' key still works as alias for code_collection."""
    reg = RepoRegistry(tmp_path / "repos.json")
    repo = tmp_path / "myrepo"
    repo.mkdir()
    reg.add(repo)
    info = reg.get(repo)
    assert info["collection"] == info["code_collection"]


def test_docs_collection_name_function():
    from nexus.registry import _docs_collection_name
    repo = Path("/some/path/myrepo")
    name = _docs_collection_name(repo)
    assert name.startswith("docs__myrepo-")
    assert len(name.split("-")[-1]) == 8  # 8-char hash
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_registry.py -v -k "both_collection or backward_compat or docs_collection_name"`
Expected: FAIL

**Step 3: Add _docs_collection_name and update add()**

Modify `src/nexus/registry.py`:

Add after `_collection_name()` (line 26):

```python
def _docs_collection_name(repo: Path) -> str:
    """Return the docs__ ChromaDB collection name for *repo*.

    Uses the same hash scheme as _collection_name() for consistency.
    """
    path_hash = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
    return f"docs__{repo.name}-{path_hash}"
```

Update `add()` method (line 47-58):

```python
    def add(self, repo: Path) -> None:
        """Register *repo*, initialising collection names and head_hash."""
        key = str(repo)
        name = repo.name
        code_col = _collection_name(repo)
        docs_col = _docs_collection_name(repo)
        with self._lock:
            self._data["repos"][key] = {
                "name": name,
                "collection": code_col,  # backward compat alias
                "code_collection": code_col,
                "docs_collection": docs_col,
                "head_hash": "",
                "status": "registered",
            }
            self._save()
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_registry.py -v`
Expected: All PASS

**Step 5: Run full test suite to check for regressions**

Run: `uv run pytest tests/ -x -q`
Expected: All pass (existing tests use `info["collection"]` which still works)

**Step 6: Commit**

```bash
git add src/nexus/registry.py tests/test_registry.py
git commit -m "feat: registry stores both code_collection and docs_collection per repo"
```

---

### Task 6: Unified Indexer — Core Rewrite

This is the largest task. The indexer's `_run_index()` is rewritten to:
1. Classify each file
2. Route code files to `code__` via voyage-code-3 (existing path)
3. Route prose files to `docs__` via `_embed_with_fallback` (CCE)
4. Route PDF files to `docs__` via `_pdf_chunks` + `_embed_with_fallback`
5. Exclude RDR paths from `docs__`

**Files:**
- Modify: `src/nexus/indexer.py` (major rewrite of `_run_index`)

**Step 1: Write failing unit tests**

Add to `tests/test_indexer.py`:

```python
# ── Smart repo indexing: classification routing ──────────────────────────────

def test_run_index_routes_prose_to_docs_collection(tmp_path: Path) -> None:
    """Markdown files should be indexed into the docs__ collection, not code__."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hello World\n\nThis is a readme.\n")

    registry = MagicMock()
    registry.get.return_value = {
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
        "collection": "code__repo",
    }

    upserted_collections: dict[str, list] = {}

    mock_db = MagicMock()
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col

    def capture_upsert(collection_name, ids, documents, embeddings, metadatas):
        upserted_collections.setdefault(collection_name, []).extend(metadatas)

    mock_db.upsert_chunks_with_embeddings.side_effect = capture_upsert

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1] * 1024]

    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value={
                    "server": {"ignorePatterns": []},
                    "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"]},
                }):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client", return_value=mock_voyage_client):
                                _run_index(repo, registry)

    # README.md should go to docs__ not code__
    assert "docs__repo" in upserted_collections, f"Expected docs__repo, got: {list(upserted_collections.keys())}"
    assert "code__repo" not in upserted_collections


def test_run_index_routes_code_to_code_collection(tmp_path: Path) -> None:
    """Python files should be indexed into the code__ collection."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("def hello():\n    pass\n")

    registry = MagicMock()
    registry.get.return_value = {
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
        "collection": "code__repo",
    }

    upserted_collections: dict[str, list] = {}

    mock_db = MagicMock()
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col

    def capture_upsert(collection_name, ids, documents, embeddings, metadatas):
        upserted_collections.setdefault(collection_name, []).extend(metadatas)

    mock_db.upsert_chunks_with_embeddings.side_effect = capture_upsert

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1] * 1024]

    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    fake_chunk = {
        "line_start": 1, "line_end": 2, "text": "def hello():\n    pass",
        "chunk_index": 0, "chunk_count": 1,
        "ast_chunked": True, "filename": "main.py", "file_extension": ".py",
    }

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value={
                    "server": {"ignorePatterns": []},
                    "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"]},
                }):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("nexus.chunker.chunk_file", return_value=[fake_chunk]):
                                with patch("voyageai.Client", return_value=mock_voyage_client):
                                    _run_index(repo, registry)

    assert "code__repo" in upserted_collections
    assert "docs__repo" not in upserted_collections


def test_run_index_excludes_rdr_paths_from_docs(tmp_path: Path) -> None:
    """Files under rdr_paths should not be indexed into docs__ (they go to docs__rdr__)."""
    from nexus.indexer import _run_index

    repo = tmp_path / "repo"
    repo.mkdir()
    rdr_dir = repo / "docs" / "rdr"
    rdr_dir.mkdir(parents=True)
    (rdr_dir / "ADR-001.md").write_text("# ADR 001\n\nDecision.\n")
    (repo / "README.md").write_text("# Project\n\nReadme text.\n")

    registry = MagicMock()
    registry.get.return_value = {
        "code_collection": "code__repo",
        "docs_collection": "docs__repo",
        "collection": "code__repo",
    }

    upserted_collections: dict[str, list] = {}

    mock_db = MagicMock()
    mock_col = MagicMock()
    mock_col.get.return_value = {"metadatas": []}
    mock_db.get_or_create_collection.return_value = mock_col

    def capture_upsert(collection_name, ids, documents, embeddings, metadatas):
        upserted_collections.setdefault(collection_name, []).extend(metadatas)

    mock_db.upsert_chunks_with_embeddings.side_effect = capture_upsert

    mock_voyage_result = MagicMock(spec=EmbeddingsObject)
    mock_voyage_result.embeddings = [[0.1] * 1024]

    mock_voyage_client = MagicMock()
    mock_voyage_client.embed.return_value = mock_voyage_result

    with patch("nexus.frecency.batch_frecency", return_value={}):
        with patch("nexus.ripgrep_cache.build_cache"):
            with patch("nexus.indexer._git_metadata", return_value={}):
                with patch("nexus.config.load_config", return_value={
                    "server": {"ignorePatterns": []},
                    "indexing": {"code_extensions": [], "prose_extensions": [], "rdr_paths": ["docs/rdr"]},
                }):
                    with patch("nexus.config.get_credential", return_value="fake-key"):
                        with patch("nexus.db.make_t3", return_value=mock_db):
                            with patch("voyageai.Client", return_value=mock_voyage_client):
                                _run_index(repo, registry)

    # README.md → docs__repo; ADR-001.md → docs__rdr__repo; neither in code__repo
    if "docs__repo" in upserted_collections:
        paths = [m.get("source_path", "") for m in upserted_collections["docs__repo"]]
        assert not any("docs/rdr" in p for p in paths), "RDR files must not be in docs__repo"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_indexer.py -v -k "routes_prose or routes_code or excludes_rdr"`
Expected: FAIL — `_run_index` doesn't know about `docs_collection`

**Step 3: Rewrite `_run_index` in `src/nexus/indexer.py`**

Replace the entire `_run_index` function (lines 139-293) and update imports:

```python
def _run_index(repo: Path, registry: "RepoRegistry") -> None:
    """Full indexing pipeline: classify → chunk → embed → upsert to code__/docs__ collections."""
    import hashlib as _hl

    from nexus.chunker import chunk_file
    from nexus.classifier import ContentClass, classify_file
    from nexus.config import load_config
    from nexus.doc_indexer import _embed_with_fallback, _markdown_chunks, _pdf_chunks
    from nexus.frecency import batch_frecency
    from nexus.md_chunker import SemanticMarkdownChunker
    from nexus.ripgrep_cache import build_cache

    info = registry.get(repo)
    if info is None:
        return

    code_collection = info.get("code_collection", info["collection"])
    docs_collection = info.get("docs_collection", f"docs__{repo.name}")

    # Load config (picks up per-repo .nexus.yml if present)
    cfg = load_config(repo_root=repo)
    cfg_patterns: list[str] = cfg.get("server", {}).get("ignorePatterns", [])
    ignore_patterns: list[str] = list(dict.fromkeys(DEFAULT_IGNORE + cfg_patterns))
    indexing_cfg: dict = cfg.get("indexing", {})
    rdr_paths: list[str] = indexing_cfg.get("rdr_paths", ["docs/rdr"])

    # Collect git metadata once for all chunks
    git_meta = _git_metadata(repo)

    # Compute frecency scores in a single git log pass
    frecency_map = batch_frecency(repo)

    # Build the set of absolute RDR directories for exclusion
    rdr_abs_paths: list[Path] = []
    for rdr_rel in rdr_paths:
        rdr_abs = repo / rdr_rel
        if rdr_abs.is_dir():
            rdr_abs_paths.append(rdr_abs.resolve())

    # Gather all files with frecency scores, classified
    code_files: list[tuple[float, Path]] = []
    prose_files: list[tuple[float, Path]] = []
    pdf_files: list[tuple[float, Path]] = []
    all_text_scored: list[tuple[float, Path]] = []

    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repo)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if _should_ignore(rel, ignore_patterns):
            continue

        # Skip files under RDR paths (handled separately)
        resolved = path.resolve()
        if any(resolved == rdr_dir or rdr_dir in resolved.parents for rdr_dir in rdr_abs_paths):
            continue

        score = frecency_map.get(path, 0.0)
        content_class = classify_file(path, indexing_config=indexing_cfg)

        match content_class:
            case ContentClass.CODE:
                code_files.append((score, path))
                all_text_scored.append((score, path))
            case ContentClass.PDF:
                pdf_files.append((score, path))
            case ContentClass.PROSE:
                all_text_scored.append((score, path))
                prose_files.append((score, path))

    # Sort descending by frecency
    code_files.sort(key=lambda x: x[0], reverse=True)
    prose_files.sort(key=lambda x: x[0], reverse=True)
    all_text_scored.sort(key=lambda x: x[0], reverse=True)

    # Update ripgrep cache (all text files, classification-agnostic)
    _repo_hash = _hl.sha256(str(repo).encode()).hexdigest()[:8]
    cache_path = Path.home() / ".config" / "nexus" / f"{repo.name}-{_repo_hash}.cache"
    build_cache(repo, cache_path, all_text_scored)

    # ── Credentials check ────────────────────────────────────────────────────
    from nexus.config import get_credential
    voyage_key = get_credential("voyage_api_key")
    chroma_key = get_credential("chroma_api_key")
    if not voyage_key or not chroma_key:
        raise CredentialsMissingError(
            f"T3 credentials missing for repo '{repo.name}' "
            f"(voyage_api_key={'set' if voyage_key else 'missing'}, "
            f"chroma_api_key={'set' if chroma_key else 'missing'})"
        )

    import voyageai
    from datetime import UTC, datetime as _dt
    from nexus.db import make_t3

    db = make_t3()
    now_iso = _dt.now(UTC).isoformat()
    voyage_client = voyageai.Client(api_key=voyage_key)

    # ── Index code files → code__ ────────────────────────────────────────────
    code_model = index_model_for_collection(code_collection)
    code_col = db.get_or_create_collection(code_collection)

    for score, file in code_files:
        _index_code_file(
            file, repo, code_collection, code_model, code_col,
            db, voyage_client, git_meta, now_iso, score,
        )

    # ── Index prose files → docs__ ───────────────────────────────────────────
    docs_model = index_model_for_collection(docs_collection)
    docs_col = db.get_or_create_collection(docs_collection)

    for score, file in prose_files:
        _index_prose_file(
            file, repo, docs_collection, docs_model, docs_col,
            db, voyage_key, git_meta, now_iso, score,
        )

    # ── Index PDF files → docs__ ─────────────────────────────────────────────
    for score, file in pdf_files:
        _index_pdf_file(
            file, repo, docs_collection, docs_model, docs_col,
            db, voyage_key, git_meta, now_iso, score,
        )

    # ── RDR discovery → docs__rdr__ ──────────────────────────────────────────
    _discover_and_index_rdrs(repo, rdr_abs_paths, db, voyage_key, now_iso)

    # ── Cross-collection pruning ─────────────────────────────────────────────
    _prune_misclassified(repo, code_collection, docs_collection, code_files, prose_files, pdf_files, db)

    _log.info(
        "index_repository complete",
        repo=str(repo),
        code_files=len(code_files),
        prose_files=len(prose_files),
        pdf_files=len(pdf_files),
    )
```

**Step 4: Add the helper functions (same file, after `_run_index`)**

```python
def _index_code_file(
    file: Path, repo: Path, collection_name: str, target_model: str,
    col, db, voyage_client, git_meta: dict, now_iso: str, score: float,
) -> None:
    """Index a single code file into the code__ collection."""
    import hashlib as _hl
    from nexus.chunker import chunk_file

    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file), error=type(exc).__name__)
        return

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check
    existing = col.get(where={"source_path": str(file)}, include=["metadatas"], limit=1)
    if existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return

    chunks = chunk_file(file, content)
    if not chunks:
        _log.debug("skipped file with no chunks", path=str(file))
        return
    total_chunks = len(chunks)

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for i, chunk in enumerate(chunks):
        title = f"{file.relative_to(repo)}:{chunk['line_start']}-{chunk['line_end']}"
        doc_id = _hl.sha256(f"{collection_name}:{title}".encode()).hexdigest()[:32]
        ext = file.suffix.lower()
        metadata: dict = {
            "title": title,
            "tags": ext.lstrip("."),
            "category": "code",
            "session_id": "",
            "source_agent": "nexus-indexer",
            "store_type": "code",
            "indexed_at": now_iso,
            "expires_at": "",
            "ttl_days": 0,
            "source_path": str(file),
            "line_start": chunk["line_start"],
            "line_end": chunk["line_end"],
            "frecency_score": float(score),
            "chunk_index": chunk.get("chunk_index", i),
            "chunk_count": chunk.get("chunk_count", total_chunks),
            "ast_chunked": chunk.get("ast_chunked", False),
            "filename": chunk.get("filename", str(file.name)),
            "file_extension": chunk.get("file_extension", ext),
            "programming_language": _EXT_TO_LANGUAGE.get(ext, ""),
            "corpus": collection_name,
            "embedding_model": target_model,
            "content_hash": content_hash,
            **git_meta,
        }
        ids.append(doc_id)
        documents.append(chunk["text"])
        metadatas.append(metadata)

    embeddings: list[list[float]] = []
    for batch_start in range(0, len(documents), _VOYAGE_EMBED_BATCH_SIZE):
        batch = documents[batch_start : batch_start + _VOYAGE_EMBED_BATCH_SIZE]
        result = voyage_client.embed(texts=batch, model=target_model, input_type="document")
        embeddings.extend(result.embeddings)

    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas,
    )


def _index_prose_file(
    file: Path, repo: Path, collection_name: str, target_model: str,
    col, db, voyage_key: str, git_meta: dict, now_iso: str, score: float,
) -> None:
    """Index a single prose file into the docs__ collection using CCE."""
    import hashlib as _hl
    from nexus.doc_indexer import _embed_with_fallback
    from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter

    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file), error=type(exc).__name__)
        return

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check
    existing = col.get(where={"source_path": str(file)}, include=["metadatas"], limit=1)
    if existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return

    ext = file.suffix.lower()

    # Chunk based on format
    if ext == ".md":
        raw_text = content
        frontmatter, body = parse_frontmatter(raw_text)
        base_meta = {"source_path": str(file), "corpus": collection_name}
        sm_chunks = SemanticMarkdownChunker().chunk(body, base_meta)
        if not sm_chunks:
            return
        chunk_texts = [c.text for c in sm_chunks]
        chunk_metas = []
        for i, c in enumerate(sm_chunks):
            chunk_metas.append({
                "title": f"{file.relative_to(repo)}:chunk-{i}",
                "tags": "markdown",
                "category": "prose",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "prose",
                "indexed_at": now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file),
                "line_start": c.metadata.get("line_start", 0),
                "line_end": c.metadata.get("line_end", 0),
                "frecency_score": float(score),
                "chunk_index": i,
                "chunk_count": len(sm_chunks),
                "ast_chunked": False,
                "filename": file.name,
                "file_extension": ext,
                "section_title": c.metadata.get("header_path", ""),
                "corpus": collection_name,
                "embedding_model": target_model,
                "content_hash": content_hash,
                **git_meta,
            })
    else:
        # Line-based chunking for non-markdown prose
        from nexus.chunker import _line_chunk
        raw_chunks = _line_chunk(content)
        if not raw_chunks:
            if not content.strip():
                return
            raw_chunks = [(1, 1, content)]
        chunk_texts = [text for _, _, text in raw_chunks]
        chunk_metas = []
        for i, (ls, le, _) in enumerate(raw_chunks):
            chunk_metas.append({
                "title": f"{file.relative_to(repo)}:{ls}-{le}",
                "tags": ext.lstrip(".") if ext else "text",
                "category": "prose",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "prose",
                "indexed_at": now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file),
                "line_start": ls,
                "line_end": le,
                "frecency_score": float(score),
                "chunk_index": i,
                "chunk_count": len(raw_chunks),
                "ast_chunked": False,
                "filename": file.name,
                "file_extension": ext,
                "corpus": collection_name,
                "embedding_model": target_model,
                "content_hash": content_hash,
                **git_meta,
            })

    if not chunk_texts:
        return

    # Generate IDs
    ids = [
        _hl.sha256(f"{collection_name}:{m['title']}".encode()).hexdigest()[:32]
        for m in chunk_metas
    ]

    # Embed with CCE fallback
    embeddings, actual_model = _embed_with_fallback(
        chunk_texts, target_model, voyage_key, input_type="document",
    )
    if actual_model != target_model:
        for m in chunk_metas:
            m["embedding_model"] = actual_model

    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids, documents=chunk_texts, embeddings=embeddings, metadatas=chunk_metas,
    )


def _index_pdf_file(
    file: Path, repo: Path, collection_name: str, target_model: str,
    col, db, voyage_key: str, git_meta: dict, now_iso: str, score: float,
) -> None:
    """Index a single PDF file into the docs__ collection."""
    import hashlib as _hl
    from nexus.doc_indexer import _embed_with_fallback
    from nexus.pdf_chunker import PDFChunker
    from nexus.pdf_extractor import PDFExtractor

    content_hash = _hl.sha256(file.read_bytes()).hexdigest()

    # Staleness check
    existing = col.get(where={"source_path": str(file)}, include=["metadatas"], limit=1)
    if existing["metadatas"]:
        stored = existing["metadatas"][0]
        if stored.get("content_hash") == content_hash and stored.get("embedding_model") == target_model:
            return

    try:
        result = PDFExtractor().extract(file)
        chunks = PDFChunker().chunk(result.text, result.metadata)
    except Exception as exc:
        _log.warning("PDF extraction failed", path=str(file), error=str(exc))
        return

    if not chunks:
        return

    chunk_texts = [c.text for c in chunks]
    ids = [f"{content_hash[:16]}_{c.chunk_index}" for c in chunks]
    chunk_metas = []
    for c in chunks:
        chunk_metas.append({
            "title": f"{file.relative_to(repo)}:page-{c.metadata.get('page_number', 0)}",
            "tags": "pdf",
            "category": "prose",
            "session_id": "",
            "source_agent": "nexus-indexer",
            "store_type": "pdf",
            "indexed_at": now_iso,
            "expires_at": "",
            "ttl_days": 0,
            "source_path": str(file),
            "line_start": 0,
            "line_end": 0,
            "frecency_score": float(score),
            "chunk_index": c.chunk_index,
            "chunk_count": len(chunks),
            "ast_chunked": False,
            "filename": file.name,
            "file_extension": ".pdf",
            "page_number": c.metadata.get("page_number", 0),
            "corpus": collection_name,
            "embedding_model": target_model,
            "content_hash": content_hash,
            **git_meta,
        })

    embeddings, actual_model = _embed_with_fallback(
        chunk_texts, target_model, voyage_key, input_type="document",
    )
    if actual_model != target_model:
        for m in chunk_metas:
            m["embedding_model"] = actual_model

    db.upsert_chunks_with_embeddings(
        collection_name=collection_name,
        ids=ids, documents=chunk_texts, embeddings=embeddings, metadatas=chunk_metas,
    )


def _discover_and_index_rdrs(
    repo: Path, rdr_abs_paths: list[Path], db, voyage_key: str, now_iso: str,
) -> None:
    """Discover and index RDR documents into docs__rdr__<repo-name>."""
    from nexus.doc_indexer import batch_index_markdowns

    _RDR_EXCLUDES = {"README.md", "TEMPLATE.md"}
    rdr_files: list[Path] = []

    for rdr_dir in rdr_abs_paths:
        if not rdr_dir.is_dir():
            continue
        rdr_files.extend(
            p for p in sorted(rdr_dir.glob("*.md"))
            if p.is_file() and p.name not in _RDR_EXCLUDES
        )

    if not rdr_files:
        return

    corpus = f"rdr__{repo.name}"
    _log.info("RDR discovery", repo=str(repo), rdr_count=len(rdr_files), corpus=f"docs__{corpus}")

    batch_index_markdowns(rdr_files, corpus)


def _prune_misclassified(
    repo: Path,
    code_collection: str,
    docs_collection: str,
    code_files: list[tuple[float, Path]],
    prose_files: list[tuple[float, Path]],
    pdf_files: list[tuple[float, Path]],
    db,
) -> None:
    """Remove chunks from the wrong collection after reclassification.

    E.g., if a .md file was previously in code__ (old indexer), delete those chunks.
    """
    code_paths = {str(f) for _, f in code_files}
    docs_paths = {str(f) for _, f in prose_files} | {str(f) for _, f in pdf_files}

    # Check code__ for prose/PDF files that don't belong
    code_col = db.get_or_create_collection(code_collection)
    _prune_collection(code_col, docs_paths)

    # Check docs__ for code files that don't belong
    docs_col = db.get_or_create_collection(docs_collection)
    _prune_collection(docs_col, code_paths)


def _prune_collection(col, wrong_paths: set[str]) -> None:
    """Delete chunks from *col* whose source_path is in *wrong_paths*."""
    for source_path in wrong_paths:
        existing = col.get(where={"source_path": source_path}, include=[])
        if existing["ids"]:
            col.delete(ids=existing["ids"])
            _log.debug("pruned misclassified chunks", source_path=source_path, count=len(existing["ids"]))
```

**Step 5: Update `_run_index_frecency_only` to handle both collections**

Replace `_run_index_frecency_only` (lines 101-137):

```python
def _run_index_frecency_only(repo: Path, registry: "RepoRegistry") -> None:
    """Update frecency_score metadata on all indexed chunks without re-embedding."""
    from nexus.config import get_credential
    from nexus.frecency import batch_frecency
    from nexus.db import make_t3

    info = registry.get(repo)
    if info is None:
        return

    voyage_key = get_credential("voyage_api_key")
    chroma_key = get_credential("chroma_api_key")
    if not voyage_key or not chroma_key:
        raise CredentialsMissingError(
            f"T3 credentials missing for frecency reindex of '{repo.name}'"
        )

    frecency_map = batch_frecency(repo)
    db = make_t3()

    # Update frecency in both code__ and docs__ collections
    collection_names = [
        info.get("code_collection", info["collection"]),
    ]
    docs_col = info.get("docs_collection")
    if docs_col:
        collection_names.append(docs_col)

    for collection_name in collection_names:
        col = db.get_or_create_collection(collection_name)
        for file, score in frecency_map.items():
            existing = col.get(
                where={"source_path": str(file)},
                include=["metadatas"],
            )
            if not existing["ids"]:
                continue
            updated_metadatas = [
                {**m, "frecency_score": float(score)}
                for m in existing["metadatas"]
            ]
            db.update_chunks(collection=collection_name, ids=existing["ids"], metadatas=updated_metadatas)
```

**Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: All PASS (including new routing tests)

**Step 7: Run full suite**

Run: `uv run pytest tests/ -x -q`
Expected: All pass

**Step 8: Commit**

```bash
git add src/nexus/indexer.py tests/test_indexer.py
git commit -m "feat: unified indexer pipeline with code/prose/PDF classification and dual collections"
```

---

### Task 7: CLI Rename — `nx index code` → `nx index repo`

**Files:**
- Modify: `src/nexus/commands/index.py:23-43`
- Modify: `tests/test_index_cmd.py`

**Step 1: Update the command**

In `src/nexus/commands/index.py`, change:

```python
@index.command("repo")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--frecency-only",
    is_flag=True,
    default=False,
    help="Update frecency scores only; skip re-embedding (faster, for re-ranking refresh).",
)
def index_repo_cmd(path: Path, frecency_only: bool) -> None:
    """Register and immediately index a code repository at PATH.

    Classifies files by extension: code files get voyage-code-3 embeddings (code__),
    prose and PDFs get voyage-context-3 embeddings (docs__), RDR documents are
    auto-discovered and indexed into docs__rdr__.
    """
    from nexus.indexer import index_repository

    reg = _registry()
    path = path.resolve()
    if reg.get(path) is None:
        reg.add(path)
        click.echo(f"Registered {path}.")

    click.echo(f"{'Updating frecency scores' if frecency_only else 'Indexing'} {path}…")
    index_repository(path, reg, frecency_only=frecency_only)
    click.echo("Done.")
```

**Step 2: Update tests — change "code" to "repo"**

In `tests/test_index_cmd.py`, replace all occurrences of `"code"` in CLI invocations with `"repo"`:

- `["index", "code", str(repo)]` → `["index", "repo", str(repo)]`
- `["index", "code", str(repo), "--frecency-only"]` → `["index", "repo", str(repo), "--frecency-only"]`
- `["index", "code", str(index_home / "nonexistent")]` → `["index", "repo", str(index_home / "nonexistent")]`

Update test names and docstrings similarly.

**Step 3: Update E2E tests — change "code" to "repo"**

In `tests/test_indexer_e2e.py`:

- `["index", "code", str(mini_repo)]` → `["index", "repo", str(mini_repo)]`
- `["index", "code", str(mini_repo), "--frecency-only"]` → `["index", "repo", str(mini_repo), "--frecency-only"]`

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_index_cmd.py tests/test_indexer_e2e.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/nexus/commands/index.py tests/test_index_cmd.py tests/test_indexer_e2e.py
git commit -m "feat: rename 'nx index code' to 'nx index repo' (clean break, no aliases)"
```

---

### Task 8: E2E Tests — Dual Collection Pipeline

**Files:**
- Modify: `tests/test_indexer_e2e.py` (add new E2E tests)

**Step 1: Create a richer mini_repo fixture**

Add to `tests/test_indexer_e2e.py`:

```python
# Additional corpus files for smart indexing tests
_PROSE_FILES = {
    "README.md": "# Nexus\n\nNexus is a semantic search and knowledge management system.\n\n"
                 "## Features\n\n- Semantic search across code repositories\n"
                 "- Persistent memory across sessions\n- Three storage tiers\n",
    "docs/architecture.md": "# Architecture\n\n## Overview\n\nNexus uses a three-tier storage model.\n\n"
                            "### T1 — Session Scratch\n\nEphemeral ChromaDB with DefaultEmbeddingFunction.\n\n"
                            "### T3 — Permanent Knowledge\n\nChromaDB Cloud with Voyage AI embeddings.\n",
    "config.yaml": "server:\n  port: 7890\n  headPollInterval: 10\nembeddings:\n  rerankerModel: rerank-2.5\n",
}

_RDR_FILES = {
    "docs/rdr/ADR-001-storage-tiers.md": "---\ntitle: Storage Tier Architecture\nstatus: accepted\n---\n\n"
                                          "# ADR-001: Storage Tier Architecture\n\n"
                                          "## Decision\n\nWe use three storage tiers.\n",
}


@pytest.fixture(scope="module")
def rich_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A git repo with code, prose, and RDR files for smart indexing tests."""
    repo = tmp_path_factory.mktemp("nexus-rich")

    # Code files
    for rel in _CORPUS_FILES:
        src = _NEXUS_ROOT / rel
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    # Prose files
    for rel, content in _PROSE_FILES.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    # RDR files
    for rel, content in _RDR_FILES.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial rich corpus"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def rich_registry(tmp_path: Path, rich_repo: Path) -> RepoRegistry:
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    return reg
```

**Step 2: Write E2E tests for dual-collection indexing**

```python
# ── Smart repo indexing E2E tests ────────────────────────────────────────────

def test_smart_index_creates_both_collections(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """index_repository creates chunks in both code__ and docs__ collections."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])

    assert code_col.count() > 0, "Expected code chunks in code__ collection"
    assert docs_col.count() > 0, "Expected prose chunks in docs__ collection"


def test_smart_index_code_files_in_code_collection(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Python source files end up in the code__ collection."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    result = code_col.get(include=["metadatas"])
    source_paths = {m.get("source_path", "") for m in result["metadatas"]}

    assert any("ttl.py" in p for p in source_paths), f"Expected ttl.py in code__; got: {source_paths}"
    assert any("corpus.py" in p for p in source_paths)
    # Prose files must NOT be in code__
    assert not any("README.md" in p for p in source_paths), "README.md should not be in code__"


def test_smart_index_prose_files_in_docs_collection(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Markdown and YAML files end up in the docs__ collection."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    result = docs_col.get(include=["metadatas"])
    source_paths = {m.get("source_path", "") for m in result["metadatas"]}

    assert any("README.md" in p for p in source_paths), f"Expected README.md in docs__; got: {source_paths}"
    assert any("architecture.md" in p for p in source_paths)
    assert any("config.yaml" in p for p in source_paths)
    # Code files must NOT be in docs__
    assert not any("ttl.py" in p for p in source_paths), "ttl.py should not be in docs__"


def test_smart_index_rdr_excluded_from_docs(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """RDR files are NOT in the docs__repo collection (they go to docs__rdr__)."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    result = docs_col.get(include=["metadatas"])
    source_paths = {m.get("source_path", "") for m in result["metadatas"]}

    assert not any("ADR-001" in p for p in source_paths), (
        f"RDR files should not be in docs__ collection; got: {source_paths}"
    )


def test_smart_index_search_code_query(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Code-specific query returns results from code__ collection."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    results = local_t3.search("parse TTL days weeks permanent", [info["code_collection"]], n_results=5)
    assert len(results) > 0
    source_paths = [r.get("source_path", "") for r in results]
    assert any("ttl.py" in p for p in source_paths)


def test_smart_index_search_prose_query(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Natural language query returns results from docs__ collection."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    results = local_t3.search(
        "semantic search knowledge management system features",
        [info["docs_collection"]],
        n_results=5,
    )
    assert len(results) > 0
    source_paths = [r.get("source_path", "") for r in results]
    assert any("README.md" in p for p in source_paths), (
        f"Expected README.md in prose search results; got: {source_paths}"
    )


def test_smart_index_embedding_model_metadata(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Chunks record the correct embedding_model in metadata."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)

    # Code chunks should record voyage-code-3
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    code_result = code_col.get(include=["metadatas"], limit=1)
    if code_result["metadatas"]:
        assert code_result["metadatas"][0].get("embedding_model") == "voyage-code-3"

    # Docs chunks should record voyage-context-3 (or voyage-4 fallback for single-chunk)
    docs_col = local_t3.get_or_create_collection(info["docs_collection"])
    docs_result = docs_col.get(include=["metadatas"], limit=1)
    if docs_result["metadatas"]:
        model = docs_result["metadatas"][0].get("embedding_model", "")
        assert model in ("voyage-context-3", "voyage-4"), f"Unexpected docs model: {model}"


def test_smart_index_staleness_check(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """Second index run skips unchanged files in both collections."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

        info = rich_registry.get(rich_repo)
        code_count = local_t3.get_or_create_collection(info["code_collection"]).count()
        docs_count = local_t3.get_or_create_collection(info["docs_collection"]).count()

        # Re-index unchanged repo
        index_repository(rich_repo, rich_registry)

        assert local_t3.get_or_create_collection(info["code_collection"]).count() == code_count
        assert local_t3.get_or_create_collection(info["docs_collection"]).count() == docs_count


def test_smart_index_git_metadata_present(
    rich_repo: Path, rich_registry: RepoRegistry, local_t3: T3Database,
) -> None:
    """All chunks carry git metadata for historical repo access."""
    from nexus.indexer import index_repository

    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, rich_registry)

    info = rich_registry.get(rich_repo)
    code_col = local_t3.get_or_create_collection(info["code_collection"])
    result = code_col.get(include=["metadatas"], limit=1)
    meta = result["metadatas"][0]

    assert meta.get("git_commit_hash"), "git_commit_hash must be set"
    assert meta.get("git_branch") == "main"
    assert meta.get("source_path"), "source_path must be set"
```

**Step 2: Run E2E tests**

Run: `uv run pytest tests/test_indexer_e2e.py -v -k "smart"`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/test_indexer_e2e.py
git commit -m "test: add E2E tests for smart repo indexing with dual collections"
```

---

### Task 9: E2E Test — Migration from Old Single-Collection

**Files:**
- Modify: `tests/test_indexer_e2e.py`

**Step 1: Write migration E2E test**

```python
def test_migration_moves_prose_from_code_to_docs(
    rich_repo: Path, tmp_path: Path, local_t3: T3Database,
) -> None:
    """Simulates old indexer (all in code__), then runs new indexer to verify migration."""
    from nexus.indexer import index_repository

    # Create a registry that only has the old-style 'collection' key
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(rich_repo)
    info = reg.get(rich_repo)
    code_col_name = info["code_collection"]
    docs_col_name = info["docs_collection"]

    # Manually insert a prose file's chunk into code__ (simulating old behavior)
    code_col = local_t3.get_or_create_collection(code_col_name)
    readme_path = str(rich_repo / "README.md")
    code_col.add(
        ids=["fake-prose-in-code"],
        documents=["Nexus is a semantic search system"],
        metadatas=[{
            "source_path": readme_path,
            "content_hash": "old-hash",
            "embedding_model": "voyage-code-3",
            "store_type": "code",
        }],
    )
    assert code_col.count() == 1

    # Run the new unified indexer
    with patch("nexus.db.make_t3", return_value=local_t3), \
         patch("nexus.config.get_credential", side_effect=lambda k: "test-key"):
        index_repository(rich_repo, reg)

    # The fake prose chunk should have been pruned from code__
    code_result = code_col.get(include=["metadatas"])
    code_sources = {m.get("source_path", "") for m in code_result["metadatas"]}
    assert readme_path not in code_sources, "README.md should be pruned from code__ after migration"

    # README.md should now be in docs__
    docs_col = local_t3.get_or_create_collection(docs_col_name)
    docs_result = docs_col.get(include=["metadatas"])
    docs_sources = {m.get("source_path", "") for m in docs_result["metadatas"]}
    assert any("README.md" in p for p in docs_sources), "README.md should be in docs__ after migration"
```

**Step 2: Run test**

Run: `uv run pytest tests/test_indexer_e2e.py::test_migration_moves_prose_from_code_to_docs -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_indexer_e2e.py
git commit -m "test: add migration E2E test for prose chunks moving from code__ to docs__"
```

---

### Task 10: Documentation Updates

**Files:**
- Modify: `spec.md` — update `nx index code` references to `nx index repo`, document new collection scheme
- Modify: `ARCHITECTURE.md` — if it exists, update indexing pipeline description
- Modify: `CLAUDE.md` — update `nx index code` → `nx index repo` reference
- Modify: `nx/skills/nexus/SKILL.md` — update quick reference command
- Modify: `nx/README.md` — update command reference if `nx index code` is mentioned

**Step 1: Find and update all references**

Search for `"nx index code"` and `"index code"` across the codebase:

Run: `uv run grep -rn "index code" --include="*.md" --include="*.py" .` to find all references

Then update each one to `nx index repo` / `index repo`.

Key files to update:

In `nx/skills/nexus/SKILL.md` (line 43):
```
nx index code <path>                 # index code repo
```
→
```
nx index repo <path>                 # index repository (code + prose + PDFs)
```

In `CLAUDE.md`, the Knowledge Architecture section:
```
2. Index current codebase: `nx index code <path>` (do this once per repo)
```
→
```
2. Index current codebase: `nx index repo <path>` (do this once per repo)
```

In `nx/hooks/scripts/permission-request-stdin.sh` if it auto-approves `nx index`:
Check if pattern-matched — likely already works since `nx index` is the prefix.

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -x -q`
Expected: All PASS

**Step 3: Commit**

```bash
git add -A
git commit -m "docs: update all references from 'nx index code' to 'nx index repo'"
```

---

### Task 11: Final Integration Test Run

**Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests pass

**Step 2: Run E2E tests specifically**

Run: `uv run pytest tests/test_indexer_e2e.py -v`
Expected: All pass, including:
- Original tests (adapted for `nx index repo`)
- Smart indexing: dual collections, search routing, metadata, staleness
- Migration: prose chunks move from code__ to docs__

**Step 3: Run classifier + config tests**

Run: `uv run pytest tests/test_classifier.py tests/test_config.py -v`
Expected: All pass

**Step 4: Verify no regressions in existing functionality**

Run: `uv run pytest tests/test_index_rdr_cmd.py tests/test_search.py -v`
Expected: All pass (RDR command and search are unchanged)
