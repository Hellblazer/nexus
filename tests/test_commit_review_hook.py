# SPDX-License-Identifier: AGPL-3.0-or-later
"""The post-commit review hook, proven in a SANDBOXED throwaway repo (nexus-jh86x).

Every test here installs into a ``tmp_path`` repository's own hooks
directory. Nothing touches this checkout's ``.git/hooks`` -- which is both
the bead's own acceptance wording ("a commit in a sandboxed checkout") and
a hard constraint from the shared-checkout coordination on 2026-09-02:
hooks live in the COMMON git dir, so an install here would arm every
worktree and every sibling session's commits.

The end-to-end legs run a REAL ``git commit`` through the REAL installed
hook script. They pin the two properties a per-commit hook must have --
it does not block, and it honours its opt-out -- at the shell layer that
actually runs, not one layer below it.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.hooks import _REVIEW_STANZA, _STANZA, _install_hook, _stanza_for, hook_stanza_state
from nexus.commit_review import pop_review_queue, review_queue_path

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=full_env
    )


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    return repo


# ── stanza shape ──────────────────────────────────────────────────────────────


def test_post_commit_carries_the_review_stanza() -> None:
    body = _stanza_for("post-commit")
    assert "nx review commit" in body
    assert "NX_COMMIT_REVIEW" in body


@pytest.mark.parametrize("hook_name", ["post-merge", "post-rewrite"])
def test_other_hooks_do_not_review(hook_name: str) -> None:
    """A rebase of twenty commits must not dispatch twenty reviews."""
    body = _stanza_for(hook_name)
    assert "nx review commit" not in body
    assert body == _STANZA, "non-post-commit hooks keep the indexing stanza byte for byte"


def test_the_review_stanza_lives_inside_the_sentinel_block() -> None:
    """Otherwise `nx hooks uninstall` would leave the reviewer behind."""
    body = _stanza_for("post-commit")
    begin = body.index(SENTINEL_BEGIN)
    end = body.index(SENTINEL_END)
    review = body.index("nx review commit")
    assert begin < review < end


def test_review_precedes_both_indexing_exit_guards() -> None:
    """The review must not sit behind either `exit 0` (2 Critical, a461db0b7).

    This replaces an earlier test that asserted the OPPOSITE order on a
    rationale that was simply wrong -- both dispatches are backgrounded and
    disowned, so running the review first delays the indexer by nothing.
    What the old order did cost was correctness: the indexing stanza's
    linked-worktree guard and its "indexer already running" guard both
    `exit 0` above the indexer's own dispatch, so anything appended after
    them inherited both exits and was silently skipped.
    """
    body = _stanza_for("post-commit")
    review_at = body.index("nx review commit")
    for guard in ("LINKED-WORKTREE GUARD", "pgrep -f \"nx index repo"):
        assert review_at < body.index(guard), (
            f"the review stanza sits after the {guard!r} guard and will be "
            "silently skipped whenever that guard fires"
        )


def test_stanza_anchor_exists() -> None:
    """_stanza_for's insertion anchor must stay present in _STANZA.

    If the REPO_TOP line is ever reworded, _stanza_for raises rather than
    falling back to appending at the end -- which is exactly the placement
    that produced the two Critical silent-skip defects.
    """
    assert 'REPO_TOP="$(git rev-parse --show-toplevel)"\n' in _STANZA


def test_the_review_dispatch_is_detached() -> None:
    """A synchronous dispatch would add its whole latency to every commit."""
    assert _REVIEW_STANZA.rstrip().endswith("fi\nfi")
    assert "disown" in _REVIEW_STANZA


# ── install / uninstall through the real CLI, into a sandbox ──────────────────


def test_install_writes_the_reviewer_only_into_post_commit(sandbox_repo: Path) -> None:
    result = CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    assert result.exit_code == 0, result.output

    hooks = sandbox_repo / ".git" / "hooks"
    assert "nx review commit" in (hooks / "post-commit").read_text()
    assert "nx review commit" not in (hooks / "post-merge").read_text()
    assert "nx review commit" not in (hooks / "post-rewrite").read_text()


def test_uninstall_removes_the_reviewer_with_the_rest(sandbox_repo: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["hooks", "install", str(sandbox_repo)])
    result = runner.invoke(main, ["hooks", "uninstall", str(sandbox_repo)])
    assert result.exit_code == 0, result.output

    post_commit = sandbox_repo / ".git" / "hooks" / "post-commit"
    if post_commit.exists():
        assert "nx review commit" not in post_commit.read_text()


def test_the_installed_hook_is_valid_shell(sandbox_repo: Path) -> None:
    """A syntax error here fails every commit in the repo, silently at
    install time and loudly at the worst possible moment."""
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    hook = sandbox_repo / ".git" / "hooks" / "post-commit"
    proc = subprocess.run(["sh", "-n", str(hook)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ── end to end: a real commit through the real hook ───────────────────────────


def test_a_real_commit_is_not_blocked_by_the_hook(sandbox_repo: Path) -> None:
    """The bead's second acceptance criterion, at the shell layer.

    ``NX_COMMIT_REVIEW=0`` keeps this test free and offline; the
    non-blocking property under test is the hook's, not the reviewer's.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    (sandbox_repo / "b.txt").write_text("b\n")
    _git(sandbox_repo, "add", "b.txt")
    proc = _git(sandbox_repo, "commit", "-m", "feat: b", env={"NX_COMMIT_REVIEW": "0"})
    assert proc.returncode == 0, proc.stderr
    assert _git(sandbox_repo, "log", "--oneline").stdout.count("\n") == 2


def test_a_broken_nx_on_path_still_does_not_block_a_commit(
    sandbox_repo: Path, tmp_path: Path
) -> None:
    """The failure mode that matters: nx missing, wedged, or erroring.

    A commit must land anyway. This shadows ``nx`` with a script that
    exits 1 and asserts the commit still succeeds -- the property the
    detached dispatch is there to guarantee.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "nx").write_text("#!/bin/sh\nexit 1\n")
    (shim_dir / "nx").chmod(0o755)

    (sandbox_repo / "c.txt").write_text("c\n")
    _git(sandbox_repo, "add", "c.txt")
    proc = _git(
        sandbox_repo,
        "commit",
        "-m",
        "feat: c",
        env={"PATH": f"{shim_dir}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stderr


# ── the review branch actually firing ────────────────────────────────────────
#
# Every test above this point either sets NX_COMMIT_REVIEW=0 or inspects the
# stanza as a string, so none of them ever executed the review branch. That
# gap is exactly what let two silent-skip defects ship: the stanza had been
# appended AFTER the indexing block's two `exit 0` guards, so the reviewer
# never ran while an indexer was alive (measured at 64-131 minutes on this
# repo) or in any linked worktree, and the only log line written said
# INDEXING was skipped. These tests run the real hook with a fake `nx` and
# assert the reviewer was actually invoked.


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """An isolated HOME so the hook's log never lands in the real config dir."""
    home = tmp_path / "home"
    (home / ".config" / "nexus").mkdir(parents=True)
    return home


def _decoy_process(cmdline: str) -> subprocess.Popen:
    """A live process whose COMMAND LINE is *cmdline*, for pgrep guards.

    ``sh -c "sleep 25" "<name>"`` does not work: on macOS the $0 override
    is not what ps reports, so pgrep -f never matches and the guard test
    passes vacuously. bash's ``exec -a`` sets the real argv[0].
    """
    return subprocess.Popen(
        ["bash", "-c", f'exec -a "{cmdline}" sleep 25'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _nx_recorder(tmp_path: Path) -> tuple[Path, Path]:
    """A fake ``nx`` on PATH that records its argv. Returns (bindir, logfile)."""
    shim_dir = tmp_path / "recorder-bin"
    shim_dir.mkdir()
    calls = tmp_path / "nx-calls.txt"
    (shim_dir / "nx").write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
    (shim_dir / "nx").chmod(0o755)
    return shim_dir, calls


def _commit_with_recorder(
    repo: Path, shim_dir: Path, name: str, home: Path, extra_env=None
) -> None:
    """Commit with the recorder on PATH and an ISOLATED HOME.

    HOME is mandatory, not optional. The hook appends to
    ``$HOME/.config/nexus/index.log``, so a test that lets the review
    branch run under the real HOME writes into the operator's actual
    config dir. tests/conftest.py's _check_real_config_dir_mutations
    guard catches it -- it caught exactly this while these tests were
    being written.
    """
    (repo / name).write_text(name)
    _git(repo, "add", name)
    env = {"PATH": f"{shim_dir}:{os.environ['PATH']}", "HOME": str(home)}
    env.update(extra_env or {})
    proc = _git(repo, "commit", "-m", f"feat: {name}", env=env)
    assert proc.returncode == 0, proc.stderr


def _await_file(path: Path, timeout: float = 10.0) -> str:
    """Wait for a DETACHED writer to produce *path*, then return its text.

    The hook backgrounds and disowns its dispatch, so the commit returns
    before the child has written anything. Asserting immediately after the
    commit is a race that fails on a fast machine and passes on a slow one.
    Returns "" on timeout so callers assert on content, not on this.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text()
        time.sleep(0.05)
    return path.read_text() if path.exists() else ""


def test_a_normal_commit_actually_invokes_the_reviewer(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """The positive case, which had no coverage at all."""
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, calls = _nx_recorder(tmp_path)
    _commit_with_recorder(sandbox_repo, shim_dir, "normal.txt", fake_home)

    text = _await_file(calls)
    assert "review commit" in text, f"reviewer not invoked; nx calls were:\n{text!r}"


def test_the_reviewer_still_runs_while_an_indexer_is_active(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """CRITICAL 1: the indexer's `exit 0` must not swallow the review.

    A previous commit's indexer runs for 64-131 minutes on this repo, so
    under that guard an ordinary burst of commits reviewed the first and
    silently dropped every one after it -- the exact release-cut case the
    review's own pgrep guard exists to handle.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, calls = _nx_recorder(tmp_path)

    # A real process whose command line matches the indexer guard's pgrep
    # pattern, and NOT the reviewer's.
    decoy = _decoy_process(f"nx index repo {sandbox_repo}")
    try:
        # Prove the decoy is visible to the same matcher the hook uses,
        # otherwise this test passes vacuously (nexus-moht0).
        found = subprocess.run(
            ["pgrep", "-f", f"nx index repo {sandbox_repo}"],
            capture_output=True, text=True,
        )
        assert found.returncode == 0, "decoy indexer not visible to pgrep; test is vacuous"

        _commit_with_recorder(sandbox_repo, shim_dir, "during-index.txt", fake_home)
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)

    text = _await_file(calls)
    assert "review commit" in text, f"reviewer skipped by the indexer guard; nx calls:\n{text!r}"
    assert "index repo" not in text, "the indexer should still have been skipped"


def test_the_reviewer_still_runs_in_a_linked_worktree(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """CRITICAL 2: the linked-worktree `exit 0` must not swallow the review.

    Worktree dispatch is this project's standard agent workflow, and hooks
    live in the COMMON git dir, so every worktree commit went unreviewed
    while the log line claimed only that INDEXING was skipped.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, calls = _nx_recorder(tmp_path)

    wt = tmp_path / "linked-wt"
    made = _git(sandbox_repo, "worktree", "add", "-q", "-b", "wt-branch", str(wt))
    assert made.returncode == 0, made.stderr
    # Non-vacuity: the hook must actually be reachable from the worktree,
    # which is the property that makes this test meaningful at all.
    assert (sandbox_repo / ".git" / "hooks" / "post-commit").exists()

    _commit_with_recorder(wt, shim_dir, "in-worktree.txt", fake_home)

    text = _await_file(calls)
    assert "review commit" in text, f"reviewer skipped by the worktree guard; nx calls:\n{text!r}"
    assert "index repo" not in text, "indexing a worktree is still correctly skipped"


def test_a_second_concurrent_review_is_queued_and_says_so(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """The review's OWN pgrep guard may serialise, but never in silence.

    A skipped review that logs nothing is indistinguishable from a review
    that ran and found nothing (nexus-moht0).
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, _ = _nx_recorder(tmp_path)

    decoy = _decoy_process(f"nx review commit HEAD --repo {sandbox_repo}")
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"nx review commit .* --repo {sandbox_repo}"],
            capture_output=True, text=True,
        )
        assert found.returncode == 0, "decoy reviewer not visible to pgrep; test is vacuous"
        _commit_with_recorder(sandbox_repo, shim_dir, "concurrent.txt", fake_home)
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)

    log = fake_home / ".config" / "nexus" / "index.log"
    assert "QUEUED (review already running)" in _await_file(log)


# ── the census can answer the hook-armed question itself (nexus-trwxr) ────────


def test_hook_stanza_state_names_armed_stale_and_missing(tmp_path):
    """The reviewer sat unarmed for two days while nx doctor reported the
    stale stanza to nobody; the census now asks the same question."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert hook_stanza_state(repo) == "not installed"
    hooks_dir = repo / ".git" / "hooks"
    _install_hook(hooks_dir, "post-commit")
    assert hook_stanza_state(repo) == "armed"
    hook = hooks_dir / "post-commit"
    hook.write_text(hook.read_text().replace("nx review commit", "nx review commit --pre-jh86x"))
    assert hook_stanza_state(repo) == "stale"
    hook.write_text("#!/bin/sh\necho mine\n")
    assert hook_stanza_state(repo) == "unmanaged"
    assert hook_stanza_state(tmp_path / "not-a-repo") == "unknown"
    assert "nx review commit" in _stanza_for("post-commit")


def test_hook_stanza_state_honours_core_hookspath(tmp_path):
    """Every sibling (install, update, status, doctor) resolves core.hooksPath;
    the first cut of this function did not and would have told such a repo
    "not installed" forever (critique [24292] ship-blocker)."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    custom = tmp_path / "custom-hooks"
    custom.mkdir()
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(custom)], check=True)
    assert hook_stanza_state(repo) == "not installed"
    _install_hook(custom, "post-commit")
    assert hook_stanza_state(repo) == "armed"
    assert not (repo / ".git" / "hooks" / "post-commit").exists()


# ── a burst is queued, not dropped (2026-09-04, session nexus-65) ─────────────


def test_a_second_concurrent_review_is_queued_where_the_drainer_reads(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """The hook's shell path and the Python reader must name the same file.

    Before this, a pgrep hit logged SKIPPED and the commit was never
    reviewed: 6 of 9 commits in one push. The hook appends HEAD to the
    queue in the common git dir, and ``review_queue_path`` is what
    ``nx review commit --drain`` pops; if the two paths drift the queue
    fills and nothing drains it, silently.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, calls = _nx_recorder(tmp_path)

    decoy = _decoy_process(f"nx review commit HEAD --repo {sandbox_repo}")
    try:
        found = subprocess.run(
            ["pgrep", "-f", f"nx review commit .* --repo {sandbox_repo}"],
            capture_output=True, text=True,
        )
        assert found.returncode == 0, "decoy reviewer not visible to pgrep; test is vacuous"
        _commit_with_recorder(sandbox_repo, shim_dir, "queued.txt", fake_home)
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)

    head = _git(sandbox_repo, "rev-parse", "HEAD").stdout.strip()
    queue = review_queue_path(sandbox_repo)
    assert _await_file(queue).strip() == head, f"queue at {queue} did not receive HEAD"
    log = (fake_home / ".config" / "nexus" / "index.log").read_text()
    assert "QUEUED (review already running)" in log
    assert "review commit" not in (calls.read_text() if calls.exists() else "")
    assert pop_review_queue(sandbox_repo) == [head]
    assert not queue.exists(), "pop must take the file, not copy it"


def test_the_uncontended_dispatch_passes_drain(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """The reviewer that runs is the one that empties the queue, so the
    hook must ask it to. A hook that queues but dispatches without --drain
    fills a file nobody reads."""
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, calls = _nx_recorder(tmp_path)
    _commit_with_recorder(sandbox_repo, shim_dir, "drain.txt", fake_home)
    text = _await_file(calls)
    assert "review commit" in text and "--drain" in text, text


def test_a_worktree_commit_queues_into_the_shared_queue(
    sandbox_repo: Path, tmp_path: Path, fake_home: Path
) -> None:
    """One queue per repository: a linked worktree's hook writes where the
    primary's drainer reads, because both resolve the COMMON git dir."""
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir, _ = _nx_recorder(tmp_path)
    wt = tmp_path / "queue-wt"
    assert _git(sandbox_repo, "worktree", "add", "-q", "-b", "queue-branch", str(wt)).returncode == 0
    assert review_queue_path(wt) == review_queue_path(sandbox_repo)

    decoy = _decoy_process(f"nx review commit HEAD --repo {wt}")
    try:
        _commit_with_recorder(wt, shim_dir, "wt-queued.txt", fake_home)
    finally:
        decoy.terminate()
        decoy.wait(timeout=10)
    head = _git(wt, "rev-parse", "HEAD").stdout.strip()
    assert _await_file(review_queue_path(sandbox_repo)).strip() == head


def test_the_stanza_refuses_to_queue_into_the_filesystem_root() -> None:
    """Review [24406] Major: with an empty common dir the queue path
    degraded to ``/nx-review-queue`` and the log still said QUEUED. The
    stanza must test the dir before writing and log a distinct line."""
    assert '[ -n "$_NX_REVIEW_COMMON" ]' in _REVIEW_STANZA
    assert "NOT QUEUED (queue unwritable" in _REVIEW_STANZA
    assert '"$_NX_REVIEW_COMMON/nx-review-queue"' in _REVIEW_STANZA
