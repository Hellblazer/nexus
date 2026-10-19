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

Self-contained on purpose: a child may not be able to import the repo's
``tests`` package, and a shim must never take a child down, so a failure here
is swallowed. The class table below is a copy of
``tests/_storage_touch_guard.py::GUARDED``; a test keeps the two equal.

Inert without ``NX_STORAGE_TOUCH_CHILD_LOG``. A process that hosts a guard
itself (``NX_STORAGE_TOUCH_GUARD_PID`` equal to its pid, e.g. an xdist worker,
which inherits this shim) records in-process and skips the file.
"""
from __future__ import annotations

import importlib.abc
import importlib.util
import json
import os
import sys
import traceback

_LOG_ENV = "NX_STORAGE_TOUCH_CHILD_LOG"
_OWNER_ENV = "NX_STORAGE_TOUCH_GUARD_PID"
_STACK_FRAMES = 6

#: module -> guarded class names. Mirrors GUARDED in the guard module.
_TARGETS: dict[str, tuple[str, ...]] = {
    "nexus.db.service_endpoint": ("ServiceEndpointUnresolvableError", "ServiceTokenUnresolvableError"),
    "nexus.db.t1": ("T1ServerNotFoundError",),
    "nexus.db.managed_endpoint": ("ManagedServiceUnreachable", "ManagedServiceIncompatible"),
}

_HERE = os.path.dirname(os.path.abspath(__file__))


def _record(error_type: str) -> None:
    try:
        path = os.environ.get(_LOG_ENV)
        if not path or os.environ.get(_OWNER_ENV) == str(os.getpid()):
            return
        frames = [
            f for f in traceback.extract_stack()
            if os.path.dirname(os.path.abspath(f.filename)) != _HERE
        ]
        line = json.dumps({
            "pid": os.getpid(),
            "current_test": os.environ.get("PYTEST_CURRENT_TEST", ""),
            "error_type": error_type,
            "argv0": sys.argv[0] if sys.argv else "",
            "stack": [f"{f.filename}:{f.lineno} in {f.name}" for f in frames[-_STACK_FRAMES:]],
        }) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())  # one write: atomic enough for O_APPEND lines
        finally:
            os.close(fd)
    except Exception:  # noqa: BLE001 — a recorder must never break the child
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


class _TouchFinder(importlib.abc.MetaPathFinder):
    """Patches a target module right after it finishes executing."""

    def __init__(self) -> None:
        self._busy: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _TARGETS or fullname in self._busy:
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
    for name in _TARGETS:
        module = sys.modules.get(name)
        if module is not None:
            _patch(module)
    sys.meta_path.insert(0, _TouchFinder())


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


try:
    _install()
    _chain()
except Exception:  # noqa: BLE001 — never break interpreter startup
    pass
