# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-rbmja.1: the mechanical eligibility gate for the plugin-only fast lane.

Covers ``scripts/check_plugin_only_eligibility.py``. Every case runs the
script as a subprocess against a real tmp git repo (no mocks). Each test
names the mutation it was shown red under, in its docstring.

Design of record: T2 nexus/design-plugin-only-fast-lane.md § Eligibility.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_plugin_only_eligibility.py"

PASS_LINE = "PLUGIN-ONLY ELIGIBILITY PASSED"
FAIL_LINE = "PLUGIN-ONLY ELIGIBILITY FAILED"
UNVERIFIED_LINE = "PLUGIN-ONLY ELIGIBILITY UNVERIFIED"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return out.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


PYPROJECT = """\
[project]
name = "conexus"
version = "7.86.3"
dependencies = [
    "click>=8.0",
]

[tool.other]
version = "1.0.0"
"""

MCPB_PYPROJECT = """\
[project]
name = "conexus-mcpb"
version = "7.86.3"
dependencies = [
    "conexus>=7.86.3",
]

[tool.uv]
package = false
"""

MCPB_MANIFEST = """\
{
  "manifest_version": "0.4",
  "name": "conexus",
  "version": "7.86.3",
  "description": "x"
}
"""

UV_LOCK = """\
version = 1

[[package]]
name = "click"
version = "8.1.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "conexus"
version = "7.86.3"
source = { editable = "." }
dependencies = [
    { name = "click" },
]

[[package]]
name = "conexus-foo"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "zeta"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }
"""

BASE_FILES = {
    "pyproject.toml": PYPROJECT,
    "mcpb/pyproject.toml": MCPB_PYPROJECT,
    "mcpb/manifest.json": MCPB_MANIFEST,
    "uv.lock": UV_LOCK,
    "CHANGELOG.md": "# Changelog\n",
    "conexus/CHANGELOG.md": "# Changelog\n",
    "conexus/PENDING_RELEASE.md": "ledger\n",
    "conexus/skills/a.md": "skill\n",
    "conexus/daemon/d.py": "d\n",
    "conexus/plans/p.json": "{}\n",
    "sn/hooks/h.json": "{}\n",
    "tool-spill/x.json": "{}\n",
    ".claude-plugin/marketplace.json": '{"v": "7.86.3"}\n',
    "conexus/.claude-plugin/plugin.json": '{"version": "7.86.3"}\n',
    "src/nexus/a.py": "a = 1\n",
    "src/nexus/engine_version.py": 'REQUIRED_ENGINE_VERSION = "0.1.164"\n',
    "scripts/s.py": "s = 1\n",
    ".github/workflows/w.yml": "on: push\n",
    "mcpb/src/bootstrap.py": "b = 1\n",
    "service/pom.xml": "<p/>\n",
    "tests/test_a.py": "def test_a(): pass\n",
    "docs/a.md": "doc\n",
    # nested look-alikes of the free prefixes, and root files no rule frees
    "src/docs/x.py": "x = 1\n",
    "src/tests/x.py": "x = 1\n",
    "README.md": "readme\n",
    "AGENTS.md": "agents\n",
    # the machinery that gates the lane (nexus-rbmja.5)
    "tests/e2e/release-battery.sh": "#!/bin/bash\n",
    "tests/e2e/plugin-lockstep-tests.sh": "#!/bin/bash\n",
    "tests/e2e/plugin-delivery-proof.sh": "#!/bin/bash\n",
    "tests/e2e/hook-cli-skew/run.sh": "#!/bin/bash\n",
    "tests/test_plugin_lockstep.py": "def test_l(): pass\n",
    "tests/hooks/test_lockstep_survives_cli_skew.py": "def test_s(): pass\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    for rel, text in BASE_FILES.items():
        _write(r, rel, text)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "v7.86.3")
    # The straddle check reads develop; with no bead named it has nothing to
    # look up, but an unreadable ref is still UNVERIFIED, so every repo has one.
    _git(r, "update-ref", "refs/remotes/origin/develop", "HEAD")
    return r


class Link(NamedTuple):
    """A symlink to commit in place of (or beside) a regular file."""

    target: str


def _pyproject_bumped(to: str = "7.86.4") -> str:
    return PYPROJECT.replace('name = "conexus"\nversion = "7.86.3"', f'name = "conexus"\nversion = "{to}"')


def _commit(
    repo: Path,
    changes: dict[str, str | Link | None],
    *,
    bump: bool = True,
    message: str = "change",
) -> None:
    """Commit *changes* on top of the tag. A release always moves the version, so
    pyproject.toml's patch bump rides along unless the case supplies its own
    (or passes ``bump=False`` to test the lane without one)."""
    if bump and "pyproject.toml" not in changes:
        changes = {**changes, "pyproject.toml": _pyproject_bumped()}
    for rel, text in changes.items():
        if isinstance(text, Link):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).unlink(missing_ok=True)
            os.symlink(text.target, repo / rel)
        elif text is None:
            (repo / rel).unlink()
        else:
            (repo / rel).unlink(missing_ok=True)  # replaces a symlink too
            _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _check(repo: Path, base: str = "v7.86.3") -> subprocess.CompletedProcess[str]:
    return _run(repo, "--base", base)


def _bump(text: str) -> str:
    return text.replace("7.86.3", "7.86.4")


# ---- eligible ---------------------------------------------------------------


def test_plugin_only_diff_passes(repo: Path) -> None:
    """Mutation: make every verdict INELIGIBLE (channel-path acceptance removed)."""
    _commit(
        repo,
        {
            "conexus/skills/a.md": "skill v2\n",
            "sn/hooks/h.json": '{"a": 1}\n',
            "tool-spill/x.json": '{"a": 1}\n',
            "tests/test_a.py": "def test_a(): assert True\n",
            "docs/a.md": "doc v2\n",
            "CHANGELOG.md": "# Changelog\nmore\n",
            "conexus/PENDING_RELEASE.md": "ledger v2\n",
        },
    )
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == PASS_LINE
    assert "examined v7.86.3..HEAD: 8 changed paths" in r.stdout


def test_full_version_bump_passes(repo: Path) -> None:
    """The measured fake 7.86.4 shape on synthetic content.

    Mutation: drop the version-surface content check (all four surfaces
    then read INELIGIBLE).
    """
    _commit(
        repo,
        {
            ".claude-plugin/marketplace.json": _bump(
                BASE_FILES[".claude-plugin/marketplace.json"]
            ),
            "conexus/.claude-plugin/plugin.json": _bump(
                BASE_FILES["conexus/.claude-plugin/plugin.json"]
            ),
            "mcpb/manifest.json": _bump(MCPB_MANIFEST),
            "mcpb/pyproject.toml": _bump(MCPB_PYPROJECT),
            "pyproject.toml": PYPROJECT.replace(
                'name = "conexus"\nversion = "7.86.3"',
                'name = "conexus"\nversion = "7.86.4"',
            ),
            "uv.lock": UV_LOCK.replace(
                'name = "conexus"\nversion = "7.86.3"',
                'name = "conexus"\nversion = "7.86.4"',
            ),
            "CHANGELOG.md": "# Changelog\n## 7.86.4\n",
            "conexus/CHANGELOG.md": "# Changelog\n## 7.86.4\n",
        },
    )
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == PASS_LINE


# ---- ineligible: category boundaries ----------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/nexus/a.py",
        "src/nexus/engine_version.py",
        "service/pom.xml",
        "scripts/s.py",
        ".github/workflows/w.yml",
        "mcpb/src/bootstrap.py",
        "conexus/daemon/d.py",
        "conexus/plans/p.json",
    ],
)
def test_non_plugin_path_fails(repo: Path, path: str) -> None:
    """Mutation: accept every path (the allowlist/denylist check removed).

    One src/ line, the engine pin, service/, scripts/, .github/, the rest
    of mcpb/, and the two DENIED conexus subtrees each fail on their own.
    """
    _commit(repo, {"conexus/skills/a.md": "ok\n", path: "changed\n"})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert path in r.stdout
    assert PASS_LINE not in r.stdout


def test_new_file_outside_allowlist_fails(repo: Path) -> None:
    """Mutation: only inspect modified paths, ignore added ones."""
    _commit(repo, {"src/nexus/new.py": "x = 1\n"})
    r = _check(repo)
    assert r.returncode == 1
    assert "src/nexus/new.py" in r.stdout


def test_deleted_file_outside_allowlist_fails(repo: Path) -> None:
    """Mutation: skip status D entries."""
    _commit(repo, {"src/nexus/a.py": None})
    r = _check(repo)
    assert r.returncode == 1
    assert "src/nexus/a.py" in r.stdout


def test_rename_out_of_allowlist_fails(repo: Path) -> None:
    """Mutation: let git detect renames (the destination alone is judged).

    Moving a src/ file into docs/ must still show the src/ deletion.
    """
    _git(repo, "mv", "src/nexus/a.py", "docs/a_moved.py")
    _git(repo, "commit", "-q", "-m", "mv")
    r = _check(repo)
    assert r.returncode == 1
    assert "src/nexus/a.py" in r.stdout


# ---- ineligible: version-surface content checks -----------------------------


def test_uv_lock_dependency_change_fails(repo: Path) -> None:
    """Mutation: treat uv.lock as wholly allowed."""
    _commit(
        repo, {"uv.lock": UV_LOCK.replace('version = "8.1.0"', 'version = "8.2.0"')}
    )
    r = _check(repo)
    assert r.returncode == 1
    assert "uv.lock" in r.stdout


def test_uv_lock_conexus_version_only_passes(repo: Path) -> None:
    """Mutation: reject any uv.lock change."""
    _commit(
        repo,
        {
            "uv.lock": UV_LOCK.replace(
                'name = "conexus"\nversion = "7.86.3"',
                'name = "conexus"\nversion = "7.86.4"',
            )
        },
    )
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_uv_lock_conexus_stanza_other_line_fails(repo: Path) -> None:
    """Mutation: normalise the whole conexus stanza rather than its version line."""
    _commit(
        repo,
        {
            "uv.lock": UV_LOCK.replace(
                '    { name = "click" },\n',
                '    { name = "click" },\n    { name = "evil" },\n',
            )
        },
    )
    r = _check(repo)
    assert r.returncode == 1
    assert "uv.lock" in r.stdout


def test_uv_lock_version_change_hidden_with_conexus_bump_fails(repo: Path) -> None:
    """Mutation: normalise every `version =` line in uv.lock.

    A conexus bump AND a click bump in one diff is a dependency change.
    """
    text = UV_LOCK.replace(
        'name = "conexus"\nversion = "7.86.3"', 'name = "conexus"\nversion = "7.86.4"'
    ).replace('version = "8.1.0"', 'version = "8.2.0"')
    _commit(repo, {"uv.lock": text})
    r = _check(repo)
    assert r.returncode == 1


def test_pyproject_version_only_passes(repo: Path) -> None:
    """Mutation: reject any pyproject.toml change."""
    _commit(
        repo,
        {
            "pyproject.toml": PYPROJECT.replace(
                'version = "7.86.3"', 'version = "7.86.4"', 1
            )
        },
    )
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_pyproject_dependency_edit_fails(repo: Path) -> None:
    """Mutation: treat pyproject.toml as wholly allowed."""
    _commit(repo, {"pyproject.toml": PYPROJECT.replace("click>=8.0", "click>=9.0")})
    r = _check(repo)
    assert r.returncode == 1
    assert "pyproject.toml" in r.stdout


def test_pyproject_other_table_version_fails(repo: Path) -> None:
    """Mutation: normalise every `version =` line, not only [project]'s."""
    _commit(
        repo,
        {"pyproject.toml": PYPROJECT.replace('version = "1.0.0"', 'version = "2.0.0"')},
    )
    r = _check(repo)
    assert r.returncode == 1


def test_mcpb_pyproject_version_and_pin_passes(repo: Path) -> None:
    """Mutation: reject the conexus>= pin change."""
    _commit(repo, {"mcpb/pyproject.toml": _bump(MCPB_PYPROJECT)})
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_mcpb_pyproject_other_edit_fails(repo: Path) -> None:
    """Mutation: treat mcpb/pyproject.toml as wholly allowed."""
    _commit(
        repo,
        {
            "mcpb/pyproject.toml": MCPB_PYPROJECT.replace(
                "package = false", "package = true"
            )
        },
    )
    r = _check(repo)
    assert r.returncode == 1
    assert "mcpb/pyproject.toml" in r.stdout


def test_mcpb_pyproject_new_dependency_fails(repo: Path) -> None:
    """Mutation: normalise any quoted `conexus...` dependency string."""
    _commit(
        repo,
        {
            "mcpb/pyproject.toml": MCPB_PYPROJECT.replace(
                '    "conexus>=7.86.3",\n', '    "conexus>=7.86.3",\n    "other>=1",\n'
            )
        },
    )
    r = _check(repo)
    assert r.returncode == 1


def test_manifest_version_only_passes(repo: Path) -> None:
    """Mutation: reject any mcpb/manifest.json change."""
    _commit(repo, {"mcpb/manifest.json": _bump(MCPB_MANIFEST)})
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_manifest_other_field_fails(repo: Path) -> None:
    """Mutation: treat mcpb/manifest.json as wholly allowed."""
    data = json.loads(MCPB_MANIFEST)
    data["description"] = "changed"
    _commit(repo, {"mcpb/manifest.json": json.dumps(data, indent=2) + "\n"})
    r = _check(repo)
    assert r.returncode == 1
    assert "mcpb/manifest.json" in r.stdout


def test_manifest_added_fails(repo: Path) -> None:
    """Mutation: content-check only when the file exists on both sides."""
    _commit(repo, {"mcpb/manifest.json": None})
    r = _check(repo)
    assert r.returncode == 1


# ---- exit codes -------------------------------------------------------------


def test_empty_diff_fails(repo: Path) -> None:
    """Mutation: an empty change set counts as eligible (vacuous pass)."""
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert PASS_LINE not in r.stdout


def test_unresolvable_base_exits_2(repo: Path) -> None:
    """Mutation: a missing base reads as an empty diff / pass / exit 1."""
    _commit(repo, {"conexus/skills/a.md": "ok\n"})
    r = _check(repo, base="v9.9.9")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "blind to tags" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == UNVERIFIED_LINE
    assert PASS_LINE not in r.stdout


def test_unresolvable_head_exits_2(repo: Path) -> None:
    """Mutation: a missing head is treated as HEAD."""
    r = _run(repo, "--base", "v7.86.3", "--head", "no-such-ref")
    assert r.returncode == 2
    assert r.stdout.rstrip().splitlines()[-1] == UNVERIFIED_LINE


def test_not_a_git_repo_exits_2(tmp_path: Path) -> None:
    """Mutation: a git failure reads as an empty diff."""
    r = _run(tmp_path, "--base", "v7.86.3")
    assert r.returncode == 2
    assert r.stdout.rstrip().splitlines()[-1] == UNVERIFIED_LINE


def test_head_option_selects_range(repo: Path) -> None:
    """Mutation: ignore --head and always diff to HEAD."""
    _commit(repo, {"conexus/skills/a.md": "ok\n"})
    good = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"src/nexus/a.py": "bad\n"})
    assert _run(repo, "--base", "v7.86.3", "--head", good).returncode == 0
    assert _run(repo, "--base", "v7.86.3").returncode == 1


# ---- nexus-rbmja.5: the version bump ---------------------------------------


def test_plugin_diff_without_a_version_bump_fails(repo: Path) -> None:
    """A plugin diff that does not move the version is the nexus-konsk non-delivery.

    Mutation (drop version_bump_problem): the diff is ELIGIBLE and the battery
    prints PASSED(PLUGIN-ONLY) for a cut Claude Code will never pick up.
    """
    _commit(repo, {"conexus/skills/a.md": "skill v2\n"}, bump=False)
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "INELIGIBLE: version bump:" in r.stdout and "7.86.3 -> 7.86.3" in r.stdout
    assert PASS_LINE not in r.stdout


@pytest.mark.parametrize("to", ["7.87.0", "8.0.0", "7.86.5", "7.86.2", "7.86.10"])
def test_only_a_patch_plus_one_bump_is_eligible(repo: Path, to: str) -> None:
    """Mutation (accept any version change, or any increase): a minor bump, a
    skipped patch, a downgrade and a jump all read ELIGIBLE."""
    _commit(repo, {"conexus/skills/a.md": "skill v2\n", "pyproject.toml": _pyproject_bumped(to)})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "not a patch bump" in r.stdout and "7.86.4 exactly" in r.stdout


@pytest.mark.parametrize("to", ["7.86.4rc1", "7.86.4.1", "7.86.x", "7.86"])
def test_a_non_numeric_version_is_ineligible(repo: Path, to: str) -> None:
    """The surface normaliser accepts any version-looking value; the bump check
    must still refuse one that is not X.Y.Z. Mutation (skip the semver parse):
    the surface check alone passes it."""
    _commit(repo, {"conexus/skills/a.md": "skill v2\n", "pyproject.toml": _pyproject_bumped(to)})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "is not X.Y.Z on both sides" in r.stdout


def test_a_deleted_pyproject_cannot_pass_the_bump_check(repo: Path) -> None:
    """Mutation (let a missing pyproject.toml read as no problem)."""
    _commit(repo, {"pyproject.toml": None}, bump=False)
    r = _check(repo)
    assert r.returncode == 1
    assert "must exist on both sides" in r.stdout


# ---- nexus-rbmja.5: ancestry and --base shape --------------------------------


def test_head_that_does_not_descend_from_base_is_ineligible(repo: Path) -> None:
    """A head on unrelated history diffs as a pile of changes against the tag.

    Mutation (drop the is-ancestor check): the unrelated head with only plugin
    changes and a bump reads ELIGIBLE.
    """
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    _commit(repo, {"conexus/skills/a.md": "skill v2\n"})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "does not descend from v7.86.3" in r.stdout
    assert PASS_LINE not in r.stdout


@pytest.mark.parametrize("base", ["main", "HEAD", "7.86.3", "v7.86", "v7.86.3-rc1", "v7.86.3^", "plugin-v7.86.3-1"])
def test_base_must_be_a_client_tag(repo: Path, base: str) -> None:
    """Mutation (drop the vX.Y.Z regex): any commit-ish is accepted as the base,
    including one that resolves."""
    _git(repo, "tag", "plugin-v7.86.3-1")
    _commit(repo, {"conexus/skills/a.md": "ok\n"})
    r = _check(repo, base=base)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "must be the last client tag" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == UNVERIFIED_LINE


# ---- nexus-rbmja.5: the gating machinery is not free -------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/e2e/release-battery.sh",
        "tests/e2e/plugin-lockstep-tests.sh",
        "tests/e2e/plugin-delivery-proof.sh",
        "tests/e2e/hook-cli-skew/run.sh",
        "tests/e2e/hook-cli-skew/new_file.py",
        "tests/test_plugin_lockstep.py",
        "tests/hooks/test_lockstep_survives_cli_skew.py",
        "tests/e2e/release-preflight.sh",
        "tests/e2e/lib/python.sh",
        "tests/e2e/lib/mandatory_pins.sh",
        "tests/test_plugin_channel.py",
        "tests/scripts/test_check_plugin_only_eligibility.py",
        "tests/scripts/test_release_battery_end_to_end.py",
        "tests/scripts/test_plugin_lockstep_wrapper.py",
    ],
)
def test_the_gating_harness_is_not_free_under_tests(repo: Path, path: str) -> None:
    """Mutation (drop the harness rule): tests/ is free and an eligible diff may
    edit the eligibility call that gates it."""
    _commit(repo, {"conexus/skills/a.md": "ok\n", path: "# edited\n"})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"INELIGIBLE: {path} [" in r.stdout
    assert "release machinery that gates this lane" in r.stdout


# ---- nexus-rbmja.5: type changes and symlinks --------------------------------


def test_a_symlink_replacing_a_channel_file_is_ineligible(repo: Path) -> None:
    """Status T. Mutation (judge by path only): conexus/skills/a.md is a channel
    path, so a symlink swapped in for it reads ELIGIBLE."""
    _commit(repo, {"conexus/skills/a.md": Link("../../src/nexus/a.py")})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "conexus/skills/a.md [T]" in r.stdout and "symlink" in r.stdout


def test_a_new_symlink_is_ineligible(repo: Path) -> None:
    """Status A with mode 120000 (no type change). Mutation (check status T only)."""
    _commit(repo, {"docs/link.md": Link("../src/nexus/a.py")})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "docs/link.md [A]" in r.stdout and "mode 000000 -> 120000" in r.stdout


# ---- nexus-rbmja.5: mutation killers ------------------------------------------


@pytest.mark.parametrize("path", ["src/docs/x.py", "src/tests/x.py"])
def test_a_free_prefix_nested_under_src_is_not_free(repo: Path, path: str) -> None:
    """Mutation (match tests/ and docs/ anywhere in the path: ``p in path``)."""
    _commit(repo, {path: "changed\n"})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"INELIGIBLE: {path} [M]" in r.stdout


@pytest.mark.parametrize("path", ["README.md", "AGENTS.md"])
def test_a_root_markdown_file_is_not_a_changelog(repo: Path, path: str) -> None:
    """Mutation (free any root .md instead of the two named files)."""
    _commit(repo, {path: "changed\n"})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"INELIGIBLE: {path} [M]" in r.stdout


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"version": 7}', ""])
def test_a_manifest_that_does_not_parse_is_ineligible(repo: Path, text: str) -> None:
    """Mutation (treat a manifest that fails to parse as unchanged-but-version)."""
    _commit(repo, {"mcpb/manifest.json": text})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "mcpb/manifest.json [M] unparsable" in r.stdout


@pytest.mark.parametrize("pin", ["evil>=7.86.4", "conexus-foo>=7.86.4", "conexus==7.86.4"])
def test_the_mcpb_pin_cannot_be_rewritten_to_another_package(repo: Path, pin: str) -> None:
    """Mutation (normalise any quoted ``<name>>=<version>`` dependency)."""
    text = _bump(MCPB_PYPROJECT).replace("conexus>=7.86.4", pin)
    assert pin in text
    _commit(repo, {"mcpb/pyproject.toml": text})
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "mcpb/pyproject.toml [M] changes more than the version" in r.stdout


@pytest.mark.parametrize("path", sorted(["pyproject.toml", "mcpb/pyproject.toml", "mcpb/manifest.json", "uv.lock"]))
def test_deleting_a_version_surface_is_ineligible(repo: Path, path: str) -> None:
    """Mutation (content-check only when both sides exist, pass a delete)."""
    _commit(repo, {path: None}, bump=False)
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"INELIGIBLE: {path} [D] status D: must exist on both sides" in r.stdout


def test_a_package_named_like_conexus_is_not_the_conexus_stanza(repo: Path) -> None:
    """Mutation (match the stanza name by prefix: ``startswith("conexus")``)."""
    _commit(
        repo,
        {
            "uv.lock": UV_LOCK.replace(
                'name = "conexus-foo"\nversion = "1.0.0"',
                'name = "conexus-foo"\nversion = "1.0.1"',
            )
        },
    )
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "uv.lock [M] changes more than the version" in r.stdout


def test_the_summary_counts_what_was_examined(repo: Path) -> None:
    """Non-vacuity: the ``examined`` line names the range, the path count and the
    category split. Mutation (drop the line, or count after filtering)."""
    _commit(
        repo,
        {
            "conexus/skills/a.md": "v2\n",
            "docs/a.md": "v2\n",
            "CHANGELOG.md": "# Changelog\nmore\n",
        },
    )
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        "examined v7.86.3..HEAD: 4 changed paths "
        "(1 changelog, 1 channel, 1 tests-docs, 1 version-surface)"
    ) in r.stdout


def test_every_offender_is_reported_with_its_reason(repo: Path) -> None:
    """Two offenders in one diff are both named, each with its reason, and the
    failure line counts them. Mutation (stop at the first offender)."""
    _commit(
        repo,
        {
            "src/nexus/a.py": "changed\n",
            "scripts/s.py": "changed\n",
            "conexus/skills/a.md": "ok\n",
        },
    )
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    reason = "outside the plugin channel, tests/, docs/ and the version surfaces"
    assert f"INELIGIBLE: src/nexus/a.py [M] {reason}" in r.stdout
    assert f"INELIGIBLE: scripts/s.py [M] {reason}" in r.stdout
    assert f"{FAIL_LINE}: 2 problem(s) in 4 paths" in r.stdout
    assert "INELIGIBLE: conexus/skills/a.md" not in r.stdout


def test_a_blind_tag_checkout_is_unverified_not_a_pass(repo: Path) -> None:
    """The base tag does not resolve: the sentinel names the cause. Mutation
    (drop assert_tag_visibility from _resolve): the failure still exits 2 but
    from a later git error, and this message is gone."""
    _commit(repo, {"conexus/skills/a.md": "ok\n"})
    _git(repo, "tag", "-d", "v7.86.3")
    r = _check(repo)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "blind to tags" in r.stdout and "--base v7.86.3" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == UNVERIFIED_LINE


# ---- file shapes from the working tree, built in a tmp repo ------------------
# These replace two tests that read v7.86.3 and a develop commit out of git
# history and skipped silently in the depth-1 PR CI clone (nexus-rbmja.5, High).
# They copy the files' CONTENT from the working tree, so no tag or history is
# needed, and none can skip.

_SHAPE_FILES = [
    ".claude-plugin/marketplace.json",
    "conexus/.claude-plugin/plugin.json",
    "sn/.claude-plugin/plugin.json",
    "tool-spill/.claude-plugin/plugin.json",
    "prose-edit/.claude-plugin/plugin.json",
    "mcpb/manifest.json",
    "mcpb/pyproject.toml",
    "pyproject.toml",
    "uv.lock",
    "CHANGELOG.md",
    "conexus/CHANGELOG.md",
]


def _current_version() -> str:
    m = re.search(
        r'(?ms)^\[project\].*?^version = "([0-9]+\.[0-9]+\.[0-9]+)"',
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert m, "pyproject.toml has no [project] version"
    return m.group(1)


def _next_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def _bump_pyproject(text: str, version: str) -> str:
    return re.sub(
        rf'(?m)^version = "{re.escape(version)}"$',
        f'version = "{_next_patch(version)}"',
        text,
        count=1,
    )


def _shape_repo(tmp_path: Path, files: list[str]) -> tuple[Path, str, dict[str, str]]:
    """A tmp repo whose base commit holds the working tree's copy of *files*,
    tagged with the version in the working tree's pyproject.toml."""
    version = _current_version()
    r = tmp_path / "shape"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    base: dict[str, str] = {}
    for rel in files:
        blob = (REPO_ROOT / rel).read_text(encoding="utf-8")
        base[rel] = blob
        _write(r, rel, blob)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", f"v{version}")
    _git(r, "update-ref", "refs/remotes/origin/develop", "HEAD")
    return r, version, base


def test_working_tree_release_files_bumped_as_a_release_does_are_eligible(tmp_path: Path) -> None:
    """The measured case: the real files, bumped the way the fake 7.86.4 was
    (sed on the version strings; uv.lock only the conexus stanza).

    Mutation: tighten any version-surface check until a real file shape fails it.
    """
    r, version, base = _shape_repo(tmp_path, _SHAPE_FILES)
    new_version = _next_patch(version)
    changes: dict[str, str | Link | None] = {}
    for rel, blob in base.items():
        if rel == "uv.lock":
            new = re.sub(
                rf'(\[\[package\]\]\nname = "conexus"\nversion = )"{re.escape(version)}"',
                rf'\1"{new_version}"',
                blob,
                count=1,
            )
        elif rel == "pyproject.toml":
            new = _bump_pyproject(blob, version)
        elif rel in ("CHANGELOG.md", "conexus/CHANGELOG.md"):
            new = f"## {new_version}\n\nPairs with an engine.\n\n" + blob
        else:
            new = blob.replace(version, new_version)
        assert new != blob, f"bump produced no change in {rel}"
        changes[rel] = new
    _commit(r, changes)

    out = _check(r, base=f"v{version}")
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.rstrip().splitlines()[-1] == PASS_LINE
    assert f"{len(_SHAPE_FILES)} changed paths" in out.stdout


def test_working_tree_hook_source_edit_is_ineligible_beside_a_plugin_change(tmp_path: Path) -> None:
    """develop's self-gc commit touched src/nexus/hooks/self_gc.py: the real file,
    edited, with a plugin change and a second src/ file in the same diff.

    Mutation: accept src/ paths, or stop at the first offender.
    """
    files = [
        "src/nexus/hooks/self_gc.py",
        "src/nexus/engine_version.py",
        "pyproject.toml",
        "conexus/hooks/hooks.json",
    ]
    r, version, base = _shape_repo(tmp_path, files)
    _commit(
        r,
        {
            "src/nexus/hooks/self_gc.py": base["src/nexus/hooks/self_gc.py"] + "# edited\n",
            "src/nexus/engine_version.py": base["src/nexus/engine_version.py"] + "# edited\n",
            "conexus/hooks/hooks.json": base["conexus/hooks/hooks.json"] + "\n",
            "pyproject.toml": _bump_pyproject(base["pyproject.toml"], version),
        },
    )
    out = _check(r, base=f"v{version}")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "INELIGIBLE: src/nexus/hooks/self_gc.py [M]" in out.stdout
    assert "INELIGIBLE: src/nexus/engine_version.py [M]" in out.stdout
    assert f"{FAIL_LINE}: 2 problem(s) in 4 paths" in out.stdout


# ---- nexus-rbmja.10: a bead may not straddle the lane ------------------------
# The cut script this lane replaced refused a ledger entry whose bead also
# touched wheel content. Cherry-picking only the plugin half of such a bead
# ships half of it, so the gate looks up every bead named in the release
# commits on develop and judges all of that bead's commits.


def _develop(repo: Path, commits: list[tuple[str, dict[str, str | None]]]) -> list[str]:
    """Land *commits* on a develop line off the current HEAD, point
    ``origin/develop`` at its tip, and return to the branch the release is
    built on. Returns the shas, oldest first."""
    main = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "-b", "dev-line")
    shas: list[str] = []
    for message, changes in commits:
        _commit(repo, dict(changes), bump=False, message=message)
        shas.append(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(repo, "checkout", "-q", main)
    _git(repo, "branch", "-D", "dev-line")
    return shas


def _pick(repo: Path, sha: str) -> None:
    """cherry-pick -x, as the skill does, then bump the version as a release does."""
    _git(repo, "cherry-pick", "-x", sha)


STRADDLE_ENGINE = ("feat: engine half (nexus-aaaaa)", {"src/nexus/a.py": "a = 2\n"})
STRADDLE_PLUGIN = ("feat: plugin half (nexus-aaaaa)", {"conexus/skills/a.md": "skill v2\n"})


def test_a_bead_with_a_src_half_on_develop_is_ineligible(repo: Path) -> None:
    """The plugin half is picked, the src/ half is not. Mutation: drop the
    straddle check (straddling_beads returns no findings): this exits 0."""
    engine, plugin = _develop(repo, [STRADDLE_ENGINE, STRADDLE_PLUGIN])
    _pick(repo, plugin)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "INELIGIBLE: bead nexus-aaaaa straddles the lane" in r.stdout
    assert engine[:10] in r.stdout
    assert "src/nexus/a.py" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1].startswith(FAIL_LINE)


@pytest.mark.parametrize(
    "path",
    ["scripts/s.py", "service/pom.xml", "tests/e2e/release-battery.sh", "conexus/daemon/d.py"],
)
def test_a_bead_half_in_any_refused_area_straddles(repo: Path, path: str) -> None:
    """The gate's own classification decides, not a src/-only list. Mutation:
    judge only src/ paths."""
    _engine, plugin = _develop(
        repo, [("fix: other half (nexus-aaaaa)", {path: "changed\n"}), STRADDLE_PLUGIN]
    )
    _pick(repo, plugin)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    r = _check(repo)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"touches {path}" in r.stdout


def test_a_bead_wholly_in_the_plugin_passes_and_says_what_it_examined(repo: Path) -> None:
    """Mutation: refuse any bead named in the commits (a check that cannot pass)."""
    first, second = _develop(
        repo,
        [
            ("feat: one (nexus-aaaaa)", {"conexus/skills/a.md": "v2\n"}),
            ("fix: two (nexus-aaaaa)", {"docs/a.md": "doc v2\n"}),
        ],
    )
    _pick(repo, first)
    _pick(repo, second)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 bead(s) named in the commits, 2 commit(s) of theirs examined" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == PASS_LINE


def test_a_bead_commit_that_only_moves_the_version_does_not_straddle(repo: Path) -> None:
    """The check reuses classify(): a version-only edit of a version surface is
    eligible. Mutation: treat every non-channel path as a straddle."""
    bump, plugin = _develop(
        repo,
        [
            ("chore: bump (nexus-aaaaa)", {"pyproject.toml": _pyproject_bumped()}),
            STRADDLE_PLUGIN,
        ],
    )
    _pick(repo, plugin)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert bump  # the bump commit really sits on develop


@pytest.mark.parametrize(
    "other",
    ["nexus-aaaaa.2", "nexus-aaaaab", "nexus-aaaa"],
    ids=["child bead", "longer id", "prefix of the id"],
)
def test_bead_ids_match_as_whole_tokens(repo: Path, other: str) -> None:
    """A different bead whose id merely contains this one is not this bead.
    Mutation: substring match on the id (the old --grep did this)."""
    _develop(repo, [(f"feat: engine work ({other})", {"src/nexus/a.py": "a = 2\n"})])
    _git(repo, "checkout", "-q", "-b", "rel")
    _commit(repo, {"conexus/skills/a.md": "v2\n"}, message="feat: plugin (nexus-aaaaa)")
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_commits_already_in_the_base_tag_are_not_a_straddle(repo: Path) -> None:
    """A bead whose src/ half shipped in an earlier release is whole by now.
    Mutation: scan develop's whole history instead of base..develop."""
    _commit(repo, {"src/nexus/a.py": "a = 2\n"}, bump=False, message="feat: engine (nexus-aaaaa)")
    _git(repo, "tag", "-f", "v7.86.3")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (plugin,) = _develop(repo, [STRADDLE_PLUGIN])
    _pick(repo, plugin)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_merge_commit_naming_the_bead_is_not_scanned(repo: Path) -> None:
    """Only the bead's own commits are judged; a merge's message is not one.
    Mutation: include merges (then the merge's first-parent diff is judged)."""
    _develop(repo, [STRADDLE_PLUGIN])
    _git(repo, "checkout", "-q", "-b", "side", "origin/develop")
    _commit(repo, {"src/nexus/a.py": "a = 3\n"}, bump=False, message="feat: unrelated")
    _git(repo, "checkout", "-q", "-b", "mergeline", "origin/develop")
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge side (nexus-aaaaa)", "side")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (plugin,) = _git(repo, "log", "--format=%H", "-1", "--grep=plugin half", "origin/develop").split()
    _pick(repo, plugin)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_unreadable_develop_ref_is_unverified_not_a_pass(repo: Path) -> None:
    """Mutation: treat a ref that does not resolve as 'no other commits'."""
    _commit(repo, {"conexus/skills/a.md": "ok\n"}, message="feat: plugin (nexus-aaaaa)")
    r = _run(repo, "--base", "v7.86.3", "--develop-ref", "origin/nope")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "--develop-ref origin/nope does not resolve" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == UNVERIFIED_LINE
    assert PASS_LINE not in r.stdout


def test_the_default_develop_ref_is_origin_develop(repo: Path) -> None:
    """Mutation: default to a ref the fixture lacks (here: develop)."""
    _commit(repo, {"conexus/skills/a.md": "ok\n"})
    r = _check(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "straddle check against origin/develop" in r.stdout


def test_develop_ref_option_selects_the_reference(repo: Path) -> None:
    """The ref is the option's, not a constant. Mutation: ignore --develop-ref."""
    engine, plugin = _develop(repo, [STRADDLE_ENGINE, STRADDLE_PLUGIN])
    _git(repo, "update-ref", "refs/heads/elsewhere", "origin/develop")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "v7.86.3")
    _pick(repo, plugin)
    _commit(repo, {}, message="chore(release): conexus 7.86.4")
    assert _check(repo).returncode == 0  # origin/develop is the tag: nothing else to find
    r = _run(repo, "--base", "v7.86.3", "--develop-ref", "elsewhere")
    assert r.returncode == 1, r.stdout + r.stderr
    assert engine[:10] in r.stdout
