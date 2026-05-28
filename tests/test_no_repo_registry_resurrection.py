# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 5.3 (nexus-tts0d.20) CI lint guard.

The legacy ``RepoRegistry`` class and ``~/.config/nexus/repos.json``
file path are DELETED. This test fails if either is re-introduced
under ``src/nexus/`` outside the documented carve-outs (the
upgrade-time migration verb is the only place that still parses the
legacy file shape).
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "nexus"


# Files that legitimately PARSE the legacy ``repos.json`` file shape
# during the deprecation window. Every other file that mentions
# ``repos.json`` either passes the path through to one of these
# parsers, or is a docstring — both are fine; only direct file reads
# need to be policed.
_REPOS_JSON_PARSE_ALLOW = {
    SRC / "commands" / "upgrade.py",
    SRC / "repos.py",
}

# ``class RepoRegistry`` must not exist anywhere — the class was
# deleted in nexus-tts0d.20.
_REPO_REGISTRY_CLASS_RE = re.compile(r"^class\s+RepoRegistry\b", re.MULTILINE)
# Direct file reads of the legacy path (json.loads on a path whose
# basename is repos.json). Heuristic match for ``json.loads(`` near
# a ``repos.json`` literal.
_REPOS_JSON_DIRECT_READ_RE = re.compile(
    r"(json\.loads|json\.load|Path\([^)]*repos\.json|\.read_text\(\)|\.read_bytes\(\))"
    r"[\s\S]{0,200}repos\.json|"
    r"repos\.json[\s\S]{0,200}(json\.loads|json\.load|\.read_text\(\)|\.read_bytes\(\))"
)


def _iter_python_files(root: Path):
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


# ``nexus/registry.py`` is the ONLY whitelisted home for the deprecated
# RepoRegistry class during the test-fixture deprecation window
# (RDR-137 Phase 5.3). Production code routes through nexus.repos
# (catalog-backed) instead. Once the test fixtures finish migrating
# this whitelist evaporates and the file itself can be deleted.
_REPO_REGISTRY_CLASS_ALLOW = {
    SRC / "registry.py",
}


def test_no_RepoRegistry_class_definition_outside_legacy_shim() -> None:
    """The legacy RepoRegistry class lives in nexus.registry only;
    re-introducing it anywhere else is a regression."""
    offenders: list[str] = []
    for p in _iter_python_files(SRC):
        if p in _REPO_REGISTRY_CLASS_ALLOW:
            continue
        text = p.read_text()
        if _REPO_REGISTRY_CLASS_RE.search(text):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, (
        "RDR-137 P5.3 (nexus-tts0d.20) regression: ``class RepoRegistry`` "
        "found in:\n  " + "\n  ".join(offenders) +
        "\n\nThe class is deprecated; use the catalog (nexus.repos.read_dual). "
        "The nexus/registry.py whitelist exists for test-fixture support only."
    )


def test_no_direct_repos_json_parsing_outside_migration_path() -> None:
    """No new code parses the legacy ``repos.json`` file directly.

    Files that mention ``repos.json`` to PASS THE PATH to
    ``nexus.repos.read_dual`` / ``nexus.repos.list_repos_dual`` are
    fine; only direct ``json.loads`` / ``Path("repos.json")`` reads
    are policed. The two parse sites in
    ``_REPOS_JSON_PARSE_ALLOW`` are the carve-outs.
    """
    offenders: list[str] = []
    for p in _iter_python_files(SRC):
        if p in _REPOS_JSON_PARSE_ALLOW:
            continue
        text = p.read_text()
        if _REPOS_JSON_DIRECT_READ_RE.search(text):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, (
        "RDR-137 P5.3 (nexus-tts0d.20) regression: direct parsing of "
        "``repos.json`` detected in:\n  " + "\n  ".join(offenders) +
        "\n\nRoute through nexus.repos.read_dual / list_repos_dual / "
        "_read_repos_json instead. If a direct parse is genuinely "
        "needed for the migration window, add the file to "
        "``_REPOS_JSON_PARSE_ALLOW`` in this test."
    )
