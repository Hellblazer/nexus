# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Ratchet: every text-mode file call in shipped code names its encoding.

THE SHAPES THIS WATCHES::

    path.read_text()            path.write_text(body)
    open(p)  open(p, "w")       path.open("a")
    os.fdopen(fd, "w")          tempfile.NamedTemporaryFile("w")
    logging.handlers.RotatingFileHandler(log_path, maxBytes=n)

With no ``encoding``, Python uses the locale's preferred encoding. On macOS
and Linux that is UTF-8, so the omission is invisible there. On Windows it
is the ANSI code page (cp1252 on an English install), so a file nexus wrote
as UTF-8, or a JSON file Claude Code wrote with a user's non-ASCII home
path in it, decodes wrong or raises ``UnicodeDecodeError``, and a write
puts cp1252 bytes where every other reader expects UTF-8 (nexus-ox0c0).
nexus-3fme8 found the hook-side instances of this; the sweep that emptied
this ratchet found 154 more across ``src/nexus`` and the sn hook scripts.

Adding ``encoding="utf-8"`` changes nothing on a UTF-8 locale, which is why
the sweep could be done in one change. Sam chose this remedy over setting
``PYTHONUTF8=1`` in the Windows launchers (2026-10-14); that variable
would reach only the processes those launchers start.

:data:`_EXEMPT` is empty. An empty map is a STRONGER gate than a drained
one, not a retired one: every file in scope is allowed zero, so any new
encoding-less call fails :func:`test_text_io_calls_name_their_encoding` by
file and line. Do not delete this file when you notice the map is empty.
With nothing left to find in the real tree, that test would also pass with
a detector that matches nothing, so
:func:`test_detector_recognises_the_shapes_it_claims_to` pins the shapes
and their near-misses on synthetic source, and
:func:`test_the_walk_finds_a_planted_file` pins the walk.

What the scan cannot see is listed rather than skipped: a ``**kwargs``
unpack into one of these calls, and a bound method or ``open`` passed as a
value (``retry(path.read_text)``) are reported as findings of their own, so
a new one has to be rewritten or named in :data:`_EXEMPT` with its reason.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests.test_windows_release_legs_workflow import WINDOWS_TEST_SET

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Shipped Python: the package, and the plugin scripts Claude Code runs on
# the user's machine (Windows included). Then the tests the Windows CI job runs,
# read from the pin on that job's list so the two cannot drift: a test there
# that writes a fixture in cp1252 fails against code that now reads UTF-8,
# which is how the first push of this sweep went red on Windows (30 tests in
# tests/daemon/, seeding unit files whose templates carry an em dash).
SCAN_ROOTS: tuple[str, ...] = ("src/nexus", "conexus", "sn", "prose-edit") + tuple(
    dict.fromkeys(entry.split("::")[0].rstrip("/") for entry in WINDOWS_TEST_SET)
)

# "<repo-relative path>:<line>" -> why that call may stay encoding-less.
_EXEMPT: dict[str, str] = {}

# Receivers whose ``.open`` is not a text file open: low-level descriptors,
# archives, image and PDF libraries, the browser.
_NON_TEXT_OPENERS: frozenset[str] = frozenset({
    "os", "tarfile", "lzma", "gzip", "bz2", "zipfile", "webbrowser",
    "pymupdf", "fitz", "Image", "shelve", "dbm",
})

_TEMPFILES: frozenset[str] = frozenset({"NamedTemporaryFile", "TemporaryFile"})

# logging file handlers: always text, encoding at this position.
_LOG_HANDLERS: dict[str, int] = {
    "FileHandler": 2,
    "WatchedFileHandler": 2,
    "RotatingFileHandler": 4,
    "TimedRotatingFileHandler": 4,
}

_BOUND_METHODS: frozenset[str] = frozenset({"read_text", "write_text", "fdopen"})


def _positions(call: ast.Call) -> tuple[str, int | None, int] | None:
    """(kind, mode position, encoding position) for a call this lint covers."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id == "open":
            return "open", 1, 3
        if func.id in _TEMPFILES:
            return func.id, 0, 3
        if func.id in _LOG_HANDLERS:
            return func.id, None, _LOG_HANDLERS[func.id]
        return None
    if not isinstance(func, ast.Attribute):
        return None
    attr = func.attr
    receiver = func.value.id if isinstance(func.value, ast.Name) else None
    if attr == "read_text":
        return "read_text", None, 0
    if attr == "write_text":
        return "write_text", None, 1
    if attr == "fdopen":
        return "fdopen", 1, 3
    if attr in _TEMPFILES:
        return attr, 0, 3
    if attr in _LOG_HANDLERS:
        return attr, None, _LOG_HANDLERS[attr]
    if attr == "open":
        if receiver == "io":
            return "io.open", 1, 3
        if receiver == "codecs":
            return "codecs.open", 1, 2
        if receiver in _NON_TEXT_OPENERS:
            return None
        # Path.open(mode, buffering, encoding). A first argument that is not
        # a string literal is some other API's open: opener.open(request),
        # tarfile-like wrappers taking a path.
        if call.args and not (
            isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        ):
            return None
        return "Path.open", 0, 2
    return None


def _mode(call: ast.Call, position: int | None) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def unencoded_text_io(source: str) -> list[tuple[int, str]]:
    """(line, kind) for every text-mode file call with no encoding in *source*."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    call_funcs = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            spec = _positions(node)
            if spec is None:
                continue
            kind, mode_at, encoding_at = spec
            mode = _mode(node, mode_at)
            if kind in _TEMPFILES and mode is None:
                continue  # the tempfile default is "w+b"
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
                continue
            if any(k.arg == "encoding" for k in node.keywords) or len(node.args) > encoding_at:
                continue
            if any(k.arg is None for k in node.keywords):
                found.append((node.lineno, f"{kind} with **kwargs"))
            else:
                found.append((node.lineno, kind))
        elif id(node) in call_funcs:
            continue
        elif isinstance(node, ast.Name) and node.id == "open" and isinstance(node.ctx, ast.Load):
            found.append((node.lineno, "open passed as a value"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in _BOUND_METHODS
            and isinstance(node.ctx, ast.Load)
            # ``Path.read_text`` itself, kept so a test can wrap the real method:
            # its callers pass their own encoding through.
            and ast.unparse(node.value) not in ("Path", "pathlib.Path")
        ):
            found.append((node.lineno, f"{node.attr} passed as a value"))
    return sorted(found)


def _census(repo: pathlib.Path, roots: tuple[str, ...]) -> tuple[int, dict[str, str]]:
    """(files scanned, "<path>:<line>" -> kind). *repo* and *roots* are
    parameters only so :func:`test_the_walk_finds_a_planted_file` can point
    the walk at a planted tree."""
    files = 0
    findings: dict[str, str] = {}
    for root in roots:
        base = repo / root
        for path in [base] if base.is_file() else sorted(base.rglob("*.py")):
            files += 1
            rel = path.relative_to(repo).as_posix()
            for line, kind in unencoded_text_io(path.read_text(encoding="utf-8")):
                findings[f"{rel}:{line}"] = kind
    return files, findings


def test_text_io_calls_name_their_encoding() -> None:
    files, findings = _census(REPO_ROOT, SCAN_ROOTS)
    assert files > 300, f"the walk reached only {files} files; scan roots moved?"
    unexpected = {site: kind for site, kind in findings.items() if site not in _EXEMPT}
    assert not unexpected, (
        "text-mode file calls with no encoding (add encoding=\"utf-8\"; the locale "
        "default is cp1252 on Windows, see this module's docstring):\n"
        + "\n".join(f"  {site}  {kind}" for site, kind in sorted(unexpected.items()))
    )
    stale = sorted(set(_EXEMPT) - set(findings))
    assert not stale, f"_EXEMPT names sites that are no longer findings: {stale}"


_MUST_MATCH: tuple[tuple[str, str], ...] = (
    ("p.read_text()", "read_text"),
    ("p.write_text(body)", "write_text"),
    ("open(p)", "open"),
    ("open(p, 'w')", "open"),
    ("open(p, mode='a', buffering=1)", "open"),
    ("open(p, mode)", "open"),
    ("p.open()", "Path.open"),
    ("p.open('w')", "Path.open"),
    ("(d / 'x').open(mode='r')", "Path.open"),
    ("os.fdopen(fd, 'w')", "fdopen"),
    ("os.fdopen(fd)", "fdopen"),
    ("io.open(p, 'w')", "io.open"),
    ("codecs.open(p, 'r')", "codecs.open"),
    ("tempfile.NamedTemporaryFile('w', delete=False)", "NamedTemporaryFile"),
    ("NamedTemporaryFile(mode='w+')", "NamedTemporaryFile"),
    ("logging.FileHandler(p)", "FileHandler"),
    ("logging.handlers.RotatingFileHandler(p, maxBytes=10, backupCount=5)", "RotatingFileHandler"),
    ("TimedRotatingFileHandler(p, 'midnight')", "TimedRotatingFileHandler"),
    ("p.read_text(**kw)", "read_text with **kwargs"),
    ("retry(p.read_text)", "read_text passed as a value"),
    ("f = open", "open passed as a value"),
)

_MUST_NOT_MATCH: tuple[str, ...] = (
    "p.read_text(encoding='utf-8')",
    "p.read_text('utf-8')",
    "p.write_text(body, encoding='utf-8')",
    "p.write_text(body, 'utf-8')",
    "p.read_bytes()",
    "p.write_bytes(data)",
    "open(p, 'rb')",
    "open(p, mode='wb')",
    "open(p, 'w', -1, 'utf-8')",
    "open(p, 'w', encoding='utf-8', errors='replace')",
    "p.open('rb')",
    "p.open('w', encoding='utf-8')",
    "os.fdopen(fd, 'wb')",
    "os.fdopen(fd, 'w', encoding='utf-8')",
    "os.open(p, os.O_RDONLY)",
    "tarfile.open(archive, 'w')",
    "tarfile.open('x.tar')",
    "lzma.open(archive)",
    "gzip.open(p)",
    "webbrowser.open(url)",
    "pymupdf.open(p)",
    "opener.open(request)",
    "_keepalive_opener().open(req, timeout=5)",
    "tempfile.NamedTemporaryFile()",
    "tempfile.NamedTemporaryFile(suffix='.bin')",
    "tempfile.NamedTemporaryFile('w', encoding='utf-8')",
    "codecs.open(p, 'r', 'utf-8')",
    "logging.FileHandler(p, encoding='utf-8')",
    "logging.FileHandler(p, 'a', 'utf-8')",
    "RotatingFileHandler(p, 'a', 10, 5, 'utf-8')",
    "logging.StreamHandler(sys.stderr)",
    "def open(self): pass",
    "real = Path.read_text",
    "real = pathlib.Path.write_text",
    "self.open = True",
)


@pytest.mark.parametrize(("source", "kind"), _MUST_MATCH)
def test_detector_recognises_the_shapes_it_claims_to(source: str, kind: str) -> None:
    assert unencoded_text_io(source) == [(1, kind)]


@pytest.mark.parametrize("source", _MUST_NOT_MATCH)
def test_detector_passes_the_near_misses(source: str) -> None:
    assert unencoded_text_io(source) == []


def test_the_walk_finds_a_planted_file(tmp_path: pathlib.Path) -> None:
    pkg = tmp_path / "pkg" / "sub"
    pkg.mkdir(parents=True)
    (pkg / "clean.py").write_text("p.read_text(encoding='utf-8')\n", encoding="utf-8")
    (pkg / "dirty.py").write_text("x = 1\np.write_text(body)\n", encoding="utf-8")
    (tmp_path / "single.py").write_text("open(p)\n", encoding="utf-8")
    files, findings = _census(tmp_path, ("pkg", "single.py"))
    assert files == 3
    assert findings == {"pkg/sub/dirty.py:2": "write_text", "single.py:1": "open"}
