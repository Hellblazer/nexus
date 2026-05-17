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


def fetch_active_bindings(
    *,
    profiles_dir: Path | None = None,
    profiles_dirs: list[Path] | None = None,
) -> ActiveBindingsResult:
    """Return the rows for every binding under every profile dir.

    nexus-6m9i (third 360° INTEG C-2): accept multiple profile dirs
    so the panel matches the daemon binding watcher's behaviour
    (which loads BOTH the builtin and user dirs). Operator-CRUD'd
    bindings under ``user_profiles_dir()`` were previously invisible
    to ``nx cockpit show active-bindings`` because the caller only
    passed the default_profiles_dir.

    Either argument may be supplied. ``profiles_dir`` is preserved
    for backwards compat (single dir). ``profiles_dirs`` takes a list.

    Returns an empty result when no directory exists. A malformed
    profile YAML is collected into ``errors`` rather than raising so the
    panel can render whatever loaded successfully.
    """
    if profiles_dir is None and profiles_dirs is None:
        raise TypeError(
            "fetch_active_bindings requires profiles_dir or profiles_dirs"
        )
    dirs: list[Path] = list(profiles_dirs or [])
    if profiles_dir is not None:
        dirs.append(profiles_dir)

    rows: list[BindingRow] = []
    errors: list[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            profiles = load_profiles_dir(d)
        except BindingProfileError as exc:
            errors.append(str(exc))
            continue
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
