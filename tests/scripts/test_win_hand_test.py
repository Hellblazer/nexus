# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/win_hand_test.py: a hand run of pytest on qwent's Windows side.

The ssh and scp calls go to a recording fake; the git calls that read the
local repo are real.
"""
from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("win_hand_test", REPO_ROOT / "scripts" / "win_hand_test.py")
assert _spec is not None and _spec.loader is not None
wht = importlib.util.module_from_spec(_spec)
sys.modules["win_hand_test"] = wht
_spec.loader.exec_module(wht)


def test_the_set_is_the_ci_steps_own_arguments() -> None:
    import yaml  # noqa: PLC0415 — test-local

    doc = yaml.safe_load(wht.WORKFLOW.read_text(encoding="utf-8"))
    runs = [s["run"] for j in doc["jobs"].values() for s in j.get("steps") or [] if s.get("name") == wht.SET_STEP]
    assert len(runs) == 1, "exactly one CI step carries the Windows set"
    words = shlex.split(runs[0])
    expected = [w for w in words[words.index("pytest") + 1:] if not w.startswith("--junitxml")]
    assert wht.ci_set_args() == expected
    assert "tests/hooks/" in expected, "non-vacuity: the set includes the hooks tests"


def test_a_missing_step_fails_loud(tmp_path: Path) -> None:
    wf = tmp_path / "wf.yml"
    wf.write_text("jobs:\n  j:\n    steps:\n      - name: other\n        run: echo\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="expected one"):
        wht.ci_set_args(wf)


def test_the_ci_checkout_fixup_runs_verbatim_after_the_checkout() -> None:
    """Git for Windows checks tracked symlinks out as stub files; CI's fixup
    step copies their targets in. Without it the daemon resources are missing
    and 70 tests fail (the first hand run, 2026-11-01)."""
    fixup = wht.ci_step_run(wht.FIXUP_STEP)
    assert "120000" in fixup and "Copy-Item" in fixup, "non-vacuity: the step materialises symlinks"
    script = wht.remote_script("wren", "abc123", ["t.py"], bundle=None, fixup=fixup)
    assert fixup.rstrip() in script
    assert script.index("git checkout -q -f --detach abc123") < script.index(fixup.rstrip()) < script.index("pytest")


def test_the_remote_script_fetches_checks_out_then_runs_like_ci() -> None:
    script = wht.remote_script("wren", "abc123", ["tests/x.py", "-k", "it's"], bundle=r"C:\b\w.bundle", fixup="# fixup")
    order = [script.index(part) for part in (
        "git fetch -q origin",
        "git fetch -q 'C:\\b\\w.bundle' '+refs/win-hand/wren:refs/win-hand/wren'",
        "Remove-Item -LiteralPath $rel -Recurse -Force",
        "git checkout -q -f --detach abc123",
        "# fixup",
        "$env:NX_TEST_T2_SUBSTRATE = 'none'",
        "lab-run-win.cmd' uv run --python 3.13 --frozen pytest 'tests/x.py' '-k' 'it''s' -p no:cacheprovider",
        "exit $LASTEXITCODE",
    )]
    assert order == sorted(order)
    assert r"C:\build\hand\wren" in script


def test_a_pushed_commit_needs_no_bundle() -> None:
    assert "bundle" not in wht.remote_script("wren", "abc123", ["t.py"], bundle=None, fixup="")


class _Recorder:
    def __init__(self, on_origin: bool) -> None:
        self.on_origin = on_origin
        self.calls: list[list[str]] = []
        self.scripts: list[str] = []

    def __call__(self, argv: list[str], **kw: object) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        if argv[:3] == ["git", "-C", str(wht.REPO_ROOT)] and argv[3] == "rev-parse":
            return subprocess.CompletedProcess(argv, 0, stdout="deadbeef\n")
        if "branch" in argv and "--contains" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="  origin/develop\n" if self.on_origin else "")
        if argv[0] == "scp" and argv[2].endswith(".ps1"):
            self.scripts.append(Path(argv[2]).read_text(encoding="utf-8"))
        if argv[0] == "git" and "bundle" in argv:
            Path(argv[argv.index("-q") + 1]).write_bytes(b"bundle")
        return subprocess.CompletedProcess(argv, 0, stdout="")


def test_a_commit_on_origin_goes_straight_to_ssh() -> None:
    rec = _Recorder(on_origin=True)
    assert wht.main(["--session", "wren", "--", "tests/x.py"], run=rec) == 0
    assert not any(c[0] == "scp" and c[2].endswith(".bundle") for c in rec.calls)
    assert rec.calls[-1][:2] == ["ssh", wht.DEFAULT_HOST] and "pwsh -NoProfile -File" in rec.calls[-1][2]
    assert "checkout -q -f --detach deadbeef" in rec.scripts[-1]


def test_an_unpushed_commit_travels_as_a_bundle_and_its_ref_is_dropped() -> None:
    rec = _Recorder(on_origin=False)
    assert wht.main(["--session", "wren", "--", "tests/x.py"], run=rec) == 0
    kinds = [" ".join(c[:5]) for c in rec.calls]
    up = next(i for i, c in enumerate(rec.calls) if "update-ref" in c and "-d" not in c)
    bundle = next(i for i, c in enumerate(rec.calls) if "bundle" in c)
    drop = next(i for i, c in enumerate(rec.calls) if "update-ref" in c and "-d" in c)
    scp = next(i for i, c in enumerate(rec.calls) if c[0] == "scp" and c[2].endswith(".bundle"))
    assert up < bundle < drop < scp, kinds
    assert "refs/win-hand/wren:refs/win-hand/wren" in rec.scripts[-1]


def test_the_exit_code_is_pytests(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(on_origin=True)
    orig = rec.__call__

    def fail_ssh(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
        out = orig(argv, **kw)
        return subprocess.CompletedProcess(argv, 1, stdout="") if argv[0] == "ssh" else out

    assert wht.main(["--session", "wren", "--", "t.py"], run=fail_ssh) == 1


@pytest.mark.parametrize("session", ["a b", "x;y", "../z", ""])
def test_a_session_name_that_could_escape_the_path_is_refused(session: str) -> None:
    with pytest.raises(SystemExit):
        wht.main(["--session", session, "--", "t.py"], run=_Recorder(True))


def test_no_tests_named_is_refused() -> None:
    with pytest.raises(SystemExit, match="give pytest arguments"):
        wht.main(["--session", "wren"], run=_Recorder(True))
