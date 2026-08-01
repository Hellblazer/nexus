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
- RDR-106 Option A backup-before-delete: FULLY RETIRED (nexus-i711w
  terminal deletion). The snapshot half followed the undelete half:
  ``nexus.catalog.catalog_backup`` and the ``list-backups`` /
  ``vacuum-backups`` verbs died with the local catalog, and destructive
  verbs no longer write local JSONL snapshots in any mode. The dry-run /
  --confirm safety rails above are what remains pinned here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests._catalog_fixture_ops import ActiveCatalog
from nexus.catalog.tumbler import Tumbler
from nexus.cli import main

# nexus-aqbrk: exercises the LOCAL catalog's own machinery (event log /
# JSONL / .catalog.db projection), which service mode deliberately opens
# read-only as a frozen migration source (RDR-176 P1 Gap 2).


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Catalog dir env pin (nexus-i711w: no local init — service catalog)."""
    cat_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    return cat_dir


@pytest.fixture
def cat(catalog_env: Path) -> ActiveCatalog:
    # nexus-i711w C-store: route seeding at the ACTIVE catalog. Service mode
    # opens the local .catalog.db READ-ONLY (frozen migration source,
    # RDR-176 P1 Gap 2), so a direct Catalog handle dies on "attempt to
    # write a readonly database". The subject here is the verb, not the
    # substrate.
    return ActiveCatalog()


# ── nexus-6ims: prune-stale uses owner.repo_root for relative paths ────────


def test_prune_stale_resolves_relative_paths_against_owner_root(
    cat: ActiveCatalog, tmp_path: Path,
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
    cat: ActiveCatalog, tmp_path: Path,
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
    cat: ActiveCatalog, tmp_path: Path,
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


def test_gc_default_is_dry_run(cat: ActiveCatalog) -> None:
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


def test_gc_no_dry_run_alone_is_still_report_only(cat: ActiveCatalog) -> None:
    """``--no-dry-run`` without ``--confirm`` still report-only."""
    owner = cat.register_owner("repo", "repo", repo_hash="abc")
    t = cat.register(owner, "old.py", content_type="code", file_path="src/old.py")
    cat.update(t, meta={"miss_count": 2})

    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "gc", "--no-dry-run"])
    assert result.exit_code == 0
    assert "treated as report-only" in result.output
    assert cat.resolve(t) is not None


def test_gc_no_dry_run_plus_confirm_actually_deletes(cat: ActiveCatalog, catalog_env: Path) -> None:
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
    # (The pre-delete backup snapshot assertion retired with catalog_backup,
    # nexus-i711w: destructive verbs no longer write local JSONL snapshots.)


# ── nexus-9nim: link-bulk-delete --confirm safety rail ─────────────────────


def test_link_bulk_delete_default_is_dry_run(cat: ActiveCatalog) -> None:
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
    cat: ActiveCatalog,
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


def test_link_bulk_delete_confirm_actually_removes(cat: ActiveCatalog, catalog_env: Path) -> None:
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
    # (Backup snapshot assertion retired with catalog_backup, nexus-i711w.)


# ── RDR-106 Option A: backup-before-delete — RETIRED (nexus-i711w) ────────
# test_delete_writes_backup_before_deleting, test_list_backups_shows_
# recent_snapshots, test_vacuum_backups_dry_run_default, test_vacuum_
# backups_actually_removes_old_files, and test_prune_stale_writes_backup_
# for_truly_stale retired with nexus.catalog.catalog_backup and the
# list-backups / vacuum-backups verbs: the pre-delete JSONL snapshot
# machinery was local-catalog-only and died with it.
