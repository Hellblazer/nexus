# SPDX-License-Identifier: AGPL-3.0-or-later
"""Documentation links for ``--help`` epilogs (nexus-z7ddd).

A user with ``conexus`` from PyPI has no ``docs/`` tree, so help text points at the GitHub page. Click rewraps
option help and docstring prose to the terminal width, and a 64- or 97-character URL does not fit an option
column: it came out as ``docs/collection`` newline ``s.md``. An epilog that starts with a ``\\b`` line is printed
as written, so a URL there pastes into a browser whole. ``tests/test_repo_only_references_lint.py`` renders every
command's ``--help`` at 80 columns and fails on a URL that comes out broken.
"""
from __future__ import annotations

_BASE = "https://github.com/Hellblazer/nexus/blob/main/docs/"


def link_epilog(label: str, page: str) -> str:
    """An epilog that prints *page* (a path under ``docs/``) on its own line, never rewrapped."""
    return f"\b\n{label}:\n{_BASE}{page}"


COLLECTIONS_RULES_EPILOG = link_epilog("Collection naming rules", "collections.md")
PROJECTION_TUNING_EPILOG = link_epilog(
    "Projection tuning and interpreting the output", "exploration/taxonomy-projection-tuning.md"
)
VOYAGE_LOCAL_MODE_EPILOG = link_epilog(
    'Steps for "Local mode with Voyage" (local.embed_model, voyage_api_key, restart)', "cli-reference.md"
)
MIGRATION_RUNBOOK_EPILOG = link_epilog("The migration runbook (service will not start), before --force", "migration-runbook.md")
