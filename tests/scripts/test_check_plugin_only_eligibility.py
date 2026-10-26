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

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_plugin_only_eligibility.py"

PASS_LINE = "PLUGIN-ONLY ELIGIBILITY PASSED"
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
    return r


def _commit(repo: Path, changes: dict[str, str | None]) -> None:
    for rel, text in changes.items():
        if text is None:
            (repo / rel).unlink()
        else:
            _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


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


# ---- real history (skipped where the objects are absent; counted) -----------


def _real_has(rev: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
        ).returncode
        == 0
    )


_REAL_BUMP_FILES = [
    ".claude-plugin/marketplace.json",
    "conexus/.claude-plugin/plugin.json",
    "sn/.claude-plugin/plugin.json",
    "tool-spill/.claude-plugin/plugin.json",
    "mcpb/manifest.json",
    "mcpb/pyproject.toml",
    "pyproject.toml",
    "uv.lock",
    "CHANGELOG.md",
    "conexus/CHANGELOG.md",
]


@pytest.mark.skipif(not _real_has("v7.86.3"), reason="tag v7.86.3 not in this clone")
def test_real_fake_7_86_4_is_eligible(tmp_path: Path) -> None:
    """The measured case: v7.86.3's own files, bumped as the fake 7.86.4 was.

    Builds a tmp repo whose base commit holds the REAL v7.86.3 blobs of the
    ten files and bumps them the way a release does (sed on the version
    strings; uv.lock only the conexus stanza). Mutation: tighten any
    version-surface check until a real file shape fails it.
    """
    r = tmp_path / "real"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    base: dict[str, str] = {}
    for rel in _REAL_BUMP_FILES:
        blob = subprocess.run(
            ["git", "show", f"v7.86.3:{rel}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        base[rel] = blob
        _write(r, rel, blob)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "tag", "v7.86.3")

    changes: dict[str, str | None] = {}
    for rel, blob in base.items():
        if rel == "uv.lock":
            new = re.sub(
                r'(\[\[package\]\]\nname = "conexus"\nversion = )"7\.86\.3"',
                r'\1"7.86.4"',
                blob,
                count=1,
            )
        elif rel == "pyproject.toml":
            new = re.sub(
                r'(?m)^version = "7\.86\.3"$', 'version = "7.86.4"', blob, count=1
            )
        elif rel in ("CHANGELOG.md", "conexus/CHANGELOG.md"):
            new = "## 7.86.4\n\nPairs with engine-service-v0.1.164.\n\n" + blob
        else:
            new = blob.replace("7.86.3", "7.86.4")
        assert new != blob, f"bump produced no change in {rel}"
        changes[rel] = new
    _commit(r, changes)

    out = _check(r)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.rstrip().splitlines()[-1] == PASS_LINE
    assert f"{len(_REAL_BUMP_FILES)} changed paths" in out.stdout


@pytest.mark.skipif(not _real_has("d004f9d46"), reason="d004f9d46 not in this clone")
def test_real_self_gc_commit_is_ineligible() -> None:
    """develop's d004f9d46 touches src/nexus/hooks/self_gc.py.

    Mutation: accept src/ paths.
    """
    r = _run(REPO_ROOT, "--base", "d004f9d46^", "--head", "d004f9d46")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "src/nexus/hooks/self_gc.py" in r.stdout
