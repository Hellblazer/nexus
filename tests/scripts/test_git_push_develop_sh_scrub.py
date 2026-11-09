# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.7: scripts/git-push-develop.sh's scrub step, on fixture repos built by the helpers of
tests/scripts/test_git_push_develop_sh.py (loaded by path; a separate module so that file stays under its
size ceiling, scripts/file_size_ratchet.py)."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "_git_push_develop_helpers", Path(__file__).with_name("test_git_push_develop_sh.py")
)
assert _spec and _spec.loader
_h = importlib.util.module_from_spec(_spec)
sys.modules["_git_push_develop_helpers"] = _h
_spec.loader.exec_module(_h)

REPO_ROOT = _h.REPO_ROOT
_git, _commit, _run, _run_flags, _remote_tip = (
    _h._git,
    _h._commit,
    _h._run,
    _h._run_flags,
    _h._remote_tip,
)
repos = _h.repos  # the fixture, re-exported so pytest finds it here

_TENANT_STUB = (
    "import pathlib, sys\n"
    "pathlib.Path('tenant-stub-ran').write_text(' '.join(sys.argv[1:]))\n"
    "print('tenant delta: +1 test(s) that mint a tenant across 1 changed module(s)')\n"
    "sys.exit({rc})\n"
)


class TestScrubGate:
    """nexus-3ceda.7: the push runs scripts/scrub_check.py on the TIP's files and refuses a
    personal home path with PUSH_REFUSED_SCRUB, exit 12. The real script is copied into the
    fixture repo; the strings are built by concatenation so this file stays clean."""

    _HOME = "/Users/" + "jdoe" + "/git/proj"

    def _arm(self, work: Path) -> None:
        (work / "scripts").mkdir(exist_ok=True)
        shutil.copy(
            REPO_ROOT / "scripts" / "scrub_check.py",
            work / "scripts" / "scrub_check.py",
        )
        (work / "tests" / "fixtures").mkdir(parents=True, exist_ok=True)
        (work / "tests" / "fixtures" / "scrub-allowlist.txt").write_text("user you\n")
        _git(
            "add",
            "scripts/scrub_check.py",
            "tests/fixtures/scrub-allowlist.txt",
            cwd=work,
        )
        _git("commit", "-q", "-m", "arm", cwd=work)
        _git("push", "-q", "origin", "develop", cwd=work)

    def _commit_text(self, work: Path, name: str, text: str) -> str:
        (work / name).write_text(text)
        _git("add", name, cwd=work)
        _git("commit", "-q", "-m", name, cwd=work)
        return _git("rev-parse", "HEAD", cwd=work)

    def test_a_tip_with_a_personal_path_is_refused(self, repos) -> None:
        origin, work = repos
        self._arm(work)
        before = _remote_tip(origin)
        sha = self._commit_text(work, "note.md", f"see {self._HOME}\n")
        proc = _run(work, sha)
        assert proc.returncode == 12, proc.stdout + proc.stderr
        assert "PUSH_REFUSED_SCRUB" in proc.stdout
        assert "note.md: 1 scrub hits, over no allowance" in proc.stdout
        assert _remote_tip(origin) == before

    def test_a_clean_tip_pushes(self, repos) -> None:
        origin, work = repos
        self._arm(work)
        sha = self._commit_text(work, "note.md", "see /Users/you/git/proj\n")
        proc = _run(work, sha)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _remote_tip(origin) == sha

    def test_a_dirty_working_tree_does_not_decide(self, repos) -> None:
        origin, work = repos
        self._arm(work)
        sha = self._commit_text(work, "note.md", "clean\n")
        (work / "note.md").write_text(f"{self._HOME}\n")  # uncommitted
        proc = _run(work, sha)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _remote_tip(origin) == sha

    def test_a_session_trailer_in_an_outbound_message_is_refused(self, repos) -> None:
        origin, work = repos
        self._arm(work)
        before = _remote_tip(origin)
        (work / "note.md").write_text("clean\n")
        _git("add", "note.md", cwd=work)
        _git(
            "commit",
            "-q",
            "-m",
            "note\n\nClaude-" + "Session: 0f0f0f0f-0000-4000-8000-000000000000",
            cwd=work,
        )
        proc = _run(work, _git("rev-parse", "HEAD", cwd=work))
        assert proc.returncode == 12, proc.stdout + proc.stderr
        assert "message: session" in proc.stdout
        assert _remote_tip(origin) == before

    def test_the_refusal_comes_before_the_tenant_hint(self, repos) -> None:
        origin, work = repos
        self._arm(work)
        (work / "scripts" / "tenant_delta.py").write_text(_TENANT_STUB.format(rc=0))
        _git("add", "scripts/tenant_delta.py", cwd=work)
        _git("commit", "-q", "-m", "stub", cwd=work)
        _git("push", "-q", "origin", "develop", cwd=work)
        (work / "tests" / "test_x.py").write_text(f"P = '{self._HOME}'\n")
        _git("add", "tests/test_x.py", cwd=work)
        _git("commit", "-q", "-m", "test", cwd=work)
        proc = _run(work, _git("rev-parse", "HEAD", cwd=work))
        assert proc.returncode == 12, proc.stdout + proc.stderr
        assert not (work / "tenant-stub-ran").exists(), (
            "the sub-second scrub refuses before the 30 s hint"
        )

    def test_a_checkout_without_the_script_says_it_skipped(self, repos) -> None:
        origin, work = repos
        sha = _commit(work, "plain")
        proc = _run(work, sha)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (
            "PUSH_SCRUB_SKIPPED reason=no scripts/scrub_check.py in this checkout"
            in proc.stderr
        )

    def test_skip_scrub_pushes_on_the_record(self, repos) -> None:
        origin, work = repos
        self._arm(work)
        sha = self._commit_text(work, "note.md", f"see {self._HOME}\n")
        proc = _run_flags(
            work,
            "--skip-scrub",
            "scrub flag test",
            "--skip-lock",
            "scrub flag test",
            "--skip-scope-audit",
            "scrub flag test",
            sha,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "PUSH_SCRUB_SKIPPED reason=scrub flag test" in proc.stderr
        assert _remote_tip(origin) == sha
