# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""What a verb is (RDR-242 Technical Design 6, 9 and 10; T2 nexus/rdr242-interfaces A).

Verbs run in the parent. The worker sends a verb name and plain arguments;
the parent checks the grant, then calls
``fn(*check_args(spec, args), deadline=..., ctx=...)`` on a helper thread and
stops waiting at the deadline, so a run returns at its wall-time limit even
when a verb does not cooperate (gate r3 S2). ``check_args`` returns the
arguments normalized to the declared types (a Lua float with no fraction
becomes an ``int`` for an ``integer`` parameter). ``deadline`` is a
``time.monotonic()`` value, the run's remaining wall-time budget: a verb with
an HTTP or subprocess call passes ``max(0, deadline - time.monotonic())`` as
its timeout, and a verb with units of work checks the deadline between them
and raises ``VerbTimeout`` once it has passed (gate r3 S2). A verb fails by
raising ``VerbError`` (or any exception); the script sees a Lua error
carrying the message. ``ctx`` carries what the caller's process knows and the
worker does not: the repository the run was started in.

Standard library only: the worker may import this module.
"""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

ParamType: TypeAlias = Literal["string", "number", "integer", "boolean"]

_PARAM_TYPES: frozenset[str] = frozenset({"string", "number", "integer", "boolean"})

# The parent-to-worker verb-result frame cap (gate r3 S3). Provisional:
# nexus-mdbds.4 sets it from the measured JSON-to-Lua memory ratio under the
# 64 MB max_memory. A verb whose result could exceed it pages or refuses with
# VerbError; it never truncates.
VERB_RESULT_CAP_BYTES: int = 20 * 1024 * 1024

# Lua 5.4 integers are 64-bit.
_LUA_INT_MIN: int = -(2**63)
_LUA_INT_MAX: int = 2**63 - 1

# A verb name is "<namespace>.<member>". A namespace may not shadow a library
# of the sandbox environment (item 7), and neither part may be a Lua keyword,
# since a script could not write it.
_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_LUA_KEYWORDS: frozenset[str] = frozenset(
    "and break do else elseif end false for function goto if in local nil not "
    "or repeat return then true until while".split()
)
_RESERVED_NAMESPACES: frozenset[str] = frozenset(
    {"string", "table", "math", "utf8", "coroutine", "debug", "os", "io", "_G", "_ENV"}
)


class VerbError(Exception):
    """A verb failed; the message reaches the script as a Lua error string."""


class VerbTimeout(VerbError):
    """A verb saw its deadline pass. The parent ends the run on wall time."""


class VerbArgError(VerbError):
    """A call's arguments do not match the verb's parameters."""


@dataclass(frozen=True)
class VerbContext:
    """What a verb may need from the caller's process.

    ``repo_root`` is the top of the git work tree ``run_script`` was called
    in (the parent's, never the worker's temp-directory cwd), or ``None``
    outside a repository.
    """

    repo_root: Path | None


def encode_result(value: JSONValue) -> bytes:
    """The one size measure for a verb result: compact UTF-8 JSON.

    The parent sends exactly these bytes; a verb that pages measures with it.
    Raises ``VerbError`` on a non-JSON or non-finite value and on a result
    over ``VERB_RESULT_CAP_BYTES``.
    """
    try:
        data = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VerbError(f"verb result is not JSON: {exc}") from exc
    if len(data) > VERB_RESULT_CAP_BYTES:
        raise VerbError(
            f"verb result is {len(data)} bytes, over the {VERB_RESULT_CAP_BYTES} cap"
        )
    return data


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType
    # optional: nil (JSON null) is allowed, and so is omitting a trailing one
    optional: bool = False

    def __post_init__(self) -> None:
        if self.type not in _PARAM_TYPES:
            raise ValueError(f"parameter {self.name!r}: unknown type {self.type!r}")


@dataclass(frozen=True)
class VerbSpec:
    name: str  # "<namespace>.<member>", e.g. "memory.get"
    params: tuple[ParamSpec, ...]
    fn: Callable[..., JSONValue]

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"verb name {self.name!r} is not <namespace>.<member>")
        ns, member = self.name.split(".")
        if ns in _RESERVED_NAMESPACES or ns in _LUA_KEYWORDS or member in _LUA_KEYWORDS:
            raise ValueError(f"verb name {self.name!r} uses a reserved name")
        if not callable(self.fn):
            raise ValueError(f"verb {self.name!r}: fn is not callable")
        _check_fn_signature(self.name, self.fn)
        names = [p.name for p in self.params]
        if len(set(names)) != len(names):
            raise ValueError(f"verb {self.name!r}: duplicate parameter name")
        seen_optional = False
        for p in self.params:
            if p.optional:
                seen_optional = True
            elif seen_optional:
                raise ValueError(
                    f"verb {self.name!r}: required parameter {p.name!r} "
                    "follows an optional one"
                )


def _check_fn_signature(name: str, fn: Callable[..., JSONValue]) -> None:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return  # a builtin with no signature: the first call shows it
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return
    keyword = {
        p.name for p in params if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
    }
    missing = sorted({"deadline", "ctx"} - keyword)
    if missing:
        raise ValueError(f"verb {name!r}: fn takes no keyword {', '.join(missing)}")


def _normalize(spec: VerbSpec, param: ParamSpec, value: object) -> object:
    def refuse() -> VerbError:
        return VerbArgError(
            f"{spec.name}: {param.name} must be a {param.type}, "
            f"got {type(value).__name__}"
        )

    if isinstance(value, bool):
        if param.type != "boolean":
            raise refuse()
        return value
    if param.type == "string":
        if not isinstance(value, str):
            raise refuse()
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise VerbArgError(
                f"{spec.name}: {param.name} is not valid UTF-8"
            ) from None
        return value
    if param.type == "boolean":
        raise refuse()
    if not isinstance(value, (int, float)):
        raise refuse()
    if isinstance(value, float) and not math.isfinite(value):
        raise VerbArgError(f"{spec.name}: {param.name} must be finite")
    if param.type == "number":
        if isinstance(value, int) and not _LUA_INT_MIN <= value <= _LUA_INT_MAX:
            raise VerbArgError(f"{spec.name}: {param.name} is outside the 64-bit range")
        return value
    # integer: a Lua integer, or a Lua float with no fraction (10/2 is 5.0)
    if isinstance(value, float):
        if not value.is_integer():
            raise refuse()
        value = int(value)
    if not _LUA_INT_MIN <= value <= _LUA_INT_MAX:
        raise VerbArgError(f"{spec.name}: {param.name} is outside the 64-bit range")
    return value


def check_args(spec: VerbSpec, args: object) -> tuple[object, ...]:
    """Check a call against ``spec`` and return its arguments normalized.

    ``args`` must be the JSON array the worker sent. Raises ``VerbError``
    naming the verb and the parameter on a wrong count or type
    (``VerbArgError``, a ``VerbError``). ``args`` is a JSON array with
    ``null`` for nil; trailing nils may be omitted. An omitted
    trailing optional is returned as ``None``.
    """
    if not isinstance(args, list):
        raise VerbArgError(f"{spec.name}: arguments must be a list")
    required = sum(1 for p in spec.params if not p.optional)
    if not required <= len(args) <= len(spec.params):
        raise VerbArgError(
            f"{spec.name} takes {len(spec.params)} argument(s) "
            f"({required} required), got {len(args)}"
        )
    out: list[object] = []
    for i, param in enumerate(spec.params):
        value = args[i] if i < len(args) else None
        if value is None:
            if not param.optional:
                raise VerbArgError(f"{spec.name}: {param.name} is required")
            out.append(None)
            continue
        out.append(_normalize(spec, param, value))
    return tuple(out)
