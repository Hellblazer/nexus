# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Code that mints a persistent identity must not read the process CWD.

nexus-yg70j / nexus-3e4s. A `file://` URI stored in `source_uri` is a
persistent IDENTITY. Deriving one with `os.path.abspath`, `os.getcwd`,
`Path.cwd` or `Path.resolve` makes that identity a function of where the
calling process happens to be standing — which is not a property of the
document. The same stored path then yields DIFFERENT identities depending on
the caller, and there is no way to tell from the value which one you got.

THIS CLASS HAS NOW BEEN FOUND THREE TIMES, each by an incident rather than by
a test:

  nexus-5i864  HttpCatalogClient returned the stored file_path verbatim, so
               relative paths re-anchored on the caller's CWD and silently
               zeroed / mis-fed the auto-linker.
  nexus-yg70j  aspect_readers.uri_for. cwd DELETED -> getcwd() raised and
               killed the aspect-worker permanently and silently (16 batches,
               ~40 min production outage). After the chdir fix, cwd merely
               UNRELATED -> file:///Users/<u>/.config/nexus/docs/rdr/x.md for a
               file that lives in a git repo. Loud failure, then quiet wrong
               answer.
  (this lint)  catalog/types.py did the same for a relative file_path with no
               repo_root — defended in the common case, exposed in the rest.

Each was fixed in isolation. Nothing stopped the next one. That is what this
lint is for.

THE RULE: a module that constructs a `file://` URI must not call a
cwd-dependent path function. The correct anchor is an EXPLICIT root — the
owner's `repo_root`, as `HttpCatalogClient.resolve_path` does — or nothing at
all. Returning no identity is recoverable; returning a wrong one is not.

Not a general ban: CLI code legitimately resolves user input against the cwd,
because there the cwd IS the user's intent. The coupling to `file://`
construction is what makes this precise rather than a style rule.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"

#: Calls whose result depends on the process working directory.
_CWD_DEPENDENT = {
    ("os", "path", "abspath"): "os.path.abspath",
    ("os", "getcwd"): "os.getcwd",
    ("Path", "cwd"): "Path.cwd",
    ("pathlib", "Path", "cwd"): "pathlib.Path.cwd",
}

#: Modules permitted to construct a `file://` URI *and* read the cwd, each with
#: the reason. Adding an entry is a decision; leaving one out is a defect.
_ALLOWED: dict[str, str] = {}

#: Vacuity floors — a sweep that inspects nothing passes everything.
_MIN_MODULES_SCANNED = 200
_MIN_URI_BUILDERS = 3


def _dotted(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def _builds_file_uri(tree: ast.AST) -> bool:
    """True when this subtree contains a literal `file://`."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "file://" in n.value:
            return True
    return False


def _functions(tree: ast.AST):
    """Yield every function/method body, plus module top level as a pseudo-scope.

    FUNCTION-scoped, not module-scoped, deliberately. A module may legitimately
    contain BOTH a CLI verb that resolves user input against the cwd (where the
    cwd IS the user's intent) and, elsewhere, an identity builder. Flagging the
    whole file conflates them and forces an allowlist entry that then hides a
    real offender added to the same module later. The coupling only means
    something inside one scope.
    """
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"{n.name}()", n
    top = ast.Module(
        body=[s for s in getattr(tree, "body", [])
              if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
        type_ignores=[],
    )
    yield "<module>", top


def _cwd_calls(tree: ast.AST) -> list[tuple[int, str]]:
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            name = _CWD_DEPENDENT.get(_dotted(n.func))
            if name:
                hits.append((n.lineno, name))
    return hits


def _modules():
    for p in sorted(SRC.rglob("*.py")):
        try:
            yield p, ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue


def test_the_sweep_is_not_vacuous() -> None:
    """Guards the parser and the glob: a sweep that finds nothing proves
    nothing, and this lint's whole value is an absence claim."""
    mods = list(_modules())
    assert len(mods) >= _MIN_MODULES_SCANNED, (
        f"only {len(mods)} modules parsed under {SRC} — the glob or the AST "
        "parse is broken, and this lint is proving nothing"
    )
    builders = [p for p, t in mods
                if any(_builds_file_uri(n) for _, n in _functions(t))]
    assert len(builders) >= _MIN_URI_BUILDERS, (
        f"only {len(builders)} modules construct a file:// URI — the detector "
        "has stopped detecting, so the assertion below cannot fail"
    )


def test_no_identity_builder_reads_the_cwd() -> None:
    offenders: list[str] = []
    for path, tree in _modules():
        rel = str(path.relative_to(REPO_ROOT))
        if rel in _ALLOWED:
            continue
        for scope, node in _functions(tree):
            if not _builds_file_uri(node):
                continue
            for lineno, call in _cwd_calls(node):
                offenders.append(f"{rel}:{lineno}  {call}  (in {scope})")
    assert not offenders, (
        "these modules construct a `file://` identity AND read the process "
        "working directory, so the identity they mint depends on where the "
        "caller stands:\n    "
        + "\n    ".join(offenders)
        + "\n  Fix: anchor on an EXPLICIT root (the owner's `repo_root`, as "
        "HttpCatalogClient.resolve_path does), or return no identity at all. "
        "A missing identity is detectable and recoverable; a cwd-anchored one "
        "is neither.\n  A genuinely-correct exception goes in _ALLOWED with "
        "its reason."
    )


def test_detector_flags_a_synthetic_offender_and_clears_a_clean_module() -> None:
    """Falsification control. Without this, a detector that matched nothing
    would satisfy the assertion above forever."""
    bad = ast.parse('import os\nu = "file://" + os.path.abspath(p)\n')
    assert _builds_file_uri(bad)
    assert _cwd_calls(bad) == [(2, "os.path.abspath")]

    good = ast.parse('import os\nu = "file://" + os.path.normpath(os.path.join(root, p))\n')
    assert _builds_file_uri(good)
    assert _cwd_calls(good) == []

    unrelated = ast.parse("import os\nx = os.getcwd()\n")
    assert not _builds_file_uri(unrelated), (
        "a module that reads the cwd but mints no identity must NOT be flagged "
        "— CLI code resolving user input against the cwd is correct"
    )
