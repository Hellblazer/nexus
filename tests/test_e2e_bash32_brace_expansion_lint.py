# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""No shell script passes an inline JSON builder as an argument word that macOS
``/bin/bash`` 3.2 brace-expands.

nexus-ecjo8: ``tests/e2e/local-service-gate.sh`` built every smoke-leg body as

    smoke_request POST /path \\
      "$("$E2E_PYTHON" -c "import json;print(json.dumps({'a':'x','b':'y'}))")"

Under ``/bin/bash`` 3.2.57 (what ``env bash`` resolves to on a Mac without a
newer bash first on PATH) the double-quoted program inside a ``"$( )"`` that
sits in ARGUMENT position is brace-expanded, so python receives three broken
programs and the substitution yields ``""``. The engine saw a POST with no body;
``/owners/upsert`` took the auto-mint path with a null ``name`` and answered
409 ``sqlstate 23502``. Bash 5 does not do this, so the gate passed wherever a
newer bash came first. The same text as an assignment right-hand side is not
expanded, which is the shape the gate uses now (``SMOKE_BODY="$(...)"``, then
``"$SMOKE_BODY"``).

Two pins:

* a static scan of every ``*.sh`` under ``tests/e2e`` and ``scripts`` for the
  hazard shape (portable, runs everywhere);
* where ``/bin/bash`` is 3.x, every ``SMOKE_BODY=`` builder in the gate is run
  under it and must print a JSON object, with a positive control proving the
  old inline form really does come back empty on this host.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_DIRS = (REPO_ROOT / "tests" / "e2e", REPO_ROOT / "scripts")
_GATE = REPO_ROOT / "tests" / "e2e" / "local-service-gate.sh"

#: A double-quoted segment (no escaped quotes in the shapes this scans).
_DQ_SEGMENT = re.compile(r'"([^"]*)"')


def has_brace_list(segment: str) -> bool:
    """True when ``segment`` holds a ``{...}`` with a comma at its own depth.

    That is a bash brace list, which bash 3.2 expands inside an
    argument-position ``"$( )"``. Nesting counts (``{'a':[{'b':1}],'c':2}`` is
    one list), and ``${...}`` is a parameter expansion, not a list.
    """
    # One entry per open brace: None for a ${...} expansion, else whether a
    # comma has been seen at that brace's own depth.
    stack: list[bool | None] = []
    for i, ch in enumerate(segment):
        if ch == "{":
            stack.append(None if i > 0 and segment[i - 1] == "$" else False)
        elif ch == "}" and stack:
            if stack.pop() is True:
                return True
        elif ch == "," and stack and stack[-1] is not None:
            stack[-1] = True
    return False

#: The gate's body builders, one per line, in the safe assignment form.
_BODY_ASSIGNMENT = re.compile(r'^\s*SMOKE_BODY=("\$\(.*\)")\s*$')

_GATE_BODY_BUILDERS = 9


def hazard_lines(text: str) -> list[tuple[int, str]]:
    """Lines where a ``"$(`` in argument position holds a brace list in double quotes.

    A ``"$(`` directly after ``=`` is an assignment right-hand side and is safe.
    Comment lines are skipped.
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r'"\$\(', line):
            if line[: m.start()].endswith("="):
                continue
            if any(has_brace_list(s) for s in _DQ_SEGMENT.findall(line[m.end():])):
                hits.append((lineno, line.strip()))
                break
    return hits


def _shell_scripts() -> list[Path]:
    return sorted(p for d in _SCAN_DIRS for p in d.rglob("*.sh"))


def test_scan_reaches_the_gate() -> None:
    scripts = _shell_scripts()
    assert _GATE in scripts, "the scan must include local-service-gate.sh"
    assert len(scripts) >= 20, f"scan found only {len(scripts)} scripts; the roots moved"


def test_detector_flags_the_pre_fix_shape_and_passes_the_fixed_one() -> None:
    old = (
        'smoke_request POST /v1/catalog/owners/upsert \\\n'
        '  "$("$E2E_PYTHON" -c "import json;print(json.dumps('
        "{'tumbler_prefix':'$P','name':'gate-smoke-owner','owner_type':'gate_smoke'}))\")\"\n"
    )
    new = (
        'SMOKE_BODY="$("$E2E_PYTHON" -c "import json;print(json.dumps('
        "{'tumbler_prefix':'$P','name':'gate-smoke-owner','owner_type':'gate_smoke'}))\")\"\n"
        'smoke_request POST /v1/catalog/owners/upsert "$SMOKE_BODY"\n'
    )
    single_quoted = 'run_j x 1 y "$(reaper_body 0 \'{"a":3,"b":2}\')"\n'
    nested = (
        '  f "$("$E2E_PYTHON" -c "import json;print(json.dumps('
        "{'ids':['$C'],'metadatas':[{'source':'s'}]}))\")\"\n"
    )
    param = 'f "$(printf "%s" "${A},${B}")"\n'
    assert [n for n, _ in hazard_lines(old)] == [2]
    assert [n for n, _ in hazard_lines(nested)] == [1], "a list whose comma sits outside the inner braces"
    assert hazard_lines(param) == [], "${...} is a parameter expansion, not a brace list"
    assert hazard_lines(new) == []
    assert hazard_lines(single_quoted) == [], "single-quoted braces are not expanded"


def test_no_script_has_an_argument_position_brace_list_builder() -> None:
    found = {
        str(p.relative_to(REPO_ROOT)): hits
        for p in _shell_scripts()
        if (hits := hazard_lines(p.read_text(encoding="utf-8", errors="replace")))
    }
    assert not found, (
        "argument-position \"$( ... \"...{a,b}...\" )\" is brace-expanded by macOS "
        "/bin/bash 3.2 and comes back empty; assign it to a variable first "
        f"(nexus-ecjo8): {found}"
    )


def _bash3() -> Path | None:
    bash = Path("/bin/bash")
    if not bash.exists():
        return None
    out = subprocess.run(
        [str(bash), "-c", 'echo "${BASH_VERSINFO[0]}"'],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return bash if out == "3" else None


#: Non-vacuity bound for the bash 3.x skip below (tests/test_skip_bound_lint.py):
#: the behavioural half may skip only where /bin/bash is not 3.x, which is Linux
#: CI; on macOS, the platform whose system shell is 3.2 and the one this guards,
#: it must run, so the number of macOS skips allowed is zero.
BASH32_MAX_SKIP_ON_DARWIN: int = 0


def test_the_bash32_behavioural_half_runs_on_macos() -> None:
    if sys.platform != "darwin":
        return
    skips = 0 if _bash3() is not None else 1
    assert skips <= BASH32_MAX_SKIP_ON_DARWIN, (
        "/bin/bash is not 3.x on this macOS host, so the bash 3.2 behavioural check would skip "
        "on the one platform it exists for"
    )


_STUB_VARS = (
    "SMOKE_UID=4242\n"
    'SMOKE_OWNER_PREFIX="9.$SMOKE_UID"\n'
    'SMOKE_TITLE="gate-smoke-doc-$SMOKE_UID"\n'
    "SMOKE_DOC_TUMBLER=9.4242.1\n"
    "SMOKE_VEC_COLLECTION=knowledge__gate-smoke__bge-base-en-v15-768__v1\n"
    "SMOKE_CHASH=" + "a" * 64 + "\n"
    "SMOKE_ORPHAN=" + "b" * 64 + "\n"
    'SMOKE_CHUNK_TEXT="gate smoke chunk $SMOKE_UID"\n'
)


@pytest.mark.skipif(_bash3() is None, reason="/bin/bash is not bash 3.x on this host")
def test_gate_body_builders_produce_json_under_bash32() -> None:
    bash = _bash3()
    assert bash is not None
    builders = [
        m.group(1)
        for line in _GATE.read_text(encoding="utf-8").splitlines()
        if (m := _BODY_ASSIGNMENT.match(line))
    ]
    assert len(builders) == _GATE_BODY_BUILDERS, (
        f"expected {_GATE_BODY_BUILDERS} SMOKE_BODY builders in the gate, found {len(builders)}"
    )

    def run(script: str) -> str:
        return subprocess.run(
            [str(bash), "-c", script],
            capture_output=True, text=True, check=True,
            env={"PATH": "/usr/bin:/bin", "E2E_PYTHON": sys.executable},
        ).stdout

    # Positive control: on this host the pre-fix argument form really is empty,
    # so a pass below is evidence about bash 3.2, not about a bash that never
    # had the defect.
    control = run(
        _STUB_VARS
        + 'show() { printf "%s" "${3:-}"; }\n'
        + "show POST /x " + builders[0] + "\n"
    )
    assert control == "", f"bash 3.2 control did not reproduce the empty body: {control!r}"

    for builder in builders:
        out = run(_STUB_VARS + "SMOKE_BODY=" + builder + '\nprintf "%s" "$SMOKE_BODY"\n')
        body = json.loads(out)
        assert isinstance(body, dict) and body, f"builder produced {out!r}: {builder}"
