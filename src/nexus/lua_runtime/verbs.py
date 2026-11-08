# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The verb table (RDR-242 Technical Design 9 and 10).

Each step ships its verbs in its own ``nexus.lua_runtime.verbs_<area>``
module exporting ``VERBS: tuple[VerbSpec, ...]``; a module whose name starts
``verbs_`` must export it. This module finds those modules by name, in sorted
order, so no step edits a shared file, and builds ``VERB_TABLE``, refusing a
duplicate name. A ``verbs_*`` module imports ``verb_types``, never this
module. The parent imports it; the worker never does (it gets verb names and
arities in the start frame).
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable, Mapping
from types import MappingProxyType, ModuleType

import nexus.lua_runtime as _pkg
from nexus.lua_runtime.verb_types import VerbSpec


def build_table(modules: Iterable[ModuleType | object]) -> Mapping[str, VerbSpec]:
    table: dict[str, VerbSpec] = {}
    for module in modules:
        label = getattr(module, "__name__", repr(module))
        specs = getattr(module, "VERBS", None)
        if specs is None:
            raise ValueError(f"verb module {label} exports no VERBS")
        for spec in specs:
            if not isinstance(spec, VerbSpec):
                raise ValueError(f"verb module {label}: {spec!r} is not a VerbSpec")
            if spec.name in table:
                raise ValueError(f"verb {spec.name!r} is defined twice")
            table[spec.name] = spec
    return MappingProxyType(table)


def verb_modules(package: ModuleType = _pkg) -> list[ModuleType]:
    names = sorted(
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if info.name.startswith("verbs_")
    )
    modules = []
    for name in names:
        qualified = f"{package.__name__}.{name}"
        try:
            modules.append(importlib.import_module(qualified))
        except Exception as exc:
            raise RuntimeError(f"verb module {qualified} failed to import") from exc
    return modules


VERB_TABLE: Mapping[str, VerbSpec] = build_table(verb_modules())
