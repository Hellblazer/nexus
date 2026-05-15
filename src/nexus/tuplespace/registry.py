# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subspace registry: YAML loader + JSON Schema validation (RDR-110 P1.1).

Loads ``nx/tuplespace/builtin/*.yml`` at MCP/CLI startup, validates each
file against an inline JSON Schema for the registry format itself, and
exposes ``Registry.get_schema_for(subspace)`` with single-segment
parameterised matching (``tasks/<project>`` matches concrete
``tasks/nexus`` but NOT ``tasks/a/b``).

Phase 1.1 is purely client-side substrate — no SQLite, no daemon. The
daemon-side admin RPC for third-party subspace registration ships under
RDR-112 ``nexus-x98k`` in Phase 2.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

import structlog
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

_log = structlog.get_logger(__name__)


# -- Errors -------------------------------------------------------------------


class RegistryError(Exception):
    """Base for all registry errors."""


class RegistryLoadError(RegistryError):
    """A YAML file failed to parse or did not satisfy the registry schema."""


class UnknownSubspaceError(RegistryError):
    """``get_schema_for`` was called with a subspace that no template matches."""


# -- Registry-format JSON Schema ---------------------------------------------


_DIMENSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["string", "int", "float", "bool", "enum"]},
        "values": {"type": "array", "items": {"type": "string"}},
        "required": {"type": "boolean"},
    },
    "required": ["type"],
    "additionalProperties": True,
}


_TAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["semantic", "exact"]},
        "floor": {"type": "number"},
        "margin": {"type": "number"},
        "match_keys": {"type": "array", "items": {"type": "string"}},
        "default_lease_seconds": {"type": "integer", "minimum": 0},
    },
    "required": ["enabled", "mode"],
    "additionalProperties": True,
}


_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "default_floor": {"type": "number"},
        "default_n": {"type": "integer", "minimum": 1},
    },
    "required": ["default_floor", "default_n"],
    "additionalProperties": True,
}


REGISTRY_FORMAT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "RDR-110 subspace registry entry",
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "tier": {"type": "string", "minLength": 1},
        "content_type": {"type": "string", "enum": ["text", "json"]},
        "embed_from": {"type": "string", "minLength": 1},
        "dimensions": {
            "type": "object",
            "additionalProperties": _DIMENSION_SCHEMA,
        },
        "take": _TAKE_SCHEMA,
        "read": _READ_SCHEMA,
        "tiers": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "retention_seconds": {"type": "integer", "minimum": 0},
    },
    "required": [
        "name",
        "tier",
        "content_type",
        "embed_from",
        "dimensions",
        "take",
        "read",
        "tiers",
        "retention_seconds",
    ],
    "additionalProperties": True,
}


_VALIDATOR = Draft202012Validator(REGISTRY_FORMAT_SCHEMA)


# -- Typed sub-shapes --------------------------------------------------------


TakeMode = Literal["semantic", "exact"]


class TakeConfig(TypedDict, total=False):
    """Type for the ``take`` block on a subspace schema.

    Mirrors RDR-110 §Step 1 and the JSON-Schema validation. ``total=False``
    because optional keys (``floor``, ``margin``, ``match_keys``,
    ``default_lease_seconds``) are conditional on ``mode``: ``floor`` and
    ``margin`` apply to semantic mode; ``match_keys`` applies to exact
    mode. Loader validation in ``_load_one`` enforces the
    mode-conditional invariants.
    """

    enabled: bool
    mode: TakeMode
    floor: float
    margin: float
    match_keys: list[str]
    default_lease_seconds: int


class ReadConfig(TypedDict):
    """Type for the ``read`` block on a subspace schema (RDR-110 §Step 1)."""

    default_floor: float
    default_n: int


# -- Schema dataclass --------------------------------------------------------


@dataclass(frozen=True)
class SubspaceSchema:
    """Parsed registry entry for one subspace template.

    ``name`` may include ``<param>`` placeholders (single-segment) that
    ``Registry.get_schema_for`` resolves against concrete subspace
    strings at lookup time.

    ``take`` and ``read`` are ``TypedDict``s rather than plain ``dict[str, Any]``
    so downstream consumers (RDR-110 nexus-8q4v Core API) get static-checker
    affordance without forcing a stringly-keyed access pattern across the
    codebase. The dict values themselves remain JSON-deserialised.
    """

    name: str
    tier: str
    content_type: str
    embed_from: str
    dimensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    take: TakeConfig = field(default_factory=lambda: TakeConfig())  # type: ignore[misc]
    read: ReadConfig = field(  # type: ignore[assignment]
        default_factory=lambda: ReadConfig(default_floor=0.0, default_n=1)
    )
    tiers: list[str] = field(default_factory=list)
    retention_seconds: int = 0
    source_path: Path | None = None


# -- Param matching ----------------------------------------------------------


# Param identifiers follow Python regex named-group rules — no dashes
# (CPython rejects ``(?P<a-b>...)``). Mirror that constraint here so a
# typo in a template name fails at YAML load rather than producing a
# broken matcher that silently never matches.
_PARAM_PATTERN = re.compile(r"<([a-zA-Z_][a-zA-Z0-9_]*)>")

# Used by ``_load_one`` to enumerate every angle-bracketed token in a
# template name (valid or invalid). ``[^>]*`` (zero-or-more) deliberately
# catches the empty-brackets case ``<>`` so the load-time guard rejects
# it; the captured tokens are then filtered against ``_PARAM_PATTERN``
# to identify syntactically invalid identifiers.
_ANGLE_TOKEN = re.compile(r"<([^>]*)>")


def _compile_template(template_name: str) -> re.Pattern[str]:
    """Compile a template like ``tasks/<project>`` into a regex.

    Each ``<param>`` matches a single path segment (one or more chars
    other than ``/``). The whole template is anchored.
    """
    escaped = re.escape(template_name)
    # ``re.escape`` escapes the angle brackets too — restore them so the
    # param substitution below works on the original character class.
    escaped = escaped.replace(r"\<", "<").replace(r"\>", ">")
    pattern = _PARAM_PATTERN.sub(r"(?P<\1>[^/]+)", escaped)
    return re.compile(rf"^{pattern}$")


# -- Registry ----------------------------------------------------------------


@dataclass
class Registry:
    """In-memory collection of validated subspace schemas."""

    _by_template: dict[str, SubspaceSchema] = field(default_factory=dict)
    _matchers: list[tuple[re.Pattern[str], SubspaceSchema]] = field(default_factory=list)

    @classmethod
    def load(cls, builtin_dir: Path, *, subdirs: Iterable[str] = ()) -> "Registry":
        """Load + validate every ``*.yml`` under *builtin_dir* and *subdirs*.

        *builtin_dir* is globbed at the top level; each name in *subdirs* is
        appended as a relative path under *builtin_dir* and globbed in turn
        (e.g. ``subdirs=("hooks",)`` includes ``builtin_dir/hooks/*.yml``).
        Subdirs not present on disk are silently skipped.

        Raises:
            RegistryLoadError: a file fails YAML parse, JSON Schema
                validation, additional load-time invariants
                (``mode=exact`` requires non-empty ``match_keys``), or a
                subspace name collides with one already loaded.
        """
        if not builtin_dir.is_dir():
            raise RegistryLoadError(
                f"builtin dir does not exist or is not a directory: {builtin_dir}"
            )

        registry = cls()
        search_dirs: list[Path] = [builtin_dir]
        for sub in subdirs:
            sub_path = builtin_dir / sub
            if sub_path.is_dir():
                search_dirs.append(sub_path)

        for d in search_dirs:
            for yml_path in sorted(d.glob("*.yml")):
                schema = _load_one(yml_path)
                if schema.name in registry._by_template:
                    raise RegistryLoadError(
                        f"{yml_path.name}: duplicate subspace name "
                        f"{schema.name!r} (also in "
                        f"{registry._by_template[schema.name].source_path})"
                    )
                registry._by_template[schema.name] = schema
                registry._matchers.append((_compile_template(schema.name), schema))

        _log.info(
            "tuplespace_registry_loaded",
            builtin_dir=str(builtin_dir),
            subdirs=tuple(subdirs),
            count=len(registry._by_template),
        )
        return registry

    def get_schema_for(self, subspace: str) -> SubspaceSchema:
        """Return the template that matches *subspace* (concrete or templated).

        Raises:
            UnknownSubspaceError: no loaded template matches.
        """
        if not subspace:
            raise UnknownSubspaceError("empty subspace")

        # Fast path: literal name match (no params).
        literal = self._by_template.get(subspace)
        if literal is not None:
            return literal

        for matcher, schema in self._matchers:
            if matcher.fullmatch(subspace) is not None:
                return schema
        raise UnknownSubspaceError(
            f"no registered template matches subspace {subspace!r}"
        )

    def schemas(self) -> list[SubspaceSchema]:
        """All loaded schemas in load order."""
        return list(self._by_template.values())


def _load_one(yml_path: Path) -> SubspaceSchema:
    """Parse, validate, and convert one YAML file into a ``SubspaceSchema``."""
    try:
        raw = yaml.safe_load(yml_path.read_text())
    except yaml.YAMLError as exc:
        raise RegistryLoadError(f"{yml_path.name}: malformed YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryLoadError(
            f"{yml_path.name}: top-level YAML must be a mapping, got {type(raw).__name__}"
        )

    try:
        _VALIDATOR.validate(raw)
    except ValidationError as exc:
        # Surface the JSON-pointer path so debugging is precise.
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise RegistryLoadError(
            f"{yml_path.name}: schema validation failed at {path}: {exc.message}"
        ) from exc

    # Reject malformed param names at load — Python's named-group syntax
    # disallows dashes (``(?P<a-b>...)`` is a regex error). A YAML author
    # writing ``mailbox/<agent-name>`` or ``mailbox/<>`` would otherwise
    # ship a template whose placeholder is treated as a literal,
    # producing an opaque UnknownSubspaceError on first ``take``.
    # ``_ANGLE_TOKEN`` uses ``[^>]*`` so empty ``<>`` is captured as the
    # empty string and filtered through ``_PARAM_PATTERN`` (which
    # requires at least one identifier char) into ``bad_params``.
    bad_params = [
        token
        for token in _ANGLE_TOKEN.findall(raw["name"])
        if _PARAM_PATTERN.fullmatch(f"<{token}>") is None
    ]
    if bad_params:
        raise RegistryLoadError(
            f"{yml_path.name}: invalid param identifier(s) {bad_params!r} "
            f"in name {raw['name']!r}; params must match "
            f"[a-zA-Z_][a-zA-Z0-9_]* (Python named-group syntax — no dashes)"
        )

    take = raw["take"]
    if take.get("mode") == "exact":
        keys = take.get("match_keys") or []
        if not keys:
            raise RegistryLoadError(
                f"{yml_path.name}: take.mode='exact' requires non-empty "
                "match_keys (RDR-110 §C2)"
            )

    return SubspaceSchema(
        name=raw["name"],
        tier=raw["tier"],
        content_type=raw["content_type"],
        embed_from=raw["embed_from"],
        dimensions=dict(raw.get("dimensions", {})),
        take=dict(take),
        read=dict(raw["read"]),
        tiers=list(raw["tiers"]),
        retention_seconds=int(raw["retention_seconds"]),
        source_path=yml_path,
    )


# -- Default builtin location ------------------------------------------------


def default_builtin_dir() -> Path:
    """Return the repo's canonical ``nx/tuplespace/builtin/`` directory.

    Resolves relative to the running package: walks up from this file
    to the repo root and then into ``nx/tuplespace/builtin/``. Works
    for editable installs (``uv sync``) and source checkouts; for wheel
    installs the path may not exist and callers can pass an explicit
    directory to ``Registry.load`` instead.
    """
    here = Path(__file__).resolve()
    # src/nexus/tuplespace/registry.py — four parent hops reach the repo
    # root: tuplespace → nexus → src → repo.
    repo_root = here.parent.parent.parent.parent
    return repo_root / "nx" / "tuplespace" / "builtin"
