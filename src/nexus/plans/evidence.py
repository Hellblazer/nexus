# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 1: the caller's evidence request on ``nx_answer``.

``nx_answer`` takes a named-source request through the existing ``bindings``
dict (RDR-232 adds no MCP parameter). Three names in that dict are reserved:

* ``source``: the document the question is about, an RDR id or a catalog
  tumbler (:class:`SourceRef`);
* ``owner`` and ``project``: the owner qualifier that says which project's
  document ``source`` means (:func:`resolve_owner_qualifier`).

They are the caller's request, not plan bindings. ``nx_answer`` strips them
before it builds the run bindings and the matcher's availability set, so
they never reach plan binding resolution or ``plan_match``. No plan may
declare one (:func:`check_no_reserved_declarations`, applied at save time),
and a plan already in the library that does is excluded from matching with
a logged diagnostic, never rewritten or deleted (the matcher calls
:func:`declared_reserved_names`).

Owner qualifier rules (RDR-232 section 1): a catalog-subtree ``scope`` with
no ``owner`` binding is itself the qualifier. Qualifiers agree when one
tumbler is a segment prefix of the other (``1.2.4686`` agrees with ``1.2``)
and resolution uses the longer; otherwise the request reports the conflict
and picks nothing. ``scope="rdr"`` alone is never a project selector.

This module imports only the standard library and ``structlog`` (and,
lazily inside :func:`catalog_name_lookup`, the catalog's owner-name
resolver), so the schema validator, the matcher and the MCP layer can all
import it without a cycle. Resolving the named source to a document is a later step
(nexus-jypmj.1.4) and takes :class:`SourceRef` and the
:class:`OwnerResolution` produced here as its inputs.

The second half of the module is the caller's corpus scope over a plan's declared corpora
(RDR-232 section 2a, nexus-jypmj.1.3): :class:`ScopeConstraint` classifies the ``scope`` string,
:func:`narrow_corpus` applies the narrowing rule to one step's corpus, and
:func:`scope_conflict_notice` renders the disclosure line. The runner and the ``single_query`` fast
path both call them, so the rule lives once.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "RESERVED_BINDINGS",
    "CorpusNarrowing",
    "EvidenceRequest",
    "OwnerNameError",
    "OwnerResolution",
    "ReservedBindingError",
    "ScopeConstraint",
    "SourceRef",
    "SourceRefError",
    "build_evidence_request",
    "parse_sources",
    "catalog_name_lookup",
    "check_no_reserved_declarations",
    "declared_reserved_names",
    "narrow_corpus",
    "reserved_binding_declarations",
    "resolve_owner_qualifier",
    "scope_conflict_clauses",
    "scope_conflict_notice",
    "scope_subtree",
    "split_reserved",
]

#: The caller's evidence request keys inside ``nx_answer(bindings=...)``.
RESERVED_BINDINGS: frozenset[str] = frozenset({"source", "owner", "project"})


# ── Reserved names: stripping and declaration scan ──────────────────────────


def split_reserved(
    bindings: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split *bindings* into ``(plan_bindings, reserved)``.

    ``None``-valued entries are dropped from both halves: a null slips
    through JSON tool arguments easily and must not count as supplied.
    """
    plan: dict[str, Any] = {}
    reserved: dict[str, Any] = {}
    for key, value in (bindings or {}).items():
        if value is None:
            continue
        (reserved if key in RESERVED_BINDINGS else plan)[key] = value
    return plan, reserved


class ReservedBindingError(ValueError):
    """A plan declares ``source``, ``owner`` or ``project``."""

    def __init__(self, declarations: Mapping[str, tuple[str, ...]]) -> None:
        self.declarations = dict(declarations)
        super().__init__(_declaration_message(self.declarations))


_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _refs(value: Any, found: set[str]) -> None:
    """Collect ``$name`` binding references from a step-argument value.

    Mirrors ``schema._binding_refs``: recurses lists and dicts, scans
    inside strings, and skips ``$stepN.field`` (a step reference the
    runner resolves, not a binding). Over-matching is the safe direction
    for a refusal, so interpolated references count.
    """
    if isinstance(value, str):
        for m in _REF_RE.finditer(value):
            if value[m.end():m.end() + 1] != ".":
                found.add(m.group(1))
    elif isinstance(value, list):
        for item in value:
            _refs(item, found)
    elif isinstance(value, dict):
        for item in value.values():
            _refs(item, found)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _reserved_in(names: Any) -> set[str]:
    if isinstance(names, dict):
        names = list(names)
    if not isinstance(names, (list, tuple, set, frozenset)):
        return set()
    return {n for n in names if isinstance(n, str) and n in RESERVED_BINDINGS}


def reserved_binding_declarations(
    plan_json: str | Mapping[str, Any] | None,
    default_bindings: str | Mapping[str, Any] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Where, if anywhere, a plan declares a reserved binding name.

    Returns ``{location: (names...)}`` with only non-empty locations:
    ``required_bindings``, ``optional_bindings``, ``default_bindings``
    (``plan_json``'s own dict and the separate ``default_bindings`` column,
    unioned) and ``step_arg_reference`` (a ``$name`` in a step's args).
    Unparseable input declares nothing; other validators own that refusal.
    """
    plan = _as_dict(plan_json)
    found: dict[str, tuple[str, ...]] = {}

    def add(location: str, names: set[str]) -> None:
        if names:
            found[location] = tuple(sorted(names))

    add("required_bindings", _reserved_in(plan.get("required_bindings")))
    add("optional_bindings", _reserved_in(plan.get("optional_bindings")))
    add(
        "default_bindings",
        _reserved_in(plan.get("default_bindings"))
        | _reserved_in(_as_dict(default_bindings)),
    )
    refs: set[str] = set()
    steps = plan.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            _refs(step.get("args"), refs)
    add("step_arg_reference", refs & RESERVED_BINDINGS)
    return found


def declared_reserved_names(
    plan_json: str | Mapping[str, Any] | None,
    default_bindings: str | Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """The reserved names a plan declares, sorted, across all locations."""
    names: set[str] = set()
    for group in reserved_binding_declarations(plan_json, default_bindings).values():
        names.update(group)
    return tuple(sorted(names))


def _declaration_message(declarations: Mapping[str, tuple[str, ...]]) -> str:
    where = "; ".join(
        f"{', '.join(repr(n) for n in names)} in {location}"
        for location, names in declarations.items()
    )
    return (
        f"plan declares reserved binding(s): {where}. 'source', 'owner' and "
        "'project' are the caller's evidence request on nx_answer, removed "
        "before plan binding resolution and matching; a plan cannot "
        "declare or reference them."
    )


def check_no_reserved_declarations(
    plan_json: str | Mapping[str, Any] | None,
    default_bindings: str | Mapping[str, Any] | None = None,
) -> None:
    """Raise :class:`ReservedBindingError` naming every reserved key declared."""
    found = reserved_binding_declarations(plan_json, default_bindings)
    if found:
        raise ReservedBindingError(found)


# ── SourceRef ────────────────────────────────────────────────────────────────


class SourceRefError(ValueError):
    """The ``source`` value is neither an RDR id nor a document tumbler."""


_RDR_ID_RE = re.compile(r"^rdr[-_ ]?(\d+)$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")
_TUMBLER_RE = re.compile(r"^\d+(?:\.\d+)+$")


@dataclass(frozen=True)
class SourceRef:
    """A named source: an RDR id or a catalog document tumbler.

    ``kind == "rdr"``: ``value`` is the canonical id (``RDR-084``) and
    ``number`` the integer; the resolver matches it against RDR file
    basenames (``rdr-084-...``) inside an owner scope. ``kind ==
    "tumbler"``: ``value`` is the dotted document address (depth 3 or
    more) and needs no resolution.
    """

    kind: Literal["rdr", "tumbler"]
    value: str
    number: int | None = None

    @classmethod
    def parse(cls, raw: object) -> SourceRef:
        if isinstance(raw, bool) or raw is None:
            raise SourceRefError(f"source {raw!r} is not an RDR id or a catalog tumbler")
        text = str(raw).strip()
        if not text:
            raise SourceRefError("source is empty; expected an RDR id (RDR-084) or a catalog tumbler (1.2.4686)")
        m = _RDR_ID_RE.match(text)
        if m is not None or _DIGITS_RE.match(text):
            number = int(m.group(1) if m else text)
            return cls(kind="rdr", value=f"RDR-{number:03d}", number=number)
        if _TUMBLER_RE.match(text):
            if text.count(".") < 2:
                raise SourceRefError(
                    f"source {text!r} is an owner tumbler, not a document; pass it as 'owner' "
                    "and name the document in 'source'"
                )
            return cls(kind="tumbler", value=text)
        raise SourceRefError(
            f"source {text!r} is not an RDR id (RDR-084) or a document tumbler (1.2.4686)"
        )

    @classmethod
    def parse_labeled(cls, label: object) -> tuple[SourceRef, str | None]:
        """Parse ``RDR-084@ART`` into ``(SourceRef, "ART")``; no ``@`` gives ``None``.

        The golden set (and any human note) names a project-qualified source
        this way. ``SourceRef.parse`` stays strict and never sees the ``@``:
        the qualifier travels as the ``project`` (or ``owner``) binding, so a
        caller turns a label into ``source=<ref.value>`` plus
        ``project=<qualifier>``.
        """
        text = str(label).strip() if label is not None else ""
        base, sep, qualifier = text.partition("@")
        ref = cls.parse(base)
        qualifier = qualifier.strip()
        if sep and not qualifier:
            raise SourceRefError(f"source {text!r} has an empty project after '@'")
        return ref, (qualifier or None)

    def to_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value}


def parse_sources(raw: object) -> tuple[tuple[SourceRef, ...], tuple[str, ...]]:
    """Parse the ``source`` binding into ``(sources, errors)``.

    One reference, a list of references, or a comma- or semicolon-separated
    string of them. Order is kept and repeats are dropped. A malformed
    element becomes an error string and the others still parse: the caller
    discloses what it could not honour and goes on with the rest.
    """
    if isinstance(raw, (list, tuple)):
        parts: list[object] = list(raw)
    elif isinstance(raw, str):
        parts = [p for p in re.split(r"[;,]", raw) if p.strip()] or [raw]
    else:
        parts = [raw]
    sources: list[SourceRef] = []
    errors: list[str] = []
    for part in parts:
        try:
            ref = SourceRef.parse(part)
        except SourceRefError as exc:
            errors.append(str(exc))
            continue
        if ref not in sources:
            sources.append(ref)
    return tuple(sources), tuple(errors)


# ── Owner qualifier ──────────────────────────────────────────────────────────


class OwnerNameError(ValueError):
    """A project or owner name names no registered owner, or several."""


NameLookup = Callable[[str], str]

_SUBTREE_RE = re.compile(r"^\d+(?:\.\d+)*$")


def scope_subtree(scope: str | None) -> str | None:
    """*scope* when it is a catalog subtree (digits and dots), else ``None``.

    A corpus prefix (``rdr``) or a full collection name is not a subtree.
    ``nx_answer`` resolves a registered owner NAME to its tumbler before
    this runs, so a name arrives here already as a subtree.
    """
    text = (scope or "").strip()
    return text if _SUBTREE_RE.match(text) else None


def _segments(tumbler: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tumbler.split("."))


def _comparable(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """Whether one is a segment prefix of the other (never a string prefix)."""
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return long_[: len(short)] == short


@dataclass(frozen=True)
class OwnerResolution:
    """The owner qualifier a request resolved to.

    ``status``: ``none`` (nothing supplied), ``resolved`` (``tumbler`` set),
    ``conflict`` (qualifiers in different subtrees; nothing picked) or
    ``unresolved`` (a named qualifier that names no single owner; nothing
    guessed). ``contributions`` records which inputs (``owner``,
    ``project``, ``scope``) supplied which tumbler.
    """

    status: Literal["none", "resolved", "conflict", "unresolved"]
    tumbler: str | None = None
    contributions: tuple[tuple[str, str], ...] = ()
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tumbler": self.tumbler,
            "contributions": dict(self.contributions),
            "detail": self.detail,
        }


def resolve_owner_qualifier(
    *,
    owner: object,
    project: object,
    scope: str | None,
    name_lookup: NameLookup,
) -> OwnerResolution:
    """Combine the ``owner`` and ``project`` bindings and a subtree ``scope``.

    A tumbler-shaped ``owner``/``project`` is used as given; any other value
    is an owner name and goes through *name_lookup* (catalog owner
    identity). A lookup that fails makes the whole qualifier ``unresolved``:
    an explicit qualifier that cannot be honoured is never replaced by a
    looser one. A subtree ``scope`` deeper than the store counts only as a
    qualifier of its own when it agrees; see the module docstring.
    """
    contributions: list[tuple[str, str]] = []
    problems: list[str] = []

    for origin, raw in (("owner", owner), ("project", project)):
        if raw is None or not str(raw).strip():
            continue
        text = str(raw).strip()
        try:
            tumbler = text if _SUBTREE_RE.match(text) else str(name_lookup(text))
            if not _SUBTREE_RE.match(tumbler):
                raise OwnerNameError(f"resolved to {tumbler!r}, which is not a tumbler")
            if len(_segments(tumbler)) < 2:
                raise OwnerNameError(
                    f"{tumbler!r} is not an owner tumbler (an owner has at least two segments, e.g. 1.2)"
                )
        except OwnerNameError as exc:
            problems.append(f"{origin}={text!r}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — the disclosure stage must never raise; a failed catalog lookup is reported as unresolved
            _log.warning("evidence_owner_lookup_failed", origin=origin, error=str(exc))
            problems.append(f"{origin}={text!r}: catalog lookup failed ({type(exc).__name__})")
            continue
        contributions.append((origin, tumbler))

    subtree = scope_subtree(scope)
    if subtree is not None and len(_segments(subtree)) >= 2:
        contributions.append(("scope", subtree))

    if problems:
        return OwnerResolution(
            "unresolved", None, tuple(contributions),
            "; ".join(problems) + ". Nothing was guessed.",
        )
    if not contributions:
        return OwnerResolution("none")

    parsed = [(origin, t, _segments(t)) for origin, t in contributions]
    for i, (oa, ta, sa) in enumerate(parsed):
        for ob, tb, sb in parsed[i + 1:]:
            if not _comparable(sa, sb):
                return OwnerResolution(
                    "conflict", None, tuple(contributions),
                    f"{oa} {ta} and {ob} {tb} name different subtrees "
                    "(neither is a prefix of the other). Nothing was picked.",
                )
    longest = max(parsed, key=lambda item: len(item[2]))[1]
    return OwnerResolution("resolved", longest, tuple(contributions))


def catalog_name_lookup(cat: Any) -> NameLookup:
    """A *name_lookup* over the catalog's registered owners.

    Delegates to :func:`nexus.catalog.owner_scope.resolve_owner_scope`
    (strict), so a project name resolves exactly as ``nx_answer(scope=...)``
    and ``query(subtree=...)`` resolve an owner name: a repo owner wins over
    a same-named curator, an unknown or ambiguous name is an error.
    """

    def lookup(name: str) -> str:
        if cat is None:
            raise OwnerNameError("the catalog is unavailable, so the name cannot be resolved")
        from nexus.catalog.owner_scope import (  # noqa: PLC0415 — deferred: keeps this module stdlib-only at import time
            OwnerScopeError,
            resolve_owner_scope,
        )

        try:
            return resolve_owner_scope(cat, name, strict=True)
        except OwnerScopeError as exc:
            raise OwnerNameError(str(exc)) from exc

    return lookup


# ── The request as a whole ───────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceRequest:
    """What the caller asked for through the reserved bindings and ``scope``."""

    requested: bool
    source: SourceRef | None
    source_error: str | None
    owner: OwnerResolution
    #: Every named source, in the order given. ``source`` is the sole one
    #: (the single-source shape callers already read) and ``None`` when
    #: there are several; read ``sources`` for the full request.
    sources: tuple[SourceRef, ...] = ()

    def to_wire(self) -> dict[str, Any] | None:
        """Envelope form, or ``None`` when no reserved binding was passed."""
        if not self.requested:
            return None
        return {
            "source": self.source.to_wire() if self.source else None,
            "source_error": self.source_error,
            "owner": self.owner.to_wire(),
            "sources": [s.to_wire() for s in self.sources],
        }

    def disclosures(self) -> list[str]:
        """One-line text-mode notices for what the request could not honour."""
        if not self.requested:
            return []
        lines: list[str] = []
        if self.source_error:
            lines.append(f"[evidence request: {self.source_error}]")
        if self.owner.status in ("conflict", "unresolved"):
            lines.append(f"[evidence request: owner qualifier {self.owner.status}: {self.owner.detail}]")
        return lines


def build_evidence_request(
    bindings: Mapping[str, Any] | None,
    *,
    scope: str | None,
    name_lookup: NameLookup,
) -> EvidenceRequest:
    """Build the :class:`EvidenceRequest` for one ``nx_answer`` call.

    Never raises: a malformed ``source`` becomes ``source_error`` and a
    failed qualifier becomes an ``unresolved`` or ``conflict`` resolution,
    both disclosed rather than fatal.
    """
    _, reserved = split_reserved(bindings)

    sources: tuple[SourceRef, ...] = ()
    source_error: str | None = None
    if "source" in reserved:
        sources, errors = parse_sources(reserved["source"])
        source_error = "; ".join(errors) or None
    source = sources[0] if len(sources) == 1 else None

    owner = resolve_owner_qualifier(
        owner=reserved.get("owner"),
        project=reserved.get("project"),
        scope=scope,
        name_lookup=name_lookup,
    )
    return EvidenceRequest(
        requested=bool(reserved),
        sources=sources,
        source=source,
        source_error=source_error,
        owner=owner,
    )


# ── Caller scope over plan-declared corpora (RDR-232 section 2a) ─────────────

ScopeKind = Literal["none", "subtree", "corpus_prefix", "collection"]

#: Tokens that are corpus words, never owner names, whatever the owner table holds (the same
#: protection ``resolve_owner_scope(strict=False)`` gives ``nx_answer(scope="knowledge")``).
_CORPUS_WORDS: frozenset[str] = frozenset({"knowledge", "code", "docs", "rdr", "paper", "all"})


@dataclass(frozen=True)
class ScopeConstraint:
    """What the caller's ``scope`` string means (RDR-232 section 2a, review S8).

    The discriminator, in order:

    1. a token of digits and dots is a catalog **subtree** (``1.2``, ``1.2.4686``);
    2. a conformant four-segment name (``content_type__owner__model__vN``) is a full
       **collection** name;
    3. with a *name_lookup*, a registered owner name is a **subtree** (its tumbler);
    4. anything else is a corpus **prefix** (``rdr``, ``knowledge__x``).

    ``nx_answer`` resolves an owner name to its tumbler before the scope reaches the runner, so the
    runner and the fast path parse without a lookup; step 3 serves a caller that holds the raw
    string.
    """

    kind: ScopeKind
    value: str = ""

    @classmethod
    def parse(
        cls, scope: object, *, name_lookup: NameLookup | None = None,
    ) -> ScopeConstraint:
        text = "" if scope is None else str(scope).strip()
        if not text:
            return cls("none", "")
        if scope_subtree(text) is not None:
            return cls("subtree", text)
        from nexus.corpus import is_conformant_collection_name  # noqa: PLC0415 — deferred: keeps this module stdlib-only at import time

        if is_conformant_collection_name(text):
            return cls("collection", text)
        if name_lookup is not None and text not in _CORPUS_WORDS and "__" not in text:
            try:
                tumbler = str(name_lookup(text))
            except OwnerNameError:
                tumbler = ""
            except Exception as exc:  # noqa: BLE001 — a failed catalog lookup leaves the token a corpus prefix, which is what it was before owner names existed
                _log.warning("evidence_scope_lookup_failed", scope=text, error=str(exc))
                tumbler = ""
            if scope_subtree(tumbler) is not None:
                return cls("subtree", tumbler)
        return cls("corpus_prefix", text)

    @property
    def narrows_corpus(self) -> bool:
        """Whether the scope is a corpus (prefix or collection) rather than a subtree."""
        return self.kind in ("corpus_prefix", "collection")


NarrowStatus = Literal[
    "no_scope",                 # the caller gave no scope: the corpus is untouched
    "scope_applied",            # the plan declared no corpus: the scope is the corpus
    "narrowed",                 # `all` or plan entries narrowed to the scope
    "within_scope",             # every plan entry was already under the scope
    "conflict",                 # no plan entry overlaps the scope: the step is skipped
    "scope_all",                # scope `all` restricts nothing
    "subtree_not_intersected",  # a catalog subtree is not a corpus and is not intersected
    "unscoped_collection_pin",  # pinned through collection(s): the plan wins (reported only)
]


@dataclass(frozen=True)
class CorpusNarrowing:
    """The outcome of applying a :class:`ScopeConstraint` to one step's resolved corpus."""

    status: NarrowStatus
    corpus: str | None
    plan_corpus: str | None
    scope: ScopeConstraint

    @property
    def conflict(self) -> bool:
        return self.status == "conflict"

    def to_diagnostic(self, *, step_index: int, tool: str) -> dict[str, Any]:
        """The wire form carried in ``PlanResult.scope_diagnostics`` and the envelope."""
        return {
            "step_index": step_index,
            "tool": tool,
            "plan_corpus": self.plan_corpus,
            "caller_scope": self.scope.value,
            "scope_kind": self.scope.kind,
            "outcome": self.status,
            "resolved_corpus": self.corpus,
        }


def _corpus_entries(plan_corpus: object) -> list[str]:
    if plan_corpus is None:
        return []
    parts = plan_corpus if isinstance(plan_corpus, (list, tuple)) else [plan_corpus]
    return [e for part in parts for e in (s.strip() for s in str(part).split(",")) if e]


def _under(entry: str, parent: str) -> bool:
    """*entry* is *parent* or a name under it (``rdr__1-1__...`` under ``rdr``): a segment match."""
    return entry == parent or entry.startswith(parent + "__")


def narrow_corpus(plan_corpus: object, scope: ScopeConstraint) -> CorpusNarrowing:
    """Apply the caller's *scope* to a step's resolved *plan_corpus* (RDR-232 section 2a).

    *plan_corpus* is whatever the step has after binding resolution and
    ``scope.taxonomy_domain``: ``None``/blank (nothing), a comma string, or a list. Matching goes
    both ways (review S8): a plan entry matches when it is under the scope (``rdr__1-1__...`` under
    ``rdr``) or the scope is under the entry (scope ``knowledge__x`` under entry ``knowledge``);
    the step's corpus becomes the narrower of the two. ``all`` is no restriction, so it gives the
    scope. No overlap is a ``conflict``: the caller does not run the step unscoped and does not
    drop the scope.

    A catalog-subtree scope is not a corpus and is returned as ``subtree_not_intersected`` with
    the plan's corpus untouched; the caller keeps its existing forwarding for it.
    """
    entries = _corpus_entries(plan_corpus)
    plan_text = ",".join(entries) or None
    if scope.kind == "none":
        return CorpusNarrowing("no_scope", plan_text, plan_text, scope)
    if scope.kind == "subtree":
        return CorpusNarrowing("subtree_not_intersected", plan_text, plan_text, scope)
    if scope.value == "all":
        return CorpusNarrowing("scope_all", plan_text or "all", plan_text, scope)
    if not entries:
        return CorpusNarrowing("scope_applied", scope.value, None, scope)
    if "all" in entries:
        return CorpusNarrowing("narrowed", scope.value, plan_text, scope)
    kept: list[str] = []
    for entry in entries:
        if _under(entry, scope.value):
            kept.append(entry)
        elif _under(scope.value, entry):
            kept.append(scope.value)
    kept = list(dict.fromkeys(kept))
    if not kept:
        return CorpusNarrowing("conflict", None, plan_text, scope)
    status: NarrowStatus = "within_scope" if kept == entries else "narrowed"
    return CorpusNarrowing(status, ",".join(kept), plan_text, scope)


def scope_conflict_clauses(diagnostics: object) -> list[str]:
    """One clause per conflicted step in *diagnostics*, 1-indexed for a human reader."""
    clauses: list[str] = []
    for d in diagnostics if isinstance(diagnostics, list) else []:
        if isinstance(d, dict) and d.get("outcome") == "conflict":
            clauses.append(
                f"step {int(d.get('step_index', 0)) + 1} ({d.get('tool', '?')}) declares corpus "
                f"{d.get('plan_corpus')!r}, outside the caller scope {d.get('caller_scope')!r}, "
                "and was skipped"
            )
    return clauses


def scope_conflict_notice(diagnostics: object) -> str | None:
    """The one-line text-mode disclosure for skipped steps, or ``None`` when nothing conflicted."""
    clauses = scope_conflict_clauses(diagnostics)
    return f"[scope conflict: {'; '.join(clauses)}]" if clauses else None
