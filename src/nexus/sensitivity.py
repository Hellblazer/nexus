# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sensitivity scope (RDR-243 Technical Design item 5, nexus-rfvyy.6.13).

A surface that hands document-derived text to a model enters :func:`llm_bound`
with its registry name (``src/nexus/tables/llm-surfaces.toml``). The three
dispatch roots, ``claude_dispatch``, ``run_text_sync`` and ``_run_claude_isolated``,
call :func:`assert_llm_bound` before any side effect, so a caller that was never
wrapped fails at its first LLM call instead of silently running outside the
policy. Phase 2 reads :func:`current_posture` once per entry point and passes the
value down to its reads.

The posture lives in a ``ContextVar``: visible on the thread that entered the scope
and on threads that were handed a copy of its context (``asyncio`` tasks and
``asyncio.to_thread`` copy it; ``loop.run_in_executor`` and ``threading.Thread`` do
not). When the variable is unset the process environment decides: a child process
started by a scoped parent carries ``NX_SENSITIVITY_POSTURE=llm_bound`` (exported
by ``_build_dispatch_env``; the nested ``nx-mcp`` also enters the scope per tool call,
``nexus.mcp._sensitivity_posture``) and is in the scope from its first line.

Imports nothing beyond the standard library: this module sits on the dispatch hot
path and on CLI startup.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import os
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any, Final, Literal, TypeVar

__all__ = [
    "DispatchOutsideSensitivityScope", "POSTURE_ENV", "Posture", "ReadSensitivity", "SensitiveDocumentRefused",
    "CHILD_SURFACE", "assert_llm_bound", "bound_to", "child_scope", "check_read_sensitivity", "current_posture", "current_surface", "llm_bound",
    "read_sensitivity",
]

Posture = Literal["default", "llm_bound"]

#: The ``sensitivity`` argument every vector read that returns chunk text takes (RDR-243 Phase 2, nexus-rfvyy.7.8):
#: ``exclude`` asks the engine to drop chunks with a live sensitive owner, ``include`` reads as before.
ReadSensitivity = Literal["exclude", "include"]

#: The environment variable a child process reads its posture from. It is for nexus parents only: a scoped
#: nexus process exports it to the children it starts. Setting it by hand (a shell, a hook) puts the process
#: in the scope without any surface having entered it, which only silences the dispatch-root bug detector
#: (``assert_llm_bound``: a caller that was never wrapped in ``llm_bound`` would otherwise fail at its first
#: LLM call). It cannot grant protection: the document gates read the catalog flag whatever the posture, and
#: Phase 2 reads exclude under ``llm_bound`` by the surface's own entry, never from this variable alone.
POSTURE_ENV: Final = "NX_SENSITIVITY_POSTURE"

#: The phrase every ``SensitivityExcludeUnavailable`` message carries (RDR-243 review r1 F2). A tool boundary renders
#: the exception to text, so the plan runner recognises a refused read by this phrase and fails the plan instead of
#: treating the error text as an empty result.
EXCLUDE_REFUSED_MARKER: Final = "sensitivity='exclude' read refused"

#: ``None`` means "never set on this context": the environment is then consulted.
_POSTURE: ContextVar[Posture | None] = ContextVar("nexus_sensitivity_posture", default=None)
_SURFACE: ContextVar[str | None] = ContextVar("nexus_sensitivity_surface", default=None)

_F = TypeVar("_F", bound=Callable[..., Any])


class DispatchOutsideSensitivityScope(Exception):
    """A dispatch root ran with the posture ``default``: its caller never entered :func:`llm_bound`."""

    def __init__(self, root: str, surface: str | None = None) -> None:
        self.root = root
        self.surface = surface
        super().__init__(
            f"{root} was called outside nexus.sensitivity.llm_bound: wrap the surface that reaches it "
            f"in llm_bound('<surface name from src/nexus/tables/llm-surfaces.toml>')"
        )


class SensitiveDocumentRefused(Exception):
    """A surface refused a sensitive document. Defined with the scope; the document gates raise it (Phase 1 Step 4b)."""

    def __init__(self, tumbler: str, surface: str | None = None) -> None:
        self.tumbler = tumbler
        self.surface = surface
        super().__init__(f"document {tumbler} is sensitive: surface {surface or '<unnamed>'} refuses it")


def current_posture() -> Posture:
    """``llm_bound`` inside :func:`llm_bound`; else the environment's ``NX_SENSITIVITY_POSTURE``
    when it is exactly ``llm_bound``; else ``default``."""
    value = _POSTURE.get()
    if value is not None:
        return value
    return "llm_bound" if os.environ.get(POSTURE_ENV) == "llm_bound" else "default"


def read_sensitivity(posture: Posture | None = None) -> ReadSensitivity:
    """The ``sensitivity`` value a read takes for *posture*: ``llm_bound`` excludes, ``default`` includes.

    An entry point calls this ONCE, on its own thread, and hands the value down as an argument. Reads never look at
    the ContextVar themselves: a worker thread started with ``threading.Thread`` or ``run_in_executor`` does not
    inherit it (RDR-243 A7). With no *posture* it reads :func:`current_posture` now."""
    return "exclude" if (current_posture() if posture is None else posture) == "llm_bound" else "include"


def check_read_sensitivity(value: object) -> None:
    """Raise ``ValueError`` for anything but ``"exclude"`` or ``"include"``: ``None`` or a typo must not read as include."""
    if not isinstance(value, str) or value not in ("exclude", "include"):
        raise ValueError(f"sensitivity must be 'exclude' or 'include', got {value!r}")


def current_surface() -> str | None:
    """The registry name of the innermost :func:`llm_bound` on this context, or ``None``."""
    return _SURFACE.get()


@contextlib.contextmanager
def llm_bound(surface: str) -> Iterator[None]:
    """Enter the scope for *surface*. Nests: the innermost name wins and the outer one returns on exit."""
    posture_token = _POSTURE.set("llm_bound")
    surface_token = _SURFACE.set(surface)
    try:
        yield
    finally:
        _SURFACE.reset(surface_token)
        _POSTURE.reset(posture_token)


#: The scope name a nested ``nx-mcp`` server enters when its parent dispatched it with
#: ``NX_SENSITIVITY_POSTURE=llm_bound`` (RDR-243 Phase 2 Step 2.5). It is not a registry row: the child
#: makes no LLM call of its own, it serves reads to a parent surface that does.
CHILD_SURFACE: Final = "nx_mcp_child"


@contextlib.contextmanager
def child_scope() -> Iterator[None]:
    """Enter :func:`llm_bound` as a scoped parent's child, under :data:`CHILD_SURFACE`."""
    with llm_bound(CHILD_SURFACE):
        yield


def assert_llm_bound(root: str) -> None:
    """Raise :class:`DispatchOutsideSensitivityScope` unless :func:`current_posture` is ``llm_bound``."""
    if current_posture() != "llm_bound":
        raise DispatchOutsideSensitivityScope(root, surface=current_surface())


def bound_to(surface: str) -> Callable[[_F], _F]:
    """Decorator form of :func:`llm_bound` for a surface entry point (sync or async).

    The wrapper is a coroutine function exactly when the wrapped one is, and
    ``functools.wraps`` keeps the signature visible to ``inspect.signature``."""

    def decorate(fn: _F) -> _F:
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with llm_bound(surface):
                    return await fn(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with llm_bound(surface):
                return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
