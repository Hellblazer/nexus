# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared fixtures of the worker-process suite (RDR-242 Phase 1 Step 2, nexus-mdbds.4).

* ``verb_table`` builds a fixture verb table (no ``verbs_*`` module is added).
* ``fake_worker`` returns the argv of a stand-in worker, a ``python -I -c``
  program that speaks the frame protocol; it lets a test send what an honest
  worker never would (an ungranted verb, an over-cap frame, a bad frame).
* ``platform_only`` marks a test for one platform and registers it, so
  ``test_platform_coverage`` can prove the marked tests exist and are not
  skipped on the platform running the suite (the non-vacuity guard).
"""

from __future__ import annotations

import sys
import textwrap
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import pytest

from nexus.lua_runtime.runtime import Limits
from nexus.lua_runtime.verb_types import ParamSpec, VerbSpec

#: platform -> test ids registered with ``platform_only``
PLATFORM_TESTS: dict[str, list[str]] = defaultdict(list)


def platform_only(platform: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Run the test only where ``sys.platform`` starts with *platform*."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        PLATFORM_TESTS[platform].append(f"{fn.__module__}.{fn.__name__}")
        return pytest.mark.skipif(
            not sys.platform.startswith(platform), reason=f"{platform} only"
        )(fn)

    return deco


def verb(
    name: str, fn: Callable[..., Any], *types: str, optional_from: int | None = None
) -> VerbSpec:
    params = tuple(
        ParamSpec(f"p{i}", t, optional=optional_from is not None and i >= optional_from)  # type: ignore[arg-type]
        for i, t in enumerate(types)
    )
    return VerbSpec(name, params, fn)


def verb_table(*specs: VerbSpec) -> dict[str, VerbSpec]:
    return {s.name: s for s in specs}


def start(source: str, *grants: str, name: str = "fixture") -> dict[str, Any]:
    return {"name": name, "source": source, "grants": list(grants)}


def limits(wall: float = 20.0, **lowered: int) -> Limits:
    return Limits().lowered(wall_time=wall, **lowered)


_PRELUDE = textwrap.dedent(
    """
    import json, os, signal, struct, sys, time
    from nexus.lua_runtime import frames
    i, o = sys.stdin.buffer, sys.stdout.buffer
    def send(obj, cap=1 << 30):
        frames.write_frame(o, obj, cap)
    def recv(cap=1 << 30):
        return frames.read_frame(i, cap)
    def raw(data):
        o.write(data); o.flush()
    def header(n):
        return struct.pack(">I", n)
    """
)


def fake_worker(body: str) -> tuple[str, ...]:
    """argv of a stand-in worker running *body* after a prelude (``send``, ``recv``, ``raw``, ``header``)."""
    return (sys.executable, "-I", "-c", _PRELUDE + textwrap.dedent(body))


def ready_then(body: str) -> tuple[str, ...]:
    """A stand-in that sends ``ready``, reads the start frame, then runs *body*."""
    return fake_worker('send({"t": "ready"})\nstart = recv()\n' + textwrap.dedent(body))


class Gate:
    """A verb body that blocks until the test opens it; counts entries."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()

    def __call__(self, *args: Any, deadline: float, ctx: Any) -> Any:
        self.entered.set()
        self.opened.wait(60)
        return "late"
