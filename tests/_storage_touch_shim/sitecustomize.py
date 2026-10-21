# SPDX-License-Identifier: AGPL-3.0-or-later
"""Child-process recorder for the storage-touch guard (nexus-j23ni).

``tests/_storage_touch_guard.py`` sees only constructions in the pytest
process. A child Python process (an ``nx`` subprocess, a hook verb, a spawned
server) has its own copy of the classes. When the guard runs under
``NX_TEST_T2_SUBSTRATE=optin`` it puts THIS directory first on ``PYTHONPATH``
and exports ``NX_STORAGE_TOUCH_CHILD_LOG``; Python imports ``sitecustomize`` at
startup, and this module patches the same classes as the guard, but appends a
JSON line to that file instead of keeping state. The parent attributes each
line to the test that was running when the child started, through the
``PYTEST_CURRENT_TEST`` variable the child inherited.

Lines (one JSON object each, ``k`` names the kind):

* ``child_start``: one per child process started while a test ran, so the
  report can say how many children it saw;
* ``touch``: a guarded error was constructed;
* ``peek``: ``get_credential("service_url" | "service_token")`` or
  ``discover_lease`` was called from a src/nexus frame outside the endpoint
  helpers (the parent decides whether a tenant made it legitimate);
* ``dropped``: touches past the distinct-signature cap.

A child dedupes like the parent: the first occurrence of a (kind, stack)
signature is written at once with ``n=1``, repeats only count, and the counts
are written as extra lines (``n`` = repeats) when the process exits.

Self-contained on purpose: a child may not be able to import the repo's
``tests`` package, and a shim must never take a child down, so a failure here
is swallowed. The class table below is a copy of
``tests/_storage_touch_guard.py::GUARDED``; a test keeps the two equal.

Inert without ``NX_STORAGE_TOUCH_CHILD_LOG``. A process that hosts a guard
itself (``NX_STORAGE_TOUCH_GUARD_PID`` equal to its pid, e.g. an xdist worker,
which inherits this shim) records in-process and skips the file.
"""
from __future__ import annotations

import atexit
import importlib.abc
import importlib.util
import json
import os
import sys

_LOG_ENV = "NX_STORAGE_TOUCH_CHILD_LOG"
_OWNER_ENV = "NX_STORAGE_TOUCH_GUARD_PID"
_STACK_FRAMES = 6
_MAX_SIGNATURES = 32  # per child process, per line kind

#: module -> guarded class names. Mirrors GUARDED in the guard module.
_TARGETS: dict[str, tuple[str, ...]] = {
    "nexus.db.service_endpoint": ("ServiceEndpointUnresolvableError", "ServiceTokenUnresolvableError"),
    "nexus.db.t1": ("T1ServerNotFoundError",),
    "nexus.db.managed_endpoint": ("ManagedServiceUnreachable", "ManagedServiceIncompatible"),
}
#: module -> function names wrapped to record soft peeks.
_PEEK_TARGETS: dict[str, tuple[str, ...]] = {
    "nexus.config": ("get_credential",),
    "nexus.db.service_endpoint": ("discover_lease",),
}
_PEEK_CREDENTIALS = frozenset({"service_url", "service_token"})
#: src/nexus files that only resolve the endpoint for a caller (see the guard).
_PEEK_HELPER_FILES = ("db/service_endpoint.py", "db/http_vector_client.py", "config.py")

_HERE = os.path.dirname(os.path.abspath(__file__))
_HERE_REAL = os.path.realpath(_HERE)
#: The test that was running when this process started. Read once here: a child that
#: runs its own pytest rewrites PYTEST_CURRENT_TEST to ITS test ids, which the parent
#: would file as late touches of same-named tests of its own (nexus-kt6kj).
_SPAWNING_TEST = os.environ.get("PYTEST_CURRENT_TEST", "")


def _refresh_spawning_test() -> None:
    """A child FORKED by the guard's own process (an xdist worker, whose start-time value is
    empty) takes the test running now. Any other forker, a nested pytest among them, keeps
    the value frozen at its start: its own variable names its nested tests."""
    global _SPAWNING_TEST
    if os.environ.get(_OWNER_ENV) == str(os.getppid()):
        _SPAWNING_TEST = os.environ.get("PYTEST_CURRENT_TEST", "")


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_refresh_spawning_test)
_real: dict[str, str] = {}  # filename -> realpath; a peek can fire thousands of times


def _realpath(filename: str) -> str:
    known = _real.get(filename)
    if known is None:
        known = _real[filename] = os.path.realpath(filename)
    return known

_seen: dict[tuple, dict] = {}  # signature -> the line written for it, with a running "n"
_dropped = 0


def _active() -> bool:
    """A log to write to, and this process is not the one hosting the guard."""
    return bool(os.environ.get(_LOG_ENV)) and os.environ.get(_OWNER_ENV) != str(os.getpid())


def _append(line: dict) -> None:
    path = os.environ.get(_LOG_ENV)
    if not path or not _active():
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(line) + "\n").encode())  # one write: atomic enough for O_APPEND lines
    finally:
        os.close(fd)


def _base(kind: str) -> dict:
    return {
        "k": kind,
        "pid": os.getpid(),
        "current_test": _SPAWNING_TEST,
        "argv0": sys.argv[0] if sys.argv else "",
    }


def _stack() -> list[tuple[str, int, str]]:
    """Outermost first, this directory's own frames removed."""
    out: list[tuple[str, int, str]] = []
    frame = sys._getframe(2)
    while frame is not None:
        code = frame.f_code
        if os.path.dirname(_realpath(code.co_filename)) != _HERE_REAL:
            out.append((code.co_filename, frame.f_lineno, code.co_name))
        frame = frame.f_back
    out.reverse()
    return out


def _fmt(frame: tuple[str, int, str]) -> str:
    return f"{frame[0]}:{frame[1]} in {frame[2]}"


def _once(kind: str, signature: tuple, build) -> None:
    """Write ``build()`` the first time ``signature`` is seen; count repeats."""
    global _dropped
    known = _seen.get((kind, *signature))
    if known is not None:
        known["n"] += 1
        return
    if sum(1 for k in _seen if k[0] == kind) >= _MAX_SIGNATURES:
        _dropped += 1
        return
    line = build()
    line["n"] = 1
    _seen[(kind, *signature)] = line
    _append(line)


def _record(error_type: str) -> None:
    try:
        if not _active():  # a guard host keeps its own books; counting here would resurface at exit
            return
        stack = [_fmt(f) for f in _stack()[-_STACK_FRAMES:]]

        def build() -> dict:
            return {**_base("touch"), "error_type": error_type, "stack": stack}

        _once("touch", (error_type, tuple(stack)), build)
    except Exception:  # noqa: BLE001 — a recorder must never break the child
        pass


def _peek_site() -> str | None:
    """The first src/nexus frame outside the endpoint helpers, innermost first."""
    nexus = sys.modules.get("nexus")
    if nexus is None or not getattr(nexus, "__file__", None):
        return None
    prefix = os.path.dirname(os.path.realpath(nexus.__file__)) + os.sep
    helpers = {os.path.join(prefix, rel) for rel in _PEEK_HELPER_FILES}
    for frame in reversed(_stack()):
        real = _realpath(frame[0])
        if real.startswith(prefix) and real not in helpers:
            return _fmt(frame)
    return None


def _peek(peek_kind: str) -> None:
    try:
        if not _active():
            return
        site = _peek_site()
        if site is None:
            return
        _once("peek", (peek_kind, site), lambda: {**_base("peek"), "peek_kind": peek_kind, "site": site})
    except Exception:  # noqa: BLE001 — see _record
        pass


def _flush() -> None:
    """At exit: the repeat counts, then the tally of what the cap dropped."""
    try:
        for line in _seen.values():
            if line["n"] > 1:
                _append({**line, "n": line["n"] - 1})
        if _dropped:
            _append({**_base("dropped"), "n": _dropped})
    except Exception:  # noqa: BLE001 — see _record
        pass


def _rebind(old, new) -> None:
    """Swap every module-level binding of ``old`` for ``new`` (``from x import f``
    copies the function object)."""
    for module in list(sys.modules.values()):
        try:
            namespace = vars(module)
            items = list(namespace.items())
        except (TypeError, RuntimeError):
            continue
        for key, value in items:
            if value is old:
                try:
                    namespace[key] = new
                except Exception:  # noqa: BLE001, S110 — a read-only namespace stays as it was
                    pass


def _patch(module) -> None:
    for name in _TARGETS.get(module.__name__, ()):
        cls = getattr(module, name, None)
        if cls is None or getattr(cls.__init__, "_nx_storage_touch_shim", False):
            continue
        original = cls.__init__

        def __init__(self, *args, _original=original, **kwargs):  # noqa: N807
            _original(self, *args, **kwargs)
            _record(type(self).__name__)

        __init__._nx_storage_touch_shim = True  # type: ignore[attr-defined]
        cls.__init__ = __init__
    for name in _PEEK_TARGETS.get(module.__name__, ()):
        original = getattr(module, name, None)
        if original is None or getattr(original, "_nx_storage_touch_shim", False):
            continue
        if name == "get_credential":

            def wrapper(*args, _original=original, **kwargs):
                credential = args[0] if args else kwargs.get("name")
                if isinstance(credential, str) and credential in _PEEK_CREDENTIALS:
                    _peek(f"get_credential:{credential}")
                return _original(*args, **kwargs)
        else:

            def wrapper(*args, _original=original, _name=name, **kwargs):
                _peek(_name)
                return _original(*args, **kwargs)

        wrapper.__name__ = getattr(original, "__name__", name)
        wrapper.__doc__ = getattr(original, "__doc__", None)
        wrapper._nx_storage_touch_shim = True  # type: ignore[attr-defined]
        setattr(module, name, wrapper)
        _rebind(original, wrapper)


class _TouchFinder(importlib.abc.MetaPathFinder):
    """Patches a target module right after it finishes executing."""

    def __init__(self) -> None:
        self._busy: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        if (fullname not in _TARGETS and fullname not in _PEEK_TARGETS) or fullname in self._busy:
            return None
        self._busy.add(fullname)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            self._busy.discard(fullname)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        loader = spec.loader
        original_exec = loader.exec_module

        def exec_module(module, _original=original_exec):
            _original(module)
            _patch(module)

        loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


def _install() -> None:
    if not os.environ.get(_LOG_ENV):
        return
    for name in {*_TARGETS, *_PEEK_TARGETS}:
        module = sys.modules.get(name)
        if module is not None:
            _patch(module)
    sys.meta_path.insert(0, _TouchFinder())
    atexit.register(_flush)
    if _SPAWNING_TEST:
        _append(_base("child_start"))


def _chain() -> None:
    """Run the next ``sitecustomize`` on the path, which this one shadows."""
    for entry in sys.path:
        if not entry or os.path.abspath(entry) == _HERE:
            continue
        candidate = os.path.join(entry, "sitecustomize.py")
        if os.path.isfile(candidate):
            with open(candidate, encoding="utf-8") as fh:
                exec(compile(fh.read(), candidate, "exec"), {"__name__": "sitecustomize", "__file__": candidate})  # noqa: S102
            return


# Separate blocks: a recorder that fails to install must not stop the real
# sitecustomize behind it, and a raising one behind it must not undo ours.
try:
    _install()
except Exception:  # noqa: BLE001 — never break interpreter startup
    pass
try:
    _chain()
except Exception:  # noqa: BLE001 — never break interpreter startup
    pass
