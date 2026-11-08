# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""What a verb is (RDR-242 Technical Design 6, 9 and 10; T2 nexus/rdr242-interfaces A).

Verbs run in the parent. The worker sends a verb name and plain arguments,
and the parent checks the call against the verb's ``VerbSpec`` before
running ``fn(*args, deadline=...)``. ``deadline`` is a ``time.monotonic()``
value: the run's remaining wall-time budget. A verb with an HTTP or
subprocess call passes ``max(0, deadline - time.monotonic())`` as its
timeout; a verb with units of work checks the deadline between them and
raises ``VerbTimeout`` once it has passed (gate r3 S2). A verb fails by raising
``VerbError`` (or any exception); the script sees a Lua error carrying the
message.

Standard library only: the worker may import this module.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

ParamType: TypeAlias = Literal["string", "number", "integer", "boolean"]

# The parent-to-worker verb-result frame cap (gate r3 S3). Provisional:
# nexus-mdbds.4 sets it from the measured JSON-to-Lua memory ratio under the
# 64 MB max_memory. A verb whose result could exceed it pages or refuses with
# VerbError; it never truncates.
VERB_RESULT_CAP_BYTES: int = 20 * 1024 * 1024


class VerbError(Exception):
    """A verb failed; the message reaches the script as a Lua error string."""


class VerbTimeout(VerbError):
    """A verb saw its deadline pass."""


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: ParamType
    # optional: nil (JSON null) is allowed, and so is omitting a trailing one
    optional: bool = False


@dataclass(frozen=True)
class VerbSpec:
    name: str  # "<namespace>.<member>", e.g. "memory.get"
    params: tuple[ParamSpec, ...]
    fn: Callable[..., JSONValue]


def _type_ok(value: object, type_: ParamType) -> bool:
    if isinstance(value, bool):
        return type_ == "boolean"
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "integer":
        return isinstance(value, int) or (
            isinstance(value, float) and value.is_integer()
        )
    if type_ == "number":
        return isinstance(value, (int, float))
    return False


def check_args(spec: VerbSpec, args: Sequence[object]) -> None:
    """Refuse a call whose count or argument types do not match ``spec``.

    Raises ``VerbError`` naming the verb and the parameter. A Lua integer
    can arrive as a JSON float with no fraction, so ``integer`` accepts it.
    """
    required = sum(1 for p in spec.params if not p.optional)
    if not required <= len(args) <= len(spec.params):
        raise VerbError(
            f"{spec.name} takes {len(spec.params)} argument(s) "
            f"({required} required), got {len(args)}"
        )
    for param, value in zip(spec.params, args):
        if value is None:
            if param.optional:
                continue
            raise VerbError(f"{spec.name}: {param.name} is required")
        if not _type_ok(value, param.type):
            raise VerbError(
                f"{spec.name}: {param.name} must be a {param.type}, "
                f"got {type(value).__name__}"
            )
