# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""No tracked script fetches the Claude Code OAuth Keychain item with a bare,
unscoped ``security find-generic-password`` outside the one shared picker
(nexus-galkv.19).

THE INCIDENT. More than one macOS Keychain item can carry the service name
``Claude Code-credentials`` — on this box an ``acct="unknown"`` item is an
empty husk (``accessToken ""``, ``refreshToken ""``, ``expiresAt 0``)
alongside the live item the CLI actually refreshes. ``security
find-generic-password -s 'Claude Code-credentials' -w`` with no ``-a``
returns an ARBITRARY match, not necessarily the live one.
``tests/cc-validation/runner.sh`` fixed this for its own use on 2026-08-28
(nexus-qs1g6) by picking the credential BY CONTENT (enumerate every account
under the service, reject anything token-less or expired-without-a-refresh,
take the freshest survivor) instead of trusting the first match — but that
fix was never shared. On 2026-09-15 two more call sites were found making
the identical bare, unscoped call: ``tests/e2e/auth-login.sh`` (which then
wrote the arbitrary match over its own fallback snapshot) and the
``--fullstack``/``--shakeout-e2e`` legs of
``tests/e2e/migration-rehearsal/run.sh`` (which mount it into a container).

THE FIX. One shared picker, ``tests/e2e/lib/claude_credentials.py``
(``pick`` / ``check FILE``), used by all three call sites plus
``tests/cc-validation/runner.sh``'s ``_cred_tool`` wrapper.

THIS LINT holds the line: no tracked ``.sh`` or ``.py`` file under
``tests/`` or ``scripts/`` — other than the shared picker itself — may call
``find-generic-password`` naming the ``Claude Code-credentials`` service.
Prose (a comment, a docstring) describing the incident or the forbidden
shape is fine; only a live, non-comment CALL is flagged, the same
comment-exclusion convention ``test_no_git_config_global_writes_lint.py``
uses for the sibling global git-config write incident (nexus-oqh4s).
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The one file allowed to actually make this call.
_SHARED_TOOL = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_credentials.py"

#: A non-comment line that both invokes `find-generic-password` and names
#: the `Claude Code-credentials` service, in either quoting style, on the
#: literal same line -- the exact incident shape (`security
#: find-generic-password -s 'Claude Code-credentials' -w` / `-s "Claude
#: Code-credentials"`). Quotes/brackets/commas are normalized to whitespace
#: first (same technique as the git-config-global lint) so a Python argv
#: list literal (`["security", "find-generic-password", "-s", "Claude
#: Code-credentials", "-w"]`) is matched by the identical pattern.
_NOISE_RE = re.compile(r"""["'\[\],]""")
_CALL_RE = re.compile(r"\bfind-generic-password\b.*\bClaude Code-credentials\b")


def _normalize(line: str) -> str:
    return _NOISE_RE.sub(" ", line)


def _violations(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if _CALL_RE.search(_normalize(line)):
            hits.append(line.strip())
    return hits


def _git_tracked(pattern: str) -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", pattern],
        check=True, capture_output=True, text=True,
    ).stdout
    return sorted(REPO_ROOT / rel for rel in out.split("\0") if rel)


def _tracked_corpus() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for pattern in ("tests/**/*.sh", "tests/**/*.py", "scripts/**/*.sh", "scripts/**/*.py"):
        files.extend(_git_tracked(pattern))
    return sorted(
        p for p in {f.resolve() for f in files}
        if ".git" not in p.parts and p != _SHARED_TOOL.resolve()
    )


def test_scan_is_non_vacuous() -> None:
    """A broken glob (wrong root, wrong pattern) would make the real
    assertion below pass on an empty set -- pin a floor so that reads as a
    failure, not a clean bill of health."""
    corpus = _tracked_corpus()
    assert len(corpus) >= 100, (
        f"only found {len(corpus)} tracked .sh/.py files under tests/ and "
        f"scripts/ -- the scan may be broken rather than the repo genuinely "
        "shrinking that far"
    )


def test_detector_flags_the_pre_fix_auth_login_shape() -> None:
    """Kill control, proved on a synthetic fixture -- never on a real repo
    file, so this can never pass vacuously because the tree happens to
    already be clean. Reproduces the EXACT line ``tests/e2e/auth-login.sh``
    carried before this fix (double-quoted service argument)."""
    synthetic = (
        'creds=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || true)\n'
    )
    hits = _violations(synthetic)
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_the_pre_fix_run_sh_shape() -> None:
    """The single-quoted variant `tests/e2e/migration-rehearsal/run.sh`
    carried in both the --fullstack and --shakeout-e2e legs before this
    fix."""
    synthetic = (
        "FRESHCREDS=\"$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null || true)\"\n"
    )
    hits = _violations(synthetic)
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_a_python_subprocess_list_argv() -> None:
    """The same call assembled as a Python list-literal argv, never a
    contiguous shell substring, must be caught by the same pattern."""
    synthetic = (
        'subprocess.run(["security", "find-generic-password", "-s", '
        '"Claude Code-credentials", "-w"])\n'
    )
    hits = _violations(synthetic)
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_ignores_comment_lines() -> None:
    """A comment documenting the forbidden shape (exactly what the three
    fixed files now carry) must not itself trip this lint."""
    synthetic = (
        "# never a bare, unscoped `security find-generic-password -s "
        "'Claude Code-credentials' -w` -- see CRED_TOOL note above\n"
    )
    assert _violations(synthetic) == []


def test_detector_ignores_the_shared_tool_own_call_shape() -> None:
    """The shared tool's own real call builds the service name as an
    argv element referencing the module-level `SERVICE` constant, not the
    literal string on the same line as `find-generic-password` -- this
    proves the detector does not accidentally flag that shape too (it is
    additionally exempted by path in `_tracked_corpus`, but the shape
    itself is also naturally clean)."""
    synthetic = (
        'SERVICE = "Claude Code-credentials"\n'
        'cmd = ["security", "find-generic-password", "-s", SERVICE]\n'
    )
    assert _violations(synthetic) == []


def test_shared_tool_exists_and_is_excluded_from_the_scan() -> None:
    assert _SHARED_TOOL.is_file(), (
        f"expected the shared picker at {_SHARED_TOOL} -- if it moved, "
        "update _SHARED_TOOL here too"
    )
    assert _SHARED_TOOL.resolve() not in {p for p in _tracked_corpus()}


def test_no_tracked_script_bypasses_the_shared_picker() -> None:
    bad: list[str] = []
    for path in _tracked_corpus():
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in _violations(text):
            bad.append(f"{path.relative_to(REPO_ROOT)}: {line}")
    assert not bad, (
        "tracked file(s) call `find-generic-password` naming the "
        "'Claude Code-credentials' service directly -- this can silently "
        "select an arbitrary keychain item, including a token-less husk "
        "(nexus-galkv.19, nexus-qs1g6): route through "
        "tests/e2e/lib/claude_credentials.py's `pick`/`check` instead:\n"
        + "\n".join(bad)
    )
