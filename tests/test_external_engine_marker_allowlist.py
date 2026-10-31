# SPDX-License-Identifier: AGPL-3.0-or-later
"""Only two files may carry ``external_engine_ok`` (nexus-ckejb, critic round 1 S3).

A test that carries the marker reads ``NX_TEST_EXTERNAL_ENGINE_URL`` and ``NX_TEST_EXTERNAL_TENANT_TOKEN`` and,
with both set, runs against that engine and tenant instead of the throwaway one. "Only the journey carries it" is
what keeps a stray exported pair from pointing anything else at production, and it was prose: nothing failed the
day a second test picked the marker up. This scan fails it.

The allowlist is exact in both directions. A file outside it that applies the marker fails, and a file inside it
that no longer carries it fails too, so a stale entry cannot sit there as an unexamined permission. The two
carriers are the journey (the one test that runs against a managed tenant) and the opt-in's own tests (which
mark their cases to drive the fixture body). ``tests/_external_engine.py`` defines the marker and reads it, and
this file names it; both are mechanism, not carriers.

A site is any non-comment line that spells ``external_engine_ok`` or reaches the constant
(``_external_engine.MARKER`` or an import of ``MARKER`` from that module). The scan is textual on purpose: an
AST pass would miss ``getattr(pytest.mark, "external_engine_ok")``, ``add_marker("external_engine_ok")`` and a
marker built from the constant, which are the spellings a copy-paste would not use but a deliberate bypass might.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_TESTS = Path(__file__).resolve().parent
_THIS = Path(__file__).resolve()

#: Files that may apply the marker. Exact: each must still carry it.
CARRIERS = frozenset({
    "test_rdr235_mvv_aspect_queue_tuples.py",
    "test_external_engine_optin.py",
})
#: Files that name the marker without applying it to a test.
MECHANISM = frozenset({"_external_engine.py", _THIS.name})

_SITE = re.compile(r"external_engine_ok|_external_engine\s*\.\s*MARKER|from\s+tests\._external_engine\s+import[^\n]*\bMARKER\b")

#: A floor under the scan, so a glob that matches nothing cannot pass (the nexus-moht0 vacuous-gate class).
_MIN_FILES_EXAMINED = 100


def _code_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]


def _scan() -> tuple[int, dict[str, int]]:
    """``(files examined, site count by path relative to tests/)`` over every ``*.py`` under tests/."""
    examined, sites = 0, {}
    for path in sorted(_TESTS.rglob("*.py")):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        examined += 1
        n = sum(1 for ln in _code_lines(path) if _SITE.search(ln))
        if n:
            sites[path.relative_to(_TESTS).as_posix()] = n
    return examined, sites


def test_only_the_allowlisted_files_apply_the_external_engine_marker() -> None:
    examined, sites = _scan()
    assert examined >= _MIN_FILES_EXAMINED, f"the scan examined {examined} files: the glob is broken"
    allowed = CARRIERS | MECHANISM
    stray = sorted(set(sites) - allowed)
    assert not stray, (
        f"{stray} spell external_engine_ok (or its MARKER constant). A test carrying it reads the external-engine "
        "pair and can be pointed at a managed tenant by an exported variable; if a new test needs that, add its "
        "file to CARRIERS here and say why in the bead, with Sam's go. Sites found: " + repr(sites)
    )
    unused = sorted(CARRIERS - set(sites))
    assert not unused, f"{unused} are allowlisted as carriers but no longer carry the marker; drop them from CARRIERS"
    assert "_external_engine.py" in sites, "the mechanism file no longer names the marker: this scan reads nothing it should"
