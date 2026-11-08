# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.2: changelog fragments are checked for shape, a hand bullet under ``## [Unreleased]``
is refused, and ``fold`` moves the fragments into ``[Unreleased]`` grouped by section, oldest first,
then deletes them. The lint case checks this repo, so CI's lint bucket enforces the rule."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "changelog_fold", _REPO / "scripts" / "changelog_fold.py"
)
assert _spec and _spec.loader
cf = importlib.util.module_from_spec(_spec)
sys.modules["changelog_fold"] = cf
_spec.loader.exec_module(cf)

_CL = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n### Added\n\n- old entry\n"


def _git(root: Path, *args: str, when: str | None = None) -> None:
    env = {
        **os.environ,
        **({"GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when} if when else {}),
    }
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
        env=env,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    for frag_dir, changelog in cf.TARGETS:
        (tmp_path / frag_dir).mkdir(parents=True)
        (tmp_path / frag_dir / cf.DIR_README).write_text("readme\n")
        (tmp_path / changelog).write_text(_CL)
    return tmp_path


def _frag(
    root: Path, name: str, body: str, frag_dir: str = "changelog/unreleased"
) -> Path:
    p = root / frag_dir / name
    p.write_text(body)
    return p


def test_clean_repo_passes(repo: Path) -> None:
    assert cf.check(repo) == []


def test_well_formed_fragment_passes(repo: Path) -> None:
    _frag(
        repo,
        "nexus-abc.1.md",
        "### Fixed\n\n- **A fix.** Detail\n  continued (nexus-abc.1).\n- second\n",
    )
    assert cf.check(repo) == []


@pytest.mark.parametrize(
    ("name", "body", "needle"),
    [
        ("Nexus-abc.md", "### Fixed\n\n- x\n", "name must be"),
        ("notes.md", "### Fixed\n\n- x\n", "name must be"),
        ("nexus-abc.md", "### Bugs\n\n- x\n", "first line must be"),
        ("nexus-abc.md", "- x\n", "first line must be"),
        ("nexus-abc.md", "### Fixed\n\n", "no '- ' bullet"),
        ("nexus-abc.md", "### Fixed\n\nprose\n", "expected a '- ' bullet"),
        ("nexus-abc.md", "", "empty"),
    ],
)
def test_malformed_fragment_is_refused(
    repo: Path, name: str, body: str, needle: str
) -> None:
    _frag(repo, name, body)
    assert any(needle in e for e in cf.check(repo)), cf.check(repo)


def test_hand_bullet_under_unreleased_is_refused_but_prose_stays(repo: Path) -> None:
    cl = repo / "CHANGELOG.md"
    cl.write_text(
        _CL.replace(
            "## [Unreleased]\n",
            "## [Unreleased]\n\nPairs with engine-service-v0.1.9.\n",
        )
    )
    assert cf.check(repo) == []
    cl.write_text(
        _CL.replace(
            "## [Unreleased]\n", "## [Unreleased]\n\n### Added\n\n- hand entry\n"
        )
    )
    assert any(
        "CHANGELOG.md: [Unreleased] holds 1 hand-written" in e for e in cf.check(repo)
    )


def test_missing_readme_is_refused(repo: Path) -> None:
    (repo / "conexus/changelog/unreleased" / cf.DIR_README).unlink()
    assert any(
        "conexus/changelog/unreleased/README.md: missing" in e for e in cf.check(repo)
    )


def test_fold_groups_by_section_orders_by_first_commit_and_deletes(repo: Path) -> None:
    old = _frag(repo, "nexus-zzz.md", "### Added\n\n- added first\n")
    _git(repo, "add", "--", str(old))
    _git(repo, "commit", "-q", "-m", "a", when="2026-01-01T00:00:00Z")
    newer = _frag(
        repo, "nexus-aaa.md", "### Added\n\n- added second\n"
    )  # sorts first by name, last by time
    _git(repo, "add", "--", str(newer))
    _git(repo, "commit", "-q", "-m", "b", when="2026-02-01T00:00:00Z")
    _frag(
        repo, "nexus-fix.md", "### Fixed\n\n- a fix\n  wrapped\n"
    )  # untracked: sorts last in its section
    _frag(
        repo,
        "nexus-plug.md",
        "### Changed\n\n- plugin change\n",
        "conexus/changelog/unreleased",
    )
    (repo / "CHANGELOG.md").write_text(
        _CL.replace("## [Unreleased]\n", "## [Unreleased]\n\nPairs with x.\n")
    )

    assert cf.fold(repo) == []
    text = (repo / "CHANGELOG.md").read_text()
    s, e = cf.unreleased_body(text)
    assert text[s:e] == (
        "\nPairs with x.\n\n### Added\n\n- added first\n- added second\n\n### Fixed\n\n- a fix\n  wrapped\n\n"
    )
    assert text[e:].startswith("## [1.0.0]")
    assert (
        "### Changed\n\n- plugin change\n"
        in (repo / "conexus/CHANGELOG.md").read_text()
    )
    assert [p.name for p in (repo / "changelog/unreleased").iterdir()] == [
        cf.DIR_README
    ]
    # Folded bullets sit under [Unreleased] until the release step moves them to [X.Y.Z] in the
    # same commit, so check reads them as hand entries in between.
    assert any("hand-written" in x for x in cf.check(repo))


def test_fold_refuses_and_writes_nothing_on_a_bad_fragment(repo: Path) -> None:
    good = _frag(repo, "nexus-ok.md", "### Added\n\n- fine\n")
    _frag(repo, "nexus-bad.md", "### Nope\n\n- x\n")
    assert cf.fold(repo)
    assert good.exists()
    assert (repo / "CHANGELOG.md").read_text() == _CL


def test_main_exit_codes(repo: Path) -> None:
    assert cf.main(["--root", str(repo), "check"]) == cf.EXIT_OK
    _frag(repo, "bad.md", "x\n")
    assert cf.main(["--root", str(repo), "check"]) == cf.EXIT_BAD


@pytest.mark.lint
def test_repo_fragments_and_unreleased_are_clean() -> None:
    for frag_dir, changelog in cf.TARGETS:
        assert (_REPO / frag_dir / cf.DIR_README).is_file(), frag_dir
        assert cf.UNRELEASED in (_REPO / changelog).read_text(encoding="utf-8"), (
            changelog
        )
    assert cf.check(_REPO) == []
