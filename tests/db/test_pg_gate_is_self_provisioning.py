# SPDX-License-Identifier: AGPL-3.0-or-later
"""No test may gate itself on an AMBIENT PostgreSQL install.

LOCKED POLICY (Hal, 2026-07-07 and restated 2026-07-27): nexus ALWAYS uses the
PostgreSQL it BUILDS. Never Homebrew, never a system package, never a
pre-existing host install. The test-side seam for that policy is
``tests.db._service_fixture.pg_bin_dir``, which self-provisions the
sigstore-verified ``nexus-pg-<target>`` bundle for ``PINNED_SERVICE_TAG`` into
``~/.cache/nexus-test-substrate/<tag>/`` and returns a nonexistent sentinel ONLY
when self-provisioning itself fails.

WHY A LINT AND NOT A NOTE. ``nexus.db.pg_provision.discover_pg_binaries`` asks a
different question — "does this BOX happen to have a PostgreSQL?" — which is a
property of the machine, not of the substrate a test needs. A skip gate built on
it converts "no host PG installed" into "these tests are fine", silently. Found
2026-07-27: ``test_nexus_diag_role.py`` and ``test_rdr182_mvv_no_content_read.py``
each resolved every fixture through ``pg_bin_dir()`` while gating on
``discover_pg_binaries()``, so their gate and their body disagreed. All 15 + 4
tests skipped on a box whose substrate cache ALREADY held the pinned tag. The
skip reason even told the reader to install ``postgresql@16``, which is the one
thing this project never does.

THE ASYMMETRY THAT HID IT. A wrong skip gate is invisible in every aggregate: a
green run reports "N passed, M skipped" and nothing distinguishes an honest skip
from a gate asking the wrong question. Only reading the gate itself finds it,
which is exactly the class of thing a lint should own rather than a reviewer.

CONTAINERS ARE NOT AN ALTERNATIVE HERE, deliberately. The Java suite boots
``pgvector/pgvector:pg17``, which carries the FULL contrib set; the shipped
bundle carries exactly ``pg_trgm``, ``plpgsql`` and ``vector``. A changeset doing
``CREATE EXTENSION unaccent`` passed the entire mvn suite and then failed every
test of the pytest engine arm, because only that arm boots the real bundle
(nexus-22r1f). Substituting a container for the self-provisioned bundle trades
the substrate users actually run for one that merely resembles it.

WHAT THIS LINT DOES NOT COVER. It checks how a module GATES, not what it uses.
Importing ``discover_pg_binaries`` and exercising it in the body is fine and
expected — the discovery logic is a product surface with its own tests. Only the
run/skip decision is constrained.

THERE IS NO ALLOWLIST, on purpose. The first draft of this lint carried one, and
auditing it found that ten of its eleven entries did not gate on ambient
discovery at all (they merely mentioned it) while the eleventh,
``tests/db/test_pg_provision.py``, was a genuine offender being exempted. An
exemption list that large hides more than it grants. The rule is absolute: even
a test OF discovery decides whether to run based on whether OUR bundle is
obtainable.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_TEST_ROOT = _REPO / "tests"

#: Deliberately EMPTY — see the module docstring. Kept as a named seam so that
#: adding an exemption is an explicit, reviewable act with a stated reason,
#: rather than an edit to the assertion itself.
SUBJECT_UNDER_TEST: dict[str, str] = {}


def _skip_gate_calls(path: Path) -> list[str]:
    """Names reachable from a module-level skip condition, resolved TRANSITIVELY.

    Deliberately AST-based rather than regex: a gate spelled across a helper is
    the normal shape here (``_pg_bins_available()``), and a substring scan would
    either miss it or fire on an unrelated mention in a docstring.

    RESOLUTION IS TRANSITIVE, and that is load-bearing. The first version of
    this walker followed only calls appearing INSIDE the gate assignment, so an
    intermediate variable defeated it entirely::

        _PG_AVAILABLE = _pg_bins_available()      # <- the ambient call lives here
        pytestmark = [pytest.mark.skipif(not _PG_AVAILABLE, reason=...)]

    ``pytestmark``'s value references a Name, not a Call, so nothing was
    collected and ``tests/db/test_pg_provision.py`` was reported clean while
    gating on ambient discovery. Caught by cross-checking the walker's verdict
    against the file read by hand. Names and called functions are now resolved
    to a fixpoint through a module-level symbol table, so any number of hops
    between the gate and the ambient call is followed.
    """
    tree = ast.parse(path.read_text())

    # Module-level symbol table: name -> defining node.
    symbols: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    symbols[t.id] = node.value
        elif isinstance(node, ast.FunctionDef):
            symbols[node.name] = node

    def _is_gate_target(name: str) -> bool:
        return name == "pytestmark" or name.startswith(
            ("_requires", "_SKIP", "_ALL_PREREQS")
        )

    worklist: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and _is_gate_target(t.id) for t in node.targets
        ):
            worklist.append(node.value)
        # A bare `pytest.mark.skipif(...)` used as a decorator or inline is a
        # gate too, even when never bound to a recognised name.
        elif isinstance(node, ast.Expr):
            worklist.append(node.value)

    names: list[str] = []
    seen: set[str] = set()
    while worklist:
        expr = worklist.pop()
        for sub in ast.walk(expr):
            ref: str | None = None
            if isinstance(sub, ast.Call):
                if isinstance(sub.func, ast.Name):
                    names.append(sub.func.id)
                    ref = sub.func.id
                elif isinstance(sub.func, ast.Attribute):
                    names.append(sub.func.attr)
            elif isinstance(sub, ast.Name):
                ref = sub.id
            elif isinstance(sub, ast.ImportFrom):
                names.extend(a.name for a in sub.names)

            if ref and ref not in seen and ref in symbols:
                seen.add(ref)
                worklist.append(symbols[ref])
    return names


def _modules() -> list[Path]:
    return sorted(
        p for p in _TEST_ROOT.rglob("test_*.py")
        if "/containers/out/" not in p.as_posix()
    )


def test_no_test_gates_on_ambient_pg_discovery() -> None:
    modules = _modules()

    # NON-VACUITY: a broken glob or a renamed seam would make the scan pass
    # while examining nothing.
    assert len(modules) > 200, (
        f"only {len(modules)} test modules discovered — the rglob is broken"
    )
    gated = [p for p in modules if _skip_gate_calls(p)]
    assert len(gated) > 20, (
        f"only {len(gated)} modules with a parsed skip gate — the AST walker "
        "stopped recognising gate assignments, so this lint is not binding"
    )

    offenders = []
    for path in modules:
        rel = path.relative_to(_REPO).as_posix()
        if rel in SUBJECT_UNDER_TEST:
            continue
        if "discover_pg_binaries" in _skip_gate_calls(path):
            offenders.append(rel)

    assert offenders == [], (
        "these modules decide whether to run by asking whether the BOX has a "
        "PostgreSQL, instead of self-provisioning the bundle nexus builds. Gate "
        "on tests.db._service_fixture.pg_bin_dir() instead — it returns a "
        "nonexistent sentinel only when self-provisioning itself fails, so the "
        "skip means 'could not build our substrate', never 'no host PG here'. "
        f"Offenders: {offenders}"
    )


def test_exemption_seam_stays_empty_and_live() -> None:
    """The exemption is empty by design; entries must be real and justified.

    Two failure modes, both of which have already happened once here: an
    exemption granted to a module that never needed it (ten of the original
    eleven), and an exemption granted to a genuine offender under a
    plausible-sounding reason (the eleventh).
    """
    for rel, reason in SUBJECT_UNDER_TEST.items():
        path = _REPO / rel
        assert path.exists(), f"exemption names a nonexistent module: {rel}"
        assert reason.strip(), f"exemption without a stated reason: {rel}"
        assert "discover_pg_binaries" in _skip_gate_calls(path), (
            f"{rel} is exempted but does not actually gate on ambient discovery "
            "— drop the entry rather than carrying a hole that grants nothing"
        )


def test_lint_detects_the_shape_it_claims_to(tmp_path) -> None:
    """Falsification: the corpus test passes on a clean tree either way."""
    violating = tmp_path / "test_ambient_gate.py"
    violating.write_text(
        "import pytest\n"
        "def _pg_bins_available():\n"
        "    from nexus.db.pg_provision import discover_pg_binaries\n"
        "    try:\n"
        "        discover_pg_binaries()\n"
        "        return True\n"
        "    except Exception:\n"
        "        return False\n"
        "pytestmark = [pytest.mark.skipif(not _pg_bins_available(), reason='x')]\n"
    )
    assert "discover_pg_binaries" in _skip_gate_calls(violating), (
        "the walker must follow a gate through the helper function it calls"
    )

    # THE SHAPE THAT DEFEATED THE FIRST WALKER: an intermediate variable between
    # the gate and the ambient call. tests/db/test_pg_provision.py is written
    # exactly this way and was reported clean until resolution went transitive.
    indirect = tmp_path / "test_indirect_gate.py"
    indirect.write_text(
        "import pytest\n"
        "def _pg_bins_available():\n"
        "    from nexus.db.pg_provision import discover_pg_binaries\n"
        "    return bool(discover_pg_binaries())\n"
        "_PG_AVAILABLE = _pg_bins_available()\n"
        "pytestmark = [pytest.mark.skipif(not _PG_AVAILABLE, reason='x')]\n"
    )
    assert "discover_pg_binaries" in _skip_gate_calls(indirect), (
        "an intermediate variable between the gate and the ambient call must "
        "not defeat the walker"
    )

    compliant = tmp_path / "test_bundle_gate.py"
    compliant.write_text(
        "import pytest\n"
        "from tests.db._service_fixture import pg_bin_dir\n"
        "_INITDB = pg_bin_dir() / 'initdb'\n"
        "pytestmark = [pytest.mark.skipif(not _INITDB.exists(), reason='x')]\n"
    )
    assert "discover_pg_binaries" not in _skip_gate_calls(compliant)

    # A module that merely MENTIONS discovery outside its gate is not an
    # offender — that distinction is the whole reason this is AST-based.
    mentions_only = tmp_path / "test_mentions.py"
    mentions_only.write_text(
        "import pytest\n"
        "from tests.db._service_fixture import pg_bin_dir\n"
        "_INITDB = pg_bin_dir() / 'initdb'\n"
        "pytestmark = [pytest.mark.skipif(not _INITDB.exists(), reason='x')]\n"
        "def test_something():\n"
        "    from nexus.db.pg_provision import discover_pg_binaries\n"
        "    assert discover_pg_binaries()\n"
    )
    assert "discover_pg_binaries" not in _skip_gate_calls(mentions_only)
