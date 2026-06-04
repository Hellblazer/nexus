# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for repo-aware helpers in indexer_utils (nexus-h74)."""
from pathlib import Path
from unittest.mock import patch

import pytest

from unittest.mock import MagicMock, patch

from nexus.indexer_utils import (
    StalenessCache,
    build_staleness_cache,
    check_staleness,
    find_repo_root,
    is_gitignored,
    load_ignore_patterns,
    should_ignore,
)


# ── should_ignore ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("node_modules/pkg/index.js", True),
    ("src/main.py", False),
    (".venv/lib/python3.12/site.py", True),
    ("dist/bundle.js", True),
    ("package-lock.lock", True),   # *.lock matches any file ending in .lock
    ("yarn.lock", True),           # *.lock matches
    ("src/vendor/lib.py", True),   # vendor in any component
    ("build/output.js", True),
])
def test_should_ignore(path: str, expected: bool) -> None:
    from nexus.indexer_utils import _DEFAULT_IGNORE
    assert should_ignore(Path(path), _DEFAULT_IGNORE) == expected


# Path-style patterns (contain '/'): match against the full POSIX-form
# relative path. Pre-fix, these patterns were silent no-ops because the
# matcher only fed each path COMPONENT to fnmatch, which treats '/' as
# a literal. ART's .nexus.yml shipped ``docs/papers/**`` for months
# expecting subtree exclusion; it never excluded anything.
@pytest.mark.parametrize("path,patterns,expected", [
    # subtree wildcards
    ("docs/papers/foo.pdf",         ["docs/papers/**"], True),
    ("docs/papers/sub/foo.pdf",     ["docs/papers/**"], True),
    ("docs/architecture.md",        ["docs/papers/**"], False),
    ("src/main.py",                 ["docs/papers/**"], False),
    # single-segment wildcard (only direct children)
    ("src/main.py",                 ["src/*.py"],       True),
    ("src/sub/main.py",             ["src/*.py"],       False),
    # explicit nested path
    ("a/b/c/d.txt",                 ["a/b/c/d.txt"],    True),
    ("a/b/c/other.txt",             ["a/b/c/d.txt"],    False),
    # path-style does NOT spuriously match part-style usage
    ("papers/foo.pdf",              ["docs/papers/**"], False),
])
def test_should_ignore_path_style(
    path: str, patterns: list[str], expected: bool,
) -> None:
    """Path-style patterns (with '/') match against the full path."""
    assert should_ignore(Path(path), patterns) == expected


# Part-style patterns (no '/'): match against any single component.
# Behaviour preserved from pre-fix implementation so existing configs
# (and _DEFAULT_IGNORE) continue to work.
@pytest.mark.parametrize("path,patterns,expected", [
    ("a/b/papers/file.pdf",         ["papers"],         True),
    ("papers/file.pdf",             ["papers"],         True),
    ("docs/architecture.md",        ["papers"],         False),
    ("a/b/foo.lock",                ["*.lock"],         True),
    ("src/foo.py",                  ["*.lock"],         False),
])
def test_should_ignore_part_style(
    path: str, patterns: list[str], expected: bool,
) -> None:
    """Part-style patterns (no '/') match against any path component."""
    assert should_ignore(Path(path), patterns) == expected


# ── find_repo_root ──────────────────────────────────────────────────────────

def test_find_repo_root_in_git_repo(tmp_path: Path) -> None:
    """find_repo_root returns repo root when inside a git repo."""
    import subprocess
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subdir = repo / "sub" / "deep"
    subdir.mkdir(parents=True)

    root = find_repo_root(subdir)
    assert root is not None
    assert root.resolve() == repo.resolve()


def test_find_repo_root_not_git(tmp_path: Path) -> None:
    """find_repo_root returns None for non-git directories."""
    assert find_repo_root(tmp_path) is None


def test_find_repo_root_from_file(tmp_path: Path) -> None:
    """find_repo_root works when given a file path (uses parent dir)."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    f = repo / "test.txt"
    f.write_text("hello")

    root = find_repo_root(f)
    assert root is not None
    assert root.resolve() == repo.resolve()


# ── is_gitignored ──────────────────────────────────────────────────────────

def test_is_gitignored_respected(tmp_path: Path) -> None:
    """is_gitignored returns True for .gitignore'd files."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    (repo / ".gitignore").write_text("*.pdf\n")
    (repo / "paper.pdf").write_bytes(b"%PDF-1.4 test")
    (repo / "code.py").write_text("x = 1\n")

    assert is_gitignored(repo / "paper.pdf", repo) is True
    assert is_gitignored(repo / "code.py", repo) is False


def test_is_gitignored_non_repo(tmp_path: Path) -> None:
    """is_gitignored returns False gracefully for non-repo dirs."""
    (tmp_path / "file.txt").write_text("test")
    assert is_gitignored(tmp_path / "file.txt", tmp_path) is False


# ── load_ignore_patterns ────────────────────────────────────────────────────

def test_load_ignore_patterns_defaults() -> None:
    """Without repo config, returns at least the default patterns."""
    patterns = load_ignore_patterns()
    assert "node_modules" in patterns
    assert ".git" in patterns
    assert "*.lock" in patterns


def test_load_ignore_patterns_merges_config(tmp_path: Path) -> None:
    """load_ignore_patterns picks up .nexus.yml ignorePatterns."""
    nexus_yml = tmp_path / ".nexus.yml"
    nexus_yml.write_text("server:\n  ignorePatterns:\n    - '*.bak'\n    - tmp\n")
    patterns = load_ignore_patterns(tmp_path)
    assert "*.bak" in patterns
    assert "tmp" in patterns
    assert "node_modules" in patterns  # defaults still present


# ── Path resolution in index_pdf ────────────────────────────────────────────

def test_index_pdf_resolves_path(tmp_path: Path, monkeypatch) -> None:
    """index_pdf normalizes pdf_path to absolute before pre-flight registration.

    Probe via the path passed to ``_register_or_lookup_doc_id`` (the
    Phase-A pre-flight call that lands BEFORE the staleness check, see
    ``doc_indexer.index_pdf:1076-1107``). RDR-101 Phase 5c moved the
    staleness ``where`` filter from ``source_path`` to ``content_hash``,
    so the legacy probe of inspecting the where clause no longer works.
    """
    import hashlib
    from nexus.doc_indexer import index_pdf

    pdf = tmp_path / "test.pdf"
    content = b"%PDF-1.4 test content"
    pdf.write_bytes(content)
    real_hash = hashlib.sha256(content).hexdigest()

    monkeypatch.setattr("nexus.doc_indexer._has_credentials", lambda: True)
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)

    captured_paths: list[Path] = []

    def fake_register(file_path, corpus, *, content_type, physical_collection, **kw):
        captured_paths.append(file_path)
        return ""

    def fake_chroma_retry(fn, **kwargs):
        return {
            "metadatas": [{"content_hash": real_hash, "embedding_model": "model"}],
            "ids": ["x"],
        }

    mock_col = type("FakeCol", (), {"get": lambda self, **kw: None})()
    mock_db = type("FakeDB", (), {
        "get_or_create_collection": lambda self, name: mock_col,
    })()

    monkeypatch.setattr("nexus.doc_indexer.make_t3", lambda: mock_db)
    monkeypatch.setattr("nexus.doc_indexer.index_model_for_collection", lambda n: "model")
    monkeypatch.setattr("nexus.doc_indexer._chroma_with_retry", fake_chroma_retry)
    monkeypatch.setattr("nexus.doc_indexer._register_or_lookup_doc_id", fake_register)

    result = index_pdf(pdf, "test")

    assert result == 0
    assert captured_paths
    assert captured_paths[0].is_absolute()
    assert captured_paths[0] == pdf.resolve()


# ── backward compat: indexer._should_ignore ─────────────────────────────────

def test_indexer_should_ignore_reexport() -> None:
    """indexer._should_ignore is the same function as indexer_utils.should_ignore."""
    from nexus.indexer import _should_ignore
    assert _should_ignore is should_ignore


def test_indexer_default_ignore_matches() -> None:
    """indexer.DEFAULT_IGNORE matches indexer_utils._DEFAULT_IGNORE."""
    from nexus.indexer import DEFAULT_IGNORE
    from nexus.indexer_utils import _DEFAULT_IGNORE
    assert DEFAULT_IGNORE is _DEFAULT_IGNORE


# ── StalenessCache + cached check_staleness ──────────────────────────────────


class TestStalenessCache:
    """Pre-built staleness cache replaces N per-file Chroma roundtrips
    with one paginated sweep. Caller-side ``check_staleness(cache=…)``
    becomes a dict lookup.
    """

    def test_build_indexes_doc_id_and_source_path(self) -> None:
        """One sweep of the collection populates both the doc_id-keyed
        and source_path-keyed indexes from chunk metadata."""
        col = MagicMock()
        # Two chunks per file is realistic; both chunks share the same
        # content_hash + embedding_model so they collapse to a single
        # cache entry per (doc_id, source_path) pair.
        col.get.return_value = {
            "ids": ["c1", "c2", "c3"],
            "metadatas": [
                {
                    "doc_id": "1.1.1",
                    "source_path": "src/a.py",
                    "content_hash": "hash-a",
                    "embedding_model": "voyage-code-3",
                },
                {
                    "doc_id": "1.1.1",
                    "source_path": "src/a.py",
                    "content_hash": "hash-a",
                    "embedding_model": "voyage-code-3",
                },
                # Legacy chunk: source_path only, no doc_id
                {
                    "doc_id": "",
                    "source_path": "legacy/old.py",
                    "content_hash": "hash-l",
                    "embedding_model": "voyage-code-3",
                },
            ],
        }

        cache = build_staleness_cache(col)

        assert cache.by_doc_id == {"1.1.1": ("hash-a", "voyage-code-3")}
        assert cache.by_source_path == {
            "src/a.py": ("hash-a", "voyage-code-3"),
            "legacy/old.py": ("hash-l", "voyage-code-3"),
        }

    def test_build_resolves_phase3_chunks_via_catalog_manifest(self) -> None:
        """nexus-0ocy (RDR-108 Phase 4 review D-M4): Phase-3 chunks
        have no doc_id in metadata, only chunk_text_hash. The
        staleness cache must batch-resolve chash -> doc_id via the
        catalog manifest so by_doc_id stays useful for Phase-3
        corpora; otherwise check_staleness falls through to the
        per-file Chroma path and the build_staleness_cache perf
        win evaporates.
        """
        chash = "a" * 64
        col = MagicMock()
        col.get.return_value = {
            "ids": ["c1"],
            "metadatas": [{
                # Phase-3: chunk_text_hash but no doc_id.
                "chunk_text_hash": chash,
                "content_hash": "hash-a",
                "embedding_model": "voyage-code-3",
            }],
        }

        # Stub the catalog reader so the resolution finds the doc_id.
        # RDR-146 P1.2: indexer_utils now reaches the catalog via
        # make_catalog_reader(); patch that seam.
        fake_cat = MagicMock()
        fake_cat.docs_for_chashes.return_value = {chash: ["1.1.42"]}
        import nexus.catalog.factory as _factory_mod
        with patch.object(
            _factory_mod, "make_catalog_reader", return_value=fake_cat,
        ):
            cache = build_staleness_cache(col)

        # by_doc_id resolved from the manifest, NOT from absent metadata.
        assert cache.by_doc_id == {"1.1.42": ("hash-a", "voyage-code-3")}
        fake_cat.docs_for_chashes.assert_called_once()

    def test_build_skips_chunks_missing_required_fields(self) -> None:
        """Chunks without content_hash or embedding_model (corrupted
        metadata, partial backfill) are skipped — the cache only holds
        rows with both load-bearing fields, so a hit always implies a
        real comparison can succeed."""
        col = MagicMock()
        col.get.return_value = {
            "ids": ["good", "no-hash", "no-model"],
            "metadatas": [
                {
                    "doc_id": "1.1.1",
                    "content_hash": "hash-a",
                    "embedding_model": "voyage-code-3",
                },
                {"doc_id": "1.1.2", "content_hash": "", "embedding_model": "x"},
                {"doc_id": "1.1.3", "content_hash": "h", "embedding_model": ""},
            ],
        }

        cache = build_staleness_cache(col)
        assert cache.by_doc_id == {"1.1.1": ("hash-a", "voyage-code-3")}

    def test_build_returns_empty_on_chroma_error(self) -> None:
        """A failure inside the paginated sweep yields an empty cache
        rather than raising — callers fall through to the per-file
        Chroma path. Failing to populate the cache must never block
        indexing."""
        col = MagicMock()
        col.get.side_effect = RuntimeError("network glitch")

        cache = build_staleness_cache(col)

        assert cache.by_doc_id == {}
        assert cache.by_source_path == {}

    def test_build_logs_warning_on_paginated_get_failure(self, caplog) -> None:
        """nexus-lrhg (RDR-108 audit finding 6): pre-fix the bare
        ``except: pass`` silently masked _paginated_get failures, which
        for Phase-3 corpora forces a whole-collection re-embed when the
        per-file fallback fires. The swallow must emit a structured
        WARNING so operators can detect spurious re-index storms.
        """
        import logging

        import structlog

        col = MagicMock()
        col.name = "code__sample"
        col.get.side_effect = RuntimeError("chroma offline")

        structlog.configure(
            processors=[structlog.stdlib.render_to_log_kwargs],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
        )
        with caplog.at_level(logging.WARNING, logger="nexus.indexer_utils"):
            cache = build_staleness_cache(col)

        assert cache.by_doc_id == {}
        events = [r.getMessage() for r in caplog.records] + [
            getattr(r, "event", "") for r in caplog.records
        ]
        assert any(
            "build_staleness_cache_paginated_get_failed" in e for e in events
        ), f"expected structured warning, got {events!r}"

    def test_check_staleness_with_cache_hit_returns_true(self) -> None:
        """Cache hit + matching hash + matching model = stale (skip)."""
        cache = StalenessCache(
            by_doc_id={"1.1.1": ("hash-a", "voyage-code-3")},
        )
        result = check_staleness(
            col=MagicMock(),
            source_file="src/a.py",
            content_hash="hash-a",
            embedding_model="voyage-code-3",
            doc_id="1.1.1",
            cache=cache,
        )
        assert result is True

    def test_check_staleness_with_cache_hit_wrong_hash_returns_false(self) -> None:
        """Cache hit but content has changed = NOT stale (re-index)."""
        cache = StalenessCache(
            by_doc_id={"1.1.1": ("hash-OLD", "voyage-code-3")},
        )
        result = check_staleness(
            col=MagicMock(),
            source_file="src/a.py",
            content_hash="hash-NEW",
            embedding_model="voyage-code-3",
            doc_id="1.1.1",
            cache=cache,
        )
        assert result is False

    def test_check_staleness_with_cache_miss_returns_false(self) -> None:
        """No cache entry = NOT stale (re-index, possibly heals a ghost
        chunk by writing new metadata that includes doc_id)."""
        cache = StalenessCache()  # empty
        result = check_staleness(
            col=MagicMock(),
            source_file="src/a.py",
            content_hash="hash-a",
            embedding_model="voyage-code-3",
            doc_id="1.1.1",
            cache=cache,
        )
        assert result is False

    def test_check_staleness_cache_does_not_call_chroma(self) -> None:
        """The whole point of the cache is to avoid the per-file Chroma
        roundtrip. A cache argument must short-circuit the network
        call entirely. Pin it explicitly so a future refactor that
        accidentally falls through to ``col.get`` is caught.
        """
        col = MagicMock()
        cache = StalenessCache(
            by_doc_id={"1.1.1": ("hash-a", "voyage-code-3")},
        )

        # Hit
        check_staleness(
            col=col, source_file="src/a.py",
            content_hash="hash-a", embedding_model="voyage-code-3",
            doc_id="1.1.1", cache=cache,
        )
        # Miss
        check_staleness(
            col=col, source_file="src/b.py",
            content_hash="hash-b", embedding_model="voyage-code-3",
            doc_id="1.1.99", cache=cache,
        )

        assert col.get.call_count == 0, (
            f"cache path leaked to col.get: {col.get.call_args_list}"
        )

    def test_check_staleness_falls_back_to_chroma_when_no_cache(self) -> None:
        """``cache=None`` (the default) preserves the legacy per-file
        Chroma roundtrip. Critical for back-compat: any direct caller
        that has not migrated to the cache stays on the original
        path with the same retry/quota semantics.
        """
        col = MagicMock()
        col.get.return_value = {
            "metadatas": [
                {
                    "doc_id": "1.1.1",
                    "content_hash": "hash-a",
                    "embedding_model": "voyage-code-3",
                },
            ],
        }

        with patch(
            "nexus.indexer_utils._chroma_with_retry",
            side_effect=lambda fn, **kw: fn(**kw),
        ):
            result = check_staleness(
                col=col, source_file="src/a.py",
                content_hash="hash-a", embedding_model="voyage-code-3",
                doc_id="1.1.1",
                # no cache=
            )

        assert result is True
        assert col.get.call_count == 1

    def test_check_staleness_legacy_path_uses_source_path(self) -> None:
        """Caller with ``doc_id=''`` (legacy / no catalog) uses the
        source_path index."""
        cache = StalenessCache(
            by_source_path={"legacy/old.py": ("hash-l", "voyage-code-3")},
        )

        # Hit on source_path
        assert check_staleness(
            col=MagicMock(), source_file="legacy/old.py",
            content_hash="hash-l", embedding_model="voyage-code-3",
            doc_id="", cache=cache,
        ) is True

        # Miss on source_path
        assert check_staleness(
            col=MagicMock(), source_file="legacy/missing.py",
            content_hash="hash-x", embedding_model="voyage-code-3",
            doc_id="", cache=cache,
        ) is False
