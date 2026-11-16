# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The on/off resolver of auras (RDR-249 Technical Design 3; bead nexus-mdbds.10.2).

Every aura ships off. The decision, lowest level first:

1. default: off;
2. global: ``auras: {<name>: on}`` in ``~/.config/nexus/config.yml``;
3. repo: the same key in the per-repo ``.nexus.yml``, which overrides global (either way);
4. env: ``NX_AURAS_OFF``, which overrides both and can only turn auras off: ``all`` turns every
   aura off, anything else is a comma-separated list of names.

:func:`nexus.config.load_config` returns one merged dict and so cannot say which level decided;
each layer is read on its own here, from the same two paths ``load_config`` reads. A value that
is not on or off (YAML reads a bare ``on`` and ``off`` as booleans; the strings ``"on"`` and
``"off"`` are accepted too) is an :class:`AuraConfigError` that names the file and the key,
never a silent off.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from nexus import config as _config

#: The environment switch that can only turn auras off (``all``, or a list of names).
ENV_OFF: str = "NX_AURAS_OFF"

Level = Literal["default", "global", "repo", "env"]


class AuraConfigError(ValueError):
    """A config file whose ``auras`` section is not readable (a value that is not a boolean or
    ``"on"``/``"off"``, or an ``auras`` that is not a mapping): names the file and the key."""

    def __init__(self, file: str, key: str, detail: str) -> None:
        super().__init__(f"{file}: {key}: {detail}")
        self.file = file
        self.key = key
        self.detail = detail


@dataclass(frozen=True)
class Enablement:
    """Whether an aura runs, the level that decided it, and where that level was read from: the
    config file's path (``global``, ``repo``), ``"NX_AURAS_OFF"`` (``env``), or ``None`` (``default``)."""

    name: str
    on: bool
    level: Level
    source: str | None


def _read_layer(path: Path | None) -> dict[str, bool]:
    """The ``auras`` section of the config file *path* as name to state; ``{}`` when the file or
    the section is absent."""
    if path is None or not path.is_file():
        return {}
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise AuraConfigError(str(path), "auras", f"the file cannot be read: {exc}") from exc
    if not isinstance(data, dict) or data.get("auras") is None:
        return {}
    section = data["auras"]
    if not isinstance(section, dict):
        raise AuraConfigError(str(path), "auras", f"must be a mapping of aura name to on or off, got {section!r}")
    out: dict[str, bool] = {}
    for name, value in section.items():
        key = f"auras.{name}"
        if value is True or value == "on":
            out[str(name)] = True
        elif value is False or value == "off":
            out[str(name)] = False
        else:
            raise AuraConfigError(str(path), key, f"must be on or off, got {value!r}")
    return out


def _env_off(environ: Mapping[str, str]) -> tuple[bool, frozenset[str]]:
    """``(every aura off, the names turned off)`` from ``NX_AURAS_OFF``."""
    items = [s.strip() for s in environ.get(ENV_OFF, "").split(",")]
    names = frozenset(s for s in items if s)
    return "all" in names, names


def resolve(
    names: Iterable[str], *, repo_root: Path | None = None, env: Mapping[str, str] | None = None
) -> dict[str, Enablement]:
    """The :class:`Enablement` of each of *names*; an entry for every name passed in.

    *repo_root* is where ``.nexus.yml`` is read (left out, the place ``load_config`` reads it:
    ``NEXUS_REPO_ROOT``, else the working directory). *env* defaults to ``os.environ``.
    Raises :class:`AuraConfigError` for a value that is not on or off.
    """
    environ = os.environ if env is None else env
    global_path = _config.nexus_config_dir() / "config.yml"
    repo_path = _config.per_repo_config_path(repo_root)
    global_layer = _read_layer(global_path)
    repo_layer = _read_layer(repo_path)
    all_off, off_names = _env_off(environ)
    out: dict[str, Enablement] = {}
    for name in names:
        if all_off or name in off_names:
            out[name] = Enablement(name, False, "env", ENV_OFF)
        elif name in repo_layer:
            out[name] = Enablement(name, repo_layer[name], "repo", str(repo_path))
        elif name in global_layer:
            out[name] = Enablement(name, global_layer[name], "global", str(global_path))
        else:
            out[name] = Enablement(name, False, "default", None)
    return out


def is_enabled(name: str, *, repo_root: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    """Whether the aura *name* is on: :func:`resolve` for one name."""
    return resolve([name], repo_root=repo_root, env=env)[name].on


def unknown_names(
    known: Iterable[str], *, repo_root: Path | None = None, env: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """The configured names that match no aura of *known*, as ``(name, source)``.

    A misspelled name fails open (an aura the user meant to turn off stays on), so a caller says
    so: *source* is the config file's path or ``"NX_AURAS_OFF"``. The value ``all`` of
    ``NX_AURAS_OFF`` is exact and lowercase, so ``ALL`` is reported as an unknown name. Order:
    global config, repo config, env. Raises :class:`AuraConfigError` as :func:`resolve` does.
    """
    environ = os.environ if env is None else env
    names = set(known)
    out: list[tuple[str, str]] = []
    for path in (_config.nexus_config_dir() / "config.yml", _config.per_repo_config_path(repo_root)):
        out += [(n, str(path)) for n in _read_layer(path) if n not in names]
    _, off_names = _env_off(environ)
    out += [(n, ENV_OFF) for n in sorted(off_names) if n != "all" and n not in names]
    return out
