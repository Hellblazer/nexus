# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``_install/layout_core.py`` must import with nexus absent.

nexus-utpuw.1. The whole reason ``layout.sh`` and ``install_layout.py`` are
twins is that the generation builder and the shim writer run from
``scripts/reinstall-tool.sh``, which may run with NOTHING installed and
therefore cannot import nexus. Collapsing the twins rests on one claim: the
constraint is importing NEXUS, not running Python, so a stdlib-only module
serves both callers.

That claim is only true while it is enforced. A single ``from nexus.x import
y`` added to ``layout_core`` at any later date would break the bootstrap
caller, and it would break it in the world this test suite does not normally
run in -- every ordinary test session has nexus installed and importable, so
nothing else in the suite can see the regression. Hence a test that removes
nexus from the picture on purpose.

The subprocess is not ceremony. ``nexus`` is already imported into this
process by the time any test runs, so an in-process check would find it in
``sys.modules`` and pass no matter what ``layout_core`` imports.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from nexus import install_layout
from nexus.errors import NexusError
from nexus.install_layout import InstallLayoutError, LayoutError

_CORE = Path(__file__).resolve().parents[1] / "src" / "nexus" / "_install" / "layout_core.py"


def test_the_core_is_present() -> None:
    # Not skipif-gated, for the reason the twins test states: a skip here could
    # only mean the module has been deleted, which is the loudest thing this
    # file could report rather than a reason to report nothing.
    assert _CORE.is_file(), f"{_CORE} is missing"


def test_no_module_scope_import_of_nexus() -> None:
    """Parsed, not grepped.

    A grep cannot tell an identifier from a sentence, and this module's
    docstrings say the word ``nexus`` repeatedly -- including in the sentences
    explaining why it must not import nexus. ``ast`` sees imports only.

    Deferred imports INSIDE a function are the documented mechanism here
    (:func:`_warn` and :func:`_error_base` both use one), so only module-scope
    imports are refused.
    """
    tree = ast.parse(_CORE.read_text())
    offenders: list[str] = []
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "nexus"]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "nexus":
                offenders.append(node.module or "")
            elif node.level:  # a relative import is a nexus import by another name
                offenders.append("." * node.level + (node.module or ""))
    assert not offenders, (
        f"layout_core imports {offenders} at module scope. It is run as a script "
        f"by layout.sh with nothing installed; defer the import into the function "
        f"that needs it, as _warn and _error_base do."
    )


def _import_with_nexus_unavailable(snippet: str) -> subprocess.CompletedProcess[str]:
    """Import the core by PATH, with ``nexus`` poisoned in ``sys.modules``.

    ``sys.modules["nexus"] = None`` makes any ``import nexus...`` raise
    ``ImportError`` -- the exact exception a missing distribution raises, which
    is what the two deferred imports catch. Loading by path rather than by
    dotted name is what keeps the poison from also blocking the module under
    test, since ``nexus._install.layout_core`` would have to traverse
    ``nexus`` to be found at all.
    """
    program = f'''
import sys, importlib.util
sys.modules["nexus"] = None
sys.modules["nexus.errors"] = None
spec = importlib.util.spec_from_file_location("layout_core_bootstrap", {str(_CORE)!r})
mod = importlib.util.module_from_spec(spec)
# Registered before exec: dataclasses resolves a field's type through
# sys.modules[cls.__module__], and Receipt is a dataclass.
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
{snippet}
'''
    return subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False,
    )


def test_it_imports_and_works_with_nexus_unimportable() -> None:
    r = _import_with_nexus_unavailable('''
import pathlib
assert mod.tools_dir().is_absolute()
assert mod.build_spec("conexus", ["local"], "7.55.1") == "conexus[local]==7.55.1"
shim = mod.render_shim("nx", tools=pathlib.Path("/t"))
assert shim.splitlines()[-1] == 'exec "$NX_GEN/bin/nx" "$@"'
print("OK")
''')
    assert r.returncode == 0, f"core did not import without nexus:\n{r.stderr}"
    assert "OK" in r.stdout


def test_the_error_falls_back_to_plain_exception_without_nexus() -> None:
    """The conditional base's OTHER branch.

    Without this the fallback in :func:`_error_base` is untested code that
    would only ever run at bootstrap, which is precisely where nobody is
    watching. The refusal must still be a refusal there: a caller does
    ``dir=$(nx_tools_dir) || exit 1``, so the error has to be raisable.
    """
    r = _import_with_nexus_unavailable('''
assert mod.LayoutError.__mro__[1] is Exception, mod.LayoutError.__mro__
try:
    mod.generation_dir("../escape")
except mod.LayoutError as exc:
    assert "generation stamp" in str(exc), exc
else:
    raise AssertionError("a traversal stamp was not refused")
print("OK")
''')
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_the_error_is_a_nexus_error_when_nexus_is_available() -> None:
    """The branch the installed world actually takes.

    ``InstallLayoutError`` is documented as a member of the nexus hierarchy and
    is caught by ``health.py`` and ``self_cmd.py``; the alias must not quietly
    demote it to a bare ``Exception``.
    """
    assert InstallLayoutError is LayoutError, (
        "InstallLayoutError must be an ALIAS, not a subclass: every error is "
        "raised by the core as LayoutError, so a subclass would catch none of them"
    )
    assert issubclass(InstallLayoutError, NexusError)


def test_a_subclass_would_have_been_wrong() -> None:
    """The mistake this arrangement exists to avoid, stated as a test.

    Not a tautology about Python: it pins the REASON the alias is an alias, so
    that someone restoring ``class InstallLayoutError(NexusError, LayoutError)``
    has to read why it was not that. The core raises the base, so a subclass is
    a name that catches nothing.
    """
    class Subclass(install_layout.LayoutError):
        pass

    try:
        install_layout.generation_dir("../escape")
    except Subclass:  # pragma: no cover -- the point is that this never runs
        raise AssertionError("unreachable: the core raises the base, not a subclass")
    except install_layout.InstallLayoutError:
        pass


def _run_cli_with_nexus_unavailable(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    """Run the core AS A SCRIPT, the way layout.sh runs it, with nexus gone.

    ``runpy`` with ``run_name="__main__"`` so the ``if __name__ == "__main__"``
    block executes: importing the module and calling ``main()`` by hand would
    skip the one line that turns a return value into an exit status, which is
    the line layout.sh's ``|| return`` depends on.

    Portable by construction rather than by finding a second interpreter on the
    box. A foreign ``python3`` that genuinely cannot import nexus is the more
    faithful world and was verified by hand, but whether one exists is a fact
    about the machine, and a test that quietly stops running on a box without
    one is worse than a slightly synthetic one that always does.
    """
    program = f'''
import sys, runpy
sys.modules["nexus"] = None
sys.modules["nexus.errors"] = None
sys.argv = ["layout_core.py"] + {argv!r}
runpy.run_path({str(_CORE)!r}, run_name="__main__")
'''
    return subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)},
    )


def test_the_cli_answers_with_nexus_unavailable(tmp_path: Path) -> None:
    """The bootstrap path end to end: layout.sh runs this as a script, and at
    that moment there is no nexus to import. Everything else in this file
    tests the module; this tests the thing layout.sh actually invokes."""
    r = _run_cli_with_nexus_unavailable(["tools_dir"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / ".local" / "share" / "nexus" / "tools")

    r = _run_cli_with_nexus_unavailable(
        ["build_spec", "conexus", "local,voyage", "7.55.1"], tmp_path
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "conexus[local,voyage]==7.55.1"


def test_the_cli_refuses_with_the_usage_status_and_an_empty_stdout(tmp_path: Path) -> None:
    """layout.sh's calling contract, which the core has to honour or every
    consumer's ``dir=$(nx_tools_dir) || exit 1`` breaks.

    stdout must be EMPTY on a refusal. A refusal that also emits a path is how
    a caller ends up installing into it, and at bootstrap there is no structlog
    and no traceback handler to make the mistake visible.
    """
    from nexus.install_layout import LAYOUT_USAGE_EXIT

    for argv in (
        ["generation_dir", "../escape"],      # a refused component
        ["render_shim", "nx$(touch PWNED)"],  # a refused shim name
        ["receipt_path", "relative/path"],    # a refused relative generation
        ["no_such_verb"],                     # layout.sh calling us wrongly
        ["render_shim"],                      # arity
        [],                                   # no verb at all
    ):
        r = _run_cli_with_nexus_unavailable(argv, tmp_path)
        assert r.returncode == LAYOUT_USAGE_EXIT, (
            f"{argv} exited {r.returncode}, expected {LAYOUT_USAGE_EXIT}: {r.stderr}"
        )
        assert r.stdout == "", f"{argv} printed to stdout on a refusal: {r.stdout!r}"
        assert r.stderr.strip(), f"{argv} refused silently"
        assert "Traceback" not in r.stderr, (
            f"{argv} produced a traceback rather than a refusal:\n{r.stderr}"
        )
