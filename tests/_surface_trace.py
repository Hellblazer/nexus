# SPDX-License-Identifier: AGPL-3.0-only
"""Record the repo surface each meta-test reads (scripts/meta_tests.py build).

Loaded only by ``scripts/meta_tests.py build`` (``-p tests._surface_trace``,
with ``NX_SURFACE_TRACE_OUT`` naming a directory). While a test file is being
collected or one of its tests runs, an audit hook and two ``os`` wrappers
record, against that file, the repo-relative paths it touches:

* ``opened``: a file opened (the ``open`` audit event). An open made by the
  import system is skipped: what a test imports is read from its source by the
  selector instead, so a test's surface is what it scans, not what it runs.
* ``statted``: a path whose existence or metadata was asked for (``os.stat``,
  ``os.lstat``); no audit event covers these, so the two are wrapped.
* ``listed``: a directory listed (``os.listdir``, ``os.scandir``; ``glob``,
  ``os.walk`` and ``pathlib`` all list through these).
* ``prefixes``: a subtree a subprocess enumerates: ``git ls-files``,
  ``grep``, ``ls-tree``, ``diff`` or ``status`` (its pathspecs, or the whole
  repo without one), and ``rg``, ``grep``, ``find`` and ``fd`` (their path
  arguments, or the working directory).
* ``opaque``: any other subprocess run from inside the repo. Its reads are
  invisible here, so the selector treats the test as reading the whole repo.

Reads outside the repo, and under ``.git``, ``.venv``, ``__pycache__`` and
``target``, are dropped. Each worker writes one JSON fragment at session end.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

_OUT = os.environ.get("NX_SURFACE_TRACE_OUT")
_ROOT = os.path.realpath(os.getcwd())
_SKIP_PARTS = {".git", ".venv", "__pycache__", "target", "node_modules", ".lab-remote"}
_GIT_ENUMERATORS = {"ls-files", "grep", "ls-tree", "diff", "status"}
_TREE_WALKERS = {"rg", "grep", "find", "fd"}
#: Tools that read process or host state, never the repo's files: not opaque.
_NO_REPO_READS = {"ps", "pgrep", "kill", "lsof", "uname", "id", "whoami", "hostname", "sysctl", "sw_vers",
                  "date", "sleep", "true", "false", "nproc", "getconf", "tty", "stty", "locale"}

_current: str | None = None
_surfaces: dict[str, dict[str, set[str]]] = {}
_busy = False


def _rel(path: Any, cwd: str | None = None) -> str | None:
    """*path* as a repo-relative posix path, or None when it is not in the repo."""
    if isinstance(path, int):
        return None
    try:
        raw = os.fsdecode(os.fspath(path))
    except TypeError:
        return None
    if not raw:
        return None
    full = os.path.realpath(os.path.join(cwd or os.getcwd(), raw))
    if full != _ROOT and not full.startswith(_ROOT + os.sep):
        return None
    rel = os.path.relpath(full, _ROOT)
    rel = "" if rel == "." else rel.replace(os.sep, "/")
    if _SKIP_PARTS.intersection(rel.split("/")):
        return None
    return rel


def _record(kind: str, rel: str | None) -> None:
    if _current is None or rel is None:
        return
    entry = _surfaces.setdefault(_current, {})
    entry.setdefault(kind, set()).add(rel)


def _opened_by_import() -> bool:
    """True when the nearest Python frame below the hook is the import system."""
    frame = sys._getframe(2)
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    if frame is None:
        return False
    name = frame.f_code.co_filename
    return name.startswith("<frozen importlib") or name.startswith("<frozen zipimport")


def _subprocess(args: Any, cwd: Any) -> None:
    if isinstance(args, (str, bytes, os.PathLike)):
        argv = [os.fsdecode(args)]
    else:
        try:
            argv = [os.fsdecode(a) for a in args]
        except TypeError:
            return
    if not argv:
        return
    where = os.fsdecode(cwd) if cwd else os.getcwd()
    if _rel(where) is None:
        return
    tool = os.path.basename(argv[0])
    rest = argv[1:]
    if tool == "git":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            # -C <dir> and -c <k=v> take a value.
            if rest[i] in ("-C", "-c"):
                if rest[i] == "-C" and i + 1 < len(rest):
                    where = os.path.join(where, rest[i + 1])
                i += 2
            else:
                i += 1
        if i >= len(rest):
            return
        sub, tail = rest[i], rest[i + 1:]
        if sub not in _GIT_ENUMERATORS:
            return
        specs = tail[tail.index("--") + 1:] if "--" in tail else []
        if sub in ("ls-files", "ls-tree") and not specs:
            specs = [t for t in tail if not t.startswith("-")]
            if sub == "ls-tree":
                specs = specs[1:]  # the first is the tree-ish
        for t in specs or ["."]:
            static = _pathspec_root(t)
            if static is not None:
                _record("prefixes", _rel(static or ".", where))
        return
    if tool in _TREE_WALKERS:
        paths = [a for a in rest if not a.startswith("-") and os.path.exists(os.path.join(where, a))]
        for p in paths or ["."]:
            _record("prefixes", _rel(p, where))
        return
    if tool in _NO_REPO_READS:
        return
    _record("opaque", " ".join(argv)[:200])


def _pathspec_root(spec: str) -> str | None:
    """The directory a git pathspec can match under: its text before the first glob
    character, cut back to a whole directory ("" is the whole repo). None for an
    exclude, which narrows a listing and adds nothing to read."""
    if spec.startswith(":"):
        if spec.startswith((":!", ":^")):
            return None
        if spec.startswith(":("):
            magic, _, spec = spec[2:].partition(")")
            if "exclude" in magic.split(","):
                return None
        else:
            spec = spec[1:]
    cut = min((i for i, c in enumerate(spec) if c in "*?["), default=len(spec))
    if cut == len(spec):
        return spec.rstrip("/")
    head = spec[:cut]
    return head.rsplit("/", 1)[0] if "/" in head else ""


def _hook(event: str, args: tuple) -> None:
    global _busy
    if _current is None or _busy:
        return
    _busy = True
    try:
        if event == "open":
            if not _opened_by_import():
                _record("opened", _rel(args[0]))
        elif event in ("os.listdir", "os.scandir"):
            _record("listed", _rel(args[0] if args and args[0] is not None else "."))
        elif event == "subprocess.Popen":
            _subprocess(args[1], args[2])
    finally:
        _busy = False


def _wrap(fn):
    def traced(path, *a, **kw):
        global _busy
        if _current is not None and not _busy:
            _busy = True
            try:
                if kw.get("dir_fd") is None:
                    _record("statted", _rel(path))
            finally:
                _busy = False
        return fn(path, *a, **kw)

    traced.__wrapped__ = fn  # type: ignore[attr-defined]
    return traced


def pytest_configure(config: pytest.Config) -> None:
    if not _OUT:
        raise pytest.UsageError("tests._surface_trace needs NX_SURFACE_TRACE_OUT (scripts/meta_tests.py build sets it)")
    sys.addaudithook(_hook)
    os.stat = _wrap(os.stat)
    os.lstat = _wrap(os.lstat)


def _file_of(path: str | os.PathLike[str]) -> str | None:
    return _rel(path)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    global _current
    previous = _current
    if isinstance(collector, pytest.Module):
        _current = _file_of(collector.path)
    try:
        yield
    finally:
        _current = previous


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    global _current
    _current = _file_of(item.path)
    try:
        yield
    finally:
        _current = None


def pytest_sessionfinish(session: pytest.Session) -> None:
    global _current
    _current = None
    out = Path(_OUT or ".")
    out.mkdir(parents=True, exist_ok=True)
    data = {f: {k: sorted(v) for k, v in kinds.items()} for f, kinds in _surfaces.items()}
    (out / f"trace-{os.getpid()}.json").write_text(json.dumps(data, sort_keys=True))
