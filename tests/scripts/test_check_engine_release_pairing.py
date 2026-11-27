# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``scripts/check_engine_release_pairing.py`` (RDR-258 Phase 2 step 1, nexus-2jomg.3).

The engine release scans ``<previous engine-service-v* tag>..<the commit being tagged>`` with the
pairing check. Everything here runs on a real temporary git repository, never a mock of git's
output: the previous-tag computation (numeric order, rerun, out-of-order cut, ignored tags, no
previous tag, shallow checkout), the kill-control (an undeclared both-halves commit between two
engine tags fails, the same commit declared in the ledger passes), and the wiring in
``engine-service-release.yml`` (the step runs before anything is created or built, on a full
checkout, and nothing else in that workflow moved).
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
import yaml

import check_engine_release_pairing as erp
import check_wire_contract_pairing as wctp

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "engine-service-release.yml"
_ENGINE_FILE = "service/Engine.java"
_CLIENT_FILE = "src/nexus/db/http_fake_client.py"
_EMPTY_LEDGER = "## Unshipped\n\n(none)\n\n## Shipped\n"


def _git(repo: pathlib.Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ["PATH"],
    }
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, env=env).stdout.strip()


def _commit(repo: pathlib.Path, message: str, files: dict[str, str]) -> str:
    """Commit ``files`` (path to text) and return the sha."""
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _both_halves(repo: pathlib.Path, n: int) -> str:
    return _commit(repo, f"both halves {n}", {_ENGINE_FILE: f"class E{n} {{}}\n", _CLIENT_FILE: f"X = {n}\n"})


def _engine_only(repo: pathlib.Path, n: int) -> str:
    return _commit(repo, f"engine only {n}", {_ENGINE_FILE: f"class E{n} {{}}\n"})


def _tag(repo: pathlib.Path, name: str, rev: str = "HEAD") -> None:
    _git(repo, "tag", "-a", "-m", name, name, rev)


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """main with one commit per half (a single base commit adding both would itself be both-halves)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _commit(r, "base: engine", {_ENGINE_FILE: "class E {}\n"})
    _commit(r, "base: client", {_CLIENT_FILE: "X = 0\n"})
    return r


@pytest.fixture
def ledger(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "ledger.md"
    path.write_text(_EMPTY_LEDGER)
    return path


def _run(repo: pathlib.Path, ledger: pathlib.Path, *args: str) -> int:
    return erp.main([*args, "--repo-root", str(repo), "--ledger", str(ledger)])


# ── the previous-tag computation ─────────────────────────────────────────────

def test_the_previous_tag_is_the_highest_version_below_the_tag_by_number_not_by_text(repo: pathlib.Path) -> None:
    for name in ("engine-service-v0.1.2", "engine-service-v0.1.9", "engine-service-v0.1.10", "engine-service-v0.1.11"):
        _tag(repo, name)
    # Text order would put v0.1.9 above v0.1.10.
    assert erp.previous_engine_tag("engine-service-v0.1.11", repo) == "engine-service-v0.1.10"
    # A rerun for a tag that exists finds its predecessor, not itself.
    assert erp.previous_engine_tag("engine-service-v0.1.10", repo) == "engine-service-v0.1.9"
    # A cut for an older commit while newer tags exist (a backport) still finds the lower neighbour.
    assert erp.previous_engine_tag("engine-service-v0.1.9", repo) == "engine-service-v0.1.2"
    # A tag not yet created: the highest below it.
    assert erp.previous_engine_tag("engine-service-v0.1.12", repo) == "engine-service-v0.1.11"
    assert erp.previous_engine_tag("engine-service-v0.2.0", repo) == "engine-service-v0.1.11"


def test_tags_that_are_not_engine_versions_are_never_the_previous_tag(repo: pathlib.Path) -> None:
    _tag(repo, "engine-service-v0.1.1")
    for name in ("v9.9.9", "engine-service-v0.1.5-rc1", "engine-service-v0.1.x", "engine-service-v0.1"):
        _tag(repo, name)
    assert erp.previous_engine_tag("engine-service-v0.1.6", repo) == "engine-service-v0.1.1"


def test_no_previous_tag_cannot_be_verified(repo: pathlib.Path, ledger: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The first release in a repository, or a checkout with no tags: exit 2, never a pass."""
    _tag(repo, "engine-service-v0.1.1")
    with pytest.raises(erp.CannotVerify, match="no engine-service-v\\* tag below"):
        erp.previous_engine_tag("engine-service-v0.1.1", repo)
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.1") == 2
    assert "Cannot verify" in capsys.readouterr().err


def test_a_malformed_tag_cannot_be_verified(repo: pathlib.Path, ledger: pathlib.Path) -> None:
    _tag(repo, "engine-service-v0.1.1")
    for tag in ("v7.1.0", "engine-service-v0.1", "engine-service-v0.1.2-rc1", ""):
        assert _run(repo, ledger, "--tag", tag, "--commit", "HEAD") == 2


def test_an_unresolvable_commit_cannot_be_verified(repo: pathlib.Path, ledger: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    _tag(repo, "engine-service-v0.1.1")
    # The tag being cut does not exist and no --commit names its commit.
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2") == 2
    assert "pass --commit" in capsys.readouterr().err
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2", "--commit", "no-such-ref") == 2


def test_a_shallow_checkout_cannot_be_verified(tmp_path: pathlib.Path, repo: pathlib.Path, ledger: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A shallow boundary reads as a root commit: a scan would pass over history it never saw."""
    _tag(repo, "engine-service-v0.1.1")
    _both_halves(repo, 1)
    _tag(repo, "engine-service-v0.1.2")
    clone = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-single-branch", f"file://{repo}", str(clone)], check=True, capture_output=True)
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    assert _run(clone, ledger, "--tag", "engine-service-v0.1.2") == 2
    assert "shallow" in capsys.readouterr().err


# ── the kill-control: the pairing check, scoped to the engine range ──────────

def test_an_undeclared_both_halves_commit_between_two_engine_tags_fails_and_declaring_it_passes(
    repo: pathlib.Path, ledger: pathlib.Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _tag(repo, "engine-service-v0.1.1")
    _engine_only(repo, 1)
    paired = _both_halves(repo, 2)
    _tag(repo, "engine-service-v0.1.2")
    # Non-vacuity: the commit is the one the detector flags in exactly this range, so a pass below
    # is a pass over a flagged commit and not over an empty range.
    assert [c.sha for c in wctp.flagged_commits("engine-service-v0.1.1..engine-service-v0.1.2", repo_root=repo)] == [paired]

    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2") == 1
    err = capsys.readouterr().err
    assert "UNDECLARED" in err and paired in err

    ledger.write_text(
        "## Unshipped\n\n"
        f"- `{paired}` -- bead nexus-x -- engine tag `engine-service-v0.1.2` -- the pair [not-additive]\n\n"
        "## Shipped\n"
    )
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2") == 0
    out = capsys.readouterr().out
    assert "engine-service-v0.1.1.." in out and "1 both-halves commit(s)" in out


def test_the_same_check_before_the_tag_exists_uses_the_commit_it_is_given(repo: pathlib.Path, ledger: pathlib.Path) -> None:
    """The pre-tag run on the frozen TAG_SHA: no tag object yet, --commit names the commit."""
    _tag(repo, "engine-service-v0.1.1")
    paired = _both_halves(repo, 1)
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2", "--commit", paired) == 1


def test_a_both_halves_commit_before_the_previous_tag_is_not_this_releases(repo: pathlib.Path, ledger: pathlib.Path) -> None:
    _both_halves(repo, 1)
    _tag(repo, "engine-service-v0.1.1")
    _engine_only(repo, 2)
    _tag(repo, "engine-service-v0.1.2")
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2") == 0


def test_a_tag_on_a_commit_that_is_not_the_branch_tip_scans_only_up_to_that_commit(repo: pathlib.Path, ledger: pathlib.Path) -> None:
    """A cut from an older develop commit: a both-halves commit landed after it is the NEXT cut's."""
    _tag(repo, "engine-service-v0.1.1")
    _engine_only(repo, 1)
    cut = _git(repo, "rev-parse", "HEAD")
    later = _both_halves(repo, 2)
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2", "--commit", cut) == 0
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2", "--commit", later) == 1
    # The same through an annotated tag object, which is not the commit it points at.
    _tag(repo, "engine-service-v0.1.2", cut)
    assert _git(repo, "rev-parse", "engine-service-v0.1.2") != cut
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2") == 0


def test_a_cut_for_an_older_commit_while_a_newer_tag_exists_keeps_its_own_base(repo: pathlib.Path, ledger: pathlib.Path) -> None:
    """A backport tagged below the newest tag: its base is the lower neighbour, so a both-halves
    commit between that neighbour and the backport commit is scanned (a range from the NEWEST tag
    would be empty here and pass)."""
    _tag(repo, "engine-service-v0.1.1")
    _both_halves(repo, 1)
    backport = _git(repo, "rev-parse", "HEAD")
    _engine_only(repo, 2)
    _tag(repo, "engine-service-v0.1.3")
    assert _run(repo, ledger, "--tag", "engine-service-v0.1.2", "--commit", backport) == 1


# ── the workflow wiring ──────────────────────────────────────────────────────

def _create_release_steps() -> list[dict]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["create-release"]["steps"]


def test_the_release_workflow_runs_the_check_first_on_a_full_checkout() -> None:
    """It fails the release before anything publishes: create-release is the job both publish
    matrices need, and the check sits before its release-creating step."""
    steps = _create_release_steps()
    (pairing,) = [i for i, s in enumerate(steps) if "check_engine_release_pairing.py" in s.get("run", "")]
    (create,) = [i for i, s in enumerate(steps) if "gh release create" in s.get("run", "")]
    (checkout,) = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout < pairing < create
    # Tags and full history, or the range cannot be named (a shallow boundary reads as a root).
    assert steps[checkout]["with"]["fetch-depth"] == 0
    assert steps[pairing]["run"].strip() == 'python3 scripts/check_engine_release_pairing.py --tag "${GITHUB_REF_NAME}"'


def test_nothing_else_in_the_release_workflow_runs_the_check() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert text.count("check_engine_release_pairing.py") == 1
    jobs = yaml.safe_load(text)["jobs"]
    assert [n for n, j in jobs.items() if "check_engine_release_pairing.py" in yaml.safe_dump(j)] == ["create-release"]
    assert jobs["create-release"]["if"] == "startsWith(github.ref, 'refs/tags/engine-service-v')"


def test_the_engine_release_skill_names_the_check() -> None:
    skill = (_REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/check_engine_release_pairing.py" in skill
