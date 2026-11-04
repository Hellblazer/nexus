# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer-object family registry (RDR-189 P2, nexus-z1d0l.2.3).

Source-controlled, versioned family declarations and a validating read path.
Declarations are TOML files at ``conexus/plans/target_families/<family_id>/v<N>.toml``
(the wheel ships ``conexus/plans`` as ``nexus/_resources/plans``). The registry
read path accepts only a validated declaration with an immutable revision
reference; it never skips or repairs a malformed one: any defect raises
:class:`FamilyDeclarationError` naming the file and the field, and the whole
tree is refused. (This is deliberately not the ``purposes.py`` drop-with-warning
idiom: a family that silently vanished would make a request fall back to the
generic route with no one told.)

Invariants this module enforces, each pinned by ``tests/test_target_families.py``:

* a declaration carries every required field, a positive non-bool ``family_version``
  and every vocabulary literal it does state (a slot's ``kind`` and a witness
  obligation's ``on_missing`` are optional) inside the checked tables of
  :mod:`nexus.tables.target_family_vocab`;
* a declaration whose status is ``active`` or ``retired`` carries a recorded
  approval, and :func:`activate` refuses a candidate without one (both read the
  ``target-family-lifecycle`` table, so the rule has one home);
* counterexamples, disagreements and exemplars stay attached to the declaration;
* versions coexist: :func:`activate` retires the previous active version but
  keeps it readable by ``(family_id, family_version)``, and
  :func:`interpret_run` reads an old run under exactly the version it recorded,
  never the latest.

Registry values are immutable; :func:`activate` returns a new registry. The
repository tree is the only persistent state: no database, no new setting.
"""
from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Final

import structlog

from nexus.tables import target_family_vocab as vocab

_log = structlog.get_logger(__name__)

__all__ = [
    "ActivationRefused",
    "Annotation",
    "Approval",
    "Constraint",
    "FamilyDeclaration",
    "FamilyDeclarationError",
    "FamilyError",
    "FamilyRegistry",
    "SlotObligation",
    "WitnessObligation",
    "activate",
    "default_registry",
    "interpret_run",
    "load_registry",
]

_ID_RE: Final = re.compile(r"[a-z][a-z0-9_-]*")
#: An immutable revision reference: a full git object id (SHA-1 or SHA-256).
_REVISION_RE: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_FILENAME_RE: Final = re.compile(r"v([1-9][0-9]*)\.toml")

_REQUIRED: Final[tuple[str, ...]] = (
    "family_id",
    "family_version",
    "status",
    "revision",
    "required_slots",
    "facets",
    "treatment_constraints",
    "witness_obligations",
    "exemplars",
    "counterexamples",
    "disagreements",
    "observed_refs",
    "environment_capabilities",
)
_ALLOWED: Final[frozenset[str]] = frozenset(_REQUIRED) | {"approval"}


class FamilyError(ValueError):
    """Anything the family registry refuses."""


class FamilyDeclarationError(FamilyError):
    """A malformed declaration or tree: names the file and the field."""

    def __init__(self, path: Path, field: str, message: str) -> None:
        super().__init__(f"{path}: {field}: {message}")
        self.path = path
        self.field = field
        self.message = message


class ActivationRefused(FamilyError):
    """The lifecycle table refused an activation; ``reason`` is its refusal code."""

    def __init__(self, family_id: str, family_version: int, reason: str, detail: str = "") -> None:
        super().__init__(f"cannot activate {family_id} v{family_version}: {reason}" + (f" ({detail})" if detail else ""))
        self.reason = reason


@dataclass(frozen=True)
class SlotObligation:
    name: str
    #: One of the slot-kinds table, or None: the RDR fixes no slot taxonomy, so a family may omit it.
    kind: str | None = None


@dataclass(frozen=True)
class WitnessObligation:
    slot: str
    kind: str
    #: The obligation state a missing witness degrades to (never ``preserved``), or None:
    #: RDR-189 P4 picks the state by cause, so a family may leave it undeclared.
    on_missing: str | None = None


@dataclass(frozen=True)
class Constraint:
    name: str
    rule: str


@dataclass(frozen=True)
class Annotation:
    """An exemplar, counterexample or unresolved disagreement: a reference and a reviewer's note."""

    ref: str
    note: str


@dataclass(frozen=True)
class Approval:
    reviewer: str
    #: The reference of the change that records the decision.
    ref: str


@dataclass(frozen=True)
class FamilyDeclaration:
    family_id: str
    family_version: int
    status: str
    revision: str
    required_slots: tuple[SlotObligation, ...]
    facets: tuple[str, ...]
    treatment_constraints: tuple[Constraint, ...]
    witness_obligations: tuple[WitnessObligation, ...]
    exemplars: tuple[Annotation, ...]
    counterexamples: tuple[Annotation, ...]
    disagreements: tuple[Annotation, ...]
    observed_refs: tuple[str, ...]
    environment_capabilities: tuple[str, ...]
    approval: Approval | None = None

    @property
    def approval_state(self) -> str:
        """The lifecycle table's ``approval`` dimension for this declaration."""
        return "recorded" if self.approval is not None else "absent"


@dataclass(frozen=True)
class FamilyRegistry:
    """Immutable set of declarations, every version of every family."""

    declarations: tuple[FamilyDeclaration, ...] = ()

    def family_ids(self) -> tuple[str, ...]:
        return tuple(sorted({d.family_id for d in self.declarations}))

    def versions(self, family_id: str) -> tuple[FamilyDeclaration, ...]:
        """Every version of *family_id*, whatever its status, oldest first."""
        return tuple(sorted((d for d in self.declarations if d.family_id == family_id), key=lambda d: d.family_version))

    def get(self, family_id: str, family_version: int) -> FamilyDeclaration:
        """The declaration at exactly ``(family_id, family_version)``; never a substitute."""
        for d in self.declarations:
            if d.family_id == family_id and d.family_version == family_version:
                return d
        raise FamilyError(f"no declaration for family {family_id!r} version {family_version!r}")

    def active(self, family_id: str) -> FamilyDeclaration | None:
        """The single active version of *family_id*, or None."""
        found = [d for d in self.versions(family_id) if d.status == "active"]
        return found[0] if found else None


# ---------------------------------------------------------------------------
# validation


def _is_positive_int(value: object) -> bool:
    # bool is an int subclass: True would otherwise read as version 1.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _text(value: object, path: Path, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FamilyDeclarationError(path, field, f"must be a non-empty string, got {value!r}")
    return value


def _one_of(value: object, allowed: tuple[str, ...], path: Path, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise FamilyDeclarationError(path, field, f"{value!r} is not one of {list(allowed)}")
    return value


def _strings(value: object, path: Path, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise FamilyDeclarationError(path, field, f"must be a list, got {type(value).__name__}")
    return tuple(_text(item, path, f"{field}[{i}]") for i, item in enumerate(value))


def _entries(
    value: object, keys: tuple[str, ...], path: Path, field: str, optional: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """A list of tables each carrying every key of *keys* and, optionally, those of *optional*."""
    if not isinstance(value, list):
        raise FamilyDeclarationError(path, field, f"must be a list of tables, got {type(value).__name__}")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        here = f"{field}[{i}]"
        if not isinstance(item, dict):
            raise FamilyDeclarationError(path, here, f"must be a table, got {type(item).__name__}")
        for key in keys:
            if key not in item:
                raise FamilyDeclarationError(path, f"{here}.{key}", "missing required field")
        for key in item:
            if key not in keys and key not in optional:
                raise FamilyDeclarationError(
                    path, f"{here}.{key}", f"unknown field (allowed: {list(keys) + list(optional)})"
                )
        out.append(item)
    return out


def _annotations(value: object, path: Path, field: str) -> tuple[Annotation, ...]:
    return tuple(
        Annotation(
            ref=_text(e["ref"], path, f"{field}[{i}].ref"),
            note=_text(e["note"], path, f"{field}[{i}].note"),
        )
        for i, e in enumerate(_entries(value, ("ref", "note"), path, field))
    )


def _parse_approval(value: object, path: Path) -> Approval:
    if not isinstance(value, dict):
        raise FamilyDeclarationError(path, "approval", f"must be a table, got {type(value).__name__}")
    for key in ("reviewer", "ref"):
        if key not in value:
            raise FamilyDeclarationError(path, f"approval.{key}", "missing required field")
    for key in value:
        if key not in ("reviewer", "ref"):
            raise FamilyDeclarationError(path, f"approval.{key}", "unknown field")
    return Approval(
        reviewer=_text(value["reviewer"], path, "approval.reviewer"),
        ref=_text(value["ref"], path, "approval.ref"),
    )


def _parse_declaration(data: Mapping[str, Any], path: Path) -> FamilyDeclaration:
    for field in _REQUIRED:
        if field not in data:
            raise FamilyDeclarationError(path, field, "missing required field")
    for field in data:
        if field not in _ALLOWED:
            raise FamilyDeclarationError(path, field, "unknown field")

    family_id = data["family_id"]
    if not isinstance(family_id, str) or not _ID_RE.fullmatch(family_id):
        raise FamilyDeclarationError(path, "family_id", f"must match {_ID_RE.pattern!r}, got {family_id!r}")
    version = data["family_version"]
    if not _is_positive_int(version):
        raise FamilyDeclarationError(path, "family_version", f"must be a positive integer (not a bool), got {version!r}")
    status = _one_of(data["status"], vocab.STATUSES, path, "status")
    revision = data["revision"]
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise FamilyDeclarationError(path, "revision", f"must be a full lowercase git object id, got {revision!r}")

    slots: list[SlotObligation] = []
    for i, entry in enumerate(_entries(data["required_slots"], ("name",), path, "required_slots", ("kind",))):
        name = _text(entry["name"], path, f"required_slots[{i}].name")
        if any(s.name == name for s in slots):
            raise FamilyDeclarationError(path, f"required_slots[{i}].name", f"duplicate slot {name!r}")
        kind = _one_of(entry["kind"], vocab.SLOT_KINDS, path, f"required_slots[{i}].kind") if "kind" in entry else None
        slots.append(SlotObligation(name, kind))
    if not slots:
        raise FamilyDeclarationError(path, "required_slots", "must declare at least one slot")

    witnesses: list[WitnessObligation] = []
    for i, e in enumerate(
        _entries(data["witness_obligations"], ("slot", "kind"), path, "witness_obligations", ("on_missing",))
    ):
        here = f"witness_obligations[{i}]"
        witness = WitnessObligation(
            slot=_one_of(e["slot"], tuple(s.name for s in slots), path, f"{here}.slot"),
            kind=_one_of(e["kind"], vocab.WITNESS_KINDS, path, f"{here}.kind"),
            on_missing=(
                _one_of(e["on_missing"], vocab.MISSING_WITNESS_STATES, path, f"{here}.on_missing")
                if "on_missing" in e
                else None
            ),
        )
        if any((w.slot, w.kind) == (witness.slot, witness.kind) for w in witnesses):
            raise FamilyDeclarationError(
                path, f"{here}.kind", f"duplicate witness obligation ({witness.slot!r}, {witness.kind!r})"
            )
        witnesses.append(witness)
    constraints: list[Constraint] = []
    for i, e in enumerate(_entries(data["treatment_constraints"], ("name", "rule"), path, "treatment_constraints")):
        name = _text(e["name"], path, f"treatment_constraints[{i}].name")
        if any(c.name == name for c in constraints):
            raise FamilyDeclarationError(path, f"treatment_constraints[{i}].name", f"duplicate constraint {name!r}")
        constraints.append(Constraint(name, _text(e["rule"], path, f"treatment_constraints[{i}].rule")))
    approval = _parse_approval(data["approval"], path) if "approval" in data else None

    declaration = FamilyDeclaration(
        family_id=family_id,
        family_version=version,
        status=status,
        revision=revision,
        required_slots=tuple(slots),
        facets=_strings(data["facets"], path, "facets"),
        treatment_constraints=tuple(constraints),
        witness_obligations=tuple(witnesses),
        exemplars=_annotations(data["exemplars"], path, "exemplars"),
        counterexamples=_annotations(data["counterexamples"], path, "counterexamples"),
        disagreements=_annotations(data["disagreements"], path, "disagreements"),
        observed_refs=_strings(data["observed_refs"], path, "observed_refs"),
        environment_capabilities=_strings(data["environment_capabilities"], path, "environment_capabilities"),
        approval=approval,
    )
    if status != "candidate":
        # active and retired are reached only through candidate -> active
        kind, outcome = vocab.transition("candidate", "activate", declaration.approval_state)
        if kind == "refuse":
            raise FamilyDeclarationError(path, "approval", f"status {status!r} without a recorded approval: {outcome}")
    return declaration


# ---------------------------------------------------------------------------
# loading


def _load_file(path: Path) -> FamilyDeclaration:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise FamilyDeclarationError(path, "<toml>", f"not valid TOML: {exc}") from exc
    declaration = _parse_declaration(data, path)
    if declaration.family_id != path.parent.name:
        raise FamilyDeclarationError(
            path, "family_id", f"{declaration.family_id!r} does not match its directory {path.parent.name!r}"
        )
    match = _FILENAME_RE.fullmatch(path.name)
    assert match is not None  # the walk below only hands over files that fit
    if int(match.group(1)) != declaration.family_version:
        raise FamilyDeclarationError(
            path, "family_version", f"{declaration.family_version} does not match the filename {path.name!r}"
        )
    return declaration


def _load_tree(root: Path) -> FamilyRegistry:
    if not root.is_dir():
        raise FamilyDeclarationError(root, "<root>", "registry directory does not exist")
    declarations: list[FamilyDeclaration] = []
    paths: dict[tuple[str, int], Path] = {}
    # Dotfiles (.DS_Store and the like) are ignored everywhere. Everything else must be a
    # family directory, README.md at the root, or v<N>.toml inside a family directory. A
    # duplicate (family_id, family_version) cannot arise: the directory fixes the id and the
    # filename the version, and a second spelling such as v01.toml is refused as a stray file.
    for entry in sorted(p for p in root.iterdir() if not p.name.startswith(".")):
        if entry.is_file():
            if entry.name != "README.md":
                raise FamilyDeclarationError(entry, "<file>", "only family directories (and README.md) belong at the registry root")
            continue
        files_here = sorted(p for p in entry.iterdir() if not p.name.startswith("."))
        if not files_here:
            raise FamilyDeclarationError(entry, "<dir>", "a family directory holds no declarations")
        for path in files_here:
            if not path.is_file() or not _FILENAME_RE.fullmatch(path.name):
                raise FamilyDeclarationError(path, "<file>", "a family directory holds only v<N>.toml declarations")
            declaration = _load_file(path)
            declarations.append(declaration)
            paths[(declaration.family_id, declaration.family_version)] = path
    for family_id in sorted({d.family_id for d in declarations}):
        active = [d for d in declarations if d.family_id == family_id and d.status == "active"]
        if len(active) > 1:
            second = paths[(family_id, active[1].family_version)]
            raise FamilyDeclarationError(
                second, "status", f"more than one active version of {family_id!r}: {[d.family_version for d in active]}"
            )
    _log.debug("target_families.loaded", root=str(root), declarations=len(declarations))
    return FamilyRegistry(tuple(declarations))


def load_registry(root: Path | None = None) -> FamilyRegistry:
    """Load and validate every declaration under *root* (default: the packaged tree).

    Raises :class:`FamilyDeclarationError` on the first defect; never returns a
    partial registry.
    """
    if root is not None:
        return _load_tree(Path(root))
    with as_file(files("nexus") / "_resources" / "plans" / "target_families") as resolved:
        return _load_tree(Path(resolved))


@lru_cache(maxsize=1)
def default_registry() -> FamilyRegistry:
    """The packaged registry, loaded once."""
    return load_registry()


# ---------------------------------------------------------------------------
# promotion and historical reads


def activate(registry: FamilyRegistry, family_id: str, family_version: int) -> FamilyRegistry:
    """A new registry with ``(family_id, family_version)`` active and the previous
    active version of that family retired.

    The lifecycle table decides: a candidate without a recorded approval, or any
    non-candidate, raises :class:`ActivationRefused` and changes nothing; so does a
    version below the currently active one (rollback is not designed). The
    retired version stays in the registry, unchanged but for its status, so a
    run that recorded it still reads its original obligations.
    """
    target = registry.get(family_id, family_version)
    kind, outcome = vocab.transition(target.status, "activate", target.approval_state)
    if kind == "refuse":
        raise ActivationRefused(family_id, family_version, outcome)
    previous = registry.active(family_id)
    if previous is not None and family_version < previous.family_version:
        raise ActivationRefused(
            family_id,
            family_version,
            "rollback-not-designed",
            f"v{previous.family_version} is active; activating an older version is a rollback, which is not designed",
        )
    changed: dict[int, FamilyDeclaration] = {family_version: replace(target, status=outcome)}
    if previous is not None:
        retire_kind, retired = vocab.transition(previous.status, "retire", previous.approval_state)
        assert retire_kind == "to"  # an active declaration can always retire
        changed[previous.family_version] = replace(previous, status=retired)
    return FamilyRegistry(
        tuple(
            changed.get(d.family_version, d) if d.family_id == family_id else d
            for d in registry.declarations
        )
    )


def interpret_run(registry: FamilyRegistry, run: Mapping[str, Any]) -> FamilyDeclaration:
    """The declaration an old run is read under: exactly the ``family_id`` and
    ``family_version`` it recorded, whatever has been activated since.

    Never reroutes to the latest or the active version. *run* must carry
    ``family_id``, ``family_version`` and a positive non-bool ``contract_version``.
    ``contract_version`` is validated but not yet used to select a declaration:
    declarations carry none, and P3 decides where it lives.
    """
    family_id = run.get("family_id")
    if not isinstance(family_id, str) or not family_id:
        raise FamilyError(f"run reference needs a non-empty family_id, got {family_id!r}")
    for field in ("family_version", "contract_version"):
        if not _is_positive_int(run.get(field)):
            raise FamilyError(f"run reference needs a positive integer {field} (not a bool), got {run.get(field)!r}")
    return registry.get(family_id, run["family_version"])
