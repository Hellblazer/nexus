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
import pathlib
import subprocess
import sys

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
