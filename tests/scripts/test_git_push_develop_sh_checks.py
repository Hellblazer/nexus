# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh's check gate (nexus-igea9, Sam 2026-11-22), end to end on fixture repositories.

Five develop reds in one day came from a check that was not run, or was run on the working tree: a write-seam 422 on the
-r12 cut, the wire ledger on the revert 4402201ede, the cloud-leg routes on de7e88cbf4, the plugin ledger on c88f51e49c and a
size ceiling. The push now runs the checks its diff selects against the TIP's tree and refuses a red (PUSH_REFUSED_<CHECK>,
exit 14), a heavy leg with no proof, and a gate that cannot start (PUSH_REFUSED_CHECK_GATE_ERROR, exit 15).

What the fixtures are, per replay (the real history is not replayed, so a shallow clone runs these too):

* the size ceiling, the wire ledger and the changelog run the REAL scripts, copied into the fixture repository; each red is
  the condition itself (a 1001-line test file with no exceptions entry; a commit touching the engine tree and an ``http_*``
  client together with no ledger line);
* the cloud-leg routes and the plugin ledger are small STAND-IN test files carrying the failing condition of the real tests
  (an unmapped ledger route; a changed plugin file the pending ledger does not name), because the real ones need this
  repository's whole test tree; the gate's part, selecting them, running them against the tip and refusing, is the real code;
* the write-seam 422 is a behavioural failure no static check sees, so what is pinned is the gate's rule: a diff that reaches
  ci.yml's write-seam path filter is refused without a proof of a green run on the same tree.
The replays against the real commits (4402201ede, de7e88cbf4, c88f51e49c, 91715b4733) were run by hand and are in the bead.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REC_NAME = "_git_push_develop_record_helpers"
if _REC_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_REC_NAME, Path(__file__).with_name("test_git_push_develop_sh_record.py"))
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_REC_NAME] = _mod
    _spec.loader.exec_module(_mod)
_rec = sys.modules[_REC_NAME]

repos = _rec.repos  # fixtures, re-exposed
fake_nx = _rec.fake_nx
REPO_ROOT = _rec.REPO_ROOT
_git, _env, _calls, _records, _opt = _rec._git, _rec._env, _rec._calls, _rec._records, _rec._opt

REAL_SCRIPTS = ("file_size_ratchet.py", "changelog_fold.py", "check_wire_contract_pairing.py", "select_docs_reader_tests.py")
SCRIPT = REPO_ROOT / "scripts" / "git-push-develop.sh"
EMPTY_LEDGER = "## Unshipped\n\n(none)\n\n## Shipped\n"
UNMAPPED = "/v1/brand-new-route"

STUB_CLOUD_LEG = '''\
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAPPED = {"/v1/a", "/v1/b"}


@pytest.mark.parametrize("i", range(45))
def test_filler(i):
    assert i >= 0


def test_every_route_in_the_live_ledger_maps():
    text = (ROOT / "docs" / "wire-contract-pending.md").read_text()
    routes = set(re.findall(r"route (/\\S+)", text))
    assert routes <= MAPPED, f"unmapped ledger routes: {sorted(routes - MAPPED)}"
'''

STUB_PLUGIN_LEDGER = '''\
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("i", range(12))
def test_filler(i):
    assert i >= 0


def test_every_drifted_file_is_declared_in_the_ledger():
    drifted = subprocess.run(["git", "diff", "--name-only", "v1.0.0", "HEAD", "--", "conexus/"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    ledger = (ROOT / "conexus" / "PENDING_RELEASE.md").read_text()
    undeclared = [f for f in drifted if f != "conexus/PENDING_RELEASE.md" and f not in ledger]
    assert not undeclared, f"plugin files differ from the pinned tag and are not declared: {undeclared}"
'''

STUB_ALL_SKIPPED = '''\
import pytest


@pytest.mark.parametrize("i", range(45))
def test_filler(i):
    pytest.skip("a prerequisite is absent")
'''


def _commit_paths(work: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git("add", *files, cwd=work)
    _git("commit", "-q", "-m", message, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


@pytest.fixture()
def armed(repos) -> tuple[Path, Path]:
    """origin and work, both holding the check gate's scripts, an empty wire ledger, a pending-release ledger, stand-ins for
    the two pytest-based checks, and the twenty-odd docs-reading tests the docs-readers selector wants; tagged v1.0.0."""
    origin, work = repos
    files: dict[str, str] = {name: (REPO_ROOT / "scripts" / name).read_text() for name in REAL_SCRIPTS}
    files = {f"scripts/{name}": text for name, text in files.items()}
    files["docs/wire-contract-pending.md"] = EMPTY_LEDGER
    files["src/nexus/__init__.py"] = ""                       # the size ratchet refuses a tree with no source or no test files
    files["service/src/main/java/dev/nexus/service/Main.java"] = "class Main {}\n"
    files["CHANGELOG.md"] = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0]\n"
    files["changelog/unreleased/README.md"] = "Fragments: one per bead.\n"
    files["conexus/PENDING_RELEASE.md"] = "# Pending\n"
    files["conexus/skills/catalog/SKILL.md"] = "catalog\n"
    files["tests/scripts/test_cloud_leg.py"] = STUB_CLOUD_LEG
    files["tests/test_plugin_release_drift_ledger.py"] = STUB_PLUGIN_LEDGER
    for i in range(22):
        files[f"tests/test_reader_{i:02d}.py"] = 'from pathlib import Path\nREPO_ROOT = Path(__file__).parents[1]\n\ndef test_r():\n    assert (REPO_ROOT / "docs").is_dir()\n'
    _commit_paths(work, "arm the check gate", files)
    _git("tag", "v1.0.0", cwd=work)
    _git("push", "-q", "origin", "develop", cwd=work)
    return origin, work


def _push(work: Path, fake: tuple[Path, Path], *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    nx_dir, log = fake
    tmp = work.parent / "gate-tmp"
    tmp.mkdir(exist_ok=True)
    merged = {"NX_PUSH_CHECK_PYTHON": sys.executable, "TMPDIR": str(tmp), **(env or {})}
    return _rec._run(work, *argv, nx_dir=nx_dir, log=log, env=merged)


def _tip_tree(work: Path, sha: str) -> str:
    return _git("rev-parse", f"{sha}^{{tree}}", cwd=work)


def _worktrees(work: Path) -> list[str]:
    return [ln for ln in _git("worktree", "list", "--porcelain", cwd=work).splitlines() if ln.startswith("worktree ")]


def _tmp_left(work: Path) -> list[str]:
    return sorted(p.name for p in (work.parent / "gate-tmp").iterdir())


def _proof_file(path: Path, *, tree: str, command: list[str], summary: str, **over: object) -> Path:
    body = {"state": "passed", "exit_code": 0, "command": command, "tree": tree, "tree_dirty": False, "summary": summary, **over}
    path.write_text(json.dumps(body))
    return path


SEAM_COMMAND = ["uv", "run", "pytest", "-o", "addopts=", "-m", "integration", "tests/db/test_write_seam_gate_integration.py",
                "tests/db/test_http_combined_query_integration.py", "-v", "-o", "faulthandler_timeout=180"]
DOCS_COMMAND = ["uv", "run", "pytest", "-o", "addopts=", "-m", "not integration and not slow", "tests/scripts/test_cloud_leg.py",
                *[f"tests/test_reader_{i:02d}.py" for i in range(22)], "-v"]
SEAM_PATH = "service/src/main/java/dev/nexus/service/http/CatalogHandler.java"


# ── the happy path and what it leaves behind ─────────────────────────────────

def test_a_diff_that_selects_nothing_still_runs_the_always_on_checks_and_pushes(armed, fake_nx) -> None:
    origin, work = armed
    sha = _commit_paths(work, "note", {"README.md": "hello\n"})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == sha
    passed = [ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_CHECKS_PASSED")]
    assert len(passed) == 1 and "file-size(" in passed[0] and "changelog(" in passed[0], proc.stderr
    assert "wire-ledger" not in passed[0] and "cloud-leg" not in passed[0] and "skipped=none" in passed[0]


def test_the_tips_tree_is_removed_on_success_and_on_refusal(armed, fake_nx) -> None:
    _origin, work = armed
    before = _worktrees(work)
    ok = _push(work, fake_nx, _commit_paths(work, "note", {"README.md": "hello\n"}))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert _worktrees(work) == before and _tmp_left(work) == []
    bad = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    refused = _push(work, fake_nx, bad)
    assert refused.returncode == 14, refused.stdout + refused.stderr
    assert _worktrees(work) == before and _tmp_left(work) == [], "a refusal leaves no worktree entry and no temp directory"


def test_the_checks_read_the_tips_tree_never_the_working_tree(armed, fake_nx) -> None:
    """A clean working tree over a red tip is still refused; a dirty one over a clean tip is still pushed."""
    origin, work = armed
    sha = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    (work / "tests" / "test_big.py").write_text("x = 1\n")            # the working tree is fixed, the tip is not
    refused = _push(work, fake_nx, sha)
    assert refused.returncode == 14 and "PUSH_REFUSED_FILE_SIZE" in refused.stdout, refused.stdout + refused.stderr
    _git("checkout", "-q", "--", "tests/test_big.py", cwd=work)
    _git("reset", "-q", "--hard", "HEAD~1", cwd=work)
    clean = _commit_paths(work, "note", {"README.md": "hello\n"})
    (work / "tests" / "test_huge.py").write_text("x = 1\n" * 2000)    # untracked and big: not in the tip
    (work / "changelog").mkdir(exist_ok=True)
    (work / "changelog" / "unreleased").mkdir(exist_ok=True)
    (work / "changelog" / "unreleased" / "Bad Name.md").write_text("nonsense\n")
    ok = _push(work, fake_nx, clean)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == clean


def test_a_gate_stopped_mid_run_removes_the_tree_and_stops_the_checks(armed, tmp_path: Path) -> None:
    """TERM to the gate while a check runs: the check (and what it started) is killed, the tip's worktree entry and the
    temp directory are gone, and the exit is the gate-error code, never a pass."""
    _origin, work = armed
    marker = tmp_path / "stub-started"
    slow = "import os, pathlib, time\npathlib.Path(os.environ['GATE_STUB_MARKER']).write_text('up')\ntime.sleep(120)\n"
    sha = _commit_paths(work, "slow changelog check", {"scripts/changelog_fold.py": slow})
    tmp = tmp_path / "gate-term-tmp"
    tmp.mkdir()
    before = _worktrees(work)
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "scripts" / "push_check_gate.py"), "--repo", str(work), "--base", "refs/remotes/origin/develop",
         "--tip", sha, "--python", sys.executable],
        env={**os.environ, "TMPDIR": str(tmp), "GATE_STUB_MARKER": str(marker)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), "the slow check never started"
        assert _worktrees(work) != before, "the tip's tree was materialised while the check ran"
        proc.terminate()
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 15
    assert _worktrees(work) == before and list(tmp.iterdir()) == []


# ── the five replays of 2026-11-22 ───────────────────────────────────────────

def test_replay_size_ceiling_a_test_file_over_its_cap_is_refused_with_its_reproduction(armed, fake_nx) -> None:
    """91715b4733's class: a file past its ceiling. The real ratchet runs against the tip."""
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    sha = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_FILE_SIZE" in proc.stdout and "tests/test_big.py" in proc.stdout
    assert "reproduce: uv run python scripts/file_size_ratchet.py check" in proc.stdout
    assert "--skip-file-size '<reason>'" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before
    assert [c[:2] for c in _calls(fake_nx[1])] == [["ci", "check-hold"]], "refused before the lock and the record"


def test_replay_wire_ledger_a_both_halves_commit_with_no_ledger_line_is_refused(armed, fake_nx) -> None:
    """4402201ede's class: a commit touching the engine tree and a client http_* module together, undeclared."""
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    sha = _commit_paths(work, "both halves", {"service/src/main/java/A.java": "class A {}\n", "src/nexus/db/http_x_client.py": "X = 1\n"})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_WIRE_LEDGER" in proc.stdout and sha[:12] in proc.stdout.replace(sha, sha[:12])
    assert "reproduce: uv run python scripts/check_wire_contract_pairing.py" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before


def test_replay_wire_ledger_a_declared_pair_pushes_and_a_dirty_tree_declaration_does_not_count(armed, fake_nx) -> None:
    origin, work = armed
    pair = _commit_paths(work, "both halves", {"service/src/main/java/A.java": "class A {}\n", "src/nexus/db/http_x_client.py": "X = 1\n"})
    entry = f"## Unshipped\n\n- `{pair}` -- bead nexus-x -- engine tag `engine-service-v9.9.9` -- the pair [additive]\n\n## Shipped\n"
    (work / "docs" / "wire-contract-pending.md").write_text(entry)           # declared in the working tree only
    refused = _push(work, fake_nx, pair)
    assert refused.returncode == 14 and "PUSH_REFUSED_WIRE_LEDGER" in refused.stdout, refused.stdout + refused.stderr
    declared = _commit_paths(work, "declare it", {"docs/wire-contract-pending.md": entry})
    docs = _proof_file(work.parent / "docs.json", tree=_tip_tree(work, declared), command=DOCS_COMMAND, summary="700 passed in 99s")
    ok = _push(work, fake_nx, "--proof", str(docs), pair, declared)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == declared


def test_replay_cloud_leg_an_unmapped_ledger_route_is_refused(armed, fake_nx) -> None:
    """de7e88cbf4's class: a docs/ change (the wire ledger) the cloud-leg test cannot map. The stand-in test carries the condition."""
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    ledger = f"## Unshipped\n\n- route {UNMAPPED} is not in the leg's map\n\n## Shipped\n"
    sha = _commit_paths(work, "ledger", {"docs/wire-contract-pending.md": ledger})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_CLOUD_LEG" in proc.stdout and UNMAPPED in proc.stdout
    assert "reproduce: uv run pytest -o addopts= -m 'not integration and not slow' tests/scripts/test_cloud_leg.py" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before


def test_replay_plugin_ledger_a_changed_skill_not_in_the_pending_ledger_is_refused(armed, fake_nx) -> None:
    """c88f51e49c's class: plugin files that differ from the pinned tag and are not declared. The stand-in reads the tag."""
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    sha = _commit_paths(work, "skill text", {"conexus/skills/catalog/SKILL.md": "catalog, reworded\n"})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_PLUGIN_LEDGER" in proc.stdout and "conexus/skills/catalog/SKILL.md" in proc.stdout
    assert "PENDING_RELEASE.md" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before
    declared = _commit_paths(work, "declare", {"conexus/PENDING_RELEASE.md": "# Pending\n- conexus/skills/catalog/SKILL.md\n"})
    ok = _push(work, fake_nx, sha, declared)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_replay_write_seam_a_seam_diff_is_refused_without_a_proof_of_the_same_tree(armed, fake_nx, tmp_path: Path) -> None:
    """The -r12 cut's 422 came from the behavioural write-seam gate: no static check sees it, so the push needs a proof."""
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    sha = _commit_paths(work, "catalog handler", {SEAM_PATH: "class CatalogHandler {}\n"})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_WRITE_SEAM" in proc.stdout and SEAM_PATH in proc.stdout
    assert "watched_pytest.py --status seam.json" in proc.stdout and "--proof seam.json" in proc.stdout
    assert "--skip-write-seam '<reason>'" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before
    good = _proof_file(tmp_path / "seam.json", tree=_tip_tree(work, sha), command=SEAM_COMMAND, summary="8 passed in 80.1s")
    ok = _push(work, fake_nx, "--proof", str(good), sha)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == sha


@pytest.mark.parametrize(("why", "over"), [
    ("another tree", {"tree": "f" * 40}),
    ("a dirty checkout", {"tree_dirty": True}),
    ("a failed run", {"state": "failed", "exit_code": 1}),
    ("a skipped prerequisite", {"summary": "7 passed, 1 skipped in 3.0s"}),
    ("a status with no tree stamp", {"tree": None}),
])
def test_a_proof_that_does_not_prove_the_tip_is_refused_and_says_why(armed, fake_nx, tmp_path: Path, why: str, over: dict) -> None:
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    sha = _commit_paths(work, "catalog handler", {SEAM_PATH: "class CatalogHandler {}\n"})
    params = {"tree": _tip_tree(work, sha), "command": SEAM_COMMAND, "summary": "8 passed in 80.1s", **over}
    bad = _proof_file(tmp_path / "bad.json", **params)
    proc = _push(work, fake_nx, "--proof", str(bad), sha)
    assert proc.returncode == 14, f"{why}: {proc.stdout}{proc.stderr}"
    assert "PUSH_REFUSED_WRITE_SEAM" in proc.stdout and "rejected proof bad.json:" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before


def test_a_proof_for_the_wrong_leg_does_not_satisfy_this_one_and_an_unreadable_one_is_named(armed, fake_nx, tmp_path: Path) -> None:
    _origin, work = armed
    sha = _commit_paths(work, "catalog handler", {SEAM_PATH: "class CatalogHandler {}\n"})
    docs_proof = _proof_file(tmp_path / "docs.json", tree=_tip_tree(work, sha), command=DOCS_COMMAND, summary="700 passed in 9s")
    junk = tmp_path / "junk.json"
    junk.write_text("{not json")
    proc = _push(work, fake_nx, "--proof", str(docs_proof), "--proof", str(junk), sha)
    assert proc.returncode == 14 and "rejected proof docs.json: its command does not name" in proc.stdout
    assert "rejected proof junk.json: cannot read it as JSON" in proc.stdout


def test_a_docs_change_needs_the_docs_readers_proof_and_runs_the_cloud_leg_check(armed, fake_nx, tmp_path: Path) -> None:
    origin, work = armed
    sha = _commit_paths(work, "a doc", {"docs/notes.md": "words\n"})
    refused = _push(work, fake_nx, sha)
    assert refused.returncode == 14, refused.stdout + refused.stderr
    assert "PUSH_REFUSED_DOCS_READERS" in proc_text(refused) and "PUSH_REFUSED_CLOUD_LEG" not in refused.stdout, "the cloud-leg stand-in passes"
    assert "tests/test_reader_00.py" in refused.stdout and "--proof docs.json" in refused.stdout
    proof = _proof_file(tmp_path / "docs.json", tree=_tip_tree(work, sha), command=DOCS_COMMAND, summary="700 passed in 99s")
    ok = _push(work, fake_nx, "--proof", str(proof), sha)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "cloud-leg(" in ok.stderr and "docs-readers(proof)" in ok.stderr


def proc_text(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


# ── non-vacuity: missing and vacuous checks refuse ───────────────────────────

def test_a_selected_check_whose_script_is_missing_from_the_tip_is_refused_not_skipped(armed, fake_nx) -> None:
    _origin, work = armed
    _git("rm", "-q", "scripts/check_wire_contract_pairing.py", cwd=work)
    _git("commit", "-q", "-m", "drop the wire check", cwd=work)
    sha = _commit_paths(work, "engine", {"service/src/main/java/A.java": "class A {}\n"})
    proc = _push(work, fake_nx, _git("rev-parse", "HEAD~1", cwd=work), sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_WIRE_LEDGER" in proc.stdout and "scripts/check_wire_contract_pairing.py" in proc.stdout
    assert "a selected check that is missing is a refusal, not a skip" in proc.stdout


def test_an_always_on_script_deleted_by_the_push_is_a_refusal_because_the_remote_arms_the_gate(armed, fake_nx) -> None:
    _origin, work = armed
    _git("rm", "-q", "scripts/changelog_fold.py", cwd=work)
    _git("commit", "-q", "-m", "drop it", cwd=work)
    proc = _push(work, fake_nx, _git("rev-parse", "HEAD", cwd=work))
    assert proc.returncode == 14 and "PUSH_REFUSED_CHANGELOG" in proc.stdout, proc.stdout + proc.stderr


def test_a_check_that_passes_without_examining_anything_is_refused_as_vacuous(armed, fake_nx) -> None:
    _origin, work = armed
    stub = "import sys\nsys.exit(0)\n"                                       # exits 0, prints nothing
    sha = _commit_paths(work, "hollow the changelog check", {"scripts/changelog_fold.py": stub})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_CHANGELOG" in proc.stdout and "vacuous" in proc.stdout


def test_a_pytest_check_whose_tests_all_skip_is_refused_as_vacuous(armed, fake_nx) -> None:
    _origin, work = armed
    sha = _commit_paths(work, "all skipped", {"tests/scripts/test_cloud_leg.py": STUB_ALL_SKIPPED, "docs/notes.md": "x\n"})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_CLOUD_LEG" in proc.stdout and "only 0 test(s) passed" in proc.stdout


# ── overrides ────────────────────────────────────────────────────────────────

def test_a_skip_overrides_one_check_and_its_reason_is_in_the_summary_and_the_record(armed, fake_nx) -> None:
    origin, work = armed
    sha = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    proc = _push(work, fake_nx, "--skip-file-size", "split lands in the next push, nexus-xyz", sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert "PUSH_CHECK_SKIPPED file-size reason=split lands in the next push, nexus-xyz" in lines
    assert lines.index("PUSH_CHECK_SKIPPED file-size reason=split lands in the next push, nexus-xyz") < next(
        i for i, ln in enumerate(lines) if ln.startswith("PUSH_OK"))
    (record,) = _records(fake_nx[1])
    assert _opt(record, "--skip") == ["file-size=split lands in the next push, nexus-xyz"]
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == sha


def test_a_skip_does_not_cover_another_red_check(armed, fake_nx) -> None:
    _origin, work = armed
    sha = _commit_paths(work, "both", {"tests/test_big.py": "x = 1\n" * 1001, "service/src/main/java/A.java": "class A {}\n",
                                       "src/nexus/db/http_x_client.py": "X = 1\n"})
    proc = _push(work, fake_nx, "--skip-file-size", "why", sha)
    assert proc.returncode == 14 and "PUSH_REFUSED_WIRE_LEDGER" in proc.stdout and "PUSH_REFUSED_FILE_SIZE" not in proc.stdout


def test_a_skip_of_a_check_nothing_selected_is_not_written_into_the_record(armed, fake_nx) -> None:
    _origin, work = armed
    proc = _push(work, fake_nx, "--skip-write-seam", "not needed", _commit_paths(work, "note", {"README.md": "hi\n"}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PUSH_CHECK_SKIPPED" not in proc.stdout
    (record,) = _records(fake_nx[1])
    assert "--skip" not in record


def test_a_blank_or_unknown_skip_flag_is_a_usage_error_before_anything_is_fetched(armed, fake_nx) -> None:
    _origin, work = armed
    sha = _commit_paths(work, "note", {"README.md": "hi\n"})
    assert _push(work, fake_nx, "--skip-nonsense", "why", sha).returncode == 64
    assert _push(work, fake_nx, "--skip-file-size").returncode == 64
    assert _push(work, fake_nx, "--proof", "/no/such/file", sha).returncode == 64


def test_the_skip_flag_has_an_environment_twin(armed, fake_nx) -> None:
    _origin, work = armed
    sha = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    proc = _push(work, fake_nx, sha, env={"NX_PUSH_SKIP_FILE_SIZE": "env twin"})
    assert proc.returncode == 0 and "PUSH_CHECK_SKIPPED file-size reason=env twin" in proc.stdout


# ── a gate that cannot start refuses ─────────────────────────────────────────

def test_no_usable_python_is_a_gate_error_with_the_skip_escape_never_a_pass(armed, fake_nx) -> None:
    origin, work = armed
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    sha = _commit_paths(work, "note", {"README.md": "hi\n"})
    proc = _push(work, fake_nx, sha, env={"NX_PUSH_CHECK_PYTHON": "/no/such/python"})
    assert proc.returncode == 15, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_CHECK_GATE_ERROR" in proc.stdout and "--skip-check-gate '<reason>'" in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before
    skipped = _push(work, fake_nx, "--skip-check-gate", "no python on this host", sha, env={"NX_PUSH_CHECK_PYTHON": "/no/such/python"})
    assert skipped.returncode == 0, skipped.stdout + skipped.stderr
    assert "PUSH_CHECK_SKIPPED check-gate reason=no python on this host" in skipped.stdout
    (record,) = _records(fake_nx[1])
    assert _opt(record, "--skip") == ["check-gate=no python on this host"]


def test_a_temp_directory_that_cannot_be_made_is_a_gate_error(armed, fake_nx) -> None:
    _origin, work = armed
    proc = _push(work, fake_nx, _commit_paths(work, "note", {"README.md": "hi\n"}), env={"TMPDIR": "/no/such/tmp"})
    assert proc.returncode == 15 and "PUSH_REFUSED_CHECK_GATE_ERROR" in proc.stdout and "no temp directory" in proc.stdout


def test_a_pytest_check_with_no_pytest_in_the_interpreter_and_no_uv_is_a_gate_error(armed, fake_nx, tmp_path: Path) -> None:
    _origin, work = armed
    bare = tmp_path / "bare-python"                                      # a python that cannot import pytest
    bare.write_text('#!/bin/sh\nif [ "$1" = "-c" ] && echo "$2" | grep -q "import pytest"; then exit 1; fi\nexec ' + sys.executable + ' "$@"\n')
    bare.chmod(0o755)
    sha = _commit_paths(work, "a doc", {"docs/notes.md": "words\n"})
    no_uv = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "uv").exists())
    proc = _push(work, fake_nx, sha, env={"NX_PUSH_CHECK_PYTHON": str(bare), "PATH": f"{fake_nx[0]}{os.pathsep}{no_uv}"})
    assert proc.returncode == 15, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_CHECK_GATE_ERROR" in proc.stdout and "no pytest" in proc.stdout


# ── where it sits ────────────────────────────────────────────────────────────

def test_a_remote_branch_with_no_gate_script_is_not_armed_and_says_so(repos, fake_nx) -> None:
    origin, work = repos
    sha = _commit_paths(work, "note", {"note.txt": "hi\n"})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PUSH_CHECKS_SKIPPED reason=the remote branch has no scripts/file_size_ratchet.py" in proc.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == sha


def test_the_gate_runs_after_the_hold_and_before_the_lock(armed, fake_nx) -> None:
    """A red check refuses before the lock is claimed; the hold's nx call is made first (a held push never pays for the checks)."""
    _origin, work = armed
    sha = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 14
    kinds = [c[:2] for c in _calls(fake_nx[1])]
    assert ["ci", "check-hold"] in kinds and ["tuple", "in"] not in kinds and ["tuple", "out"] not in kinds
    held = _push(work, fake_nx, sha, env={"FAKE_NX_GATE_EXIT": "13", "FAKE_NX_GATE_OUT": "PUSH_REFUSED_DEVELOP_RED held"})
    assert held.returncode == 13 and "PUSH_REFUSED_FILE_SIZE" not in held.stdout, "the hold answers first; the checks never ran"


def test_the_scope_audit_and_the_scrub_gate_still_come_first(armed, fake_nx) -> None:
    _origin, work = armed
    sha = _commit_paths(work, "big", {"tests/test_big.py": "x = 1\n" * 1001})
    scope = _push(work, fake_nx, sha, env={"NX_PUSH_SKIP_SCOPE_AUDIT": "", "NX_PUSH_ALLOWED_PATHS": "docs/"})
    assert scope.returncode == 7 and "PUSH_REFUSED_FILE_SIZE" not in scope.stdout


def test_the_light_path_is_fast(armed, fake_nx) -> None:
    """The always-on checks and the wire pairing on a small fixture: seconds. (The measurement on the real repository is in the bead.)"""
    _origin, work = armed
    sha = _commit_paths(work, "engine only", {"service/src/main/java/A.java": "class A {}\n"})
    started = time.monotonic()
    proc = _push(work, fake_nx, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert time.monotonic() - started < 30
    assert "wire-ledger(" in proc.stderr


def test_the_script_documents_the_new_flags_and_exit_codes() -> None:
    text = SCRIPT.read_text()
    for token in ("--proof", "--skip-<check>", "PUSH_REFUSED_CHECK_GATE_ERROR", "#  14  PUSH_REFUSED_<CHECK>", "#  15  PUSH_REFUSED_CHECK_GATE_ERROR"):
        assert token in text, token
    assert shutil.which("bash")
