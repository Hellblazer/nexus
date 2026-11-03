#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run pytest on native Windows (qwent's Windows side) for a commit, by hand.

CI's Windows set runs only on the ``win-release`` runner, after a push, so a
Windows-only red used to be found a CI run late. This runs the same thing
before the push, in the slot ``lab-run-win`` shares with that runner (the
labrun README, "Windows native builds"):

    uv run scripts/windows_hand_test.py --check                       # does this branch need it?
    uv run scripts/windows_hand_test.py --session wren -- tests/hooks/test_x.py
    uv run scripts/windows_hand_test.py --session wren --set          # CI's whole Windows set

It tests a COMMIT (default ``HEAD``); ``--uncommitted`` adds the working
tree's uncommitted changes as a patch applied after checkout, for an agent
that cannot commit. Without it a dirty tree is warned about. It works under ``C:\\build\\hand\\<session>\\nexus``
(a partial clone made on first use and kept), checks out the commit, applies
CI's own checkout fixup step, and runs
``lab-run-win.cmd uv run --python 3.13 --frozen pytest ... -p no:cacheprovider``
with ``NX_TEST_T2_SUBSTRATE=none``, as the CI job does. A commit that is not
in ``origin/develop`` travels as an incremental git bundle over scp. ``--set``
reads the Windows-set step's pytest arguments and runs CI's non-vacuity floor
step after it; ``--check`` reads the workflow's ``on.push.paths`` and says
whether this branch's diff touches them. Everything CI-specific is read from
the workflow file, never copied, so a hand run cannot drift from CI.

Exit codes: pytest's own code; 97 when a setup step failed (clone, fetch,
checkout, the fixup step, a commit mismatch), so a setup failure is never read
as a test result; 98 when pytest passed and the floor step failed.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
WORKFLOW: Path = REPO_ROOT / ".github" / "workflows" / "windows-pg-bundle-rehearsal.yml"
SET_STEP: str = "Windows test set"
#: CI re-checks out with LF and copies each tracked symlink's target over its
#: stub (Git for Windows has core.symlinks=false); a hand run does the same.
FIXUP_STEP: str = "Checkout fixups (LF, symlinks materialised)"
FLOOR_STEP: str = "Non-vacuity floor"
DEFAULT_HOST: str = "qwentescence"
LAB_RUN_WIN: str = r"C:\build\lab\lab-run-win.cmd"
CLONE_URL: str = "https://github.com/Hellblazer/nexus.git"
SETUP_FAILED: int = 97
FLOOR_FAILED: int = 98
#: An --uncommitted patch above this is refused: an untracked dump, not work.
MAX_PATCH_BYTES: int = 20 * 1024 * 1024

#: Every character PowerShell takes as a single-quote delimiter.
_PS_SINGLE_QUOTES: str = "'\u2018\u2019\u201a\u201b"
#: lab-run-win.cmd hands the command to cmd.exe, which re-parses these.
_CMD_METACHARS: frozenset[str] = frozenset('|&<>^%"')
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
)

Runner = Callable[..., subprocess.CompletedProcess]


def _workflow(workflow: Path) -> dict:
    import yaml  # noqa: PLC0415 — the workflow paths need it; run through uv

    return yaml.safe_load(workflow.read_text(encoding="utf-8"))


def ci_step_run(name: str, workflow: Path = WORKFLOW) -> str:
    """The ``run`` text of the workflow step called *name* (exactly one)."""
    runs = [
        str(step["run"])
        for job in (_workflow(workflow).get("jobs") or {}).values()
        for step in job.get("steps") or []
        if step.get("name") == name
    ]
    if len(runs) != 1:
        raise SystemExit(f"windows_hand_test: {len(runs)} steps named {name!r} in {workflow}, expected one")
    return runs[0]


def ci_set_args(workflow: Path = WORKFLOW) -> list[str]:
    """The pytest arguments of the workflow's "Windows test set" step, as CI
    passes them (its ``--junitxml`` included: the floor step reads that file)."""
    words = shlex.split(ci_step_run(SET_STEP, workflow))
    if words.count("pytest") != 1:
        raise SystemExit(f"windows_hand_test: the {SET_STEP!r} step must run pytest exactly once; its shape changed")
    return words[words.index("pytest") + 1:]


def push_paths(workflow: Path = WORKFLOW) -> list[str]:
    """The workflow's ``on.push.paths`` filter: the files whose change makes
    CI run the Windows set, so the files a hand run should cover first."""
    doc = _workflow(workflow)
    on = doc.get("on", doc.get(True)) or {}  # YAML 1.1 reads a bare `on:` key as True
    paths = ((on.get("push") or {}).get("paths")) or []
    if not paths:
        raise SystemExit(f"windows_hand_test: no on.push.paths in {workflow}")
    return [str(p) for p in paths]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """GitHub path-filter glob: ``**`` crosses directories, ``*`` does not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(rf"\A{out}\Z")


def files_needing_windows(changed: Sequence[str], workflow: Path = WORKFLOW) -> list[str]:
    pats = [_glob_to_regex(p) for p in push_paths(workflow)]
    return [f for f in changed if any(p.match(f) for p in pats)]


def _ps_quote(word: str) -> str:
    """One argument as a PowerShell single-quoted string; every character
    PowerShell reads as a single quote is doubled."""
    return "'" + "".join(c * 2 if c in _PS_SINGLE_QUOTES else c for c in word) + "'"


def _check_pytest_arg(arg: str) -> str:
    bad = sorted(set(arg) & _CMD_METACHARS)
    if bad:
        raise SystemExit(
            f"windows_hand_test: argument {arg!r} carries {''.join(bad)!r}, which cmd.exe "
            "(lab-run-win.cmd) would re-parse; select the test another way"
        )
    return arg


#: Seconds between the client's heartbeat bytes on the run's ssh stdin.
HEARTBEAT_S: float = 5.0
#: Seconds of heartbeat silence after which the remote run ends. Above ssh's own tolerance for a dead link (the
#: qwentescence config's ServerAliveInterval 30 x ServerAliveCountMax 3 = 90 s), so a link stall that ssh survives
#: never ends a run; a false kill loses a whole run, while a late kill only holds the slot a little longer.
WATCHDOG_SILENCE_S: int = 150
#: Set in the run's ssh command; the watchdog arms only when it is set, so a hand rerun of the script over plain
#: ssh (or ssh -n, whose stdin is empty) is never ended for a heartbeat nobody sends.
HEARTBEAT_ENV: str = "NX_WIN_HAND_HEARTBEAT"

#: Ends this script's process tree when its client is gone (nexus-bj8e7). Windows OpenSSH does not end a session's
#: processes when the client goes away: measured 2026-11-03 on qwent, a killed client left pwsh, its powershell
#: parent and both sshd session processes alive, so a client killed while queued for lab-run-win left the wrapper to
#: take the build slot later and run with nobody reading it. The TCP table was no answer: a hidden process polling
#: it on qwent missed a live session's connection for whole runs, twice. The client's stdin is: the client sends a
#: byte every HEARTBEAT_S on the run's ssh stdin (:func:`run_with_heartbeat`), and a watchdog thread in this script
#: reads it. End of file (the client python died, whatever killed it, or its ssh did) or WATCHDOG_SILENCE_S without
#: a byte (a half-open link, a sleeping laptop) runs taskkill /T on this script and everything under it: setup's git
#: calls, lab-run-win's wait for the slot, pytest. It runs first, before setup, and arms only under HEARTBEAT_ENV.
#: Once the thread holds the heartbeat stream, the script's own stdin becomes NUL, so no child (git, uv, cmd,
#: pytest, a hook a test spawns) reads the beats or blocks on the pipe. A kill, or a watchdog that fails, writes a
#: line to stderr and to watchdog.log beside the run; a failure never kills. Limit: the beats prove the client
#: python is alive, not that anything reads its output (a client orphaned by a SIGKILLed `uv run` parent keeps
#: beating).


def watchdog_lines(session: str) -> list[str]:
    """The first lines of the remote script: the heartbeat watchdog (see the comment above)."""
    log = rf"C:\build\hand\{session}\watchdog.log"
    return [
        "# End this run when its client is gone (nexus-bj8e7). A watchdog failure never ends the run.",
        "try {",
        f"  if ($env:{HEARTBEAT_ENV} -eq '1') {{",
        "    $wdIn = [Console]::OpenStandardInput()",
        "    Add-Type -Namespace WinHand -Name Std -MemberDefinition @'",
        "[StructLayout(LayoutKind.Sequential)] public struct SA { public int n; public IntPtr d; public bool inherit; }",
        "[DllImport(\"kernel32.dll\", SetLastError=true)] public static extern bool SetStdHandle(int which, IntPtr h);",
        "[DllImport(\"kernel32.dll\", SetLastError=true, CharSet=CharSet.Unicode)] public static extern IntPtr "
        "CreateFileW(string name, uint access, uint share, ref SA sa, uint disposition, uint flags, IntPtr template);",
        "'@",
        "    $sa = New-Object WinHand.Std+SA; $sa.n = [Runtime.InteropServices.Marshal]::SizeOf($sa); $sa.inherit = $true",
        "    $nul = [WinHand.Std]::CreateFileW('NUL', [uint32]3221225472, 3, [ref]$sa, 3, 0, [IntPtr]::Zero)",
        "    if ($nul -ne [IntPtr]::new(-1)) { [void][WinHand.Std]::SetStdHandle(-10, $nul) }",
        "    $wdRunspace = [runspacefactory]::CreateRunspace(); $wdRunspace.Open()",
        "    $wdShell = [powershell]::Create(); $wdShell.Runspace = $wdRunspace",
        "    [void]$wdShell.AddScript({",
        "      param($target, $silence, $in, $log)",
        "      try {",
        "        $buf = New-Object byte[] 256",
        "        while ($true) {",
        "          $read = $in.ReadAsync($buf, 0, $buf.Length)",
        "          if (-not $read.Wait($silence * 1000)) { $why = \"no client heartbeat for $silence s\"; break }",
        "          if ($read.Result -le 0) { $why = 'the client closed its heartbeat'; break }",
        "        }",
        "      } catch {",
        "        \"$(Get-Date -Format o) watchdog failed, run continues unwatched: $_\" | Add-Content -LiteralPath $log -ErrorAction SilentlyContinue",
        "        [Console]::Error.WriteLine(\"windows_hand_test: watchdog failed, run continues unwatched: $_\")",
        "        return",
        "      }",
        "      \"$(Get-Date -Format o) $why; ending the run\" | Add-Content -LiteralPath $log -ErrorAction SilentlyContinue",
        "      [Console]::Error.WriteLine(\"windows_hand_test: $why; ending the run\")",
        "      & taskkill.exe /T /F /PID $target | Out-Null",
        f"    }}).AddArgument($PID).AddArgument({WATCHDOG_SILENCE_S}).AddArgument($wdIn).AddArgument({_ps_quote(log)})",
        "    $null = $wdShell.BeginInvoke()",
        "    Write-Host 'windows_hand_test: watchdog on the client heartbeat'",
        "  }",
        "} catch {",
        f"  \"$(Get-Date -Format o) no watchdog: $_\" | Add-Content -LiteralPath {_ps_quote(log)} -ErrorAction SilentlyContinue",
        "  Write-Host \"windows_hand_test: no watchdog ($_)\"",
        "}",
    ]


def run_with_heartbeat(argv: Sequence[str], *, interval: float = HEARTBEAT_S,
                       popen: Callable[..., subprocess.Popen] = subprocess.Popen) -> int:
    """Run *argv* (the run's ssh call) with a byte written to its stdin every *interval* seconds, and return its
    exit code. The remote watchdog (:func:`watchdog_lines`) ends the run on end of file or on silence. The pipe's write end
    lives only in this process, so any death of the client closes it; an exception here (SystemExit from
    :func:`end_on_signal` included) kills the child first."""
    proc = popen(list(argv), stdin=subprocess.PIPE)
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                proc.stdin.write(b".")
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    try:
        threading.Thread(target=beat, name="windows-hand-heartbeat", daemon=True).start()
        return proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        stop.set()
        try:
            proc.stdin.close()
        except OSError:
            pass


def remote_script(session: str, sha: str, pytest_args: Sequence[str], *, bundle: str | None,
                  fixup: str, floor: str | None, patch: str | None = None) -> str:
    """The PowerShell (pwsh) script the remote side runs. Any setup failure
    (a git call's nonzero exit, a terminating error, the commit not checked
    out) exits SETUP_FAILED before pytest can run on the wrong tree."""
    base = rf"C:\build\hand\{session}"
    repo = base + "\\nexus"
    fail = SETUP_FAILED

    def must(what: str) -> str:
        return f"if ($LASTEXITCODE -ne 0) {{ Write-Host 'windows_hand_test: {what} failed'; exit {fail} }}"

    lines = [
        *watchdog_lines(session),
        f"trap {{ Write-Host \"windows_hand_test: setup error: $_\"; exit {fail} }}",
        "$ErrorActionPreference = 'Stop'",
        # $LASTEXITCODE is $null until a native command runs, so the clone's
        # check sits inside the branch that clones (a kept clone runs no git here).
        # A clone killed midway leaves a directory that is not a repo: remove it and clone again.
        f"if ((Test-Path {_ps_quote(repo)}) -and -not (Test-Path {_ps_quote(repo + chr(92) + '.git' + chr(92) + 'HEAD')})) "
        f"{{ Remove-Item -LiteralPath {_ps_quote(repo)} -Recurse -Force }}",
        f"if (-not (Test-Path {_ps_quote(repo)})) {{ git clone -q -c core.autocrlf=false --filter=blob:none {CLONE_URL} {_ps_quote(repo)}; "
        + must("clone") + " }",
        f"Set-Location {_ps_quote(repo)}",
        # A kept clone may come from another tool or an older session of the same name whose origin is a local
        # bundle file; such a clone never sees develop's tip, and the bundle fetch below then fails on a missing
        # prerequisite. The script owns this directory, so origin is always the repo it clones from.
        f"git remote set-url origin {CLONE_URL}",
        must("pointing origin at the repo"),
        "git fetch -q origin",
        must("git fetch origin"),
    ]
    if bundle is not None:
        lines += [
            f"git fetch -q {_ps_quote(bundle)} '+refs/win-hand/{session}:refs/win-hand/{session}'",
            must("bundle fetch"),
        ]
    lines += [
        "git config core.autocrlf false",
        "# The previous run replaced each tracked symlink stub with a copy; drop the copies.",
        "foreach ($rel in @(git ls-files -s | Where-Object { $_ -match '^120000 ' } | ForEach-Object { ($_ -split \"`t\", 2)[1] })) {",
        "  if (Test-Path -LiteralPath $rel -PathType Container) { Remove-Item -LiteralPath $rel -Recurse -Force }",
        "}",
        "# Untracked files a previous --uncommitted run added (ignored ones such as .venv stay).",
        "git clean -fdq",
        must("git clean"),
        f"git checkout -q -f --detach {sha}",
        must("checkout"),
    ]
    expected_head = "HEAD"
    if patch is not None:
        # Applied BEFORE CI's fixup and kept as a throwaway local commit: the
        # fixup's hard reset then keeps it, and the copies it makes of each
        # tracked symlink's target (src/nexus/_resources/daemon <- conexus/daemon)
        # are made from the patched targets. Applied after the fixup, an edit to
        # conexus/daemon reached the target but not its copy (review r1).
        lines += [
            # The patch is LF; a clone made with Windows git's default autocrlf=true
            # (or one a previous run left CRLF) has CRLF working files, and
            # checkout -f does not rewrite a file whose stat matches the index.
            # Normalise the tree to LF the way CI's fixup does, before applying
            # (confirmation r2: every fresh session exited 97 here).
            "git rm -r -q --cached .",
            must("normalising line endings (rm --cached)"),
            "git reset -q --hard HEAD",
            must("normalising line endings (reset --hard)"),
            f"git apply --binary --whitespace=nowarn {_ps_quote(patch)}",
            must("applying the uncommitted changes"),
            "git add -A",
            must("staging the uncommitted changes"),
            "git -c user.name=windows_hand_test -c user.email=windows_hand_test@localhost -c commit.gpgsign=false "
            "commit -q --no-verify -m 'windows_hand_test: uncommitted changes'",
            must("committing the uncommitted changes"),
        ]
        expected_head = "HEAD~1"
    lines += [
        "# CI's own checkout fixup step, verbatim from the workflow:",
        fixup.rstrip(),
        must("checkout fixup"),
        f"$head = (git rev-parse {expected_head}).Trim()",
        f"if ($head -ne '{sha}') {{ Write-Host \"windows_hand_test: {expected_head} is $head, not {sha}\"; exit {fail} }}",
        f"Write-Host 'windows_hand_test: testing {sha}" + (" plus uncommitted changes'" if patch is not None else "'"),
    ]
    lines += [
        "$ErrorActionPreference = 'Continue'",
        "$env:NX_TEST_T2_SUBSTRATE = 'none'",
        f"& {_ps_quote(LAB_RUN_WIN)} uv run --python 3.13 --frozen pytest "
        + " ".join(_ps_quote(a) for a in pytest_args)
        + " -p no:cacheprovider",
        "$code = $LASTEXITCODE",
    ]
    if floor is not None:
        lines += [
            "# CI's non-vacuity floor step, verbatim from the workflow:",
            floor.rstrip(),
            f"if ($code -eq 0 -and $LASTEXITCODE -ne 0) {{ $code = {FLOOR_FAILED} }}",
        ]
    lines.append("exit $code")
    return "\n".join(lines) + "\n"


def _say(text: str, *, err: bool = False) -> None:
    (sys.stderr if err else sys.stdout).write(text + "\n")


def _git(run: Runner, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, encoding="utf-8", check=check)


def uncommitted_patch(run: Runner, repo: Path = REPO_ROOT) -> bytes:
    """The working tree's changes against HEAD as one binary patch: tracked
    edits (staged or not) and untracked files that are not ignored. Empty
    when the tree is clean."""
    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return run(["git", "-C", str(repo), *args], capture_output=True, check=check)

    # Fixed diff options: a caller's diff.noprefix, diff.external or a textconv
    # driver would otherwise change the patch's shape (review r1).
    opts = ("--binary", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/")
    parts = [git("diff", *opts, "HEAD").stdout]
    for raw in git("ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0"):
        if raw:
            # --no-index exits 1 when the files differ, which they always do here.
            parts.append(git("diff", *opts, "--no-index", "--", "/dev/null", os.fsdecode(raw), check=False).stdout)
    return b"".join(parts)


def _in_origin_develop(sha: str, run: Runner) -> bool:
    """True only when *sha* is in origin/develop, the one ref the remote is
    sure to have after ``git fetch origin``: a feature branch's tracking ref
    can be stale or name another remote."""
    return _git(run, "merge-base", "--is-ancestor", sha, "origin/develop", check=False).returncode == 0


def _check(run: Runner) -> int:
    changed = _git(run, "diff", "--name-only", "origin/develop...HEAD").stdout.split()
    hits = files_needing_windows(changed)
    if hits:
        _say("windows_hand_test: this branch touches the Windows set's push filter; run it on Windows first:")
        for f in hits:
            _say(f"  {f}")
        return 1
    _say(f"windows_hand_test: none of {len(changed)} changed files is in the Windows set's push filter")
    return 0


def main(argv: Sequence[str] | None = None, *, run: Runner = subprocess.run,
         beat: Callable[[Sequence[str]], int] | None = None) -> int:
    """Exit with pytest's code, or SETUP_FAILED when any local or remote setup
    step fails, so a setup failure is never read as a test result. The run's
    ssh call goes through *beat* (default :func:`run_with_heartbeat`); with an
    injected *run* and no *beat* it goes through *run*, as tests want."""
    if beat is None and run is subprocess.run:
        beat = run_with_heartbeat
    try:
        return _main(argv, run=run, beat=beat)
    except (subprocess.CalledProcessError, OSError) as exc:
        _say(f"windows_hand_test: setup failed: {exc}", err=True)
        return SETUP_FAILED


def _main(argv: Sequence[str] | None, *, run: Runner, beat: Callable[[Sequence[str]], int] | None) -> int:
    ap = argparse.ArgumentParser(description="Run pytest on native Windows (qwent) for a commit.")
    ap.add_argument("--session", help="your session name: work goes under C:\\build\\hand\\<session>")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--ref", default="HEAD", help="the commit to test (default HEAD of this checkout)")
    ap.add_argument("--set", action="store_true", help="run CI's whole Windows test set and its floor")
    ap.add_argument("--check", action="store_true", help="say whether this branch needs a Windows run; exit 1 if so")
    ap.add_argument("--uncommitted", action="store_true",
                    help="also test the working tree's uncommitted changes (for an agent that cannot commit)")
    ap.add_argument("pytest_args", nargs="*", help="pytest arguments, after --")
    ns = ap.parse_args(argv)
    if ns.check:
        return _check(run)
    if not ns.session or not _SESSION_RE.fullmatch(ns.session) or ns.session.lower() in _WINDOWS_DEVICE_NAMES:
        raise SystemExit("windows_hand_test: --session must be 1-40 ASCII letters, digits, - or _, and not a device name")
    args = (ci_set_args() if ns.set else []) + [_check_pytest_arg(a) for a in ns.pytest_args]
    if not args:
        raise SystemExit("windows_hand_test: give pytest arguments after --, or --set")

    sha = _git(run, "rev-parse", "--verify", f"{ns.ref}^{{commit}}").stdout.strip()
    if ns.uncommitted and ns.ref != "HEAD":
        raise SystemExit("windows_hand_test: --uncommitted applies to HEAD's working tree; drop --ref")
    patch_bytes = uncommitted_patch(run) if ns.uncommitted else b""
    if ns.uncommitted and not patch_bytes:
        raise SystemExit("windows_hand_test: --uncommitted given but the tree has no uncommitted changes")
    if len(patch_bytes) > MAX_PATCH_BYTES:
        raise SystemExit(
            f"windows_hand_test: the uncommitted changes come to {len(patch_bytes)} bytes, over the "
            f"{MAX_PATCH_BYTES} cap; an untracked dump is likely (git status shows it), commit or ignore it"
        )
    if patch_bytes:
        files = patch_bytes.count(b"\ndiff --git ") + patch_bytes.startswith(b"diff --git ")
        _say(f"windows_hand_test: sending {files} changed file(s), {len(patch_bytes)} bytes of uncommitted changes")
    if not ns.uncommitted and ns.ref == "HEAD" and _git(run, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        _say("windows_hand_test: the tree has uncommitted changes; they are NOT tested "
             "(commit them, or pass --uncommitted to include them)", err=True)
    run(["ssh", ns.host, f"New-Item -ItemType Directory -Force 'C:\\build\\hand\\{ns.session}' | Out-Null"], check=True)
    bundle_remote: str | None = None
    with tempfile.TemporaryDirectory() as tmp:
        if not _in_origin_develop(sha, run):
            local = Path(tmp) / "win-hand.bundle"
            # A bundle carries refs, not bare shas: name the commit under a
            # per-session ref for the bundle's life, then always drop the ref.
            tmp_ref = f"refs/win-hand/{ns.session}"
            _git(run, "update-ref", tmp_ref, sha)
            try:
                _git(run, "bundle", "create", "-q", str(local), tmp_ref, "^origin/develop")
            finally:
                _git(run, "update-ref", "-d", tmp_ref, check=False)
            bundle_remote = rf"C:\build\hand\{ns.session}\win-hand.bundle"
            run(["scp", "-q", str(local), f"{ns.host}:C:/build/hand/{ns.session}/win-hand.bundle"], check=True)
        patch_remote: str | None = None
        if patch_bytes:
            local_patch = Path(tmp) / "win-hand.patch"
            local_patch.write_bytes(patch_bytes)
            patch_remote = rf"C:\build\hand\{ns.session}\win-hand.patch"
            run(["scp", "-q", str(local_patch), f"{ns.host}:C:/build/hand/{ns.session}/win-hand.patch"], check=True)
        script = remote_script(
            ns.session, sha, args, bundle=bundle_remote,
            fixup=ci_step_run(FIXUP_STEP), floor=ci_step_run(FLOOR_STEP) if ns.set else None,
            patch=patch_remote,
        )
        local_ps1 = Path(tmp) / "win-hand.ps1"
        local_ps1.write_text(script, encoding="utf-8")
        run(["scp", "-q", str(local_ps1), f"{ns.host}:C:/build/hand/{ns.session}/win-hand.ps1"], check=True)
    # qwent's ssh shell is Windows PowerShell 5.1, which turns a child's exit
    # code other than 0 or 1 into 1 unless it is passed on explicitly.
    remote_ps1 = rf"C:\build\hand\{ns.session}\win-hand.ps1"
    # An unset $LASTEXITCODE (pwsh never started) would exit 0: count it as setup.
    ssh_argv = ["ssh", ns.host,
                f"$env:{HEARTBEAT_ENV} = '1'; pwsh -NoProfile -File '{remote_ps1}'; "
                f"if ($LASTEXITCODE -eq $null) {{ exit {SETUP_FAILED} }} else {{ exit $LASTEXITCODE }}"]
    if beat is not None:
        return beat(ssh_argv)
    return run(ssh_argv, check=False).returncode


def end_on_signal() -> None:
    """SIGTERM and SIGHUP end the client through SystemExit (nexus-bj8e7): :func:`run_with_heartbeat` then kills its
    ssh child on the way out, and the remote run ends at once on end of file. Without it the default action ends
    Python at once; the ssh child's stdin still closes and the remote still ends, but the ssh process lingers."""
    def _exit(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):  # no SIGHUP on Windows
        if (sig := getattr(signal, name, None)) is not None:
            signal.signal(sig, _exit)


if __name__ == "__main__":
    end_on_signal()
    sys.exit(main())
