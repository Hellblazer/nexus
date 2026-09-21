# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation layout, for the installed world.

nexus-utpuw.1 (P0). THE LOGIC IS NOT HERE. It is in
:mod:`nexus._install.layout_core`, a stdlib-only module that imports nothing
from nexus, and this module re-exports it under the name the installed world
already imports (``health.py``, ``upgrade_finish.py``, ``install_advice.py``,
``install_census.py``, ``commands/self_cmd.py``).

WHY THE SPLIT EXISTS

``src/nexus/_install/layout.sh`` states the same contract in shell because the
generation builder and the shim writer run from ``scripts/reinstall-tool.sh``
with possibly NOTHING installed, and cannot import nexus. Two statements of one
rule drift until the stale one wins an argument it should not, and the symptom
of THIS rule drifting is not a crash -- it is a doctor reporting green about a
tree nobody is running from. ``tests/test_install_layout_twins_agree.py`` is
what prevents that today.

Moving the logic into ``_install/layout_core.py`` is the first step of removing
the second statement rather than continuing to police it: the constraint was
always importing NEXUS, never running Python, so a module that imports nothing
from nexus can serve the bootstrap caller too. Nothing in the shell half has
changed yet, so the twins pin is still doing its full job.

ON ``InstallLayoutError``

It is an ALIAS for :class:`~nexus._install.layout_core.LayoutError`, not a
subclass of it. One class, one identity: every error this package raises is
raised by the core, so a subclass here would be a name that catches nothing
the core actually throws. In the installed world -- the only world this module
exists in -- ``LayoutError`` derives from ``NexusError``, so the alias is a
genuine member of the nexus hierarchy exactly as the class defined here was.
"""
from __future__ import annotations

from nexus._install.layout_core import (
    # Private, and re-exported deliberately: the twins pin reads this query
    # off THIS module to compare it against the one layout.sh runs verbatim.
    _DECLARED_SCRIPTS_QUERY,
    BIN_DIR_ENV,
    BUILDING_MARKER_NAME,
    CURRENT_LINK_NAME,
    DEPENDENCY_SCRIPTS,
    GENERATION_PREFIX,
    INSTALLER_SCHEMA,
    LAYOUT_USAGE_EXIT,
    LEGACY_GENERATION_NAME,
    NEVER_SHIM,
    PREVIOUS_LINK_NAME,
    RECEIPT_NAME,
    RECEIPT_SCHEMA,
    SHIM_NO_CURRENT_EXIT,
    SOURCE_KINDS,
    TOOLS_DIR_ENV,
    LayoutError,
    Receipt,
    bin_dir,
    build_spec,
    current_generation,
    current_link,
    declared_console_scripts,
    generation_dir,
    is_stale,
    is_under_uv_tool_install,
    legacy_generation_link,
    list_generations,
    owned_shim_names,
    previous_link,
    read_receipt,
    receipt_path,
    reclaimed_shims,
    render_shim,
    tools_dir,
    uv_conexus_venv,
    uv_tool_root,
)

#: The name the installed world catches. See the module docstring: an alias,
#: deliberately, because a subclass would catch nothing the core raises.
InstallLayoutError = LayoutError

__all__ = [
    "BIN_DIR_ENV",
    "BUILDING_MARKER_NAME",
    "CURRENT_LINK_NAME",
    "DEPENDENCY_SCRIPTS",
    "GENERATION_PREFIX",
    "INSTALLER_SCHEMA",
    "LAYOUT_USAGE_EXIT",
    "LEGACY_GENERATION_NAME",
    "NEVER_SHIM",
    "PREVIOUS_LINK_NAME",
    "RECEIPT_NAME",
    "RECEIPT_SCHEMA",
    "SHIM_NO_CURRENT_EXIT",
    "SOURCE_KINDS",
    "TOOLS_DIR_ENV",
    "InstallLayoutError",
    "LayoutError",
    "Receipt",
    "bin_dir",
    "build_spec",
    "current_generation",
    "current_link",
    "declared_console_scripts",
    "generation_dir",
    "is_stale",
    "is_under_uv_tool_install",
    "legacy_generation_link",
    "list_generations",
    "owned_shim_names",
    "previous_link",
    "read_receipt",
    "receipt_path",
    "reclaimed_shims",
    "render_shim",
    "tools_dir",
    "uv_conexus_venv",
    "uv_tool_root",
]
