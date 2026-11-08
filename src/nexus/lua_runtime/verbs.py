# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The verb table (RDR-242 Technical Design 9 and 10).

Each step ships its verbs in its own ``nexus.lua_runtime.verbs_<area>``
module exporting ``VERBS: tuple[VerbSpec, ...]``. This module finds those
modules by name, so no step edits a shared file, and builds ``VERB_TABLE``,
refusing a duplicate name. The parent imports it; the worker never does (it
gets verb names and arities in the start frame).
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from collections.abc import Iterable, Mapping
from types import ModuleType

import nexus.lua_runtime as _pkg
from nexus.lua_runtime.verb_types import VerbSpec

_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def build_table(modules: Iterable[ModuleType | object]) -> dict[str, VerbSpec]:
    table: dict[str, VerbSpec] = {}
    for module in modules:
        for spec in getattr(module, "VERBS"):
            if not _NAME.match(spec.name):
                raise ValueError(f"verb name {spec.name!r} is not <namespace>.<member>")
            if spec.name in table:
                raise ValueError(f"verb {spec.name!r} is defined twice")
            table[spec.name] = spec
    return table


def _verb_modules() -> list[ModuleType]:
    names = sorted(
        info.name
        for info in pkgutil.iter_modules(_pkg.__path__)
        if info.name.startswith("verbs_")
    )
    return [importlib.import_module(f"{_pkg.__name__}.{n}") for n in names]


VERB_TABLE: Mapping[str, VerbSpec] = build_table(_verb_modules())
