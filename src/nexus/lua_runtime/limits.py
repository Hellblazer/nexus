# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The limits of a script run (RDR-242 Technical Design 11): defaults, each lowered by the caller and never raised.

Standard library only: the worker, the parent and the runtime all import this.
A limit is a finite number above zero. ``inf`` and ``nan`` reach the parent's
timer and ``lupa`` as ``OverflowError`` and ``ValueError``, and a memory limit
of zero or below reaches ``lupa`` as "no limit" (``max_memory`` 2**64), so
they are refused where the limit is made.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

_MIB = 1024 * 1024

# The lowest memory limit a run may have. Below about 130 KB lupa aborts the
# worker (exit 134, "PANIC: unprotected error in call to Lua API (not enough
# memory)") while it pushes the source and builds the runtime outside any
# protected call, so no limit there can be a clean ScriptLimitError (nexus-mdbds.5
# review r1). 1 MiB keeps every run well above it.
MIN_MEMORY: int = 1 * _MIB


@dataclass(frozen=True)
class Limits:
    """Technical Design 11's defaults, each lowered by the caller and never raised."""

    instructions: int = 50_000_000
    memory: int = 64 * _MIB
    wall_time: float = 60.0
    result: int = 1 * _MIB
    depth: int = 32

    def __post_init__(self) -> None:
        # A limit is a finite number above zero. inf and nan reach the parent's timer and lupa as OverflowError and
        # ValueError, and 0 or a negative memory limit reaches lupa as "no limit" (max_memory 2**64).
        for key, value in self.__dict__.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    f"limit {key} must be a finite number above zero, got {value!r}"
                )
        if self.memory < MIN_MEMORY:
            raise ValueError(
                f"limit memory must be at least {MIN_MEMORY} bytes, got {self.memory!r}"
            )

    def lowered(self, **changes: float) -> Limits:
        values = dict(self.__dict__)
        for key, value in changes.items():
            if key not in values:
                raise ValueError(f"unknown limit {key!r}")
            if value > values[key]:
                raise ValueError(
                    f"limit {key} can be lowered, not raised ({value} > {values[key]})"
                )
            try:
                values[key] = type(values[key])(value)
            except (ValueError, OverflowError, TypeError):
                raise ValueError(
                    f"limit {key} must be a finite number above zero, got {value!r}"
                ) from None
        return Limits(**values)

    def to_frame(self) -> dict[str, float]:
        return dict(self.__dict__)

    @classmethod
    def from_frame(cls, raw: dict[str, Any]) -> Limits:
        base = cls()
        return cls(
            **{k: type(v)(raw[k]) if k in raw else v for k, v in base.__dict__.items()}
        )


DEFAULT_LIMITS = Limits()


__all__ = ["DEFAULT_LIMITS", "Limits"]
