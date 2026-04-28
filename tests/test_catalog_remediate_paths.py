# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx catalog remediate-paths``.

Two layers:

  * Pure-function unit tests for the ``_build_basename_index``,
    ``_entry_needs_remediation``, ``_resolve_candidate`` helpers.
  * CLI integration tests that drive the full Click command against a
    real catalog backed by tmp_path.

The pure-function helpers carry the actual logic; the CLI command is a
thin wrapper around them. Cover both so a future refactor that moves
logic between the two surfaces still verifies the contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main
from nexus.commands.catalog import (
    _build_basename_index,
    _entry_needs_remediation,
    _resolve_candidate,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def initialized_catalog(catalog_env):
    cat = Catalog.init(catalog_env)
    cat.register_owner("test-papers", "curator")
    return cat


@pytest.fixture
def papers_dir(tmp_path: Path) -> Path:
    """A simulated git-backed papers archive with a few files."""
    root = tmp_path / "papers"
    (root / "ml").mkdir(parents=True)
    (root / "db").mkdir(parents=True)
    (root / "ml" / "sage.pdf").write_bytes(b"%PDF-1.4 SAGE")
    (root / "db" / "delos.pdf").write_bytes(b"%PDF-1.4 DELOS")
    (root / "db" / "consensus.pdf").write_bytes(b"%PDF-1.4 CONSENSUS")
    (root / "ml" / "notes.md").write_text("# notes")
    return root


# ── Helper: _build_basename_index ───────────────────────────────────────────


def test_basename_index_walks_recursively(papers_dir: Path) -> None:
    idx = _build_basename_index(papers_dir)
    assert "sage.pdf" in idx
    assert "delos.pdf" in idx
    assert "consensus.pdf" in idx
    assert "notes.md" in idx
    assert all(len(v) == 1 for v in idx.values())


def test_basename_index_collects_duplicates(tmp_path: Path) -> None:
    """Two files sharing a basename in different dirs both land in the index."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "dup.pdf").write_bytes(b"A")
    (tmp_path / "b" / "dup.pdf").write_bytes(b"B")
    idx = _build_basename_index(tmp_path)
    assert len(idx["dup.pdf"]) == 2


def test_basename_index_filters_by_extension(papers_dir: Path) -> None:
    """Default extensions filter excludes non-PDF/MD files."""
    (papers_dir / "ignore.bin").write_bytes(b"x")
    idx = _build_basename_index(papers_dir)
    assert "ignore.bin" not in idx


def test_basename_index_extensions_none_matches_all(papers_dir: Path) -> None:
    """``extensions=None`` is the ``--extensions *`` flag — matches everything."""
    (papers_dir / "ignore.bin").write_bytes(b"x")
    idx = _build_basename_index(papers_dir, extensions=None)
    assert "ignore.bin" in idx
    assert "sage.pdf" in idx


def test_basename_index_prunes_hidden_dirs(tmp_path: Path) -> None:
    """``.git``, ``.venv`` etc. are pruned to keep walk fast on real repos."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "junk.pdf").write_bytes(b"x")
    (tmp_path / "real.pdf").write_bytes(b"y")
    idx = _build_basename_index(tmp_path)
    assert "real.pdf" in idx
    assert "junk.pdf" not in idx


def test_basename_index_returns_absolute_paths(papers_dir: Path) -> None:
    idx = _build_basename_index(papers_dir)
    for paths in idx.values():
        for p in paths:
            assert p.is_absolute()


# ── Helper: _entry_needs_remediation ───────────────────────────────────────


class _FakeEntry:
    """Stand-in for CatalogEntry — only file_path matters for these tests."""
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path


def test_needs_remediation_detects_basename_only() -> None:
    needs, reason = _entry_needs_remediation(_FakeEntry("paper.pdf"))
    assert needs
    assert reason == "basename"


def test_needs_remediation_detects_missing_abspath(tmp_path: Path) -> None:
    needs, reason = _entry_needs_remediation(_FakeEntry(str(tmp_path / "ghost.pdf")))
    assert needs
    assert reason == "missing"


def test_needs_remediation_passes_existing_abspath(papers_dir: Path) -> None:
    needs, reason = _entry_needs_remediation(
        _FakeEntry(str(papers_dir / "ml" / "sage.pdf")),
    )
    assert not needs
    assert reason == ""


def test_needs_remediation_skips_empty_file_path() -> None:
    """MCP-stored knowledge entries have file_path='' and aren't remediable."""
    needs, reason = _entry_needs_remediation(_FakeEntry(""))
    assert not needs
    assert reason == "no-file-path"


# ── Helper: _resolve_candidate ─────────────────────────────────────────────


def test_resolve_candidate_unique() -> None:
    chosen, note = _resolve_candidate(
        _FakeEntry("x.pdf"), [Path("/a/x.pdf")],
    )
    assert chosen == Path("/a/x.pdf")
    assert note == "unique"


def test_resolve_candidate_none() -> None:
    chosen, note = _resolve_candidate(_FakeEntry("x.pdf"), [])
    assert chosen is None
    assert note == "none"


def test_resolve_candidate_ambiguous_default_skips() -> None:
    chosen, note = _resolve_candidate(
        _FakeEntry("x.pdf"),
        [Path("/a/x.pdf"), Path("/b/x.pdf")],
    )
    assert chosen is None
    assert note == "ambiguous"


def test_resolve_candidate_prefer_deepest_picks_longest() -> None:
    chosen, note = _resolve_candidate(
        _FakeEntry("x.pdf"),
        [Path("/short/x.pdf"), Path("/very/much/longer/path/x.pdf")],
        prefer_deepest=True,
    )
    assert chosen == Path("/very/much/longer/path/x.pdf")
    assert note == "deepest"


# ── CLI integration ────────────────────────────────────────────────────────


def _register_paper(cat: Catalog, title: str, file_path: str,
                    physical_collection: str = "") -> str:
    """Helper: register a paper entry with a custom file_path. Returns tumbler."""
    from nexus.catalog.tumbler import Tumbler
    owner_row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = 'test-papers'",
    ).fetchone()
    owner = Tumbler.parse(owner_row[0])
    tumbler = cat.register(
        owner=owner, title=title, content_type="paper",
        file_path=file_path,
        physical_collection=physical_collection,
    )
    return str(tumbler)


class TestRemediatePathsCLI:
    def test_dry_run_reports_without_writing(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        """--dry-run shows the transition table and writes nothing."""
        tumbler = _register_paper(initialized_catalog, "SAGE", "sage.pdf")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir), "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "1 entries need remediation" in result.output
        assert "1 resolvable" in result.output
        assert "dry-run" in result.output.lower()

        # Confirm the catalog is unchanged
        from nexus.catalog.tumbler import Tumbler
        entry = initialized_catalog.resolve(Tumbler.parse(tumbler))
        assert entry.file_path == "sage.pdf"  # unchanged

    def test_resolves_basename_to_absolute(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        """A basename file_path is updated to the matching absolute path."""
        tumbler = _register_paper(initialized_catalog, "SAGE", "sage.pdf")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output

        from nexus.catalog.tumbler import Tumbler
        # Re-open the catalog so we read the freshly-written JSONL state.
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        expected = (papers_dir / "ml" / "sage.pdf").resolve()
        assert entry.file_path == str(expected)

    def test_skips_already_good_entries(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        """Entries with a valid abspath are not touched."""
        good_path = str((papers_dir / "ml" / "sage.pdf").resolve())
        _register_paper(initialized_catalog, "SAGE", good_path)

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "0 entries need remediation" in result.output
        assert "skipped 1 already-good" in result.output

    def test_ambiguous_entry_skipped_by_default(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        """Two basename matches → entry is skipped (no update) by default."""
        src = tmp_path / "src"
        (src / "a").mkdir(parents=True)
        (src / "b").mkdir(parents=True)
        (src / "a" / "dup.pdf").write_bytes(b"A")
        (src / "b" / "dup.pdf").write_bytes(b"B")

        tumbler = _register_paper(initialized_catalog, "Dup", "dup.pdf")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(src),
        ])
        assert result.exit_code == 0, result.output
        assert "1 ambiguous" in result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        assert entry.file_path == "dup.pdf"  # unchanged

    def test_prefer_deepest_breaks_ambiguity(
        self, initialized_catalog: Catalog, catalog_env: Path, tmp_path: Path,
    ) -> None:
        src = tmp_path / "src"
        (src / "shallow").mkdir(parents=True)
        (src / "very" / "deep" / "nested").mkdir(parents=True)
        (src / "shallow" / "dup.pdf").write_bytes(b"A")
        (src / "very" / "deep" / "nested" / "dup.pdf").write_bytes(b"B")

        tumbler = _register_paper(initialized_catalog, "Dup", "dup.pdf")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(src), "--prefer-deepest",
        ])
        assert result.exit_code == 0, result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        assert "very/deep/nested" in entry.file_path

    def test_no_candidate_leaves_alone_by_default(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        """Entries with no matching basename in SOURCE_DIR are untouched."""
        tumbler = _register_paper(initialized_catalog, "Lost", "missing.pdf")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "1 no candidate found" in result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        assert entry.file_path == "missing.pdf"
        assert entry.meta.get("status") != "missing"

    def test_mark_missing_tags_unrecoverable_entries(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        tumbler = _register_paper(initialized_catalog, "Lost", "missing.pdf")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir), "--mark-missing",
        ])
        assert result.exit_code == 0, result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        assert entry.meta.get("status") == "missing"

    def test_collection_filter_scopes_to_one_collection(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        """--collection limits remediation to one physical_collection."""
        from nexus.catalog.tumbler import Tumbler
        t1 = Tumbler.parse(_register_paper(
            initialized_catalog, "SAGE", "sage.pdf",
            physical_collection="knowledge__a",
        ))
        t2 = Tumbler.parse(_register_paper(
            initialized_catalog, "DELOS", "delos.pdf",
            physical_collection="knowledge__b",
        ))

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
            "--collection", "knowledge__a",
        ])
        assert result.exit_code == 0, result.output

        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        # SAGE was scoped in → updated to abspath.
        sage = cat2.resolve(t1)
        assert sage.file_path.endswith("sage.pdf")
        assert "/" in sage.file_path
        # DELOS was out of scope → still basename.
        delos = cat2.resolve(t2)
        assert delos.file_path == "delos.pdf"

    def test_idempotent(
        self, initialized_catalog: Catalog, catalog_env: Path, papers_dir: Path,
    ) -> None:
        """Running twice is a no-op the second time."""
        _register_paper(initialized_catalog, "SAGE", "sage.pdf")
        runner = CliRunner()
        runner.invoke(main, ["catalog", "remediate-paths", str(papers_dir)])
        result2 = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result2.exit_code == 0, result2.output
        assert "0 entries need remediation" in result2.output


# ── nexus-srck: DEVONthink-aware remediation ────────────────────────────────


def _register_paper_with_meta(
    cat: Catalog, title: str, file_path: str, *, meta: dict,
) -> str:
    """Variant of ``_register_paper`` that attaches arbitrary meta —
    needed to plant ``devonthink_uri`` on a registered entry without
    going through the (still-evolving) DT ingest path.
    """
    from nexus.catalog.tumbler import Tumbler
    owner_row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = 'test-papers'",
    ).fetchone()
    owner = Tumbler.parse(owner_row[0])
    tumbler = cat.register(
        owner=owner, title=title, content_type="paper",
        file_path=file_path, meta=meta,
    )
    return str(tumbler)


class TestRemediateViaDevonthink:
    """When an entry's meta carries ``devonthink_uri``, remediate-paths
    should ask DEVONthink for the current path before falling back to
    a SOURCE_DIR basename scan. Tests stub the AppleScript bridge so
    they run on every platform and don't require DT to be installed.
    """

    def test_resolves_via_meta_when_dt_returns_existing_path(
        self,
        initialized_catalog: Catalog,
        catalog_env: Path,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """The DT-resolved path takes precedence over basename scanning
        — the file lives in DT's ``Files.noindex`` tree which is NOT
        included in the SOURCE_DIR walk, but DT knows where it is.
        """
        # Plant the "real" DT-managed file outside SOURCE_DIR.
        dt_tree = tmp_path / "DT" / "Files.noindex" / "pdf"
        dt_tree.mkdir(parents=True)
        real_pdf = dt_tree / "graph-rag.pdf"
        real_pdf.write_bytes(b"%PDF-1.4 dt managed body")

        # SOURCE_DIR is unrelated and contains no candidates.
        source_dir = tmp_path / "papers"
        source_dir.mkdir()

        tumbler = _register_paper_with_meta(
            initialized_catalog,
            "Graph RAG",
            "/old/broken/graph-rag.pdf",  # absolute, missing → triggers remediation
            meta={"devonthink_uri": "x-devonthink-item://UUID-A"},
        )

        # Patch the DT resolver + force the macOS gate open so the
        # remediator's _resolve_via_devonthink helper accepts the call
        # on Linux/Windows runners.
        def fake_resolver(uuid: str) -> tuple[str | None, str]:
            assert uuid == "UUID-A"
            return str(real_pdf), ""

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default", fake_resolver,
        )
        monkeypatch.setattr("sys.platform", "darwin")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(source_dir),
        ])
        assert result.exit_code == 0, result.output
        assert "via DEVONthink" in result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        assert entry.file_path == str(real_pdf)

    def test_falls_through_to_basename_when_dt_resolver_fails(
        self,
        initialized_catalog: Catalog,
        catalog_env: Path,
        papers_dir: Path,
        monkeypatch,
    ) -> None:
        """If DT can't resolve (record missing, app not running, etc.)
        the entry should still get the legacy basename treatment so
        we never regress relative to pre-nexus-srck behavior.
        """
        tumbler = _register_paper_with_meta(
            initialized_catalog,
            "SAGE",
            "sage.pdf",
            meta={"devonthink_uri": "x-devonthink-item://NOT-IN-DT"},
        )

        def fake_resolver(uuid: str) -> tuple[str | None, str]:
            return None, "record not found"

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default", fake_resolver,
        )
        monkeypatch.setattr("sys.platform", "darwin")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        # Basename scan succeeded → file_path now absolute under papers_dir.
        assert entry.file_path.endswith("sage.pdf")
        assert "/" in entry.file_path

    def test_falls_through_when_dt_path_doesnt_exist_on_disk(
        self,
        initialized_catalog: Catalog,
        catalog_env: Path,
        papers_dir: Path,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        """DT can return a path that's been moved/deleted out from
        under it. Treat that as "DT doesn't really know" and fall
        back to basename — never persist a path the resolver lied
        about.
        """
        ghost = tmp_path / "ghost-dt-path.pdf"  # never written

        tumbler = _register_paper_with_meta(
            initialized_catalog,
            "SAGE",
            "sage.pdf",
            meta={"devonthink_uri": "x-devonthink-item://STALE-UUID"},
        )

        def fake_resolver(uuid: str) -> tuple[str | None, str]:
            return str(ghost), ""

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default", fake_resolver,
        )
        monkeypatch.setattr("sys.platform", "darwin")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        # Basename scan against papers_dir found sage.pdf.
        assert entry.file_path.endswith("sage.pdf")
        assert "ghost-dt-path" not in entry.file_path

    def test_no_dt_meta_skips_dt_path_entirely(
        self,
        initialized_catalog: Catalog,
        catalog_env: Path,
        papers_dir: Path,
        monkeypatch,
    ) -> None:
        """An entry without ``devonthink_uri`` must NOT trigger any
        resolver call — keeps the resolver from being a hot path on
        catalogs that have no DT-managed entries at all.
        """
        _register_paper_with_meta(
            initialized_catalog, "SAGE", "sage.pdf", meta={"arxiv_id": "1234.5678"},
        )

        calls: list[str] = []

        def tracking_resolver(uuid: str) -> tuple[str | None, str]:
            calls.append(uuid)
            return None, "should not be called"

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default", tracking_resolver,
        )
        monkeypatch.setattr("sys.platform", "darwin")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_non_macos_platform_short_circuits_dt_resolution(
        self,
        initialized_catalog: Catalog,
        catalog_env: Path,
        papers_dir: Path,
        monkeypatch,
    ) -> None:
        """On non-darwin runners the helper must early-return without
        even reading meta — the resolver call would be meaningless.
        """
        _register_paper_with_meta(
            initialized_catalog,
            "SAGE",
            "sage.pdf",
            meta={"devonthink_uri": "x-devonthink-item://UUID-X"},
        )

        calls: list[str] = []

        def tracking_resolver(uuid: str) -> tuple[str | None, str]:
            calls.append(uuid)
            return "/should/not/matter", ""

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default", tracking_resolver,
        )
        monkeypatch.setattr("sys.platform", "linux")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(papers_dir),
        ])
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_dry_run_with_dt_does_not_write(
        self,
        initialized_catalog: Catalog,
        catalog_env: Path,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        dt_tree = tmp_path / "DT" / "Files.noindex"
        dt_tree.mkdir(parents=True)
        real_pdf = dt_tree / "doc.pdf"
        real_pdf.write_bytes(b"%PDF-1.4 body")

        source_dir = tmp_path / "papers"
        source_dir.mkdir()

        tumbler = _register_paper_with_meta(
            initialized_catalog,
            "Doc",
            "/old/missing/doc.pdf",
            meta={"devonthink_uri": "x-devonthink-item://UUID-DRY"},
        )

        def fake_resolver(uuid: str) -> tuple[str | None, str]:
            return str(real_pdf), ""

        monkeypatch.setattr(
            "nexus.aspect_readers._devonthink_resolver_default", fake_resolver,
        )
        monkeypatch.setattr("sys.platform", "darwin")

        runner = CliRunner()
        result = runner.invoke(main, [
            "catalog", "remediate-paths", str(source_dir), "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "via DEVONthink" in result.output
        assert "dry-run" in result.output.lower()

        from nexus.catalog.tumbler import Tumbler
        cat2 = Catalog(catalog_env, catalog_env / ".catalog.db")
        entry = cat2.resolve(Tumbler.parse(tumbler))
        assert entry.file_path == "/old/missing/doc.pdf"  # untouched
