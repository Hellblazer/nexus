#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run pytest on native Windows (qwent's Windows side) for a commit, by hand.

CI's Windows set runs only on the ``win-release`` runner, after a push, so a
Windows-only red used to be found a CI run late. This runs the same thing
before the push, in the slot ``lab-run-win`` shares with that runner (the
labrun README, "Windows native builds"):

    scripts/win_hand_test.py --session wren -- tests/hooks/test_x.py
    scripts/win_hand_test.py --session wren --set      # CI's whole Windows set

It works under ``C:\\build\\hand\\<session>\\nexus`` (a partial clone made on
first use), checks out the commit, and runs
``lab-run-win.cmd uv run --python 3.13 --frozen pytest ... -p no:cacheprovider``
with ``NX_TEST_T2_SUBSTRATE=none``, as the CI job does. A commit that is not on
origin (a worktree's unpushed work) is sent as an incremental git bundle over
scp, so nothing has to be pushed first. ``--set`` reads the pytest arguments
of the "Windows test set" step from the workflow file, so a hand run cannot
drift from what CI runs. Exits with pytest's code.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "windows-pg-bundle-rehearsal.yml"
SET_STEP: str = "Windows test set"
#: CI re-checks out with LF and copies each tracked symlink's target over its
#: stub (Git for Windows has core.symlinks=false); a hand run does the same.
FIXUP_STEP: str = "Checkout fixups (LF, symlinks materialised)"
DEFAULT_HOST: str = "qwentescence"
LAB_RUN_WIN: str = r"C:\build\lab\lab-run-win.cmd"
CLONE_URL: str = "https://github.com/Hellblazer/nexus.git"

Runner = Callable[..., subprocess.CompletedProcess]


def ci_step_run(name: str, workflow: Path = WORKFLOW) -> str:
    """The ``run`` text of the workflow step called *name* (exactly one)."""
    import yaml  # noqa: PLC0415 — only this path needs it

    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    runs = [
        str(step["run"])
        for job in (doc.get("jobs") or {}).values()
        for step in job.get("steps") or []
        if step.get("name") == name
    ]
    if len(runs) != 1:
        raise SystemExit(f"win_hand_test: {len(runs)} steps named {name!r} in {workflow}, expected one")
    return runs[0]


def ci_set_args(workflow: Path = WORKFLOW) -> list[str]:
    """The pytest arguments of the workflow's "Windows test set" step, minus
    ``--junitxml`` (CI's floor check reads that file; a hand run has none)."""
    words = shlex.split(ci_step_run(SET_STEP, workflow))
    start = words.index("pytest") + 1
    return [w for w in words[start:] if not w.startswith("--junitxml")]


def _ps_quote(word: str) -> str:
    """One argument as a PowerShell single-quoted string."""
    return "'" + word.replace("'", "''") + "'"


def remote_script(session: str, sha: str, pytest_args: Sequence[str], *, bundle: str | None,
                  fixup: str) -> str:
    """The PowerShell (pwsh) script the remote side runs: clone once, bring the
    commit in, undo the previous run's materialised symlinks, check the commit
    out, apply CI's own checkout fixup step (*fixup*), run pytest through
    lab-run-win, and exit with its code."""
    base = rf"C:\build\hand\{session}"
    repo = base + "\\nexus"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        f"New-Item -ItemType Directory -Force {_ps_quote(base)} | Out-Null",
        f"if (-not (Test-Path {_ps_quote(repo)})) {{ git clone -q --filter=blob:none {CLONE_URL} {_ps_quote(repo)} }}",
        f"Set-Location {_ps_quote(repo)}",
        "git fetch -q origin",
    ]
    if bundle is not None:
        lines.append(f"git fetch -q {_ps_quote(bundle)} '+refs/win-hand/{session}:refs/win-hand/{session}'")
    lines += [
        "git config core.autocrlf false",
        "# The previous run replaced each tracked symlink stub with a copy; drop the copies.",
        "foreach ($rel in @(git ls-files -s | Where-Object { $_ -match '^120000 ' } | ForEach-Object { ($_ -split \"`t\", 2)[1] })) {",
        "  if (Test-Path -LiteralPath $rel -PathType Container) { Remove-Item -LiteralPath $rel -Recurse -Force }",
        "}",
        f"git checkout -q -f --detach {sha}",
        "# CI's own checkout fixup step, verbatim from the workflow:",
        fixup.rstrip(),
        "$ErrorActionPreference = 'Continue'",
        "$env:NX_TEST_T2_SUBSTRATE = 'none'",
        f"& {_ps_quote(LAB_RUN_WIN)} uv run --python 3.13 --frozen pytest "
        + " ".join(_ps_quote(a) for a in pytest_args)
        + " -p no:cacheprovider",
        "exit $LASTEXITCODE",
    ]
    return "\n".join(lines) + "\n"


def _on_origin(sha: str, run: Runner) -> bool:
    proc = run(["git", "-C", str(REPO_ROOT), "branch", "-r", "--contains", sha],
               capture_output=True, text=True, encoding="utf-8", check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def main(argv: Sequence[str] | None = None, *, run: Runner = subprocess.run) -> int:
    ap = argparse.ArgumentParser(description="Run pytest on native Windows (qwent) for a commit.")
    ap.add_argument("--session", required=True, help="your session name: work goes under C:\\build\\hand\\<session>")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--ref", default="HEAD", help="the commit to test (default HEAD of this checkout)")
    ap.add_argument("--set", action="store_true", help="run CI's whole Windows test set")
    ap.add_argument("pytest_args", nargs="*", help="pytest arguments, after --")
    ns = ap.parse_args(argv)
    if not ns.session.replace("_", "").replace("-", "").isalnum():
        raise SystemExit("win_hand_test: --session must be letters, digits, - or _")
    args = (ci_set_args() if ns.set else []) + list(ns.pytest_args)
    if not args:
        raise SystemExit("win_hand_test: give pytest arguments after --, or --set")

    sha = run(["git", "-C", str(REPO_ROOT), "rev-parse", ns.ref], capture_output=True,
              text=True, encoding="utf-8", check=True).stdout.strip()
    bundle_remote: str | None = None
    if not _on_origin(sha, run):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "win-hand.bundle"
            # A bundle carries refs, not bare shas: name the commit under a
            # per-session ref for the bundle's life, then drop the ref.
            tmp_ref = f"refs/win-hand/{ns.session}"
            run(["git", "-C", str(REPO_ROOT), "update-ref", tmp_ref, sha], check=True)
            try:
                run(["git", "-C", str(REPO_ROOT), "bundle", "create", "-q", str(local), tmp_ref, "^origin/develop"],
                    check=True)
            finally:
                run(["git", "-C", str(REPO_ROOT), "update-ref", "-d", tmp_ref], check=False)
            bundle_remote = rf"C:\build\hand\{ns.session}\win-hand.bundle"
            run(["ssh", ns.host, f"New-Item -ItemType Directory -Force 'C:\\build\\hand\\{ns.session}' | Out-Null"], check=True)
            run(["scp", "-q", str(local), f"{ns.host}:C:/build/hand/{ns.session}/win-hand.bundle"], check=True)
    script = remote_script(ns.session, sha, args, bundle=bundle_remote, fixup=ci_step_run(FIXUP_STEP))
    remote_ps1 = rf"C:\build\hand\{ns.session}\win-hand.ps1"
    with tempfile.TemporaryDirectory() as tmp:
        local_ps1 = Path(tmp) / "win-hand.ps1"
        local_ps1.write_text(script, encoding="utf-8")
        run(["ssh", ns.host, f"New-Item -ItemType Directory -Force 'C:\\build\\hand\\{ns.session}' | Out-Null"], check=True)
        run(["scp", "-q", str(local_ps1), f"{ns.host}:C:/build/hand/{ns.session}/win-hand.ps1"], check=True)
    proc = run(["ssh", ns.host, f"pwsh -NoProfile -File '{remote_ps1}'"], check=False)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
