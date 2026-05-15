# SPDX-License-Identifier: AGPL-3.0-or-later
"""Active-bindings panel: bindings registered under each loaded profile.

Reads profiles via :func:`nexus.cockpit.bindings.load_profiles_dir` (the
existing loader from nexus-0xaq) rather than parsing YAML directly. The
panel is pure read; loading a profile does NOT activate it on the
watcher loop.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from nexus.cockpit.bindings import (
    Action,
    BindingProfileError,
    load_profiles_dir,
)


@dataclasses.dataclass(frozen=True)
class BindingRow:
    profile: str
    binding_name: str
    match_summary: str
    action_ref: str


@dataclasses.dataclass(frozen=True)
class ActiveBindingsResult:
    rows: list[BindingRow]
    errors: list[str] = dataclasses.field(default_factory=list)


def _format_match(match: dict[str, object]) -> str:
    if not match:
        return "<any>"
    return ", ".join(f"{k}={v}" for k, v in sorted(match.items()))


def _format_action(action: Action) -> str:
    return f"{action.kind}:{action.target}"


def fetch_active_bindings(*, profiles_dir: Path) -> ActiveBindingsResult:
    """Return the rows for every binding under every profile in *profiles_dir*.

    Returns an empty result when the directory does not exist. A malformed
    profile YAML is collected into ``errors`` rather than raising so the
    panel can render whatever loaded successfully.
    """
    rows: list[BindingRow] = []
    errors: list[str] = []
    if not profiles_dir.is_dir():
        return ActiveBindingsResult(rows=[], errors=[])
    try:
        profiles = load_profiles_dir(profiles_dir)
    except BindingProfileError as exc:
        return ActiveBindingsResult(rows=[], errors=[str(exc)])
    for profile in profiles:
        for binding in profile.bindings:
            rows.append(
                BindingRow(
                    profile=profile.name,
                    binding_name=binding.name,
                    match_summary=_format_match(binding.match),
                    action_ref=_format_action(binding.action),
                )
            )
    return ActiveBindingsResult(rows=rows, errors=errors)
