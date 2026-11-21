# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 item 1: the parser definitions and the migration-source floor.

A collection name is a label. Everything a reader needs to know about a
collection (its content type, owner, embedding model, model version) is a column
of its registry row, read through :func:`nexus.catalog.rows.resolve.resolve_collection`.
What stays here is the reading of a name that has no row: a name the user types, one
``nx`` is about to register (the *mint* class), a name that came from another install or
an older era (*migration source*), or a name a diagnostic reports against its row
(*diagnostic*). A caller of anything in this module is one of those three, and
``tests/test_collection_name_parse_census.py`` records which beside the call. A site
that picks behaviour by the name's content-type prefix is *routing*, which never
calls this module (it reads the row's ``content_type``, item 12).

Every other name read is a classified census caller of something here. P4.3 deletes the
parsers outside this module (RDR-229, Phase 4); this module stays as the floor, and P4.4
removes the bundle callers of it. The blank-``model_version`` fallback
(:func:`version_of_blank_row_label`) is removed once D2's cloud run
(``nexus-f3yqd.2.23``) completes; no bead owns that removal. Nothing in this module imports
another ``nexus`` module, because ``nexus.corpus`` imports it.
"""
from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

import structlog

_log = structlog.get_logger(__name__)

#: The content-type segments a conformant name may carry. ``nexus.corpus.CONTENT_TYPES``
#: is the same tuple; this module cannot import it (corpus imports this module), so
#: ``tests/test_legacy_names.py`` pins them equal.
NAME_CONTENT_TYPES: tuple[str, ...] = ("code", "docs", "rdr", "knowledge")

_CT_ALTERNATION = "|".join(NAME_CONTENT_TYPES)
#: The owner-segment grammar, shared wherever an owner-shaped value is
#: validated (RDR-204 Phase 3 item 7, coordinator grammar decision
#: 2026-09-08, engine-side hygiene-004-1 / nexus-ztafa): SINGLE
#: underscores only, never a run of two. ``"__"`` is always the segment
#: separator, so an owner containing a literal ``"__"`` would make a
#: two-segment name ambiguous with a four-segment one (e.g. is
#: ``code__my__repo`` a 2-segment name with owner ``"my__repo"``, or a
#: malformed 3-segment one?). ``is_conformant_collection_name`` used to be
#: STRICTER than this (rejecting any underscore at all) and then, briefly
#: during this bead, LOOSER (admitting an unrestricted run) -- this is the
#: settled middle: an underscore is allowed as an internal separator
#: between alnum/hyphen runs, never doubled.
_OWNER_SEGMENT_RE = r"[a-zA-Z0-9-]+(?:_[a-zA-Z0-9-]+)*"

_CONFORMANT_COLLECTION_RE = re.compile(
    rf"^(?P<ct>{_CT_ALTERNATION})"
    rf"__(?P<owner>{_OWNER_SEGMENT_RE})"
    r"__(?P<model>[a-z][a-z0-9-]*)"
    r"__v(?P<ver>\d+)$"
)


def is_conformant_collection_name(name: str) -> bool:
    """Return True if ``name`` matches the RDR-101 §"Collection naming"
    canonical schema ``<content_type>__<owner_id>__<embedding_model>__v<n>``.

    The bead spec uses ``@`` as the version separator; ChromaDB's name
    regex disallows ``@``, so this implementation encodes the ``@`` as a
    fourth ``__`` separator. Tumbler-style owner IDs (which contain
    dots, e.g. ``1.1``) must be supplied with dots replaced by hyphens
    so the segment fits ChromaDB's charset.

    Returns False for legacy 2-segment names (``docs__nexus-571b8edd``),
    fallback names (``docs__default``, ``knowledge__knowledge``), and
    taxonomy-prefixed names. Such names are valid grandfathered
    identities; this predicate only describes whether a name conforms
    to the post-Phase-6 canonical schema. Read paths must continue to
    accept legacy names per RDR-101 (failing-loud at read time is
    rejected as operationally hostile).
    """
    return bool(_CONFORMANT_COLLECTION_RE.match(name))


def parse_conformant_collection_name(name: str) -> dict[str, str]:
    """Decompose a conformant name into its four canonical segments.

    Raises ValueError if ``name`` is not conformant; callers wanting a
    safe parse should gate with :func:`is_conformant_collection_name`.
    """
    match = _CONFORMANT_COLLECTION_RE.match(name)
    if not match:
        raise ValueError(
            f"Collection name {name!r} is not conformant: "
            f"expected <content_type>__<owner_id>__<embedding_model>__v<n>"
        )
    g = match.groupdict()
    return {
        "content_type": g["ct"],
        "owner_id": g["owner"],
        "embedding_model": g["model"],
        "model_version": f"v{g['ver']}",
    }


def embedding_model_for_collection_name(collection_name: str) -> str | None:
    """Return the embedding-model token parsed from a conformant
    collection name, or ``None`` if *collection_name* is not conformant.

    RDR-109 Phase 2: read-side dispatch reads the model identity from
    the name itself rather than inferring from the prefix. The
    inference-from-prefix shape (:func:`voyage_model_for_collection`)
    is preserved for legacy two-segment names; conformant four-segment
    names use the embedded token directly so local-token names route
    through the local EF without colliding with the voyage default.
    """
    match = _CONFORMANT_COLLECTION_RE.match(collection_name)
    if not match:
        return None
    return match.groupdict()["model"]


def model_version_for_collection_name(collection_name: str) -> str | None:
    """Return the ``v<n>`` model-version segment of a conformant collection
    name (e.g. ``"v1"``), or ``None`` if *collection_name* is not
    conformant.

    RDR-204 Phase 3 (nexus-ft04v.27): the sibling of
    :func:`embedding_model_for_collection_name` for the fourth
    ``CollectionName`` field. A registration site that is re-registering an
    EXISTING physical collection it did not just render (a backfill of a
    name already sitting in T3, a reindex re-registration, an operator-
    typed rename target) has no in-memory ``CollectionName`` to read the
    version off of -- the version segment embedded in the name is the
    only place that fact lives until the collection is registered. Reads
    the SAME permissive regex :func:`is_conformant_collection_name` and
    :func:`embedding_model_for_collection_name` use (no canonical-model-set
    check), so it accepts the same non-canonical test/fixture model tokens
    (e.g. ``stub-code-1024``) those two already do -- unlike
    ``CollectionName.parse``, which validates the model segment against
    :data:`CANONICAL_EMBEDDING_MODELS` / :data:`LOCAL_EMBEDDING_MODELS` and
    would reject such a name outright, changing what today's registration
    sites happily accept.
    """
    match = _CONFORMANT_COLLECTION_RE.match(collection_name)
    if not match:
        return None
    return f"v{match.groupdict()['ver']}"


#: Regex equivalent of "split at the FIRST '__', or ('', whole-string) when
#: there is none" -- see :func:`split_candidate_collection_name`. Written as
#: a regex (like :data:`_CONFORMANT_COLLECTION_RE`) rather than
#: ``partition("__")``/``"__" in``, so the primitive hides its parse behind a
#: function call. ``tests/test_collection_name_parse_census.py`` counts its
#: callers through the fifth class (the derived helper set: every function in
#: corpus function that reaches a parser is a counted helper, RDR-229 Technical
#: Design item 4); the regex itself is why the census could not see it through
#: its original four classes.
#: The non-greedy first group stops at the EARLIEST "__", matching
#: ``partition``'s first-occurrence semantics exactly; ``re.DOTALL`` so a
#: newline inside a (pathological) candidate string cannot break the match.
_LEGACY_SPLIT_RE = re.compile(r"^(.*?)__(.*)$", re.DOTALL)


def split_candidate_collection_name(name: str) -> tuple[str, str]:
    """(first segment, remainder) for a candidate NAME STRING that is not
    (or is not yet) a registered collection -- e.g. a bare or legacy
    ``--collection``/``--corpus`` argument being resolved into a name to
    MINT, never a lookup against an existing collection's attributes (see
    :func:`collection_content_type` for that read-side job, which no
    longer calls this).

    PUBLIC (nexus-ft04v.26): ``mcp.core._resolve_corpus_target`` needs the
    exact same "does this user-typed --corpus token contain a '__'
    separator" shape check on an ARGUMENT that is not itself necessarily
    an existing collection -- a raw ``"__" in part`` there would re-add a
    counted site to ``tests/test_collection_name_parse_census.py``'s AST
    scan (mcp/core.py was funnelled to zero by nexus-ft04v.21). Since
    RDR-229 P1.1 its callers are counted too, as ``primitive_call`` sites
    of the derived helper set.

    RDR-204 Phase 3 repoint (nexus-ft04v.26): this is the shared
    STRING-SHAPE primitive for the handful of corpus.py sites that derive
    a candidate content_type/owner_id from a not-yet-existing name --
    :func:`t3_collection_name`'s promotion logic, :func:`_refuse_placeholder_subject`,
    and :func:`validate_collection_name`'s overflow hint. Those sites
    cannot become catalog-row lookups: the whole point of the string
    they are parsing is that no row exists for it yet (that is what
    registration is for). Mirrors the two legacy conventions every
    pre-funnel raw site already used ad hoc: a name with no ``__`` at all
    has no separate content-type segment (first ``""``) and the whole
    string IS the identity being handled (remainder ``name``); a name
    WITH a ``__`` splits at the FIRST occurrence only, so the remainder
    can itself still contain further ``__`` (a compound owner/subject, or
    a malformed 3+-segment name, keeps its embedded double underscores
    intact).
    """
    m = _LEGACY_SPLIT_RE.match(name)
    if not m:
        return "", name
    return m.group(1), m.group(2)


def candidate_knowledge_subject(name: str) -> str:
    """The subject of a ``--collection`` argument when it names a knowledge collection, else ``""`` (GH #1611).

    A bare name (``delos``, which :func:`t3_collection_name` makes ``knowledge__delos__...``) is its own subject;
    ``knowledge__delos`` and ``knowledge__delos__<model>__v<n>`` give ``delos``. A ``docs__``/``code__``/``rdr__`` name
    has no subject (its second segment is a tumbler-derived owner id such as ``1-48``), nor does a name with an empty
    subject. Parses a CANDIDATE string with :func:`split_candidate_collection_name`, as the argument may name a
    collection not registered yet; never a read of an existing collection's attributes.
    """
    first, rest = split_candidate_collection_name(name)
    if not first:
        return name
    if first != "knowledge":
        return ""
    subject, _ = split_candidate_collection_name(rest)
    return subject or rest  # no further "__": the remainder is the subject


#: The three candidate-string-derivation PRIMITIVES (RDR-204 Phase 3 fix
#: round, nexus-ft04v.28 item 6). They are the NON-VACUITY FLOOR of the
#: census's derived helper set, not its definition: since RDR-229 P1.1
#: (nexus-p2m1x.1) ``tests/_collection_name_census_scan.py`` derives the
#: counted helpers by walking this module's call graph from the parser
#: definitions (the two regexes and any raw ``"__"`` test), and the census
#: asserts the derived set contains these three, so a rename that drops one
#: from the derivation fails loud. The tuple stays single-sourced here; the
#: test also asserts every name still resolves to a real function.
#:
#: Each derives a NAME-LEVEL fact (embedding_model / model_version /
#: content_type+owner) that COULD instead come from the authoritative
#: catalog row (:func:`nexus.mcp_infra.get_collection_row`): a caller
#: reaching for one where a row is available is doing what the row-based
#: funnel helpers (:func:`collection_content_type` /
#: :func:`collection_owner` / :func:`collection_model`) exist to replace,
#: RDR-204's "the row is authoritative, GH #667" premise. All three are
#: regex-based (:data:`_LEGACY_SPLIT_RE` / :data:`_CONFORMANT_COLLECTION_RE`).
#:
#: :func:`is_conformant_collection_name` / :func:`parse_conformant_collection_name`
#: are NOT in this tuple, and are still counted: they are parser roots of the
#: derived set, because they apply ``_CONFORMANT_COLLECTION_RE`` (RDR-229
#: P1.1). They answer "is this string well-formed" / "what are its four
#: segments", which no catalog row can answer, so a caller is classified
#: (mint, migration source, diagnostic) rather than moved to a row read.
_CANDIDATE_STRING_PRIMITIVES: tuple[str, ...] = (
    "split_candidate_collection_name",
    "embedding_model_for_collection_name",
    "model_version_for_collection_name",
)


@dataclass(frozen=True, slots=True)
class CandidateAttributes:
    """What a name that has no registry row says about itself.

    ``conformant`` names carry all four fields. A legacy two-segment name carries
    a content type and an owner (``embedding_model`` and ``model_version`` are
    ``None``); a bare name has no content type (``""``) and is its own owner.
    """

    name: str
    content_type: str
    owner_id: str
    embedding_model: str | None
    model_version: int | None
    conformant: bool


def candidate_attributes(name: str) -> CandidateAttributes | None:
    """The attributes a candidate *name* encodes, or ``None`` when it encodes none.

    The name parser every caller reads through (RDR-229 item 1). ``None`` for a non-string, an empty
    name, and a name with a ``__`` separator but nothing on one side of it
    (``docs__``, ``__weird``): there is no owner, or no content type, to derive.
    Never a read of a registered collection's attributes; that is
    :func:`nexus.catalog.rows.resolve.resolve_collection`.
    """
    if not isinstance(name, str) or not name:
        return None
    match = _CONFORMANT_COLLECTION_RE.match(name)
    if match is not None:
        g = match.groupdict()
        return CandidateAttributes(
            name=name, content_type=g["ct"], owner_id=g["owner"],
            embedding_model=g["model"], model_version=int(g["ver"]), conformant=True,
        )
    first, rest = split_candidate_collection_name(name)
    if "__" in name and (not first or not rest):
        return None
    return CandidateAttributes(
        name=name, content_type=first, owner_id=rest,
        embedding_model=None, model_version=None, conformant=False,
    )


def is_taxonomy_collection_name(name: str) -> bool:
    """Whether *name* is a ``taxonomy__`` collection, by its first segment (a migration source).

    Exactly ``name.startswith("taxonomy__")``, read through the one name parser (RDR-229 P2.2): a
    taxonomy collection has no catalog row attribute that tells it apart from a content-typed one,
    so a name no row can decide is the only evidence (P2.9 ruling). Taxonomy collections are
    outside RDR-229's content types.
    """
    return split_candidate_collection_name(name)[0] == "taxonomy"


#: A repo-synth owner stem: ``<name>-<hash8>`` (``nexus.repo_identity._safe_collection``), the hash being
#: the first 8 hex characters of the main repo path's sha256, which is the repo owner's ``repo_hash``.
_REPO_STEM = re.compile(r"^(?P<slug>.+)-(?P<hash>[0-9a-f]{8})$")


def owner_stem(collection_name: str) -> tuple[str, tuple[str, str] | None]:
    """``(stem, repo)`` of *collection_name*: its owner segment (``<type>__<stem>__...``, ``""`` when there
    is none) and, when that segment is ``<slug>-<hash8>``, ``(slug, hash8)``, else ``None``.

    Migration source (RDR-229 item 1; moved here from ``nxexp_owner`` so the parsers sit in one module that
    P4.3 keeps as the floor): the name is read because it comes from another install's ``.nxexp`` bundle
    and no row of this catalog backs it. ``nexus.nxexp_owner.resolve_import_owner_tumbler`` is the caller.
    """
    parts = collection_name.split("__")
    stem = parts[1] if len(parts) > 1 else ""
    m = _REPO_STEM.match(stem)
    return stem, ((m["slug"], m["hash"]) if m else None)


def version_of_blank_row_label(label: str | None, *, models: Collection[str]) -> int | None:
    """The model version a registry row with a BLANK ``model_version`` takes from its label.

    Migration source, temporary (P1.4 A, moved here by P2.2; removed once D2's cloud run,
    ``nexus-f3yqd.2.23``, completes). The column is ``NOT NULL DEFAULT ''`` and an upsert never
    rewrites it, so rows exist whose version is the empty string (53 live ones until D2). A
    conformant label carries the version in its suffix: read it, and warn
    (``collection_for_row_model_version_empty``). A label that carries none (a legacy slug), or
    whose model token is not in *models*, answers ``None`` with its own warning, and the caller
    decides what a row with no readable version means. *models* is the closed set of
    embedding-model tokens a conformant name may carry (``CollectionName.parse`` refused any
    other, and this fallback keeps that: a ``stub-code-1024`` label has no readable version);
    this module cannot import them from ``nexus.corpus``, so the caller passes them. A label
    whose suffix reads ``v0`` raises ``ValueError``: no collection version is below 1.
    """
    match = _CONFORMANT_COLLECTION_RE.match(label) if isinstance(label, str) else None
    if match is None or match.group("model") not in models:
        _log.warning("collection_for_row_model_version_unreadable", collection=label)
        return None
    version = int(match.group("ver"))
    if version < 1:
        raise ValueError(f"collection row {label!r} has an empty model_version and name version v0")
    _log.warning("collection_for_row_model_version_empty", collection=label)
    return version
