# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Declarative auras (RDR-249): the aura file parser and loader."""
from __future__ import annotations

from nexus.auras.aura import AURA_SUFFIX, KINDS, ArgSource, Aura, AuraError, parse_aura
from nexus.auras.enable import ENV_OFF, AuraConfigError, Enablement, Level, is_enabled, resolve
from nexus.auras.loader import AuraEntry, enumerate_auras, list_auras, load_aura

__all__ = [
    "AURA_SUFFIX", "ENV_OFF", "KINDS", "ArgSource", "Aura", "AuraConfigError", "AuraEntry", "AuraError",
    "Enablement", "Level", "enumerate_auras", "is_enabled", "list_auras", "load_aura", "parse_aura", "resolve",
]
