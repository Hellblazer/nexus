# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_engine_cut_riders.py (nexus-ujbz8): ancestry before the tag, sizes after."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_engine_cut_riders as cr  # noqa: E402

SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"
_MIB = 1024 * 1024


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> dict[str, str]:
    """base -> rider -> tip on main, plus a side branch cut BEFORE the rider."""
    _git(tmp_path, "init", "-q", "-b", "main")
    shas: dict[str, str] = {}
    for name in ("base", "rider", "tip"):
        (tmp_path / "f.txt").write_text(name)
        _git(tmp_path, "add", "f.txt")
        _git(tmp_path, "commit", "-q", "-m", name)
        shas[name] = _git(tmp_path, "rev-parse", "HEAD")
        if name == "base":
            _git(tmp_path, "branch", "old")
    shas["path"] = str(tmp_path)
    return shas


def _riders(sha: str) -> tuple[tuple[str, str, str], ...]:
    return ((sha, "nexus-test", "a rider"),)


def test_a_commit_that_descends_from_the_rider_passes(repo: dict[str, str]) -> None:
    rc, lines = cr.check_ancestry(Path(repo["path"]), "main", _riders(repo["rider"]))
    assert rc == 0, lines
    assert lines[-1].startswith("PASSED")


def test_a_tag_commit_placed_before_the_rider_names_it_missing(repo: dict[str, str]) -> None:
    rc, lines = cr.check_ancestry(Path(repo["path"]), "old", _riders(repo["rider"]))
    assert rc == 1, lines
    assert any(line.startswith("MISSING") and repo["rider"] in line for line in lines)


def test_an_annotated_tag_is_peeled_to_its_commit(repo: dict[str, str]) -> None:
    _git(Path(repo["path"]), "tag", "-a", "engine-service-vtest", "-m", "t", repo["tip"])
    rc, _ = cr.check_ancestry(Path(repo["path"]), "engine-service-vtest", _riders(repo["rider"]))
    assert rc == 0


def test_a_rider_that_does_not_resolve_is_unverifiable_never_a_pass(repo: dict[str, str]) -> None:
    rc, lines = cr.check_ancestry(Path(repo["path"]), "main", _riders("0" * 40))
    assert rc == 2, lines
    assert any("UNRESOLVED" in line for line in lines)


def test_a_tag_commit_that_does_not_resolve_is_unverifiable(repo: dict[str, str]) -> None:
    rc, lines = cr.check_ancestry(Path(repo["path"]), "no-such-ref", _riders(repo["rider"]))
    assert rc == 2, lines


def test_an_empty_rider_list_is_unverifiable(repo: dict[str, str]) -> None:
    rc, _ = cr.check_ancestry(Path(repo["path"]), "main", ())
    assert rc == 2


@pytest.mark.lint
def test_the_shipped_riders_are_real_commits_of_this_repo() -> None:
    """The list is a record of commits that exist; a typo'd sha would make every cut UNVERIFIABLE.

    Lint-marked because it needs full history: CI's `test` job checks out at depth 1
    (nexus-dhs30), where a rider this old is not in the clone, and the `test-lint`
    job checks out with fetch-depth 0. The shallow assert is the non-vacuity guard:
    a lint run on a shallow clone FAILS, it never skip-passes.
    """
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert shallow != "true", (
        "this check needs full history; deepen the clone (git fetch --unshallow) "
        "rather than read a missing rider as a typo"
    )
    assert len(cr.RIDERS) >= 2
    for sha, bead, _what in cr.RIDERS:
        assert bead.startswith("nexus-")
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
        assert probe.returncode == 0, f"{sha} ({bead}) is not a commit in this clone"


def _assets(amd: float, arm: float, mac: float) -> list[dict]:
    return [
        {"name": "nexus-service-linux-amd64", "size": int(amd * _MIB)},
        {"name": "nexus-service-linux-arm64", "size": int(arm * _MIB)},
        {"name": "nexus-service-mac-arm64", "size": int(mac * _MIB)},
        {"name": "nexus-service-linux-amd64.sha256", "size": 100},
    ]


def test_the_fixed_sizes_pass() -> None:
    rc, lines = cr.check_sizes(_assets(150, 148, 154))
    assert rc == 0, lines


def test_the_v0_1_142_sizes_fail_on_all_three() -> None:
    rc, lines = cr.check_sizes(_assets(231.8, 227.0, 193.3))
    assert rc == 1
    assert sum(1 for line in lines if line.startswith("TOO BIG")) == 3


def test_one_regressed_binary_fails_alone() -> None:
    rc, lines = cr.check_sizes(_assets(150, 148, 193.3))
    assert rc == 1
    assert [line for line in lines if line.startswith("TOO BIG")][0].count("mac-arm64") == 1


def test_linux_arm64_has_no_looser_ceiling_than_amd64() -> None:
    """nexus-lhr6a never measured arm64; its ceiling follows amd64's logic, not a guess 40 MiB high.
    A 189 MiB arm64 binary (a partial regression, ~40 MiB over the ~147 expected) passed the old 190."""
    assert cr.SIZE_CEILINGS_MIB["nexus-service-linux-arm64"] <= cr.SIZE_CEILINGS_MIB["nexus-service-linux-amd64"]
    rc, lines = cr.check_sizes(_assets(150, 189, 154))
    assert rc == 1, lines
    assert [line for line in lines if line.startswith("TOO BIG")][0].count("linux-arm64") == 1


def test_a_missing_binary_is_a_failure_not_a_skip() -> None:
    assets = [a for a in _assets(150, 148, 154) if a["name"] != "nexus-service-mac-arm64"]
    rc, lines = cr.check_sizes(assets)
    assert rc == 1
    assert any(line.startswith("MISSING") for line in lines)


def test_a_zero_byte_asset_is_a_failure() -> None:
    assets = _assets(150, 148, 154)
    assets[0]["size"] = 0
    rc, _ = cr.check_sizes(assets)
    assert rc == 1


def test_main_reads_a_saved_release_and_overrides_a_ceiling(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    saved = tmp_path / "rel.json"
    saved.write_text(json.dumps({"assets": _assets(150, 148, 154)}))
    assert cr.main(["sizes", "engine-service-vX", "--assets-json", str(saved)]) == 0
    assert cr.main(["sizes", "engine-service-vX", "--assets-json", str(saved), "--max", "nexus-service-linux-amd64=100"]) == 1
    assert cr.main(["sizes", "engine-service-vX", "--assets-json", str(tmp_path / "absent.json")]) == 2
    capsys.readouterr()


def test_the_engine_release_skill_runs_both_subcommands() -> None:
    text = SKILL.read_text()
    assert "scripts/check_engine_cut_riders.py ancestry" in text
    assert "scripts/check_engine_cut_riders.py sizes" in text
