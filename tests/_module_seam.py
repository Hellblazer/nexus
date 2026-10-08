# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fake what one module sees of a stdlib module, without touching the stdlib module.

``patch("nexus.retry.time.sleep")``, ``patch("nexus.x.subprocess.run")`` and
``monkeypatch.setattr(mod.subprocess, "run", f)`` do not patch ``nexus.retry`` or
``mod``: ``mod.subprocess`` IS the one ``subprocess`` module, so they replace
``subprocess.run`` for the whole process. Every other thread, fixture and teardown
in the worker sees the fake while it is live. ``subprocess.run(timeout=...)``
waits in a loop around ``time.sleep``, so a substrate teardown that ran under a
test's blocking fake sleep hung its xdist worker for 50 minutes (nexus-hkafl).
The bare forms (``patch("subprocess.run")``, ``monkeypatch.setattr(sys,
"platform", "win32")``) are the same thing spelled directly.

The helpers here install a :class:`ModuleProxy` as the module's own global for
that name instead (``mod.subprocess``, ``mod.time``, ...). The proxy forwards
every attribute to the real module except the ones a test sets on it, so the
code under test sees the fake and nothing else does. A dotted path reaches into
a submodule the same way: ``"os.path.exists"`` gives ``mod`` a private ``os``
whose ``path`` is a private ``os.path``.

* :func:`patch_in` is ``unittest.mock.patch`` for such a path: context manager,
  decorator (it passes the mock in, as ``patch`` does), ``start``/``stop``.
* :func:`setattr_in` is ``monkeypatch.setattr`` for such a path, and
  :func:`delattr_in` is ``monkeypatch.delattr``.
* :func:`module_proxy` returns the installed proxy to set attributes on.
* :func:`module_time` and :func:`patch_time` are those for ``time``.

The module argument can be a module, a dotted module name, or a sequence of
them when more than one module's binding must see the fake.
``tests/test_module_seam.py`` pins the locality and lints ``tests/`` for the
global forms.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from types import ModuleType
from typing import Any
from unittest.mock import DEFAULT, _patch  # noqa: PLC2701 - subclassed to keep patch()'s full surface

import pytest

ModuleRef = ModuleType | str
ModuleRefs = ModuleRef | Sequence[ModuleRef]

#: Stored on a proxy to make an attribute absent there (:func:`delattr_in`).
_ABSENT = object()

#: A test faking ``nexus.retry``'s sleep has always faked two more layers through
#: the global module, and keeps doing so by sharing the proxy: the default
#: ``RateLimitBrake``, whose sleep and clock are looked up in ``nexus.rate_brake``'s
#: own ``time`` global when used (nexus-q81g7), and the vector client's gateway
#: retry under the wrapper (``nexus.db.http_vector_client``'s 502/503/504 sleeps).
_COUPLED: dict[tuple[str, str], tuple[str, ...]] = {
    ("nexus.retry", "time"): ("nexus.rate_brake", "nexus.db.http_vector_client"),
}


class ModuleProxy:
    """Stands in for one module inside one or more other modules.

    Attributes set on an instance override; everything else is the real
    module's."""

    def __init__(self, real: ModuleType) -> None:
        object.__setattr__(self, "_seam_real", real)

    def __getattribute__(self, name: str) -> Any:
        own = object.__getattribute__(self, "__dict__")
        if name in own:
            if own[name] is _ABSENT:
                raise AttributeError(f"{name!r} is hidden from this module by a test")
            return own[name]
        if name.startswith("__") and name.endswith("__"):
            return object.__getattribute__(self, name)
        return getattr(own["_seam_real"], name)

    def __repr__(self) -> str:
        real = object.__getattribute__(self, "_seam_real")
        return f"<ModuleProxy for {real.__name__}>"


def _resolve(module: ModuleRef) -> ModuleType:
    return importlib.import_module(module) if isinstance(module, str) else module


def _targets(modules: ModuleRefs, binding: str, also: Sequence[ModuleRef] = ()) -> list[ModuleType]:
    refs = [modules] if isinstance(modules, str | ModuleType) else list(modules)
    mods: list[ModuleType] = []
    for ref in [*refs, *also]:
        mod = _resolve(ref)
        for m in (mod, *map(_resolve, _COUPLED.get((mod.__name__, binding), ()))):
            if m not in mods:
                mods.append(m)
    return mods


def _proxy_for(mods: list[ModuleType], binding: str) -> ModuleProxy:
    real: Any = None
    for mod in mods:
        if binding not in vars(mod):
            raise AttributeError(
                f"{mod.__name__} has no module-level {binding!r} to replace; if it does "
                f"'from {binding} import ...', patch that name on {mod.__name__} directly",
            )
        current = vars(mod)[binding]
        if isinstance(current, ModuleProxy):
            return current
        if not isinstance(current, ModuleType):
            raise TypeError(f"{mod.__name__}.{binding} is {type(current).__name__}, not a module")
        if real is not None and current is not real:
            raise TypeError(f"{binding!r} names different modules in {[m.__name__ for m in mods]}")
        real = current
    return ModuleProxy(real)


_Setter = Callable[[Any, str, Any], None]


def _bind(mods: list[ModuleType], binding: str, setter: _Setter) -> ModuleProxy:
    proxy = _proxy_for(mods, binding)
    for mod in mods:
        if vars(mod)[binding] is not proxy:
            setter(mod, binding, proxy)
    return proxy


def _install(mods: list[ModuleType], path: str, setter: _Setter) -> tuple[ModuleProxy, str]:
    """Bind a proxy for ``path``'s first segment in each of ``mods`` and a nested
    proxy for each further module segment; returns the leaf proxy and attribute."""
    if "." not in path:
        raise ValueError(f"path must be '<module>.<attr>', got {path!r}")
    binding, *middle, attr = path.split(".")
    leaf = _bind(mods, binding, setter)
    for name in middle:
        current = vars(leaf).get(name)
        if not isinstance(current, ModuleProxy):
            sub = getattr(leaf, name)
            if not isinstance(sub, ModuleType):
                raise TypeError(f"{path}: {name!r} is not a module")
            current = ModuleProxy(sub)
            setter(leaf, name, current)
        leaf = current
    return leaf, attr


def module_proxy(
    monkeypatch: pytest.MonkeyPatch, module: ModuleRefs, binding: str, *also: ModuleRef,
) -> ModuleProxy:
    """Give ``module`` (and ``also``) a private ``binding`` for the rest of the test.

    Returns the proxy; set what to fake on it::

        module_proxy(monkeypatch, sub, "subprocess").run = fake_run

    Calling it again for a module that already has a proxy returns that proxy.
    ``monkeypatch`` restores each binding at teardown, and the stdlib module is
    never modified."""
    return _bind(_targets(module, binding, also), binding, monkeypatch.setattr)


def setattr_in(
    monkeypatch: pytest.MonkeyPatch, module: ModuleRefs, path: str, value: Any, *, raising: bool = True,
) -> None:
    """``monkeypatch.setattr(<module>.<path>, value)`` kept local to ``module``.

    ``setattr_in(monkeypatch, sub, "subprocess.run", fake)`` replaces what ``sub``
    sees as ``subprocess.run`` and nothing else."""
    mods = _targets(module, path.split(".", 1)[0])
    leaf, attr = _install(mods, path, monkeypatch.setattr)
    if raising and not hasattr(leaf, attr):
        raise AttributeError(f"{path} does not exist")
    monkeypatch.setattr(leaf, attr, value, raising=False)


def delattr_in(monkeypatch: pytest.MonkeyPatch, module: ModuleRefs, path: str, *, raising: bool = True) -> None:
    """``monkeypatch.delattr(<module>.<path>)`` kept local to ``module``: the
    attribute is absent (``hasattr`` is False, access raises ``AttributeError``)
    as ``module`` sees it, e.g. ``os.killpg`` on a Windows-shaped run."""
    mods = _targets(module, path.split(".", 1)[0])
    leaf, attr = _install(mods, path, monkeypatch.setattr)
    if raising and not hasattr(leaf, attr):
        raise AttributeError(f"{path} does not exist")
    monkeypatch.setattr(leaf, attr, _ABSENT, raising=False)


class _SeamPatch(_patch):
    """``unittest.mock.patch`` whose target is a proxy bound into the module(s)
    only while the patch is active."""

    def __init__(
        self, module: ModuleRefs, path: str, also: Sequence[ModuleRef], new: Any, spec: Any, create: bool,
        spec_set: Any, autospec: Any, new_callable: Any, kwargs: dict[str, Any],
    ) -> None:
        self._seam_args = (module, path, tuple(also))
        self._seam_undo: list[tuple[Any, str, Any]] = []
        self._seam_leaf: ModuleProxy | None = None
        attr = path.rsplit(".", 1)[-1]
        super().__init__(
            lambda: self._seam_leaf, attr, new, spec, create, spec_set, autospec, new_callable, kwargs,
        )

    def copy(self) -> _SeamPatch:
        module, path, also = self._seam_args
        patcher = _SeamPatch(
            module, path, also, self.new, self.spec, self.create, self.spec_set, self.autospec,
            self.new_callable, self.kwargs,
        )
        patcher.attribute_name = self.attribute_name
        patcher.additional_patchers = [p.copy() for p in self.additional_patchers]
        return patcher

    def _seam_set(self, obj: Any, name: str, value: Any) -> None:
        self._seam_undo.append((obj, name, vars(obj).get(name, _MISSING)))
        setattr(obj, name, value)

    def _seam_restore(self) -> None:
        while self._seam_undo:
            obj, name, old = self._seam_undo.pop()
            if old is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, old)
        self._seam_leaf = None

    def __enter__(self) -> Any:
        module, path, also = self._seam_args
        if self._seam_leaf is None:
            self._seam_leaf, _ = _install(_targets(module, path.split(".", 1)[0], also), path, self._seam_set)
        try:
            return super().__enter__()
        except BaseException:
            self._seam_restore()
            raise

    def __exit__(self, *exc_info: Any) -> Any:
        try:
            return super().__exit__(*exc_info)
        finally:
            self._seam_restore()


_MISSING = object()


def patch_in(
    module: ModuleRefs,
    path: str,
    new: Any = DEFAULT,
    *,
    also: Sequence[ModuleRef] = (),
    spec: Any = None,
    create: bool = False,
    spec_set: Any = None,
    autospec: Any = None,
    new_callable: Any = None,
    **kwargs: Any,
) -> Any:
    """``patch("<module>.<path>")`` kept local to ``module``.

    ``patch_in("nexus.x", "subprocess.run", side_effect=f)`` fakes ``subprocess.run``
    as ``nexus.x`` sees it. Same arguments and same three uses (``with``,
    decorator, ``start()``) as ``unittest.mock.patch``; the module is imported
    when the patch starts, as ``patch`` imports its target."""
    return _SeamPatch(module, path, also, new, spec, create, spec_set, autospec, new_callable, kwargs)


def module_time(monkeypatch: pytest.MonkeyPatch, module: ModuleRefs, *also: ModuleRef) -> ModuleProxy:
    """:func:`module_proxy` for ``time``: ``module_time(mp, retry_mod).sleep = f``."""
    return module_proxy(monkeypatch, module, "time", *also)


@contextmanager
def patch_time(
    module: ModuleRefs,
    attr: str,
    new: Any = DEFAULT,
    *,
    also: tuple[ModuleRef, ...] = (),
    **mock_kwargs: Any,
) -> Iterator[Any]:
    """Context-manager twin of ``patch("<module>.time.<attr>")`` that stays local.

    Yields the replacement: a ``MagicMock(**mock_kwargs)`` unless ``new`` is
    given, exactly as ``unittest.mock.patch`` would."""
    with patch_in(module, f"time.{attr}", new, also=also, **mock_kwargs) as value:
        yield value
