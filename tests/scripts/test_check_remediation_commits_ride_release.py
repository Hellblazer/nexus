# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_remediation_commits_ride_release.py`` (nexus-fix9t).

Root cause this closes: nexus-3n7pr's remediation was explicitly sequenced
"after the client release ships"; 7.7.0 shipped, but the manifest_backfill
safety fixes it implicitly depended on (commit 5f59ede70, nexus-gvmbo /
nexus-b91tv) were NOT an ancestor of v7.7.0 -- so the installed nx carried
the pre-fix destructive module. Nothing checked this at release time. This
gate makes it mechanical: scan every non-closed bead for a required-commit
marker (structured or free-text), assert each named sha is an ancestor of
the release ref being cut.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml`` (same as the sibling ``check_engine_release_floor``
tests), so the gate module imports directly with no ``sys.path`` hack.

Every test here builds its own throwaway git repo and/or a synthetic
``bd export``-shaped JSONL fixture in ``tmp_path`` -- no live ``bd``
dependency, no dependency on this checkout's own bead state.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import check_remediation_commits_ride_release as gate


# ── Synthetic git repo helper ────────────────────────────────────────────
#
# Mirrors the inline `-c user.email=... -c user.name=...` idiom already used
# elsewhere in this suite (tests/test_routing_no_direct_push_to_main.py) so
# these tests don't depend on the runner having a global git identity
# configured.


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@dataclass(frozen=True)
class _Repo:
    """A throwaway git repo plus the shas its fixture commits produced.

    ``pathlib.Path`` does not support arbitrary attribute assignment, so
    the commit shas ride alongside the path in this small wrapper rather
    than being bolted onto the ``Path`` object itself.
    """

    path: Path
    sha_a: str
    sha_b: str
    sha_c: str

    def __fspath__(self) -> str:  # lets `_Repo` stand in wherever a path is expected
        return str(self.path)


@pytest.fixture
def repo(tmp_path: Path) -> _Repo:
    """A tiny two-branch git repo: a ``main`` line and an unmerged sibling.

    ``main`` carries commit A (the "required" commit for the green cases)
    followed by commit B, so A is an ancestor of ``main``. A second commit
    C is made on an orphan branch that is never merged anywhere -- C is
    NOT an ancestor of ``main``, the fixture for the red / non-ancestor
    case.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    sha_a = _commit(root, "a.txt", "a", "commit A (the required fix)")
    sha_b = _commit(root, "b.txt", "b", "commit B (advances main past A)")
    _git(root, "checkout", "-q", "--orphan", "unmerged")
    _git(root, "reset", "-q", "--hard")
    sha_c = _commit(root, "c.txt", "c", "commit C (never merged into main)")
    _git(root, "checkout", "-q", "main")
    return _Repo(path=root, sha_a=sha_a, sha_b=sha_b, sha_c=sha_c)


def _bead(
    bead_id: str,
    title: str = "",
    status: str = "open",
    description: str = "",
    notes: str = "",
    design: str = "",
    acceptance_criteria: str = "",
    comments: list[dict] | None = None,
) -> dict:
    return {
        "id": bead_id,
        "title": title or bead_id,
        "status": status,
        "description": description,
        "notes": notes,
        "design": design,
        "acceptance_criteria": acceptance_criteria,
        "comments": comments or [],
    }


def _write_export(tmp_path: Path, beads: list[dict]) -> Path:
    path = tmp_path / "beads_export.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for bead in beads:
            f.write(json.dumps(bead) + "\n")
    return path


# ── Marker / free-text extraction ────────────────────────────────────────


def test_structured_marker_is_parsed() -> None:
    bead = _bead("nexus-aaa", description="Sequencing note.\nrequires-commit: 5f59ede70\n")
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].sha == "5f59ede70"
    assert reqs[0].source == "structured-marker"
    assert reqs[0].locus == "description"


def test_free_text_requires_commit_is_parsed() -> None:
    bead = _bead("nexus-bbb", description="This remediation requires commit abc1234 before it can run safely.")
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].sha == "abc1234"
    assert reqs[0].source == "free-text:requires-commit"


def test_free_text_must_include_is_parsed() -> None:
    bead = _bead("nexus-ccc", description="Any release cutting this must include 89abcde or it is unsafe.")
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].sha == "89abcde"
    assert reqs[0].source == "free-text:must-include"


def test_marker_found_in_comments_not_just_description() -> None:
    bead = _bead(
        "nexus-ddd",
        description="No marker here.",
        comments=[{"id": "c1", "text": "Update:\nrequires-commit: 1234567"}],
    )
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].sha == "1234567"
    assert reqs[0].locus == "comment:c1"


def test_duplicate_sha_across_loci_is_deduplicated() -> None:
    bead = _bead(
        "nexus-eee",
        description="requires-commit: 1234567",
        comments=[{"id": "c1", "text": "requires-commit: 1234567"}, {"id": "c2", "text": "requires commit 1234567"}],
    )
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1


def test_design_field_is_scanned() -> None:
    bead = _bead("nexus-fff", design="Approach: land the fix first.\nrequires-commit: 89abcde")
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].locus == "design"


def test_acceptance_criteria_field_is_scanned() -> None:
    bead = _bead("nexus-ggg", acceptance_criteria="Done when this ships.\nrequires-commit: 89abcde")
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].locus == "acceptance_criteria"


# ── Dual bead-JSON-shape support (nexus-fix9t code-review round) ─────────
#
# Two distinct shapes exist in this repo's tooling: `bd export`'s per-issue
# dict (comments inlined, per bd 1.0.5's own --help) and the git-tracked
# `.beads/issues.jsonl` "beads-native" snapshot (no `comments` field at
# all). The parser is duck-typed via dict.get and needs no format
# detection -- these tests pin that both real shapes actually work.


def test_bd_export_shape_with_comments_is_parsed() -> None:
    """Mirrors `bd export`'s actual per-issue dict shape (verified against
    a live `bd export` during development): comments inlined as a list of
    {id, text}, plus design/acceptance_criteria present on ~9% of issues."""
    bead = {
        "_type": "issue",
        "id": "nexus-hhh",
        "title": "bd-export shaped bead",
        "description": "See comments for sequencing.",
        "status": "open",
        "priority": 2,
        "issue_type": "task",
        "owner": "test@test",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "comments": [{"id": "c1", "text": "requires-commit: fedcba9"}],
    }
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].sha == "fedcba9"
    assert reqs[0].locus == "comment:c1"


def test_beads_native_tracked_jsonl_shape_without_comments_is_parsed() -> None:
    """Mirrors the git-tracked `.beads/issues.jsonl` shape (verified
    against the real file: no `comments` field at all, but `design` /
    `acceptance_criteria` are present). The absence of `comments` must not
    raise -- it is simply nothing to scan, not a malformed input."""
    bead = {
        "id": "nexus-iii",
        "title": "beads-native shaped bead",
        "description": "",
        "status": "open",
        "priority": 2,
        "issue_type": "task",
        "owner": "test@test",
        "created_at": "2026-08-01T00:00:00Z",
        "created_by": "Hellblazer",
        "updated_at": "2026-08-01T00:00:00Z",
        "design": "requires-commit: fedcba9",
        # deliberately no "comments" key at all
    }
    assert "comments" not in bead
    reqs = gate.extract_bead_requirements(bead)
    assert len(reqs) == 1
    assert reqs[0].sha == "fedcba9"
    assert reqs[0].locus == "design"


# ── Ancestor checking (green / red) ──────────────────────────────────────


def test_ancestor_commit_passes(repo: _Repo) -> None:
    reqs = [gate.Requirement("nexus-x", "x", repo.sha_a, "structured-marker", "description")]
    rc = gate.check_requirements(reqs, "main", repo)
    assert rc == 0


def test_non_ancestor_commit_fails_and_names_the_bead(capsys: pytest.CaptureFixture[str], repo: _Repo) -> None:
    reqs = [gate.Requirement("nexus-y", "the incident bead", repo.sha_c, "structured-marker", "description")]
    rc = gate.check_requirements(reqs, "main", repo)
    assert rc == 1
    err = capsys.readouterr().err
    assert "nexus-y" in err
    assert repo.sha_c in err
    assert "not an ancestor" in err.lower() or "NOT" in err
    assert "Remedy" in err


def test_unresolvable_sha_is_treated_as_a_failure(capsys: pytest.CaptureFixture[str], repo: _Repo) -> None:
    reqs = [gate.Requirement("nexus-z", "z", "0000000", "structured-marker", "description")]
    rc = gate.check_requirements(reqs, "main", repo)
    assert rc == 1
    assert "could not resolve" in capsys.readouterr().err.lower()


# ── Closed beads ignored ─────────────────────────────────────────────────


def test_closed_bead_is_not_scanned(tmp_path: Path) -> None:
    beads = [_bead("nexus-closed", status="closed", description="requires-commit: 1234567")]
    reqs = gate.scan_beads(beads)
    assert reqs == []


def test_open_bead_alongside_closed_bead_is_still_scanned(tmp_path: Path) -> None:
    beads = [
        _bead("nexus-closed", status="closed", description="requires-commit: 1234567"),
        _bead("nexus-open", status="open", description="requires-commit: 89abcde"),
    ]
    reqs = gate.scan_beads(beads)
    assert {r.bead_id for r in reqs} == {"nexus-open"}


def test_deferred_and_blocked_beads_are_scanned_like_open() -> None:
    """Only the literal ``closed`` status is excluded (critique T2 [22635]):
    a deferred or blocked remediation bead still names a commit that must
    ride the release, so both are scanned."""
    beads = [
        _bead("nexus-deferred", status="deferred", description="requires-commit: 1234567"),
        _bead("nexus-blocked", status="blocked", description="requires-commit: 89abcde"),
        _bead("nexus-closed", status="closed", description="requires-commit: fedcba9"),
    ]
    reqs = gate.scan_beads(beads)
    assert {r.bead_id for r in reqs} == {"nexus-deferred", "nexus-blocked"}


# ── Non-vacuity: zero beads / zero requirements ──────────────────────────


def test_no_requirements_is_green_with_explicit_zero_count(
    capsys: pytest.CaptureFixture[str], repo: _Repo
) -> None:
    rc = gate.check_requirements([], "main", repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 remediation beads scanned" in out


def test_no_requirements_with_require_at_least_fails_closed(
    capsys: pytest.CaptureFixture[str], repo: _Repo
) -> None:
    rc = gate.check_requirements([], "main", repo, require_at_least=1)
    assert rc == 2
    err = capsys.readouterr().err
    assert "UNVERIFIABLE" in err
    assert "require-at-least" in err


def test_require_at_least_satisfied_by_a_real_finding_passes(repo: _Repo) -> None:
    reqs = [gate.Requirement("nexus-x", "x", repo.sha_a, "structured-marker", "description")]
    rc = gate.check_requirements(reqs, "main", repo, require_at_least=1)
    assert rc == 0


# ── End-to-end self-test: synthetic repo + synthetic bd-export JSONL ─────
#
# No live `bd` dependency (nexus-fix9t's explicit requirement): `main()` is
# driven entirely via --bd-export-json against a hand-built fixture and
# --repo-root against the throwaway git repo above.


def test_end_to_end_green_when_release_ref_carries_the_commit(tmp_path: Path, repo: _Repo) -> None:
    beads = [_bead("nexus-req1", description=f"requires-commit: {repo.sha_a}")]
    export_path = _write_export(tmp_path, beads)
    rc = gate.main(
        ["--release-ref", "main", "--repo-root", str(repo.path), "--bd-export-json", str(export_path)]
    )
    assert rc == 0


def test_end_to_end_red_when_release_ref_lacks_the_commit(
    tmp_path: Path, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    beads = [_bead("nexus-req2", title="the incident bead", description=f"requires-commit: {repo.sha_c}")]
    export_path = _write_export(tmp_path, beads)
    rc = gate.main(
        ["--release-ref", "main", "--repo-root", str(repo.path), "--bd-export-json", str(export_path)]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "nexus-req2" in err
    assert repo.sha_c in err


def test_end_to_end_free_text_form_also_gates_correctly(tmp_path: Path, repo: _Repo) -> None:
    beads = [_bead("nexus-req3", description=f"This must include {repo.sha_c} before cutting.")]
    export_path = _write_export(tmp_path, beads)
    rc = gate.main(
        ["--release-ref", "main", "--repo-root", str(repo.path), "--bd-export-json", str(export_path)]
    )
    assert rc == 1


def test_end_to_end_beads_with_no_markers_is_a_green_non_vacuous_run(
    tmp_path: Path, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """A NON-EMPTY export where nothing carries a marker is a legitimate
    green -- the common case for most releases. Distinct from the
    zero-beads-parsed case below, which is always a hard failure."""
    beads = [_bead("nexus-plain", description="No sequencing note here.")]
    export_path = _write_export(tmp_path, beads)
    rc = gate.main(
        ["--release-ref", "main", "--repo-root", str(repo.path), "--bd-export-json", str(export_path)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 bead(s) parsed" in out
    assert "0 requirement(s) found" in out
    assert "0 remediation beads scanned" in out  # check_requirements' own vacuous-green message


def test_end_to_end_zero_beads_parsed_is_always_a_hard_failure(
    tmp_path: Path, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """Zero TOTAL beads parsed (e.g. a stale/empty/wrong export) is NEVER a
    silent pass, with no flag required -- distinct from "beads were parsed
    but none carry a marker", which stays a legitimate green above."""
    export_path = _write_export(tmp_path, [])
    rc = gate.main(
        ["--release-ref", "main", "--repo-root", str(repo.path), "--bd-export-json", str(export_path)]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "UNVERIFIABLE" in err
    assert "0 beads parsed" in err


def test_run_gate_prints_pipeline_summary_before_verdict(repo: _Repo, capsys: pytest.CaptureFixture[str]) -> None:
    beads = [
        _bead("nexus-closed", status="closed", description="requires-commit: 1234567"),
        _bead("nexus-open", status="open", description=f"requires-commit: {repo.sha_a}"),
    ]
    rc = gate.run_gate(beads, "main", repo.path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 bead(s) parsed, 1 beads scanned (non-closed), 1 requirement(s) found" in out


def test_help_exits_cleanly_without_touching_bd_or_git() -> None:
    with pytest.raises(SystemExit) as exc_info:
        gate.main(["--help"])
    assert exc_info.value.code == 0


# ── bd export unavailable (fail-closed) ──────────────────────────────────


def test_bd_export_unavailable_is_unverifiable_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, repo: _Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "run_bd_export", lambda repo_root: gate._EXPORT_UNAVAILABLE)
    rc = gate.main(["--release-ref", "main", "--repo-root", str(repo.path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "UNVERIFIABLE" in err
    assert "bd export" in err
