# SPDX-License-Identifier: AGPL-3.0-or-later
"""4.29.1 destructive-verb safety regression tests.

Pins:

- nexus-6ims P0: ``catalog prune-stale`` resolves relative paths
  against ``owner.repo_root``, NOT cwd. The pre-fix logic
  mass-misclassified valid relative-path entries as stale whenever
  the verb was run from a different repo's cwd.
- nexus-tnz3 P1: ``catalog gc`` defaults to dry-run; requires
  ``--no-dry-run --confirm`` to actually delete.
- nexus-9nim P2: ``catalog link-bulk-delete`` defaults to dry-run;
  requires ``--no-dry-run --confirm`` to actually delete.
- RDR-106 Option A: every destructive verb writes a JSONL backup
  snapshot to ``$catalog_dir/.deleted-backups/`` BEFORE the actual
  delete. ``nx catalog undelete`` re-emits the documents (and their
  links) via event-sourced ``DocumentRegistered`` / ``LinkCreated``
  events.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.cli import main


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialised catalog with NEXUS_CATALOG_PATH pointed at it."""
    cat_dir = tmp_path / "catalog"
    Catalog.init(cat_dir)
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    return cat_dir


@pytest.fixture
def cat(catalog_env: Path) -> Catalog:
    return Catalog(catalog_env, catalog_env / ".catalog.db")


# ── nexus-6ims: prune-stale uses owner.repo_root for relative paths ────────


def test_prune_stale_resolves_relative_paths_against_owner_root(
    cat: Catalog, tmp_path: Path,
) -> None:
    """A relative file_path that exists under the owner's repo_root
    must NOT be classified as stale, regardless of the cwd from
    which the verb runs."""
    # Owner with explicit repo_root.
    repo = tmp_path / "myrepo"
    repo.mkdir()
    src = repo / "src" / "main.py"
    src.parent.mkdir(parents=True)
    src.write_text("print('hello')\n")

    owner = cat.register_owner(
        "myrepo", "repo", repo_hash="hash", repo_root=str(repo),
    )
    cat.register(
        owner, "main.py", content_type="code",
        file_path="src/main.py",  # RELATIVE to repo_root
    )

    runner = CliRunner()
    # Run from a DIFFERENT cwd to expose the pre-fix bug.
    different_cwd = tmp_path / "elsewhere"
    different_cwd.mkdir()
    cwd_save = os.getcwd()
    os.chdir(different_cwd)
    try:
        result = runner.invoke(main, ["catalog", "prune-stale"])
    finally:
        os.chdir(cwd_save)

    assert result.exit_code == 0, result.output
    # The pre-fix bug would say "1 stale entry" because Path("src/main.py").exists()
    # is False from /elsewhere. Post-fix: 0 stale.
    assert "0 stale" in result.output, result.output


def test_prune_stale_skips_relative_paths_when_owner_has_no_repo_root(
    cat: Catalog, tmp_path: Path,
) -> None:
    """nexus-6ims fail-safe: owner.repo_root empty → refuse to
    classify; skip with a structured warning. Better to leave a
    real stale entry around than to mass-delete valid ones."""
    # Curator-style owner: no repo_root.
    owner = cat.register_owner("curator", "curator")
    cat.register(
        owner, "doc.md", content_type="prose",
        file_path="some/relative/path.md",  # owner has NO repo_root
    )

    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "prune-stale"])
    assert result.exit_code == 0
    # The verb refuses to classify; output reports the skip count.
    assert "skipped" in result.output.lower()
    assert "no repo_root" in result.output.lower()
    assert "0 stale" in result.output


def test_prune_stale_classifies_truly_missing_absolute_path_as_stale(
    cat: Catalog, tmp_path: Path,
) -> None:
    """Absolute path that doesn't exist on disk → classified as stale
    (the original happy-path of the verb still works)."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    cat.register(
        owner, "gone.md", content_type="prose",
        file_path="/var/folders/definitely-not-a-real-path-9d8f7a/file.md",
    )
    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "prune-stale"])
    assert result.exit_code == 0
    assert "1 stale" in result.output


# ── nexus-tnz3: catalog gc dry-run is the default ──────────────────────────


def test_gc_default_is_dry_run(cat: Catalog) -> None:
    """nexus-tnz3: 4.29.1 made dry-run the default. ``nx catalog gc``
    with no flags must NOT delete."""
    owner = cat.register_owner("repo", "repo", repo_hash="abc")
    t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
    cat.update(t, meta={"miss_count": 2})

    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "gc"])
    assert result.exit_code == 0
    assert cat.resolve(t) is not None, (
        "default `nx catalog gc` deleted entry — dry-run-default contract broken"
    )
    assert "would be deleted" in result.output


def test_gc_no_dry_run_alone_is_still_report_only(cat: Catalog) -> None:
    """``--no-dry-run`` without ``--confirm`` still report-only."""
    owner = cat.register_owner("repo", "repo", repo_hash="abc")
    t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
    cat.update(t, meta={"miss_count": 2})

    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "gc", "--no-dry-run"])
    assert result.exit_code == 0
    assert "treated as report-only" in result.output
    assert cat.resolve(t) is not None


def test_gc_no_dry_run_plus_confirm_actually_deletes(cat: Catalog) -> None:
    """Both flags required: ``--no-dry-run --confirm``."""
    owner = cat.register_owner("repo", "repo", repo_hash="abc")
    t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
    cat.update(t, meta={"miss_count": 2})

    runner = CliRunner()
    result = runner.invoke(
        main, ["catalog", "gc", "--no-dry-run", "--confirm"],
    )
    assert result.exit_code == 0
    assert cat.resolve(t) is None
    # Backup snapshot should have been written.
    backup_dir = cat._dir / ".deleted-backups"
    assert backup_dir.exists()
    assert any(backup_dir.glob("catalog-gc-*.jsonl"))


# ── nexus-9nim: link-bulk-delete --confirm safety rail ─────────────────────


def test_link_bulk_delete_default_is_dry_run(cat: Catalog) -> None:
    """nexus-9nim: 4.29.1 default flipped to dry-run."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    a = cat.register(owner, "A", content_type="prose", file_path="a.md")
    b = cat.register(owner, "B", content_type="prose", file_path="b.md")
    cat.link(a, b, "cites", created_by="t")

    runner = CliRunner()
    result = runner.invoke(
        main, ["catalog", "link-bulk-delete", "--type", "cites"],
    )
    assert result.exit_code == 0
    # Default dry-run; link survives.
    assert "Would remove 1 link(s)" in result.output
    assert len(cat.links_from(a)) == 1


def test_link_bulk_delete_no_confirm_is_still_report_only(
    cat: Catalog,
) -> None:
    owner = cat.register_owner("o", "repo", repo_hash="h")
    a = cat.register(owner, "A", content_type="prose", file_path="a.md")
    b = cat.register(owner, "B", content_type="prose", file_path="b.md")
    cat.link(a, b, "cites", created_by="t")

    runner = CliRunner()
    result = runner.invoke(main, [
        "catalog", "link-bulk-delete", "--type", "cites", "--no-dry-run",
    ])
    assert result.exit_code == 0
    assert len(cat.links_from(a)) == 1


def test_link_bulk_delete_confirm_actually_removes(cat: Catalog) -> None:
    owner = cat.register_owner("o", "repo", repo_hash="h")
    a = cat.register(owner, "A", content_type="prose", file_path="a.md")
    b = cat.register(owner, "B", content_type="prose", file_path="b.md")
    cat.link(a, b, "cites", created_by="t")

    runner = CliRunner()
    result = runner.invoke(main, [
        "catalog", "link-bulk-delete", "--type", "cites",
        "--no-dry-run", "--confirm",
    ])
    assert result.exit_code == 0
    assert "Removed 1 link" in result.output
    assert len(cat.links_from(a)) == 0
    # Backup snapshot.
    backup_dir = cat._dir / ".deleted-backups"
    assert any(
        p for p in backup_dir.glob("catalog-link-bulk-delete-*.jsonl")
    )


# ── RDR-106 Option A: backup-before-delete + undelete ──────────────────────


def test_delete_writes_backup_before_deleting(
    cat: Catalog,
) -> None:
    """``nx catalog delete <tumbler> --yes`` writes a JSONL snapshot
    BEFORE calling delete_document."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    t = cat.register(
        owner, "doomed.md", content_type="prose", file_path="doomed.md",
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["catalog", "delete", str(t), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert cat.resolve(t) is None
    backup_dir = cat._dir / ".deleted-backups"
    backups = list(backup_dir.glob("catalog-delete-*.jsonl"))
    assert len(backups) == 1, "expected exactly one delete backup"
    # Backup carries the deleted document.
    with backups[0].open() as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert lines[0]["kind"] == "header"
    assert lines[0]["verb"] == "delete"
    assert any(
        rec.get("kind") == "document" and rec.get("tumbler") == str(t)
        for rec in lines
    )


def test_undelete_round_trips_document_via_event_log(
    cat: Catalog,
) -> None:
    """delete + undelete round-trip preserves the document AND its links."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    a = cat.register(
        owner, "alice.md", content_type="prose", file_path="alice.md",
    )
    b = cat.register(
        owner, "bob.md", content_type="prose", file_path="bob.md",
    )
    cat.link(a, b, "cites", created_by="test")
    cat.link(b, a, "relates", created_by="test")

    # Delete A (writes backup).
    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "delete", str(a), "--yes"])
    assert result.exit_code == 0
    assert cat.resolve(a) is None

    # Find the backup file.
    backup_dir = cat._dir / ".deleted-backups"
    backups = list(backup_dir.glob("catalog-delete-*.jsonl"))
    assert len(backups) == 1
    backup_name = backups[0].name

    # Restore.
    result = runner.invoke(main, ["catalog", "undelete", backup_name])
    assert result.exit_code == 0, result.output
    assert "Restored 1 document" in result.output
    # Document is back.
    restored = cat.resolve(a)
    assert restored is not None
    assert restored.title == "alice.md"
    # Links re-emitted.
    out_links = cat.links_from(a)
    in_links = cat.links_to(a)
    assert any(l.link_type == "cites" for l in out_links)
    assert any(l.link_type == "relates" for l in in_links)


def test_list_backups_shows_recent_snapshots(cat: Catalog) -> None:
    """``nx catalog list-backups`` enumerates JSONL files newest-first."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    t = cat.register(
        owner, "doomed.md", content_type="prose", file_path="doomed.md",
    )
    runner = CliRunner()
    runner.invoke(main, ["catalog", "delete", str(t), "--yes"])
    result = runner.invoke(main, ["catalog", "list-backups"])
    assert result.exit_code == 0, result.output
    assert "catalog-delete-" in result.output
    assert "verb=delete" in result.output
    assert "rows=1" in result.output


def test_vacuum_backups_dry_run_default(cat: Catalog) -> None:
    """``nx catalog vacuum-backups`` defaults to dry-run."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    t = cat.register(
        owner, "doomed.md", content_type="prose", file_path="doomed.md",
    )
    runner = CliRunner()
    runner.invoke(main, ["catalog", "delete", str(t), "--yes"])

    # Default dry-run; days = 30 means recent file is kept.
    result = runner.invoke(main, ["catalog", "vacuum-backups"])
    assert result.exit_code == 0
    assert "Would remove 0 backup file" in result.output
    backup_dir = cat._dir / ".deleted-backups"
    assert any(backup_dir.glob("*.jsonl"))


def test_vacuum_backups_actually_removes_old_files(
    cat: Catalog,
) -> None:
    """Files older than the retention window are removed when
    ``--no-dry-run`` is passed."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    t = cat.register(
        owner, "doomed.md", content_type="prose", file_path="doomed.md",
    )
    runner = CliRunner()
    runner.invoke(main, ["catalog", "delete", str(t), "--yes"])

    backup_dir = cat._dir / ".deleted-backups"
    backups = list(backup_dir.glob("*.jsonl"))
    assert len(backups) == 1
    # Backdate the file beyond retention.
    old_time = backups[0].stat().st_mtime - (40 * 86400)
    os.utime(backups[0], (old_time, old_time))

    result = runner.invoke(
        main, ["catalog", "vacuum-backups", "--no-dry-run"],
    )
    assert result.exit_code == 0
    assert "Removed 1 backup" in result.output
    assert not any(backup_dir.glob("*.jsonl"))


def test_prune_stale_writes_backup_for_truly_stale(
    cat: Catalog, tmp_path: Path,
) -> None:
    """When prune-stale --no-dry-run --confirm runs, it writes a
    backup snapshot covering the deleted entries."""
    owner = cat.register_owner("o", "repo", repo_hash="h")
    cat.register(
        owner, "gone.md", content_type="prose",
        file_path="/definitely/missing/path-9876/gone.md",
    )

    runner = CliRunner()
    result = runner.invoke(main, [
        "catalog", "prune-stale", "--no-dry-run", "--confirm",
    ])
    assert result.exit_code == 0
    assert "deleted 1" in result.output.lower()
    backup_dir = cat._dir / ".deleted-backups"
    assert any(backup_dir.glob("catalog-prune-stale-*.jsonl"))
