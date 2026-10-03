# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-u67ow: the e2e harness must not run on a bare ``python3`` of unknown age.

On hellmini ``/usr/bin/python3`` is 3.9.6. A harness module that used ``match`` (3.10+) made
the 2026-10-02 cut battery abort with "artifacts manifest does not verify against this tree
(SyntaxError ...)": a harness defect that read as a manifest mismatch, and no engine leg ran.
``tests/e2e/lib/python.sh`` now resolves one interpreter,
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
_EARLY_TOOLS = ("git", "dirname", "grep", "cat", "sed", "tr", "date", "mkdir", "rm", "wc", "head", "uname", "basename")


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
    release-battery.sh and this goes red (the battery then dies later, on a SyntaxError,
    with no mention of the interpreter)."""
    bindir = _curated_path(tmp_path, python3="3.9.6")
    r = _run_battery(bindir, "--plan")
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "is 3.9.6" in r.stderr, r.stderr
    assert "no Python 3.10 or newer found" in r.stderr, r.stderr
    assert "SyntaxError" not in r.stderr + r.stdout
    assert "does not verify against this tree" not in r.stderr + r.stdout
    assert "RELEASE BATTERY: work=" not in r.stdout, "the battery made a work dir before refusing"


def test_battery_with_a_qualifying_python_gets_past_the_resolver(tmp_path: Path) -> None:
    """Non-vacuity for the test above: the same PATH with a 3.12 python3.12 beside the 3.9
    python3 must NOT stop at the resolver: ``--plan`` makes its work directory (which the refusal
    above never does) and prints the leg plan."""
    bindir = _curated_path(tmp_path, python3="3.9.6", python3_12="3.12.4")
    r = _run_battery(bindir, "--plan")
    assert "no Python 3.10 or newer found" not in r.stderr, r.stderr
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "RELEASE BATTERY: work=" in r.stdout, r.stdout
    assert "PLAN lsg group" in r.stdout, r.stdout


# ── no bare python3 on the host side ─────────────────────────────────────────────────────────

#: Files whose python3 runs INSIDE a container image that pins its own interpreter (apt python3 plus
#: a uv-linked python3.12), or that is the resolver's own test. Whole-file, with the reason.
_WHOLE_FILE = {
    "hook-surface-shakeout/shakeout_in_container.sh": "runs inside the shakeout image (Dockerfile apt python3)",
    "lib/python_test.sh": "the resolver's own test: python3 is a stub's name there",
    "lib/python.sh": "the resolver: it names python3 as a candidate",
}
_REHEARSE_PREFIX = "migration-rehearsal/rehearse_"  # every rehearse_*.sh runs inside its container image

#: (file, line text fragment) -> why a bare python3 stays on that line. (The hooks.json text the
#: hook-surface-shakeout driver writes for its container quotes the name, so the pattern never
#: sees it; it needs no entry.)
_LINES = {
    ("local-service-gate.sh", "uv run python3 -c"): "runs in the repo's uv environment, not the host python",
    ("hook-surface-shakeout/run.sh", '"command": "python3 /home/nexus/turn_end.py"'): (
        "hooks.json text written for the shakeout container image"
    ),
}

#: Scripts that use "$E2E_PYTHON" without sourcing python.sh themselves.
_NO_OWN_SOURCE = {"scenarios/00_debug_load.sh": "sourced by run.sh, which resolves first"}

# Quote and hyphen are NOT in the lookbehind: `bash -c "python3 ..."`, `eval "python3 ..."` and
# `"${X:-python3}"` are all host-side calls (the last is a pre-change shape in a
# since-deleted gate). Text that merely names python3 goes in _LINES with the reason.
_BARE = re.compile(r"(?<![\w./$])python3(?![\w.=-])")


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


def _bare_hits(path: Path, rel: str, used: set[tuple[str, str]] | None = None) -> list[str]:
    """Code lines of ``path`` that name a bare python3 and are not allowlisted in _LINES."""
    hits: list[str] = []
    for i, ln in _code_lines(path):
        if not _BARE.search(ln):
            continue
        hit = next(((f, frag) for (f, frag) in _LINES if f == rel and frag in ln), None)
        if hit:
            if used is not None:
                used.add(hit)
            continue
        hits.append(f"{rel}:{i}: {ln.strip()[:120]}")
    return hits


def test_no_host_side_e2e_script_calls_a_bare_python3() -> None:
    offenders: list[str] = []
    used: set[tuple[str, str]] = set()
    scanned = 0
    for path in _host_scripts():
        rel = path.relative_to(E2E).as_posix()
        scanned += 1
        offenders += _bare_hits(path, rel, used)
    assert not offenders, (
        "bare python3 on the host side of the e2e harness (nexus-u67ow): route it through "
        '"$E2E_PYTHON" after `source tests/e2e/lib/python.sh; e2e_python_resolve`, or, for text '
        "that is not a host call, add it to _LINES with the reason:\n" + "\n".join(offenders)
    )
    # Non-vacuity: the scan looked at the harness, and every allowance still matches a line, so a
    # stale entry cannot go on excusing a line that was deleted or moved.
    assert scanned >= 37, f"only {scanned} host scripts scanned: the glob or the allowlist swallowed the tree"
    stale = sorted(set(_LINES) - used)
    assert not stale, f"allowlist entries that match no line any more: {stale}"


#: Each shape a host call can take that a name-boundary lookbehind once let through. Every one of
#: these must be flagged; the control lines must not be.
_SHAPES = [
    'bash -c "python3 -c \'print(1)\'"',
    'eval "python3 \"$script\" --check"',
    'out="$(printf x | "${PY:-python3}" -c \'import sys\')"',
    'out="$(python3 -c \'print(1)\')"',
    "python3 tool.py",
]
_CONTROLS = [
    '"$E2E_PYTHON" -c \'print(1)\'',
    'out="$("${PY:-$E2E_PYTHON}" -c \'print(1)\')"',
    "/usr/bin/python3 tool.py",   # an explicit path is a deliberate choice, not a PATH lookup
    "uv run python3.12 -c x",
    "# python3 in a comment",
]


@pytest.mark.parametrize("shape", _SHAPES)
def test_the_lint_flags_every_shape_of_a_bare_python3(tmp_path: Path, shape: str) -> None:
    """The lint's own falsifiability: a temp script carrying one shape is flagged by the same
    function the tree scan uses. Mutation: put a quote or a hyphen back in _BARE's lookbehind and
    the bash -c / eval / ${X:-python3} cases go green-on-the-bug, i.e. this test goes red."""
    script = tmp_path / "gate.sh"
    script.write_text(f"#!/usr/bin/env bash\n{shape}\n")
    assert _bare_hits(script, "gate.sh"), f"not flagged: {shape}"


@pytest.mark.parametrize("control", _CONTROLS)
def test_the_lint_passes_the_resolved_and_explicit_forms(tmp_path: Path, control: str) -> None:
    script = tmp_path / "gate.sh"
    script.write_text(f"#!/usr/bin/env bash\n{control}\n")
    assert not _bare_hits(script, "gate.sh"), f"flagged but fine: {control}"


_SOURCE_LINE = re.compile(r"^\s*(?:source|\.)\s+.*\bpython\.sh\b")
_RESOLVE_CALL = re.compile(r"\be2e_python_resolve\b")


def _first(path: Path, pred) -> int | None:
    return next((i for i, ln in _code_lines(path) if pred(ln)), None)


def test_the_sweep_actually_routed_the_calls_through_the_resolver() -> None:
    """A lint that only forbids a spelling passes on a tree where the calls were deleted. Count the
    routed sites, and require, per script, that it sources python.sh, calls the resolver, and does
    both BEFORE the first line that uses $E2E_PYTHON."""
    routed = 0
    problems: list[str] = []
    for path in _host_scripts():
        rel = path.relative_to(E2E).as_posix()
        text = "\n".join(ln for _, ln in _code_lines(path))
        routed += text.count("$E2E_PYTHON")
        if "$E2E_PYTHON" not in text or rel in _NO_OWN_SOURCE:
            continue
        src = _first(path, _SOURCE_LINE.search)
        res = _first(path, lambda ln: bool(_RESOLVE_CALL.search(ln)) and not _SOURCE_LINE.search(ln))
        use = _first(path, lambda ln: "$E2E_PYTHON" in ln)
        if src is None or res is None:
            problems.append(f"{rel}: uses $E2E_PYTHON but never sources lib/python.sh and calls e2e_python_resolve")
        elif not (src < res <= use):
            problems.append(f"{rel}: first use at line {use} precedes source (line {src}) / resolve (line {res})")
    assert routed >= 47, f"only {routed} routed call sites; the sweep was undone"
    assert not problems, "\n".join(problems)


def test_the_ordering_check_catches_a_use_before_the_resolve(tmp_path: Path) -> None:
    """The ordering predicate above is not a tautology: on a script that uses $E2E_PYTHON on a line
    before it resolves, source < resolve <= use is false."""
    bad = tmp_path / "bad.sh"
    bad.write_text('"$E2E_PYTHON" -c x\nsource "$R/tests/e2e/lib/python.sh"\ne2e_python_resolve || exit 2\n')
    src = _first(bad, _SOURCE_LINE.search)
    res = _first(bad, lambda ln: bool(_RESOLVE_CALL.search(ln)) and not _SOURCE_LINE.search(ln))
    use = _first(bad, lambda ln: "$E2E_PYTHON" in ln)
    assert (src, res, use) == (2, 3, 1)
    assert not (src < res <= use)


@pytest.mark.parametrize("rel", sorted(_NO_OWN_SOURCE))
def test_the_scripts_that_do_not_resolve_themselves_are_sourced_by_one_that_does(rel: str) -> None:
    """run.sh resolves, then sources every scenario in a loop. Checked on code lines only (the
    header comment names the scenarios too), in order: resolve, then the scenario loop, and the
    loop's body calls a function that ``source``s its argument."""
    run_sh = E2E / "run.sh"
    resolve = _first(run_sh, lambda ln: bool(_RESOLVE_CALL.search(ln)) and not _SOURCE_LINE.search(ln))
    loop = _first(run_sh, lambda ln: ln.lstrip().startswith("for ") and "/scenarios/" in ln)
    sourcing = _first(run_sh, lambda ln: re.match(r"^\s*source\s+\"\$file\"\s*$", ln) is not None)
    assert resolve is not None, "run.sh no longer calls e2e_python_resolve"
    assert loop is not None, f"run.sh no longer loops over scenarios/, so {rel} would never run"
    assert sourcing is not None, f'run.sh no longer does `source "$file"`, so {rel} would never be sourced'
    assert resolve < loop, "run.sh sources the scenarios before it resolves an interpreter"
    assert (E2E / rel).is_file()
