# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-7lb9: Binding CRUD helpers operating on user-profile YAMLs.

The MCP tools in :mod:`nexus.mcp.core` wrap these helpers. Each helper
operates on YAML files in a configurable ``profiles_dir`` (default:
:func:`nexus.cockpit.bindings.user_profiles_dir`). The watcher reloads
profiles on file mtime change, so CRUD writes take effect without a
daemon restart.

Design notes:

- One YAML file per profile. The file name is ``<profile>.yml``;
  the ``profile:`` field inside MUST match the file stem on round-trip.
- Bindings within a profile are stored as a list under the
  ``bindings:`` key, preserving declaration order so the watcher
  dispatches in a stable sequence.
- ``delete_binding`` removes the YAML file entirely when the last
  binding is removed (an empty ``bindings: []`` would fail validation
  on the next load via :func:`load_profile`).
- All four helpers are synchronous and side-effecting via the
  filesystem. There is no in-memory cache to invalidate; the watcher
  picks changes up via mtime poll.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

import structlog

from nexus.cockpit.bindings import (
    BindingProfileError,
    load_profile,
    user_profiles_dir,
)

_log = structlog.get_logger(__name__)


def _resolve_dir(profiles_dir: Optional[Path]) -> Path:
    """Return the effective profiles dir, creating it on demand."""
    target = profiles_dir if profiles_dir is not None else user_profiles_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


# nexus-3tl3.1 (SR-1): allowlist for binding profile names. Anything
# outside this charset is rejected by ``_profile_path`` so an
# attacker-controlled ``profile=\"../foo\"`` or ``profile=\"/etc/passwd\"``
# cannot escape ``profiles_dir`` to plant a YAML at an arbitrary
# user-writable path. Same-UID threat model: a misbehaving MCP-capable
# agent could otherwise drop a ``kind: python`` action callable into
# the watcher's scan roots and get arbitrary code execution on the
# next event tick.
import re as _re

_VALID_PROFILE_NAME_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _validate_profile_name(profile: str) -> None:
    """Raise ``ValueError`` for any profile name that escapes the dir."""
    if not isinstance(profile, str) or not _VALID_PROFILE_NAME_RE.fullmatch(
        profile
    ):
        raise ValueError(
            f"invalid binding profile name {profile!r}; must match "
            f"^[A-Za-z0-9][A-Za-z0-9_-]*$ (no path separators, no "
            "traversal sequences, no whitespace, no shell metacharacters)"
        )


def _profile_path(profiles_dir: Path, profile: str) -> Path:
    """Return the canonical YAML path for ``profile`` in ``profiles_dir``.

    Raises ``ValueError`` if ``profile`` contains anything outside the
    allowlist (path separators, parent-dir tokens, shell meta, etc.).
    """
    _validate_profile_name(profile)
    return profiles_dir / f"{profile}.yml"


def _read_profile_dict(path: Path) -> dict[str, Any]:
    """Parse ``path`` into a plain dict, raising on malformed YAML."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise BindingProfileError(
            f"{path.name}: malformed YAML, {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise BindingProfileError(
            f"{path.name}: top-level YAML must be a mapping, "
            f"got {type(raw).__name__}"
        )
    return raw


def _write_profile_dict(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as YAML to ``path``. Stable key ordering."""
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def create_binding(
    *,
    profile: str,
    name: str,
    match: dict[str, Any],
    action: dict[str, Any],
    enabled: bool = True,
    profiles_dir: Optional[Path] = None,
) -> None:
    """Create a new binding in the named profile.

    The profile YAML is created on demand if it doesn't exist yet.
    Adding a duplicate binding name within the same profile raises
    :class:`BindingProfileError`.

    Args:
        profile: Profile name (and YAML basename without ``.yml``).
        name: Unique binding name within the profile.
        match: Predicate dict (e.g. ``{"subspace": "tasks/x"}``).
        action: Action dict (e.g. ``{"kind": "log", "marker": "m"}``).
        enabled: Initial enabled state. Defaults to True.
        profiles_dir: Override target dir. Defaults to
            :func:`user_profiles_dir`.
    """
    target_dir = _resolve_dir(profiles_dir)
    path = _profile_path(target_dir, profile)

    if path.exists():
        body = _read_profile_dict(path)
        bindings = body.get("bindings", [])
        if not isinstance(bindings, list):
            raise BindingProfileError(
                f"{path.name}: 'bindings' must be a list, "
                f"got {type(bindings).__name__}"
            )
        if any(b.get("name") == name for b in bindings if isinstance(b, dict)):
            raise BindingProfileError(
                f"{path.name}: duplicate binding name {name!r}"
            )
    else:
        body = {"profile": profile, "bindings": []}
        bindings = body["bindings"]

    bindings.append({
        "name": name,
        "enabled": enabled,
        "match": dict(match),
        "action": dict(action),
    })
    body["bindings"] = bindings

    # Validate by round-trip through load_profile so a malformed match
    # or action surfaces here rather than at watcher reload time.
    _write_profile_dict(path, body)
    try:
        load_profile(path)
    except BindingProfileError:
        # Undo the partial write so the user sees the original state.
        if len(bindings) == 1:
            path.unlink(missing_ok=True)
        else:
            bindings.pop()
            body["bindings"] = bindings
            _write_profile_dict(path, body)
        raise

    _log.info(
        "binding_created",
        profile=profile,
        name=name,
        enabled=enabled,
        path=str(path),
    )


def list_bindings(
    *,
    profile: Optional[str] = None,
    enabled_only: bool = False,
    profiles_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """List all bindings across YAML files in the profiles dir.

    Each entry is a dict with keys ``profile``, ``name``, ``enabled``,
    ``match``, ``action`` (the action and match are returned as plain
    dicts, suitable for JSON serialisation).

    Args:
        profile: When set, filter results to a single profile.
        enabled_only: When True, omit bindings with ``enabled=False``.
        profiles_dir: Override source dir. Defaults to
            :func:`user_profiles_dir`.
    """
    target_dir = _resolve_dir(profiles_dir)
    out: list[dict[str, Any]] = []
    for yml in sorted(target_dir.glob("*.yml")):
        try:
            prof = load_profile(yml)
        except BindingProfileError as exc:
            _log.warning(
                "binding_list_profile_load_failed",
                path=str(yml),
                error=str(exc),
            )
            continue
        if profile is not None and prof.name != profile:
            continue
        for b in prof.bindings:
            if enabled_only and not b.enabled:
                continue
            out.append({
                "profile": prof.name,
                "name": b.name,
                "enabled": b.enabled,
                "match": dict(b.match),
                "action": {
                    "kind": b.action.kind,
                    # 'target' is stored back under its kind-specific
                    # key for symmetry with the YAML shape.
                    ("callable" if b.action.kind == "python" else "marker"):
                        b.action.target,
                },
            })
    return out


def toggle_binding(
    profile: str,
    name: str,
    *,
    enabled: bool,
    profiles_dir: Optional[Path] = None,
) -> bool:
    """Flip the ``enabled`` flag on the named binding. Returns the new value.

    Raises:
        KeyError: Profile or binding does not exist.
    """
    target_dir = _resolve_dir(profiles_dir)
    path = _profile_path(target_dir, profile)
    if not path.exists():
        raise KeyError(f"profile {profile!r} not found")
    body = _read_profile_dict(path)
    bindings = body.get("bindings", [])
    if not isinstance(bindings, list):
        raise BindingProfileError(
            f"{path.name}: 'bindings' must be a list"
        )
    for b in bindings:
        if isinstance(b, dict) and b.get("name") == name:
            b["enabled"] = bool(enabled)
            _write_profile_dict(path, body)
            _log.info(
                "binding_toggled",
                profile=profile,
                name=name,
                enabled=enabled,
            )
            return bool(enabled)
    raise KeyError(f"binding {profile}:{name} not found")


def delete_binding(
    profile: str,
    name: str,
    *,
    profiles_dir: Optional[Path] = None,
) -> bool:
    """Remove the named binding. Returns True on successful removal.

    When the binding was the last one in the profile, the YAML file is
    removed entirely so the next watcher reload doesn't trip the
    "bindings must be non-empty" validation in :func:`load_profile`.

    Raises:
        KeyError: Profile or binding does not exist.
    """
    target_dir = _resolve_dir(profiles_dir)
    path = _profile_path(target_dir, profile)
    if not path.exists():
        raise KeyError(f"profile {profile!r} not found")
    body = _read_profile_dict(path)
    bindings = body.get("bindings", [])
    if not isinstance(bindings, list):
        raise BindingProfileError(
            f"{path.name}: 'bindings' must be a list"
        )
    new = [b for b in bindings if not (isinstance(b, dict) and b.get("name") == name)]
    if len(new) == len(bindings):
        raise KeyError(f"binding {profile}:{name} not found")
    if not new:
        path.unlink()
        _log.info("binding_deleted_profile_emptied", profile=profile, name=name)
        return True
    body["bindings"] = new
    _write_profile_dict(path, body)
    _log.info("binding_deleted", profile=profile, name=name)
    return True
