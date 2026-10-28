# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 Phase 1 Step 3 (nexus-tr1rj.5): Docling is gone, and the repository says so.

``git grep -il docling`` may name only the files below, each with the reason it still does.
A new mention elsewhere is a Docling path coming back, or a doc that still describes the
removed extractor. The allow-list is the whole contract: it holds

* the alias and notice sites (``auto`` and ``docling`` still run as ``mineru``) and the doctor row
  for a leftover Docling model cache,
* records that describe history and must not be rewritten (RDRs, changelogs, a shipped
  Liquibase changeset, spike and experiment results, service test fixtures), and
* the tests that pin the above.

Non-vacuity: the allow-list is not empty, every entry exists, every file entry still contains the
word (a stale entry would let a mention back in unseen), and the sweep examined files at all.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parent.parent

#: Path -> why it may still say "docling". A trailing ``/`` is a directory prefix.
ALLOWED: dict[str, str] = {
    # The alias and notice sites, the doctor row and the stored legacy values.
    "src/nexus/config.py": "the alias table: auto and docling run as mineru, with a notice",
    "src/nexus/commands/index.py": "--extractor choices and help, and the retired --on-formula-oom choice",
    "src/nexus/commands/dt.py": "--extractor choices and help for nx dt index and nx dt capture",
    "src/nexus/health.py": "the Docling model cache doctor row (a leftover cache is reported, never deleted)",
    # Documentation of the same sites.
    "docs/cli-reference.md": "names docling as an accepted alias, the doctor row and the legacy extraction_method values",
    "docs/configuration.md": "names docling as an accepted pdf.extractor alias",
    "docs/architecture.md": "the legacy extraction_method values still readable on old documents",
    # History that must not be rewritten.
    "docs/rdr/": "RDR records, including RDR-237 itself (closed or accepted decision text)",
    "docs/wire-contract-pending.md": "the wire-contract ledger quotes past commit subjects",
    "CHANGELOG.md": "release history, and the entry that removed Docling",
    "conexus/CHANGELOG.md": "plugin release history",
    "service/src/main/resources/db/changelog/pipeline-001-baseline.xml": "a shipped Liquibase changeset; never edited",
    "service/src/test/": "test fixtures: docling is sample extraction_method data in catalog rows",
    "scripts/experiments/out/": "recorded experiment output",
    "scripts/spikes/": "recorded spike scripts and results",
    ".test_durations.json": "test ids of this file and the alias tests carry the word",
    # Held back until Sam's go: web/ publishes from develop on every push.
    "web/index.html": "install-size text; web/ changes wait for Sam's go (nexus-tr1rj.5)",
    "web/install.ps1": "install-size text; web/ changes wait for Sam's go (nexus-tr1rj.5)",
    "web/install.sh": "install-size text; web/ changes wait for Sam's go (nexus-tr1rj.5)",
    # The tests that pin the above.
    "tests/test_docling_residual_lint.py": "this file",
    "tests/test_extractor_aliases.py": "the alias table and the title chain, which name the old values",
    "tests/test_docling_model_cache_doctor_row.py": "the leftover-cache doctor row",
    "tests/test_mineru_importable_doctor_row.py": "asserts the removed extractor is not a pipeline module",
    "tests/test_index_cmd.py": "nx index pdf accepts the old --extractor names",
    "tests/test_commands_dt.py": "nx dt index accepts the old --extractor names",
    "tests/test_dt_capture_cmd.py": "nx dt capture accepts the old --extractor names",
    "tests/scripts/test_check_dependency_drift.py": "a bump of the removed package is routine, not a finding",
    "tests/test_degraded_pages_line.py": "documents indexed before RDR-237 keep their legacy extraction_method",
    "tests/test_metadata_extraction_method.py": "legacy extraction_method values stay storable",
    "tests/test_rdr237_resume_provenance.py": "legacy extraction_method values are not recomputed on resume",
    "tests/integration/test_rdr237_catalog_hook_record.py": "legacy extraction_method values stay readable in the catalog",
}

#: Files that must never be listed: the extractor module is the place Docling code lived.
NEVER_ALLOWED = ("src/nexus/pdf_extractor.py",)


def _files_naming_docling() -> list[str]:
    result = subprocess.run(
        ["git", "grep", "-ilI", "--untracked", "docling"],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode in (0, 1), result.stderr
    return sorted(line for line in result.stdout.splitlines() if line)


def _is_allowed(path: str) -> bool:
    return any(path == entry or (entry.endswith("/") and path.startswith(entry)) for entry in ALLOWED)


def test_docling_is_named_only_where_the_allow_list_says() -> None:
    hits = _files_naming_docling()
    stray = [path for path in hits if not _is_allowed(path)]
    assert stray == [], (
        "these files still name Docling (RDR-237 removed it); remove the mention, or add the file to "
        f"ALLOWED with a reason if it is an alias site or history: {stray}"
    )


def test_the_extractor_module_names_docling_nowhere() -> None:
    for relative in NEVER_ALLOWED:
        assert relative not in ALLOWED
        assert "docling" not in (REPO / relative).read_text(encoding="utf-8").lower(), relative


def test_the_allow_list_is_not_vacuous() -> None:
    """Every entry exists and still earns its place; the sweep looked at a real tree."""
    assert ALLOWED, "an empty allow-list means the sweep proves nothing about what it allows"
    missing = [entry for entry in ALLOWED if not (REPO / entry.rstrip("/")).exists()]
    assert missing == [], f"allow-list entries that no longer exist: {missing}"
    hits = _files_naming_docling()
    assert len(hits) >= 10, f"the sweep found only {len(hits)} files; git grep is not looking at the tree"
    unused = [
        entry for entry in ALLOWED
        if not any(path == entry or (entry.endswith("/") and path.startswith(entry)) for path in hits)
    ]
    assert unused == [], (
        f"allow-list entries whose files no longer name Docling (remove them, so a mention cannot come back unseen): {unused}"
    )
