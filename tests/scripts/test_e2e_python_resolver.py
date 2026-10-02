# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-u67ow: the e2e harness must not run on a bare ``python3`` of unknown age.

On hellmini ``/usr/bin/python3`` is 3.9.6. ``tests/e2e/lib/artifact_manifest.py`` uses
``match`` (3.10+), so the 2026-10-02 cut battery aborted with "artifacts manifest does not
verify against this tree (SyntaxError ...)": a harness defect that read as a manifest
mismatch, and no engine leg ran. ``tests/e2e/lib/python.sh`` now resolves one interpreter,
checks its version and refuses by name when none qualifies; ``release-battery.sh`` and the
gates source it. The resolver's own cases are ``tests/e2e/lib/python_test.sh`` (wired in
``test_shell_suite_wiring.py``). This file pins the two things that suite cannot:

* the real battery script, started with a PATH whose ``python3`` reports 3.9.6, stops at
  once with the version in the message (not a SyntaxError, not a manifest mismatch, and
  before it has made a work directory);
* no host-side e2e script goes back to calling a bare ``python3``.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"
BATTERY = E2E / "release-battery.sh"
RESOLVER = E2E / "lib" / "python.sh"

# Tools the battery runs before it reaches the resolver. A PATH made only of these (as
# symlinks) plus a stub python3 cannot find any other interpreter, so the test does not
# depend on what the box happens to have installed.
_EARLY_TOOLS = ("git", "dirname", "grep", "cat", "sed", "tr", "date", "mkdir", "wc", "head", "uname", "basename")


def _curated_path(tmp_path: Path, **interpreters: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in _EARLY_TOOLS:
        found = shutil.which(tool)
        assert found, f"{tool} is not on this box's PATH; the fixture cannot be built"
        (bindir / tool).symlink_to(found)
    for name, version in interpreters.items():
        stub = bindir / name.replace("_", ".")
        # Answers only the resolver's version probe; any other program text prints nothing, so a
        # battery that gets past the resolver cannot mistake the stub's output for real output.
        stub.write_text(f'#!/bin/sh\ncase "$2" in *version_info*) echo {version} ;; esac\n')
        stub.chmod(0o755)
    return bindir


def _run_battery(bindir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(BATTERY), *args],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        env={"PATH": str(bindir), "HOME": str(bindir.parent), "NX_BATTERY_ALLOW_DEVELOP": "1"},
    )


def test_battery_with_a_3_9_python3_stops_at_once_naming_the_version(tmp_path: Path) -> None:
    """The hellmini case. Mutation: delete the ``e2e_python_resolve || exit 2`` line from
    release-battery.sh and this goes red (the battery then dies later, on a SyntaxError or a
    missing REQUIRED_ENGINE, with no mention of the interpreter)."""
    bindir = _curated_path(tmp_path, python3="3.9.6")
    r = _run_battery(bindir, "--cut", "--plan")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "is 3.9.6" in r.stderr, r.stderr
    assert "no Python 3.10 or newer found" in r.stderr, r.stderr
    assert "SyntaxError" not in r.stderr + r.stdout
    assert "does not verify against this tree" not in r.stderr + r.stdout
    assert "RELEASE BATTERY: work=" not in r.stdout, "the battery made a work dir before refusing"


def test_battery_with_a_qualifying_python_gets_past_the_resolver(tmp_path: Path) -> None:
    """Non-vacuity for the test above: the same PATH with a 3.12 python3.12 beside the 3.9
    python3 must NOT stop at the resolver. The stub cannot run the REQUIRED_ENGINE parse, so
    the battery dies there, which is the proof it was past the resolver: it names
    REQUIRED_ENGINE_VERSION, not an interpreter."""
    bindir = _curated_path(tmp_path, python3="3.9.6", python3_12="3.12.4")
    r = _run_battery(bindir, "--cut", "--plan")
    assert "no Python 3.10 or newer found" not in r.stderr, r.stderr
    assert "could not parse REQUIRED_ENGINE_VERSION" in r.stderr, (r.returncode, r.stdout, r.stderr)


# ── no bare python3 on the host side ─────────────────────────────────────────────────────────

#: Files whose python3 runs INSIDE a container image that pins its own interpreter (apt python3 plus
#: a uv-linked python3.12), or that is the resolver's own test. Whole-file, with the reason.
_WHOLE_FILE = {
    "hook-surface-shakeout/shakeout_in_container.sh": "runs inside the shakeout image (Dockerfile apt python3)",
    "migration-rehearsal/lib/assert_build_ref.sh": "sourced inside a rehearsal container",
    "lib/python_test.sh": "the resolver's own test: python3 is a stub's name there",
    "lib/python.sh": "the resolver: it names python3 as a candidate",
    "post-publish-dispatch-check.sh": (
        "runs against an INSTALLED box with no checkout (its own header), so it cannot source a "
        "repo lib; its two inline snippets are stdlib-only and run on 3.9"
    ),
}
_REHEARSE_PREFIX = "migration-rehearsal/rehearse_"  # every rehearse_*.sh runs inside its container image

#: (file, line text fragment) -> why a bare python3 stays on that line. (The hooks.json text the
#: rdr208-mvv and hook-surface-shakeout drivers write for their containers quotes the name, so the
#: pattern never sees it; they need no entry.)
_LINES = {
    ("release-sandbox.sh", "(run: python3 tests/e2e/lib/claude_credentials.py status)"): "operator-facing message text",
    ("local-service-gate.sh", "uv run python3 -c"): "runs in the repo's uv environment, not the host python",
}

#: Scripts that use "$E2E_PYTHON" without sourcing python.sh themselves.
_NO_OWN_SOURCE = {"scenarios/00_debug_load.sh": "sourced by run.sh, which resolves first"}

_BARE = re.compile(r"(?<![\w./$\"'-])python3(?![\w.=-])")


def _host_scripts() -> list[Path]:
    out = []
    for p in sorted(E2E.rglob("*.sh")):
        rel = p.relative_to(E2E).as_posix()
        if rel in _WHOLE_FILE or rel.startswith(_REHEARSE_PREFIX):
            continue
        out.append(p)
    return out


def _code_lines(path: Path) -> list[tuple[int, str]]:
    return [(i, ln) for i, ln in enumerate(path.read_text().splitlines(), 1) if not ln.lstrip().startswith("#")]


def test_no_host_side_e2e_script_calls_a_bare_python3() -> None:
    offenders: list[str] = []
    used: set[tuple[str, str]] = set()
    scanned = 0
    for path in _host_scripts():
        rel = path.relative_to(E2E).as_posix()
        scanned += 1
        for i, ln in _code_lines(path):
            if not _BARE.search(ln):
                continue
            hit = next(((f, frag) for (f, frag) in _LINES if f == rel and frag in ln), None)
            if hit:
                used.add(hit)
                continue
            offenders.append(f"{rel}:{i}: {ln.strip()[:120]}")
    assert not offenders, (
        "bare python3 on the host side of the e2e harness (nexus-u67ow): route it through "
        '"$E2E_PYTHON" after `source tests/e2e/lib/python.sh; e2e_python_resolve`, or, for text '
        "that is not a host call, add it to _LINES with the reason:\n" + "\n".join(offenders)
    )
    # Non-vacuity: the scan looked at the harness, and every allowance still matches a line, so a
    # stale entry cannot go on excusing a line that was deleted or moved.
    assert scanned >= 40, f"only {scanned} host scripts scanned: the glob or the allowlist swallowed the tree"
    stale = sorted(set(_LINES) - used)
    assert not stale, f"allowlist entries that match no line any more: {stale}"


def test_the_sweep_actually_routed_the_calls_through_the_resolver() -> None:
    """A lint that only forbids a spelling passes on a tree where the calls were deleted. Count the
    routed sites, and require that every script using them resolves first."""
    routed = 0
    unresolved: list[str] = []
    for path in _host_scripts():
        rel = path.relative_to(E2E).as_posix()
        text = "\n".join(ln for _, ln in _code_lines(path))
        n = text.count("$E2E_PYTHON")
        routed += n
        if n and rel not in _NO_OWN_SOURCE and "python.sh" not in text:
            unresolved.append(rel)
    assert routed >= 100, f"only {routed} routed call sites; the sweep was undone"
    assert not unresolved, f"use $E2E_PYTHON but never source lib/python.sh: {unresolved}"


@pytest.mark.parametrize("rel", sorted(_NO_OWN_SOURCE))
def test_the_scripts_that_do_not_resolve_themselves_are_sourced_by_one_that_does(rel: str) -> None:
    run_sh = (E2E / "run.sh").read_text()
    assert "e2e_python_resolve" in run_sh
    assert "scenarios" in run_sh, f"run.sh no longer sources the scenarios, so {rel} would run with no resolved interpreter"


def test_the_resolver_floor_matches_the_one_module_that_needs_it() -> None:
    """artifact_manifest.py is the file whose ``match`` broke hellmini; the resolver's default floor
    must not fall below what that module needs."""
    assert re.search(r"^\s*match ", (E2E / "lib" / "artifact_manifest.py").read_text(), re.M)
    assert 'local min="${1:-10}"' in RESOLVER.read_text()
    assert os.access(RESOLVER, os.R_OK)
