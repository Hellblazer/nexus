# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit coverage for ``scripts/credential_janitor.py`` (RDR-219 Phase 3 Step
2b, nexus-wauo1.24) -- the release-battery leg that fails on any
credential-shaped file left under a harness-owned root.

THE DESIGN. Two independent sweeps, never conflated:

1. FILENAME sweep for the two known credential-artifact names
   (``.credentials.json``, ``.claude-credentials.json``) across every root,
   including the repo tree (enumerated via ``git ls-files``, matching this
   repo's existing ``test_claude_credentials_single_source_lint.py``
   convention) and ``$TMPDIR`` (bounded to ``--max-depth``, since a real
   ``$TMPDIR`` can carry tens of thousands of unrelated top-level entries
   -- nexus-wauo1.24's own comment measured an unbounded recursive grep
   there running unfinished for several minutes).
2. CONTENT sweep for the token pattern (``sk-ant-oat``/``sk-ant-ort``),
   restricted to TEXT files (binary excluded by a NUL-byte sniff, the same
   heuristic ``grep -I``/git use) and restricted to harness-owned roots
   only (the agent scratchpad dirs, ``$TMPDIR``'s ``*.artifacts``/
   ``rdr208-mvv.*`` stage folders, and ``~/nexus-sandbox``) -- deliberately
   NEVER the repo tree, because two tracked fixtures
   (``tests/test_claude_credentials.py``, ``tests/test_run_ladder_credentials.py``)
   carry a synthetic ``sk-ant-oat...`` token by design and must never fail
   this check.

Every root is passed explicitly so these tests touch no ambient machine
state (no real ``$TMPDIR``, no real ``~/nexus-sandbox``, no real
``/private/tmp/claude-*``) -- each is a ``tmp_path`` fixture directory.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "credential_janitor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("credential_janitor", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cj():
    return _load_module()


def _git_init(root: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


# ===========================================================================
# Filename sweep
# ===========================================================================


def test_planted_credentials_json_in_repo_tree_is_flagged(tmp_path, cj):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    bad = repo / "sub" / ".credentials.json"
    bad.parent.mkdir()
    bad.write_text('{"accessToken": "not-a-real-secret"}')
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert bad in result.findings


def test_planted_claude_credentials_json_in_repo_tree_is_flagged(tmp_path, cj):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    bad = repo / ".claude-credentials.json"
    bad.write_text('{"oauthAccount": "x"}')
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert bad in result.findings


def test_planted_credentials_json_under_tmpdir_artifacts_folder_is_flagged(tmp_path, cj):
    """Reproduces the exact shape the bead's scope-addition comment names:
    11 of 13 leftover files on 2026-09-25 were under
    ``hook-shakeout-*.artifacts/dot-claude/``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    bad = tmpdir / "hook-shakeout-abc123.artifacts" / "dot-claude" / ".credentials.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"accessToken": "not-a-real-secret"}')
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert bad in result.findings


def test_clean_roots_pass(tmp_path, cj):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hello\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert result.findings == []
    assert result.error is None


# ===========================================================================
# Content sweep -- harness-owned roots only
# ===========================================================================


def test_token_pattern_in_harness_owned_content_root_is_flagged(tmp_path, cj):
    """`content_roots` (`$TMPDIR`'s `*.artifacts`/`rdr208-mvv.*` stage
    folders, `~/nexus-sandbox`) are the small, bounded, self-contained
    roots that get the unbounded content sweep."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    artifacts = tmpdir / "hook-shakeout-abc.artifacts"
    artifacts.mkdir()
    leaked = artifacts / "session-log.txt"
    leaked.write_text("token was sk-ant-oat01-REALLOOKINGBUTFAKE0000000000000000\n")
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[artifacts],
        max_depth=4,
    )
    assert leaked in result.findings


def test_token_pattern_in_scratchpad_root_content_is_not_flagged(tmp_path, cj):
    """The agent scratchpad root (`/private/tmp/claude-*`) is a LIVE
    session working directory, not bounded harness output -- measured on
    this box at 65 GB for one session alone (nexus-wauo1.24
    implementation, 2026-09-25). It gets the filename sweep only, same as
    `$TMPDIR`; a token-shaped string in an ordinarily-named file there is
    a known, accepted gap, not a defect -- an unbounded content grep over
    a directory that size did not finish inside several minutes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    scratch = tmp_path / "claude-999"
    scratch.mkdir()
    leaked = scratch / "session-log.txt"
    leaked.write_text("token was sk-ant-oat01-REALLOOKINGBUTFAKE0000000000000000\n")
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[scratch], content_roots=[], max_depth=4,
    )
    assert leaked not in result.findings
    assert result.findings == []


def test_credentials_json_filename_in_scratchpad_root_is_still_flagged(tmp_path, cj):
    """The scratchpad root's bound is on CONTENT scanning only -- the
    cheap filename sweep still runs there, unchanged, bounded to
    `max_depth` same as `$TMPDIR`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    scratch = tmp_path / "claude-1000"
    bad = scratch / "dot-claude" / ".credentials.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"accessToken": "x"}')
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[scratch], content_roots=[], max_depth=4,
    )
    assert bad in result.findings


def test_token_pattern_ort_variant_is_flagged(tmp_path, cj):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    sandbox = tmp_path / "nexus-sandbox"
    sandbox.mkdir()
    leaked = sandbox / "export.json"
    leaked.write_text('{"refreshToken": "sk-ant-ort01-REALLOOKINGBUTFAKE00000000000"}')
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[sandbox], max_depth=4,
    )
    assert leaked in result.findings


def test_token_pattern_in_repo_tree_is_never_content_scanned(tmp_path, cj):
    """Kill control for the exact false-positive this design exists to
    avoid: a file shaped exactly like ``tests/test_claude_credentials.py``'s
    own synthetic fixture, living in the repo tree (never a harness-owned
    root), must NOT be flagged -- the repo tree gets the filename sweep
    only, never the content sweep."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    fixture = repo / "tests" / "test_claude_credentials.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        '_FAKE_TOKEN = "sk-ant-oat01-FAKE00000000000000000000000000000000000000000000"\n'
    )
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert result.findings == []


def test_binary_file_with_token_bytes_in_harness_root_is_not_flagged(tmp_path, cj):
    """Kill control for the other named benign match: the claude CLI binary
    itself embeds the token regex as bytes. A binary file (detected by a
    NUL byte in its first bytes, the same heuristic ``grep -I``/git use)
    must never be content-scanned, wherever it lands."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    sandbox = tmp_path / "nexus-sandbox"
    sandbox.mkdir()
    binary = sandbox / "nexus-service-binary"
    binary.write_bytes(b"\x7fELF\x00\x00\x00sk-ant-oat\x00\x01\x02\xff\xfe")
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[sandbox], max_depth=4,
    )
    assert binary not in result.findings
    assert result.findings == []


def test_content_scan_does_not_flood_from_filename_match(tmp_path, cj):
    """A harness-owned root's file that is BOTH named a credential file
    AND contains the token is reported exactly once."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    scratch = tmp_path / "claude-1"
    scratch.mkdir()
    bad = scratch / ".credentials.json"
    bad.write_text('{"accessToken": "sk-ant-oat01-REALLOOKINGBUTFAKE0000000000000"}')
    result = cj.scan(
        repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[scratch], content_roots=[], max_depth=4,
    )
    assert result.findings.count(bad) == 1


# ===========================================================================
# Non-vacuity: a required root that does not exist is a FAILURE, not a
# silent clean pass.
# ===========================================================================


def test_missing_repo_root_fails_loud_not_vacuously(tmp_path, cj):
    missing = tmp_path / "does-not-exist-repo"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = cj.scan(
        repo_root=missing, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert result.error is not None
    assert "does-not-exist-repo" in result.error


def test_missing_tmpdir_fails_loud_not_vacuously(tmp_path, cj):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    missing = tmp_path / "does-not-exist-tmp"
    result = cj.scan(
        repo_root=repo, tmpdir=missing, scratchpad_roots=[], content_roots=[], max_depth=4,
    )
    assert result.error is not None
    assert "does-not-exist-tmp" in result.error


def test_missing_optional_content_root_is_not_an_error(tmp_path, cj):
    """Scratchpad/sandbox roots are dynamic -- a session with none open, or
    a fresh box with no ``~/nexus-sandbox`` yet, is not a failure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    never_created = tmp_path / "claude-nonexistent"
    result = cj.scan(
        repo_root=repo,
        tmpdir=tmpdir,
        scratchpad_roots=[never_created],
        content_roots=[never_created],
        max_depth=4,
    )
    assert result.error is None
    assert result.findings == []


# ===========================================================================
# Never print contents -- file names only.
# ===========================================================================


def test_report_names_files_but_never_their_contents(tmp_path, cj):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    artifacts = tmpdir / "hook-shakeout-def.artifacts"
    artifacts.mkdir()
    secret_text = "sk-ant-oat01-THISEXACTSTRINGMUSTNEVERAPPEARONSTDOUT00000"
    leaked = artifacts / "leak.txt"
    leaked.write_text(f"token: {secret_text}\n")
    report = cj.format_report(
        cj.scan(
            repo_root=repo, tmpdir=tmpdir, scratchpad_roots=[], content_roots=[artifacts],
            max_depth=4,
        )
    )
    assert str(leaked) in report
    assert secret_text not in report


# ===========================================================================
# CLI wiring: main() prints the release-battery verdict line, exits
# non-zero on findings, and the status warning never fails the leg.
# ===========================================================================


def test_main_exits_nonzero_and_prints_failed_verdict_on_a_finding(tmp_path, cj, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    planted = repo / ".credentials.json"
    planted.write_text('{"accessToken": "x"}')  # a planted fixture, not a credential
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text("import sys\nsys.exit(1)\n")  # absent token -- must not fail the leg
    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "CREDENTIAL JANITOR FAILED" in out
    assert str(repo / ".credentials.json") in out


def test_main_exits_zero_and_prints_passed_verdict_on_a_clean_tree(tmp_path, cj, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hi\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text("import sys\nsys.exit(0)\n")
    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "CREDENTIAL JANITOR PASSED" in out


def test_main_status_warning_never_fails_the_leg_even_on_expired_token(tmp_path, cj, capsys):
    """`claude_credentials.py status` exits 2 on an expired token -- the
    bead's own text says this leg 'prints a warning, without failing'.
    The leg's own pass/fail must depend only on the scan, never on
    status's exit code."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hi\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text(
        "import sys\n"
        "print('expired -- created 2024-01-01, 40 day(s) ago')\n"
        "sys.exit(2)\n"
    )
    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "CREDENTIAL JANITOR PASSED" in out
    assert "expired" in out


def test_main_forwards_the_30_day_warning_line(tmp_path, cj, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hi\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text(
        "import sys\n"
        "print('present -- created 2026-08-27, 14 day(s) to expiry')\n"
        "print('warning: automation token expires in 14 day(s) -- run `claude setup-token` to renew')\n"
        "sys.exit(0)\n"
    )
    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "warning: automation token expires in 14 day(s)" in out


# ===========================================================================
# The token/filename constants are single-sourced.
# ===========================================================================


def test_token_pattern_matches_the_bead_documented_shapes(cj):
    assert cj.TOKEN_RE.search("sk-ant-oat01-abc")
    assert cj.TOKEN_RE.search("sk-ant-ort01-abc")
    assert not cj.TOKEN_RE.search("sk-ant-apiKey01-abc")


def test_credential_filenames_constant_has_both_known_shapes(cj):
    assert ".credentials.json" in cj.CREDENTIAL_FILENAMES
    assert ".claude-credentials.json" in cj.CREDENTIAL_FILENAMES


def test_protected_env_var_names_constant_has_both_known_shapes(cj):
    assert "CLAUDE_CODE_OAUTH_TOKEN" in cj.PROTECTED_ENV_VAR_NAMES
    assert "NX_HARNESS_CLAUDE_OAUTH_TOKEN" in cj.PROTECTED_ENV_VAR_NAMES


# ===========================================================================
# Process scan -- a live process holding a protected token in its
# environment, not merely a file on disk (T2 nexus_rdr/219-leftover-tmux-
# servers-2026-09-25: five orphaned tmux servers, each holding the
# automation token in process memory only, invisible to every sweep above).
# ===========================================================================


def test_process_scan_with_protected_var_fails_and_never_prints_value(cj):
    """macOS path: a fake `ps -Eww` line whose command text carries a
    protected env var, older than the threshold, is flagged -- and the
    matched VALUE never reaches the report, only pid/etime/comm."""
    secret = "FAKESECRETVALUE_MUST_NEVER_APPEAR_IN_OUTPUT_00000"
    fake_output = (
        f"12345 02:00:00 python3 /usr/bin/python3 NX_HARNESS_CLAUDE_OAUTH_TOKEN={secret} --foo bar\n"
    )
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids=set(), platform_name="darwin",
        ps_runner=lambda: fake_output,
    )
    assert len(findings) == 1
    assert findings[0].pid == 12345
    assert findings[0].comm == "python3"
    result = cj.ScanResult(process_findings=findings)
    report = cj.format_report(result)
    assert "pid=12345" in report
    assert "etime=02:00:00" in report
    assert "comm=python3" in report
    assert secret not in report
    assert "CREDENTIAL JANITOR FAILED" in report


def test_process_scan_younger_than_threshold_passes(cj):
    fake_output = "12345 00:05:00 python3 /usr/bin/python3 NX_HARNESS_CLAUDE_OAUTH_TOKEN=whatever\n"
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids=set(), platform_name="darwin",
        ps_runner=lambda: fake_output,
    )
    assert findings == []


def test_process_scan_without_a_protected_var_is_not_flagged(cj):
    fake_output = "12345 05:00:00 bash /bin/bash SOME_OTHER_VAR=x --foo bar\n"
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids=set(), platform_name="darwin",
        ps_runner=lambda: fake_output,
    )
    assert findings == []


def test_process_scan_excludes_a_pid_in_the_exclude_set(cj):
    """The janitor's own process and its ancestors are excluded by pid,
    passed in via `exclude_pids` -- `main()` populates this from
    `_own_ancestor_pids()`."""
    fake_output = "12345 02:00:00 python3 /usr/bin/python3 NX_HARNESS_CLAUDE_OAUTH_TOKEN=whatever\n"
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids={12345}, platform_name="darwin",
        ps_runner=lambda: fake_output,
    )
    assert findings == []


def test_own_ancestor_pids_includes_self(cj):
    assert os.getpid() in cj._own_ancestor_pids()


def _write_fake_proc_pid(
    proc_root: pathlib.Path, pid: int, *, comm: str, environ: bytes, starttime_ticks: int,
) -> None:
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir()
    (pid_dir / "comm").write_text(f"{comm}\n")
    (pid_dir / "environ").write_bytes(environ)
    # /proc/<pid>/stat: `pid (comm) state ppid ... starttime ...` -- starttime
    # is field 22 (1-indexed), i.e. index 19 of the tokens after the comm
    # parenthetical. Padded with enough trailing zero fields to be a
    # plausible stat line; only index 19 is ever read by this module.
    after = ["S", "1", str(pid), str(pid), "0", "-1", "4194304"] + ["0"] * 8 + [
        "20", "0", "1", "0", str(starttime_ticks),
    ] + ["0"] * 18
    (pid_dir / "stat").write_text(f"{pid} ({comm}) {' '.join(after)}\n")


def test_linux_process_scan_flags_old_protected_env_process_via_proc(tmp_path, cj):
    """Linux path: /proc/<pid>/environ read directly (NUL-separated), age
    derived from /proc/<pid>/stat's starttime against /proc/uptime -- no
    `ps` involved, matching this box class's actual read mechanism."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "uptime").write_text("100000.0 0.0\n")
    secret = b"FAKESECRETVALUE_MUST_NEVER_APPEAR_00000"
    _write_fake_proc_pid(
        proc_root, 54321, comm="python3",
        environ=b"PATH=/usr/bin\x00CLAUDE_CODE_OAUTH_TOKEN=" + secret + b"\x00",
        starttime_ticks=100,  # started ~1s after boot (clk_tck=100) -> age ~99999s
    )
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids=set(), platform_name="linux",
        proc_root=proc_root, clk_tck=100,
    )
    assert len(findings) == 1
    assert findings[0].pid == 54321
    assert findings[0].comm == "python3"
    report = cj.format_report(cj.ScanResult(process_findings=findings))
    assert secret.decode() not in report


def test_linux_process_scan_does_not_flag_a_young_process(tmp_path, cj):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "uptime").write_text("100000.0 0.0\n")
    _write_fake_proc_pid(
        proc_root, 54322, comm="python3",
        environ=b"CLAUDE_CODE_OAUTH_TOKEN=$NOT_A_LITERAL_FAKE_VALUE\x00",
        starttime_ticks=9999000,  # started ~99990s -> age ~10s, well under threshold
    )
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids=set(), platform_name="linux",
        proc_root=proc_root, clk_tck=100,
    )
    assert findings == []


def test_linux_process_scan_without_a_protected_var_is_not_flagged(tmp_path, cj):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "uptime").write_text("100000.0 0.0\n")
    _write_fake_proc_pid(
        proc_root, 54323, comm="bash",
        environ=b"PATH=/usr/bin\x00",
        starttime_ticks=100,
    )
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids=set(), platform_name="linux",
        proc_root=proc_root, clk_tck=100,
    )
    assert findings == []


def test_linux_process_scan_excludes_a_pid_in_the_exclude_set(tmp_path, cj):
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "uptime").write_text("100000.0 0.0\n")
    _write_fake_proc_pid(
        proc_root, 54324, comm="python3",
        environ=b"CLAUDE_CODE_OAUTH_TOKEN=$NOT_A_LITERAL_FAKE_VALUE\x00",
        starttime_ticks=100,
    )
    findings = cj.scan_processes(
        min_age_seconds=3600, exclude_pids={54324}, platform_name="linux",
        proc_root=proc_root, clk_tck=100,
    )
    assert findings == []


# ===========================================================================
# Tmux socket discovery -- a live tmux server on a known harness socket
# name is exactly the shape that stranded five orphaned servers for ~7
# hours (T2 nexus_rdr/219-leftover-tmux-servers-2026-09-25); each held the
# automation token in the server's own environment, invisible to any file
# sweep.
# ===========================================================================


def test_tmux_socket_matching_harness_pattern_is_flagged(tmp_path, cj):
    root = tmp_path / "tmux-501"
    root.mkdir()
    sock = root / "nexus-e2e-4242"
    sock.write_bytes(b"")
    old_time = time.time() - 3 * 3600
    os.utime(sock, (old_time, old_time))
    findings = cj.discover_tmux_sockets(
        roots=[root], min_age_seconds=7200, tmux_runner=lambda name: True,
    )
    assert len(findings) == 1
    assert findings[0].socket_name == "nexus-e2e-4242"


def test_tmux_socket_not_matching_any_pattern_is_not_flagged(tmp_path, cj):
    root = tmp_path / "tmux-501"
    root.mkdir()
    sock = root / "some-unrelated-socket"
    sock.write_bytes(b"")
    old_time = time.time() - 3 * 3600
    os.utime(sock, (old_time, old_time))
    findings = cj.discover_tmux_sockets(
        roots=[root], min_age_seconds=7200, tmux_runner=lambda name: True,
    )
    assert findings == []


def test_tmux_socket_younger_than_threshold_is_not_flagged(tmp_path, cj):
    root = tmp_path / "tmux-501"
    root.mkdir()
    sock = root / "cc-val-sock"
    sock.write_bytes(b"")  # mtime is "now" -- well under the threshold
    findings = cj.discover_tmux_sockets(
        roots=[root], min_age_seconds=7200, tmux_runner=lambda name: True,
    )
    assert findings == []


def test_tmux_socket_with_no_live_server_is_not_flagged(tmp_path, cj):
    """A stale socket FILE with no server behind it (`tmux -L <name> ls`
    fails) is not a finding -- there is nothing to kill and nothing holding
    the token."""
    root = tmp_path / "tmux-501"
    root.mkdir()
    sock = root / "release-sandbox-sock"
    sock.write_bytes(b"")
    old_time = time.time() - 3 * 3600
    os.utime(sock, (old_time, old_time))
    findings = cj.discover_tmux_sockets(
        roots=[root], min_age_seconds=7200, tmux_runner=lambda name: False,
    )
    assert findings == []


def test_tmux_socket_veh77_ladder_glob_variant_is_flagged(tmp_path, cj):
    root = tmp_path / "tmux-501"
    root.mkdir()
    sock = root / "veh77-ladder-9981"
    sock.write_bytes(b"")
    old_time = time.time() - 3 * 3600
    os.utime(sock, (old_time, old_time))
    findings = cj.discover_tmux_sockets(
        roots=[root], min_age_seconds=7200, tmux_runner=lambda name: True,
    )
    assert len(findings) == 1
    assert findings[0].socket_name == "veh77-ladder-9981"


def test_tmux_socket_report_never_prints_more_than_name_and_remedy(tmp_path, cj):
    root = tmp_path / "tmux-501"
    root.mkdir()
    sock = root / "shakeout-sock"
    sock.write_bytes(b"")
    old_time = time.time() - 3 * 3600
    os.utime(sock, (old_time, old_time))
    findings = cj.discover_tmux_sockets(
        roots=[root], min_age_seconds=7200, tmux_runner=lambda name: True,
    )
    report = cj.format_report(cj.ScanResult(tmux_findings=findings))
    assert "shakeout-sock" in report
    assert "kill-server" in report
    assert "CREDENTIAL JANITOR FAILED" in report


# ===========================================================================
# main() wiring: process/tmux findings reach the report and the exit code,
# via the injected `scan_processes`/`discover_tmux_sockets` module
# functions (so this test touches no real host process table).
# ===========================================================================


def test_main_reports_and_fails_on_a_process_finding(tmp_path, cj, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hi\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text("import sys\nsys.exit(0)\n")

    monkeypatch.setattr(
        cj, "scan_processes",
        lambda **kwargs: [cj.ProcessFinding(pid=999, etime="03:00:00", comm="tmux")],
    )
    monkeypatch.setattr(cj, "discover_tmux_sockets", lambda **kwargs: [])

    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "CREDENTIAL JANITOR FAILED" in out
    assert "pid=999" in out
    assert "comm=tmux" in out


def test_main_reports_and_fails_on_a_tmux_finding(tmp_path, cj, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hi\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text("import sys\nsys.exit(0)\n")

    monkeypatch.setattr(cj, "scan_processes", lambda **kwargs: [])
    monkeypatch.setattr(
        cj, "discover_tmux_sockets",
        lambda **kwargs: [
            cj.TmuxFinding(socket_name="nexus-e2e-777", socket_path=tmp_path / "nexus-e2e-777")
        ],
    )

    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "CREDENTIAL JANITOR FAILED" in out
    assert "nexus-e2e-777" in out
    assert "kill-server" in out


def test_main_passes_when_process_and_tmux_scans_find_nothing(tmp_path, cj, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("hi\n")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    fake_cred_tool = tmp_path / "fake_claude_credentials.py"
    fake_cred_tool.write_text("import sys\nsys.exit(0)\n")

    monkeypatch.setattr(cj, "scan_processes", lambda **kwargs: [])
    monkeypatch.setattr(cj, "discover_tmux_sockets", lambda **kwargs: [])

    rc = cj.main(
        [
            "--repo-root", str(repo),
            "--tmpdir", str(tmpdir),
            "--home", str(tmp_path / "home"),
            "--scratchpad-parent", str(tmp_path / "no-scratchpad-parent"),
            "--cred-tool", str(fake_cred_tool),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "CREDENTIAL JANITOR PASSED" in out
