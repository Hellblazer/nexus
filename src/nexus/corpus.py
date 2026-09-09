# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re
import threading
from collections.abc import Callable
from typing import TypeVar

import click
import structlog

_log = structlog.get_logger(__name__)

# ChromaDB collection name constraints:
# - 3–63 characters
# - Must start and end with an alphanumeric character
# - May contain alphanumeric characters, hyphens, or underscores in the middle
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$")

# Canonical content_type prefixes.  Defined here (before validate_collection_name)
# so the overflow hint can include a concrete ``--content-type`` flag when the
# name's prefix is recognisable.  The public alias ``CONTENT_TYPES`` is re-exported
# below for backward-compat.
_CONTENT_TYPES = ("code", "docs", "rdr", "knowledge")


def validate_collection_name(name: str) -> None:
    """Raise ValueError if *name* violates ChromaDB collection name constraints.

    Enforces two sets of rules:
    1. Structural (open-source ChromaDB): 3–63 characters, alphanumeric + hyphens/underscores,
       must start and end with alphanumeric.
    2. Cloud byte-length limit: name must not exceed 128 bytes when UTF-8 encoded.
       Relevant if names ever contain multi-byte characters; all current ASCII names
       are well within this limit since they cap at 63 chars = 63 bytes.

    This is CHARSET legality of the WHOLE name string (start/end
    alphanumeric, alphanumeric+hyphen+underscore in between) -- it has no
    concept of segments and is deliberately NOT the owner-segment
    ambiguity rule (RDR-204 Phase 3 item 7, coordinator grammar decision
    2026-09-08): that structural check -- an owner admits single
    underscores only, never a run of two, because "__" is always the
    segment separator -- lives in :func:`is_conformant_collection_name` /
    :data:`_OWNER_SEGMENT_RE`. Do not re-derive it here: this function
    validates names that legitimately contain three literal "__"
    separators (a full 4-segment conformant name), so a segment-aware
    rule cannot live in this flat charset check without rejecting every
    conformant name at creation time.
    """
    # Length check fires first for <3 chars; regex rejects other invalid patterns.
    # Both gates are needed: length for clear error messages, regex for charset/boundary validation.
    if len(name) > 63:
        # Overflow: derive an actionable hint.  The canonical remedy is to
        # rename to the repo-id-conformant name (e.g. ``code__1-36__bge-base-en-v15-768__v1``,
        # 35 chars) which fits under the cap and preserves vectors (no reindex).
        # Derive content_type from the name prefix (before the first ``__``) when
        # present so the hint can be concrete; fall back to a generic flag otherwise.
        # RDR-204 Phase 3 repoint (nexus-ft04v.26): an oversized *name* is
        # necessarily NOT a real (registered) collection -- collection_content_type
        # now reads the catalog row and would raise CollectionNotRegisteredError
        # here every time. This is a best-effort HINT about a candidate string,
        # so it uses the string-shape primitive directly, same as
        # t3_collection_name's own candidate-parsing sites below.
        # split_candidate_collection_name returns "" for a name with no "__" at
        # all, which fails the membership test below exactly like the old
        # "__" in name guard did.
        _ct_hint = ""
        _prefix, _ = split_candidate_collection_name(name)
        if _prefix in _CONTENT_TYPES:
            _ct_hint = f" --content-type {_prefix}"
        raise ValueError(
            f"Collection name {name!r} must be 3–63 characters (got {len(name)}). "
            f"The name is too long for ChromaDB's 63-character cap. "
            f"To get a conformant name that fits and preserves vectors (no reindex), run:\n"
            f"  nx catalog collection-name{_ct_hint} --repo <repo-path>"
        )
    if len(name) < 3:
        raise ValueError(
            f"Collection name {name!r} must be 3–63 characters (got {len(name)})"
        )
    if not _COLLECTION_NAME_RE.match(name):
        raise ValueError(
            f"Collection name {name!r} must start and end with an alphanumeric character "
            "and contain only alphanumeric characters, hyphens, or underscores"
        )
    # ChromaDB Cloud additional constraint: 128-byte limit (byte length, not char length).
    name_bytes = len(name.encode())
    if name_bytes > 128:
        raise ValueError(
            f"Collection name {name!r} exceeds ChromaDB Cloud 128-byte limit "
            f"(encoded as {name_bytes} bytes)"
        )


CONTENT_TYPES: tuple[str, ...] = _CONTENT_TYPES

#: Subject names that are containers, not subjects (docs/collections.md
#: Rule 1). A write that names one of these as the subject is refused
#: (nexus-0fw11): ``knowledge__knowledge`` and ``docs__default`` on the
#: production tenant were both minted by taking a default where a subject
#: was needed. Reads are unaffected, and a full four-segment conformant
#: name passes through untouched, which is the deliberate escape for the
#: collections that already exist. ``tests/test_corpus.py`` pins this set
#: to the list docs/collections.md prints.
PLACEHOLDER_SUBJECTS: frozenset[str] = frozenset({"default", "knowledge", "notes", "tmp", "test"})
"""Public alias for the canonical content_type values used in the
RDR-103 ``<content_type>__<owner_id>__<embedding_model>__v<n>`` schema.
``CollectionName`` validates against this tuple."""

CANONICAL_EMBEDDING_MODELS: frozenset[str] = frozenset({
    "voyage-context-3",
    "voyage-code-3",
})
"""RDR-103 canonical-set guard. Any embedding-model segment NOT in this
set is treated as legacy/unknown by ``CollectionName.parse``. Pinned
decision #1: migrations use the indexer's CURRENT canonical model rather
than parsing the model out of the legacy collection name; allowing
non-canonical models here would defeat that invariant. The
``_CONFORMANT_COLLECTION_RE`` regex stays permissive so legacy names
remain readable as strings; canonical-set validation lives in
``CollectionName.parse``."""

LOCAL_EMBEDDING_MODELS: frozenset[str] = frozenset({
    "minilm-l6-v2-384",
    "bge-base-en-v15-768",
})
"""RDR-109 Phase 2: tokens for the local embedders. The write path uses
these when ``is_local_mode()`` is True so a collection name produced in
local mode tells the truth about which vectors live inside. The
bidirectional name-aware dispatch in ``T3Database._embedding_fn`` uses
the set to detect local-token names so a local-mode caller against a
voyage-named collection fails loud instead of producing 384-dim vectors
against a 1024-dim space (RDR-059 hazard, inverted)."""

_CT_ALTERNATION = "|".join(_CONTENT_TYPES)
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


def canonical_embedding_model(content_type: str) -> str:
    """Return the RDR-103 canonical embedding model for ``content_type``.

    Single source of truth for the per-content-type model policy:

    - ``code`` to ``voyage-code-3``
    - ``docs`` / ``rdr`` / ``knowledge`` to ``voyage-context-3`` (CCE)

    Raises ``ValueError`` for unknown content types so the caller does
    not silently fall through to a wrong model.
    ``Catalog.collection_for_repo`` uses this; legacy
    :func:`voyage_model_for_collection` continues to dispatch off the
    physical name for read paths.
    """
    if content_type == "code":
        return "voyage-code-3"
    if content_type in ("docs", "rdr", "knowledge"):
        return "voyage-context-3"
    raise ValueError(
        f"canonical_embedding_model: unknown content_type {content_type!r}; "
        f"expected one of {CONTENT_TYPES}"
    )


class CollectionNotRegisteredError(LookupError):
    """A funnel helper (:func:`collection_content_type`, :func:`collection_owner`,
    :func:`collection_model`) was asked to read the catalog row of a name
    that has none (RDR-204 Phase 3, nexus-ft04v.26 THE REPOINT).

    These three helpers stop parsing the collection name and read
    ``nexus.catalog_collections`` instead (the collection-row cache,
    :func:`nexus.mcp_infra.get_collection_row`) -- a name with no row is
    never re-derived by falling back to a string parse (the no-silent-
    fallbacks-for-correctness hot rule): the engine already 422s a direct
    read of an unregistered collection, and a name whose string shape
    disagrees with its row is exactly the drift class RDR-204 exists to
    close (GH #667). Callers doing a bare-corpus FAN-OUT over a live
    collection list (``resolve_corpus``) drop an unregistered name with a
    logged warning instead of raising -- see that function's docstring;
    this exception is for a caller reading ONE named collection's
    attributes directly.
    """


class PlaceholderCollectionError(click.ClickException, ValueError):
    """A write named a placeholder (``default``, ``knowledge``, ``notes``,
    ``tmp``, ``test``) where a subject was required (nexus-0fw11).

    A ``click.ClickException`` so every CLI writer that shares the resolver
    (``nx store put``, ``nx memory promote``, ``nx index pdf/md``, ``nx dt
    index``) prints the message and exits 1 instead of a traceback, without
    each command catching it; the MCP tools ``str()`` it into their
    ``Error:`` reply. Also a ``ValueError`` for library callers."""


class LocalVoyageCredentialMissingError(RuntimeError):
    """``local.embed_model`` is voyage-shaped but no ``voyage_api_key`` is
    configured (nexus-35ok4 / GH #1461).

    Raised at WRITE time from :func:`effective_embedding_model_for_writes`
    (and, deliberately, ONLY from write-classified callers of
    :func:`t3_collection_name` — see its ``for_write`` parameter) rather
    than silently minting a collection name the engine will 422 on first
    write, or — worse — silently falling back to bge and indexing with a
    model the user did not ask for (the no-silent-fallbacks hot rule: a
    local install choosing voyage and getting bge anyway is a correctness
    bug, not a degraded-but-working state).

    MUST NEVER surface from a read path (search / store list / store get
    / store delete): looking at pre-existing data must not require a
    credential the user may not have configured, or may have removed
    since the data was written (code-review-expert CRITICAL, nexus-35ok4
    round 2 — this exception used to fire unconditionally from
    :func:`t3_collection_name`'s promoted-name construction, before any
    ``collection_exists`` check, breaking reads against perfectly
    readable pre-existing bge/minilm collections on any half-configured
    voyage install).
    """


class EmbeddingProfileMismatchError(RuntimeError):
    """The client's own configured intent (``local.embed_model`` /
    ``voyage_api_key``) for a collection's ``content_type`` disagrees
    with the engine's ``nexus.embedding_profile`` row for it (RDR-204
    Phase 3 item 3, nexus-ft04v.26; coordinator design correction
    2026-09-09).

    SAME FAMILY as :class:`LocalVoyageCredentialMissingError` — both are
    "the client's intent cannot be honored, and minting under the wrong
    model silently would be a data-correctness bug" — but this is the
    STALE-PROFILE case, not the missing-credential case: the client has
    everything it needs LOCALLY (mode, key), but the ENGINE's profile
    still reflects an OLDER decision because the service has not been
    restarted since the config changed (the engine reads ``local.embed_model``
    / ``voyage_api_key`` only at spawn — RDR-204 Technical Design 1). The
    canonical repro: ``local.embed_model=voyage-*`` with a key configured,
    but the engine profile still says ``bge-base-en-v15-768`` for this
    content type because the service predates the key being set.

    Raised from :func:`ensure_collection_registered`, immediately before
    its ``writer.register_collection`` call — the REGISTRATION SEAM, not
    the write-model computation chokepoint
    (:func:`effective_embedding_model_for_writes`, which is pure and
    network-free again after the design correction below). This is
    Technical Design 1a's "profile-as-data" honoured at the point a
    catalog client is already in hand and the model is about to be
    committed, an EARLY, more actionable diagnostic layered on top of
    the engine's own register-time 422 on a mismatch (which remains the
    correctness guard for every OTHER registration call site this seam
    does not yet cover — those still get the engine's late refusal, not
    this early one, until nexus-ft04v.27 consolidates them through this
    same funnel).
    """


class CatalogReaderUnavailableError(RuntimeError):
    """:func:`nexus.catalog.factory.make_catalog_reader` returned
    ``None`` instead of a reader (RDR-204 Phase 3, nexus-ft04v.26,
    fixture-seam round 2026-09-09).

    ``make_catalog_reader``'s own docstring calls its ``Optional``
    return type "historical" and callers' None-guards "dead but
    harmless" — true for a real, correctly-configured install (it
    always returns a live handle), but the registration seam
    (:func:`_profile_model_for_content_type`, called from
    :func:`ensure_collection_registered` before every
    ``writer.register_collection``) is reached from EVERY write path,
    including ones a misconfigured storage backend or an incompletely
    faked test double can drive through this branch. Left unguarded,
    a ``None`` reader surfaced as a bare ``AttributeError: 'NoneType'
    object has no attribute 'embedding_profile'`` two frames later —
    a genuine misconfiguration wearing an unrelated exception type,
    exactly the class of bug the no-silent-fallback-for-correctness
    hot rule exists to prevent. Named here so the actual cause (the
    reader factory, not the profile row) is what a caller sees.
    """


#: The restart recipe, verbatim as ``commands/config_cmd.py``'s
#: ``SERVICE_RESTART_COMMAND`` (nexus-ft04v.25) and this module's own
#: pre-existing :class:`LocalVoyageCredentialMissingError` message both
#: already state it. Duplicated as a literal here rather than imported
#: from ``commands.config_cmd`` — corpus.py is core; commands/ is the
#: CLI layer built ON TOP of it, and importing downward would invert
#: that dependency. ``tests/test_config_cmd.py`` pins the command text
#: in commands/config_cmd.py; keep this string byte-identical to it.
_SERVICE_RESTART_COMMAND = "nx daemon service stop && nx daemon service start"


def _write_intent_embedding_model(content_type: str) -> str:
    """The CLIENT's own configured INTENT for the write model of
    *content_type* — what ``local.embed_model``/``voyage_api_key``
    (local mode) or the fixed cloud policy (cloud mode) says the model
    SHOULD be, computed with NO network access.

    Split out of :func:`effective_embedding_model_for_writes` (RDR-204
    Phase 3 item 3, nexus-ft04v.26; coordinator ruling 2026-09-09) so
    that function's new profile-read half can be layered ON TOP of this
    unchanged diagnosis, while :func:`_promoted_model_token_for_read`'s
    read-path delegation keeps using ONLY the intent — a read must stay
    network-free and must never risk :class:`EmbeddingProfileMismatchError`
    just to construct a candidate name to probe (the same contract
    :class:`LocalVoyageCredentialMissingError`'s own docstring already
    states for the credential check below).

    Raises :class:`LocalVoyageCredentialMissingError` when
    ``local.embed_model`` is voyage-shaped but no ``voyage_api_key`` is
    configured — unchanged from this function's pre-nexus-ft04v.26 body,
    including the zero-network-access property nexus-o5x2c's regression
    suite (``tests/test_o5x2c_write_chokepoint_repros.py``) pins.
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — circular-dep avoidance (config)
    if is_local_mode():
        from nexus.config import local_embed_model_is_voyage  # noqa: PLC0415 — circular-dep avoidance (config)
        if local_embed_model_is_voyage():
            from nexus.config import get_credential, local_embed_model_choice  # noqa: PLC0415 — circular-dep avoidance (config)
            if not get_credential("voyage_api_key"):
                raise LocalVoyageCredentialMissingError(
                    f"local.embed_model={local_embed_model_choice()!r} requires a "
                    "Voyage API key, but none is configured. Set one with "
                    "`nx config set voyage_api_key <key>` (or export "
                    "VOYAGE_API_KEY), then restart the local service so the "
                    f"engine re-reads it: `{_SERVICE_RESTART_COMMAND}`."
                )
            return canonical_embedding_model(content_type)
        # nexus-xq8f9: in service-vector mode (the 6.0 default) the nexus-service
        # embeds server-side with bge-768 (RDR-160), independent of whether the
        # CLIENT has the [local]/fastembed extra. Naming the collection from the
        # client's local-EF tier (which falls back to minilm-384 when fastembed
        # is absent) makes the service refuse the write (HTTP 422, model
        # mismatch). Follow the service's embedder token instead.
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (db.http_vector_client)
        if is_vector_service_mode():
            from nexus.db.local_ef import _MODEL_TOKENS, _TIER1_MODEL  # noqa: PLC0415 — circular-dep avoidance (db.local_ef)
            return _MODEL_TOKENS[_TIER1_MODEL]  # bge-base-en-v15-768
        from nexus.db.local_ef import local_model_token  # noqa: PLC0415 — circular-dep avoidance (db.local_ef)
        return local_model_token()
    return canonical_embedding_model(content_type)


def _profile_model_for_content_type(content_type: str) -> "str | None":
    """The engine's ``nexus.embedding_profile`` row for *content_type*,
    or ``None`` when the tenant has no row for it — an unprofiled
    tenant, or a profile with rows for OTHER content types but not this
    one, treated identically per RDR-204's own text: "an unprofiled
    tenant gets [] and NOT a default".

    :class:`~nexus.catalog.http_catalog_client.EmbeddingProfileRouteMissingError`
    propagates UNCAUGHT (a pre-Phase-2 engine) — deliberate, the
    no-silent-fallback-for-a-data-correctness-problem hot rule.
    No caching: mirrors :meth:`HttpCatalogClient.embedding_profile`'s own
    "no second cache, the profile is small and per-tenant" contract
    (bead nexus-ft04v.33) — this helper adds no memo of its own either.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle (catalog)
    reader = make_catalog_reader()
    if reader is None:
        raise CatalogReaderUnavailableError(
            "make_catalog_reader() returned None -- the registration seam "
            "cannot read the engine's embedding profile without one. This "
            "is a storage-backend misconfiguration (or an incompletely "
            "faked test double), not a missing profile row; see "
            "CatalogReaderUnavailableError's docstring."
        )
    for row in reader.embedding_profile():
        if row.get("content_type") == content_type:
            return row.get("embedding_model")
    return None


def effective_embedding_model_for_writes(content_type: str) -> str:
    """Return the embedding-model token to write into NEW collection
    names and per-chunk metadata for ``content_type``.

    RDR-109 Phase 2. Pure, network-free local computation — see
    :func:`_write_intent_embedding_model`, which now holds this
    function's entire original body verbatim; this name is kept as a
    thin delegation for every existing caller and test.

    RDR-204 Phase 3 item 3 (nexus-ft04v.26) history: a first pass
    (commit 5935b1bf8) put a real ``nexus.embedding_profile`` read
    INSIDE this function. Coordinator design correction (2026-09-09,
    after a full-suite run measured 155 failures): this chokepoint is
    reached from every write path with only the db/T3 layer mocked, so
    a network call here is wrong, not under-fixtured — a conftest-level
    stub to paper over it would itself be the silent fallback the RDR
    forbids. The profile comparison moved to the REGISTRATION SEAM
    instead (:func:`ensure_collection_registered`, immediately before
    its ``writer.register_collection`` call, where a catalog client is
    already in hand and the model is about to be committed) — see that
    function's docstring for the outcome table. This function reverted
    to pure local computation, restoring outcome 1
    (:class:`LocalVoyageCredentialMissingError` on a voyage-shaped
    ``local.embed_model`` with no key) with zero network access before
    it raises, exactly as before 5935b1bf8 (nexus-o5x2c's regression
    pins stay green unmocked).

    Raises :class:`LocalVoyageCredentialMissingError` when
    ``local.embed_model`` is voyage-shaped but no ``voyage_api_key`` is
    configured. THIS FUNCTION IS UNCONDITIONALLY WRITE-SHAPED — every
    caller MUST already know it is about to mint/require a real,
    about-to-be-written collection identity; it is not safe to call from
    a read path. :func:`t3_collection_name` (the read/write-shared
    resolver) does NOT call this function for read-classified requests —
    see its ``for_write`` parameter and ``_promoted_model_token_for_read``.

    Read paths must continue to dispatch off the physical collection
    name via :func:`voyage_model_for_collection` /
    :func:`embedding_model_for_collection_name`; this function is for
    WRITE-side decisions only.
    """
    return _write_intent_embedding_model(content_type)


def _promoted_model_token_for_read(content_type: str) -> str:
    """The read-path counterpart of :func:`effective_embedding_model_for_writes`.

    Computing a CANDIDATE collection name to probe with
    ``collection_exists()`` is not the same as committing to write under
    it — a read must never need a Voyage credential, nor a network call,
    just to construct a string to check for existence (code-review-expert
    CRITICAL, nexus-35ok4 round 2; the network-free half restated by
    nexus-ft04v.26, RDR-204 Phase 3 item 3, when
    ``effective_embedding_model_for_writes`` grew a real profile read).
    When ``local.embed_model`` is voyage-shaped this returns
    :func:`canonical_embedding_model` directly, BYPASSING the credential
    gate entirely — deliberately, regardless of whether
    ``voyage_api_key`` is currently configured, so a pre-existing
    voyage-named collection (created back when the key WAS present) is
    still a probeable candidate on a keyless read. When
    ``local.embed_model`` is not voyage-shaped this delegates to
    :func:`_write_intent_embedding_model` — the LOCAL-INTENT half only,
    never :func:`effective_embedding_model_for_writes` itself, which
    would now also validate against the engine's profile over the
    network and could raise :class:`EmbeddingProfileMismatchError` /
    :class:`EmbeddingProfileEmptyError` for what must stay a pure,
    offline candidate-name construction.
    """
    from nexus.config import is_local_mode, local_embed_model_is_voyage  # noqa: PLC0415 — circular-dep avoidance (config)
    if is_local_mode() and local_embed_model_is_voyage():
        return canonical_embedding_model(content_type)
    return _write_intent_embedding_model(content_type)


def resolve_read_embedding_model(content_type: str) -> str:
    """Public wrapper around :func:`_promoted_model_token_for_read` for
    callers OUTSIDE this module that need a credential-free CANDIDATE
    name to probe for existence — never to commit a write under.

    nexus-o5x2c (nexus-35ok4 round 4): ``indexer.py``'s
    ``_migration_source_candidates`` builds a list of names to CHECK
    whether legacy/pre-migration data already lives there — a read/probe
    shape, not a write mint — so it uses this, not
    :func:`resolve_write_embedding_model`. Symmetric public counterpart
    to that function: reads go through here, writes go through there,
    and neither reaches into this module's underscore-prefixed internals
    from another module.
    """
    return _promoted_model_token_for_read(content_type)


def _resolve_promoted_model_token(content_type: str, *, for_write: bool) -> str:
    """Dispatch to the write-shaped or read-shaped model resolver.

    Single chokepoint inside :func:`t3_collection_name` so its two
    internal call sites (the ambiguous-bare-prefix picker and the main
    promoted-name builder) cannot independently drift on which resolver
    they use. ``for_write=True`` is the ONLY path that can raise
    :class:`LocalVoyageCredentialMissingError`.
    """
    if for_write:
        return effective_embedding_model_for_writes(content_type)
    return _promoted_model_token_for_read(content_type)


def _probe_local_token_collections(
    collection_exists: Callable[[str], bool],
) -> str | None:
    """Iterate :data:`LOCAL_EMBEDDING_MODELS` (bounded, 2 entries),
    calling *collection_exists* with each token; return the first token
    it accepts, or ``None`` if none match.

    The ONE shared iteration primitive for the local-token grandfather
    probe — used by both :func:`resolve_write_embedding_model` (below)
    and :func:`t3_collection_name`'s own read-path probe, so the bounded
    token set, iteration order, and per-candidate exception handling are
    never independently re-implemented (nexus-o5x2c).
    """
    for local_token in sorted(LOCAL_EMBEDDING_MODELS):
        try:
            if collection_exists(local_token):
                return local_token
        except Exception as exc:  # noqa: BLE001 — best-effort probe; one broken candidate must not block the others or the caller's fallback
            # nexus-o5x2c (code-review-expert Important): loud at debug
            # level so an operator can tell "the probe substrate is
            # unreachable" apart from "genuinely nothing to grandfather
            # onto" when resolve_write_embedding_model raises next —
            # both look identical from the caller's exception alone.
            _log.debug(
                "local_token_collection_probe_failed",
                local_token=local_token,
                error=str(exc),
                # nexus-z0idx follow-on: "detail", not "message" — stdlib
                # logging reserves "message" on LogRecord.
                detail=(
                    "grandfather probe raised for this candidate token; "
                    "treated as no-match and the next candidate (or the "
                    "strict resolver) was tried instead."
                ),
            )
            continue
    return None


def resolve_write_embedding_model(
    content_type: str,
    *,
    collection_exists: Callable[[str], bool] | None = None,
) -> str:
    """THE single chokepoint every write-path caller uses to resolve the
    embedding-model token for a collection it is about to write into.

    nexus-o5x2c (nexus-35ok4 round 4, substantive-critic SHIP-BLOCKER):
    the grandfather-or-raise truth table (round 2/3: local mode +
    local.embed_model voyage-shaped + no key configured + a pre-existing
    bge/minilm collection already exists -> the write grandfathers onto
    it instead of raising) previously lived ONLY inside
    :func:`t3_collection_name`'s internals. Six other call sites build
    their OWN collection name and called
    :func:`effective_embedding_model_for_writes` DIRECTLY, with no
    grandfathering at all — catalog registration
    (``catalog/http_catalog_client.py:collection_for_repo``, the hot path
    for ``nx index repo`` on an already-registered repo),
    :func:`docs_leaf_fallback_collection_name` below (``nx index
    md``/``pdf`` without ``--collection``), ``indexer.py``'s ad-hoc
    fallbacks, ``repo_identity.py``'s synthesis fallback, and
    ``commands/dt.py``'s DEVONthink import. Each of those crashed
    (``LocalVoyageCredentialMissingError``, an uncaught ``RuntimeError``)
    on a keyless voyage-configured local install instead of grandfathering
    onto the caller's existing bge/minilm collection — live-repro'd for
    both ``nx index repo`` and ``nx index md``.

    This function is that single chokepoint. :func:`t3_collection_name`'s
    OWN write path (``for_write=True``) calls THIS function too (not a
    re-implementation — see its internals), so the grandfather-or-raise
    DECISION exists in exactly one place; only the read-path probe (which
    has a genuinely different truth-table row: reads always probe,
    regardless of key state) has its own call, sharing the bounded
    iteration primitive :func:`_probe_local_token_collections` rather
    than the decision logic.

    ``collection_exists`` lets EACH caller supply its OWN way to answer
    "does a collection already exist for THIS content_type+owner under
    local model token X" — called with each of
    :data:`LOCAL_EMBEDDING_MODELS` in turn (T3-backed callers close over
    ``t3.collection_exists`` against their own built name; the
    catalog-tier caller closes over its own tuple-registration lookup,
    since it has no T3 vector client at hand). ``None`` means no probe is
    available — matches :func:`t3_collection_name`'s historical
    ``t3=None`` "stay pure, always strict" contract.

    Truth table (mode = local, local.embed_model voyage-shaped; every
    other mode/config is unaffected and delegates straight through to
    :func:`effective_embedding_model_for_writes`, unchanged — see
    docs/cli-reference.md "Local mode with Voyage" for the full table,
    including the read-path row this write-only function does not own):

    ==========================  ==============================================
    key / probe state           Result
    ==========================  ==============================================
    key ABSENT, probe finds a   that local token (grandfather onto the
    local-token collection      existing collection)
    key ABSENT, nothing found   raises :class:`LocalVoyageCredentialMissingError`
    / no probe supplied         (genuine new mint, misconfigured)
    key PRESENT (any probe      the canonical voyage token (new sibling — the
    state)                      engine is voyage-only once restarted, so
                                 grandfathering onto bge would silently strand
                                 the write with no restart-remedy sentinel;
                                 see nexus-ddmfg)
    ==========================  ==============================================
    """
    from nexus.config import get_credential, is_local_mode, local_embed_model_is_voyage  # noqa: PLC0415 — circular-dep avoidance (config)
    if is_local_mode() and local_embed_model_is_voyage():
        key_present = bool(get_credential("voyage_api_key"))
        if not key_present and collection_exists is not None:
            found = _probe_local_token_collections(collection_exists)
            if found is not None:
                # nexus-o5x2c (code-review-expert Important): the ONE
                # place every grandfather actually taken is logged,
                # regardless of which of the 7 call sites triggered it —
                # names the content_type and the local token grandfathered
                # onto so an operator sees WHY a write landed in an
                # existing bge/minilm collection instead of minting voyage.
                _log.debug(
                    "resolve_write_embedding_model_grandfathered",
                    content_type=content_type,
                    grandfathered_token=found,
                    detail=(
                        "local.embed_model is voyage-shaped with no "
                        "voyage_api_key configured; a pre-existing local "
                        "collection was found for this content_type, so "
                        "the write is grandfathered onto its model "
                        "instead of raising."
                    ),
                )
                return found
    return effective_embedding_model_for_writes(content_type)


def docs_leaf_fallback_collection_name(
    corpus: str, *, collection_exists: Callable[[str], bool] | None = None,
) -> str:
    """Return the conformant ``docs__<corpus>__<model>__v1`` collection
    name for the RDR-103 Phase 5 leaf fallback: an ad-hoc/dry-run/
    diagnostic call site that has no ``collection_name``/``--collection``
    to work with and must reconstruct the name the production write path
    would have used.

    Single source of truth for this derivation. Prior to this helper the
    formula (``corpus.replace("_", "-")`` folded into
    ``f"docs__{owner_segment}__{effective_embedding_model_for_writes('docs')}__v1"``)
    was hand-duplicated at five call sites (three in ``doc_indexer.py``,
    two in ``commands/index.py``) — including ``index.py``'s
    ``_index_run_refused_message`` diagnostic, which derives this name
    specifically to compare it against the catalog's stamped
    ``physical_collection`` and name a genuine mismatch (nexus-2t63u). A
    silent drift between copies would reintroduce that defect class in
    the diagnostic path (a wrong derived "expected" name falsely accusing
    a healthy document of a stale-collection mismatch) while the
    production write path stayed correct — asymmetric enough to go
    unnoticed. ``content_type`` is hardcoded to ``'docs'``: this fallback
    only ever fires from docs/PDF/markdown indexing paths.

    The owner segment is the corpus tag with underscores rewritten to
    hyphens (``_`` is the conformant grammar's segment separator).

    ``collection_exists`` (nexus-o5x2c, nexus-35ok4 round 4
    SHIP-BLOCKER): optional grandfather probe forwarded to
    :func:`resolve_write_embedding_model`. ``None`` (the default)
    preserves the historical strict/pure behavior — REQUIRED for the
    ``_index_run_refused_message`` diagnostic comparison above, which
    must compute the strict "expected" name regardless of what already
    exists, or a real mismatch would be masked by grandfathering. The
    two production write-target callers (``doc_indexer.py``'s
    ``collection_name is None`` fallbacks) pass a real probe so ``nx
    index md``/``pdf`` without ``--collection`` grandfathers onto a
    pre-existing bge/minilm collection exactly like ``nx store put``,
    instead of crashing on a keyless voyage-configured local install
    (the live-repro'd bug this parameter fixes).
    """
    owner_segment = corpus.replace("_", "-")
    model = resolve_write_embedding_model(
        "docs",
        collection_exists=(
            None if collection_exists is None
            else lambda token: collection_exists(f"docs__{owner_segment}__{token}__v1")
        ),
    )
    return f"docs__{owner_segment}__{model}__v1"


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
#: ``partition("__")``/``"__" in`` specifically so this candidate-string
#: primitive is INVISIBLE to ``tests/test_collection_name_parse_census.py``'s
#: AST scan (which watches split/rsplit/partition/rpartition/startswith/
#: endswith calls and ``"__" in``/``not in`` compares -- a regex ``.match()``
#: is none of those, exactly like ``is_conformant_collection_name`` and
#: :func:`model_version_for_collection_name` already dodge the same scan).
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
    scan (mcp/core.py was funnelled to zero by nexus-ft04v.21); this
    regex-based primitive stays invisible to that scan.

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


def collection_content_type(name: str) -> str:
    """RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26): the content-type
    column of collection *name*'s catalog row.

    No longer parses. Reads :func:`nexus.mcp_infra.get_collection_row`
    (the SAME collection-list round trip already fetched and cached for
    collection counts -- a field read on a call already made, never a new
    hot-path query). Raises :class:`CollectionNotRegisteredError` when
    *name* has no row -- the engine 422s a direct read of an unregistered
    collection anyway, and silently falling back to parsing the name is
    exactly the two-sources-of-truth bug (GH #667) RDR-204 exists to
    close. Callers deriving a CANDIDATE name to mint (not yet a real
    collection) must not call this -- see :func:`split_candidate_collection_name`.
    """
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)
    row = get_collection_row(name)
    if row is None:
        raise CollectionNotRegisteredError(
            f"collection_content_type: {name!r} has no catalog row -- it is "
            "either unregistered or owns no live chunks. This function "
            "reads the catalog row, never the name; register the "
            "collection (or use a candidate-name parser) before calling it."
        )
    return row["content_type"]


def collection_owner(name: str) -> str:
    """RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26): the owner_id column
    of collection *name*'s catalog row. See :func:`collection_content_type`'s
    docstring for the row-cache and fail-loud contract shared by all three
    funnel helpers.
    """
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)
    row = get_collection_row(name)
    if row is None:
        raise CollectionNotRegisteredError(
            f"collection_owner: {name!r} has no catalog row -- it is either "
            "unregistered or owns no live chunks. This function reads the "
            "catalog row, never the name; register the collection (or use "
            "a candidate-name parser) before calling it."
        )
    return row["owner_id"]


def collection_model(name: str) -> str:
    """RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26): the embedding_model
    column of collection *name*'s catalog row. See
    :func:`collection_content_type`'s docstring for the row-cache and
    fail-loud contract shared by all three funnel helpers.
    """
    from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)
    row = get_collection_row(name)
    if row is None:
        raise CollectionNotRegisteredError(
            f"collection_model: {name!r} has no catalog row -- it is either "
            "unregistered or owns no live chunks. This function reads the "
            "catalog row, never the name; register the collection (or use "
            "a candidate-name parser) before calling it."
        )
    return row["embedding_model"]


def voyage_model_for_collection(collection_name: str) -> str:
    """Return the Voyage AI model for a T3 collection (index and query).

    The same model MUST be used at both index and query time —
    mismatched models yield random noise (RDR-059).

    docs__/knowledge__/rdr__ → voyage-context-3 (CCE)
    code__ and all others    → voyage-code-3

    In local mode, callers bypass this and use ``LocalEmbeddingFunction``.

    RDR-204 Phase 3 (nexus-ft04v.26): deliberately NOT the row-based
    collection_content_type. This function's own contract is a NAME-PREFIX
    dispatch table (its docstring above states it in exactly those terms),
    called broadly on collections that may be live with data but never
    registered (a legacy pre-Phase-1 collection, or a fresh test/CLI
    fixture) -- query/index model dispatch must not crash on that.
    split_candidate_collection_name, the shared candidate-string primitive.
    """
    if split_candidate_collection_name(collection_name)[0] in ("docs", "knowledge", "rdr"):
        return "voyage-context-3"
    return "voyage-code-3"


def default_projection_threshold(collection_name: str) -> float:
    """Return the default projection cosine threshold for *collection_name*.

    RDR-077 Phase 4a: per-corpus-type defaults calibrated for the rawness
    of embedding cosine distributions in each corpus type. Explicit
    ``--threshold`` on ``nx taxonomy project`` overrides this; the table
    only kicks in when no explicit value is supplied.

    =================  ======  ==============================================
    Prefix             Value   Rationale
    =================  ======  ==============================================
    ``code__*``        0.70    Syntax inflates raw cosine; high bar
    ``knowledge__*``   0.50    Dense prose, semantically rich
    ``docs__*``        0.55    Mixed prose + code
    ``rdr__*``         0.55    Same as docs
    =================  ======  ==============================================

    Unknown prefixes fall back to 0.70 (safer under-match bias).
    See ``docs/exploration/taxonomy-projection-tuning.md`` for calibration methodology.

    RDR-204 Phase 3 (nexus-ft04v.26): candidate-string derivation, not the
    row-based collection_content_type -- see voyage_model_for_collection's
    docstring for why (this default calibration table is called on the
    same broad, possibly-unregistered collection population).
    """
    content_type = split_candidate_collection_name(collection_name)[0]
    if content_type == "knowledge":
        return 0.50
    if content_type in ("docs", "rdr"):
        return 0.55
    return 0.70


def embedding_model_for_collection(collection_name: str) -> str:
    """Return the embedding model for *collection_name*.

    Fix 4 (nexus-6e6u1 / local-daemon-client-embed): conformant 4-segment
    names (``<ct>__<owner>__<model>__v<n>``) carry the model token directly
    in the name -- return that token instead of guessing from the prefix.
    Legacy 2-segment names fall back to the voyage inference.

    This ensures ``collection_list`` labels bge/minilm collections correctly
    instead of displaying ``voyage-code-3`` / ``voyage-context-3``.
    """
    parsed = embedding_model_for_collection_name(collection_name)
    if parsed is not None:
        return parsed
    return voyage_model_for_collection(collection_name)


# index_model_for_collection is semantically the same (same model for index + query).
index_model_for_collection = embedding_model_for_collection


def _legacy_content_type_for_collection(collection_name: str) -> str:
    """Map a legacy (non-conformant) collection name to its RDR-103
    content type, via the SAME prefix convention
    :func:`voyage_model_for_collection` uses: ``docs__``/``knowledge__``/
    ``rdr__`` map to their own type; everything else (including
    ``code__``) defaults to ``"code"``.
    """
    # RDR-204 Phase 3 (nexus-ft04v.26): candidate-string derivation, not
    # the row-based collection_content_type -- same reason as
    # voyage_model_for_collection above.
    #
    # NOT `split_candidate_collection_name(...)[0] or "code"`: that only
    # substitutes on an EMPTY (no-"__") result, but this function's
    # historical contract defaults to "code" for ANY unrecognized prefix
    # too (e.g. "other__x" -> "code"), not just a dunder-free name -- and
    # split_candidate_collection_name deliberately returns an unrecognized
    # raw prefix UNFILTERED, so it must be filtered here explicitly.
    content_type = split_candidate_collection_name(collection_name)[0]
    return content_type if content_type in ("docs", "knowledge", "rdr") else "code"


def embedding_model_for_collection_calibrated(collection_name: str) -> str:
    """Resolve the embedding model for *collection_name* for CROSS-MODEL
    CALIBRATION purposes (nexus-mc1l1) -- distinct from
    :func:`embedding_model_for_collection`, which callers needing the
    collection's REAL, historically-written model (query dispatch,
    ``nx collection list`` labeling) must keep using unchanged.

    Conformant 4-segment names carry their model token directly in the
    name, so the parse is delegated exactly as
    :func:`embedding_model_for_collection` does -- unaffected by this
    function's existence.

    LEGACY 2-segment names are the nexus-mc1l1 defect:
    :func:`embedding_model_for_collection`'s fallback
    (:func:`voyage_model_for_collection`) infers the model purely from
    the collection-name PREFIX and always guesses cloud/Voyage,
    regardless of what the install actually embeds with. In a
    local-mode install every collection shares ONE embedder regardless
    of content_type, so that guess reproduces the full nexus-tox2m bug
    for any pre-RDR-103 (2026-05-03) install with grandfathered legacy
    collections: two results at an identical raw distance, one
    ``code__`` one ``knowledge__``, scored 1.0 vs 0.0 purely from the
    collection name.

    For a legacy name this instead asks what the install ACTUALLY
    embeds that content type with RIGHT NOW --
    :func:`resolve_read_embedding_model`, the same credential-free
    read-path resolver ``t3_collection_name`` uses to probe candidate
    names, so this reuses the one already-correct install-mode-aware
    resolver rather than re-deriving the truth table. In local mode
    (not opted into Voyage) that resolver ignores content_type entirely
    and returns the single active local token for every call, so every
    legacy name collapses onto the SAME resolved model and calibration
    is a genuine no-op -- satisfying "no model can be determined for a
    legacy name -> treat all legacy names as one model" without a
    special-cased sentinel, because the single local embedder IS that
    one model. In cloud mode (or local mode explicitly opted into
    Voyage, nexus-35ok4), it returns the same per-content-type
    voyage-code-3/voyage-context-3 split
    :func:`voyage_model_for_collection` already produced, so that
    (correct) split is unaffected.
    """
    parsed = embedding_model_for_collection_name(collection_name)
    if parsed is not None:
        return parsed
    content_type = _legacy_content_type_for_collection(collection_name)
    return resolve_read_embedding_model(content_type)


def _refuse_placeholder_subject(user_arg: str) -> None:
    """Raise :class:`PlaceholderCollectionError` when the subject segment of
    a bare or two-segment name is a placeholder (nexus-0fw11). Only write
    resolution calls this; the four-segment conformant form never reaches it.

    RDR-204 Phase 3 repoint (nexus-ft04v.26): *user_arg* here is a
    CANDIDATE string being resolved into a name to mint, not yet (and
    possibly never) a registered collection -- uses the string-shape
    primitive, not the row-based :func:`collection_owner`.
    """
    _, rest = split_candidate_collection_name(user_arg)
    if rest in PLACEHOLDER_SUBJECTS:
        raise PlaceholderCollectionError(
            f"collection {user_arg!r} names a placeholder, not a subject: a knowledge "
            "collection is a subject area a reader would browse (distributed-systems, "
            "vector-search), never default/knowledge/notes/tmp/test. Name the subject, "
            "reusing an existing one from `nx collection list` where it fits; see "
            "docs/collections.md Rule 1."
        )


def t3_collection_name(
    user_arg: str, *, t3: object | None = None, for_write: bool = False,
    allow_placeholder: bool = False,
) -> str:
    """Resolve a --collection argument to a T3 collection name.

    Inputs land in one of three shapes:

    - ``foo`` (no underscores) becomes
      ``knowledge__foo__voyage-context-3__v1``.
    - ``knowledge__foo`` (legacy 2-segment) is auto-promoted to
      ``knowledge__foo__voyage-context-3__v1``.
    - ``knowledge__foo__voyage-context-3__v1`` (already 4-segment
      conformant) passes through untouched.

    Auto-promotion satisfies ``T3Database``'s strict-naming guard
    (RDR-103 Phase 5) while preserving the operator habit of typing
    short ``--collection`` arguments.

    nexus-hmxi: when *t3* is supplied, the resolver checks for an
    existing T3 collection at the user-typed name BEFORE returning
    the auto-promoted target. If the legacy 2-segment collection
    exists in T3 and the conformant target does not, the legacy name
    is returned so the operator continues to read and write the same
    collection across all CLI tools (``nx store list``, ``nx store
    put``, ``nx search``). Without *t3*, the function stays pure and
    always auto-promotes (used by static contexts and tests). The
    transparent grandfathering matches RDR-103's stated read-side
    policy ("pre-existing legacy collections remain readable") and
    extends it to operator-typed write inputs so a put + list
    round-trip cannot land in two different collections.

    ``for_write`` (nexus-35ok4 / GH #1461 round 2, code-review-expert
    CRITICAL): callers that are about to WRITE new content under the
    returned name — ``nx store put``, the MCP ``store_put`` tool, ``nx
    memory promote``, the indexers — MUST pass ``for_write=True``. All
    other callers (search/query corpus resolution, ``store_get``,
    ``store_list``, ``store_delete``, ``store_get_many``, and their CLI
    equivalents) leave it at the default ``False``.

    This flag governs ONLY whether :class:`LocalVoyageCredentialMissingError`
    is allowed to propagate. With ``for_write=False`` the resolver NEVER
    raises: candidate names are built via the read-shaped, credential-free
    resolver (:func:`_resolve_promoted_model_token` with
    ``for_write=False``), so LOOKING AT pre-existing data never needs a
    Voyage key — a keyless local install with ``local.embed_model``
    voyage-shaped still finds and reads a pre-existing bge/minilm-named
    collection for the same corpus (probed as an extra candidate below).
    With ``for_write=True``, if no pre-existing collection is found to
    grandfather onto (this IS a brand-new mint), the identity is
    recomputed strictly via :func:`effective_embedding_model_for_writes`,
    which raises loud when ``local.embed_model`` is voyage-shaped and no
    key is configured — never silently falls back to bge.
    """
    if is_conformant_collection_name(user_arg):
        return user_arg

    # nexus-0fw11: a write that names a placeholder subject is refused here,
    # the one resolver every writer uses. ``allow_placeholder`` is for a
    # writer restoring a collection that already exists under that name
    # (the recovery-bundle import), never for a new mint.
    if for_write and not allow_placeholder:
        _refuse_placeholder_subject(user_arg)

    # GH #545: when the user typed a BARE content-type prefix
    # (``"code"``, ``"docs"``, ``"rdr"``, ``"knowledge"``) AND no
    # ``__`` is present, the historical else-branch treated the value
    # as an owner-name under content_type=``knowledge`` -- so
    # ``--collection code`` resolved to
    # ``knowledge__code__voyage-context-3__v1``, the wrong namespace.
    # The 4.26.2 fix (#536) only covered the special case where the
    # legacy 2-segment ``knowledge__knowledge`` happened to exist; for
    # ``code``/``docs``/``rdr`` there's no ``<x>__<x>`` convention, so
    # the bug stayed silent on those prefixes. Resolve via live-T3
    # probe instead: if exactly one ``{prefix}__*`` collection exists,
    # use it; on no/multiple matches fall through to the existing
    # owner-segment-promotion branch (which then still has the
    # ``knowledge__knowledge`` legacy fallback from #536).
    # RDR-204 Phase 3 repoint (nexus-ft04v.26): user_arg is a CANDIDATE
    # argument being resolved, not yet a registered collection -- the
    # string-shape primitive, not the row-based collection_owner.
    if (
        t3 is not None
        and split_candidate_collection_name(user_arg)[1] == user_arg
        and user_arg in CONTENT_TYPES
    ):
        try:
            matches = [
                c["name"]
                for c in t3.list_collections()  # type: ignore[attr-defined]
                if c["name"].startswith(f"{user_arg}__")
            ]
        except Exception:  # noqa: BLE001 — best-effort collection-listing probe; empty match list on any backend failure
            matches = []
        if len(matches) == 1:
            return matches[0]
        # nexus-0f3h: GH #545 follow-up. The original 4.26.3 fix only
        # handled the unique-match case. On installs with MANY
        # ``{prefix}__*`` collections (e.g. ``code`` matching 22 repos),
        # falling through to the promotion branch produced
        # ``knowledge__code__voyage-context-3__v1`` -- the wrong
        # namespace, silently.
        #
        # Multi-match pick is content-type-specific. For ``knowledge``,
        # falling through is SAFE because the promotion branch produces
        # the correct ``knowledge__knowledge__...`` namespace plus the
        # ``knowledge__knowledge`` legacy fallback from #536 at the
        # bottom of the function. The historical behaviour the test
        # suite locks (``store_put(collection="knowledge")`` resolves
        # to ``knowledge__knowledge``) lives in that fallthrough path.
        #
        # For ``code``/``docs``/``rdr``, falling through is the bug:
        # the promotion produces ``knowledge__<x>__...``, the wrong
        # namespace. Pick deterministically among the matches:
        # prefer ``{prefix}__{prefix}__<canonical_model>__v1`` (the
        # canonical default), then ``{prefix}__{prefix}`` (the legacy
        # 2-seg default), then alphabetical first. Log a warning so
        # the operator sees the choice and can pass a more specific
        # name on subsequent calls.
        if len(matches) > 1 and user_arg != "knowledge":
            # nexus-35ok4: this is picking among ALREADY-EXISTING live
            # matches, never minting anything new — read-shaped
            # resolution regardless of the caller's for_write, so this
            # picker can never raise on a misconfigured voyage key.
            preferred_4seg = (
                f"{user_arg}__{user_arg}__"
                f"{_resolve_promoted_model_token(user_arg, for_write=False)}__v1"
            )
            preferred_2seg = f"{user_arg}__{user_arg}"
            picked: str | None = None
            if preferred_4seg in matches:
                picked = preferred_4seg
            elif preferred_2seg in matches:
                picked = preferred_2seg
            else:
                picked = sorted(matches)[0]
            _log.warning(
                "t3_collection_name_bare_prefix_ambiguous",
                user_arg=user_arg,
                match_count=len(matches),
                picked=picked,
                candidates=matches[:10],
            )
            return picked
        # zero matches OR bare ``knowledge``: fall through to the
        # promotion branch. Greenfield installs still get the
        # conformant target; ``knowledge`` keeps its
        # ``knowledge__knowledge`` legacy bridge at the bottom of
        # the function.

    # RDR-204 Phase 3 repoint (nexus-ft04v.26): user_arg is a CANDIDATE
    # string being resolved into a name to mint, not a lookup against an
    # existing collection's row -- the string-shape primitive
    # (split_candidate_collection_name), not collection_content_type/
    # collection_owner. Its ("", user_arg) result for a no-"__" name is
    # exactly the historical "knowledge" bare-name default's trigger --
    # but that "no separator" case must stay distinct from a "__"-having
    # user_arg whose first segment happens to be empty (ct == ""), since
    # the membership check right below treats "" and "knowledge"
    # differently. So the bare-name case is branched explicitly rather
    # than folded into a single `split_candidate_collection_name(...)[0] or
    # "knowledge"` expression.
    _ct_probe, _owner_probe = split_candidate_collection_name(user_arg)
    if _owner_probe == user_arg:
        ct, rest = "knowledge", user_arg
    else:
        ct, rest = _ct_probe, _owner_probe

    if ct not in CONTENT_TYPES:
        return user_arg

    owner_segment = rest.replace("_", "-")
    # nexus-35ok4 CRITICAL fix (code-review-expert round 2): build the
    # CANDIDATE name for existence-probing via the read-shaped resolver,
    # which never requires a Voyage credential — computing a string to
    # check ``collection_exists()`` against is not the same as committing
    # to write under it. The strict, potentially-raising resolver
    # (:func:`effective_embedding_model_for_writes`) is only invoked
    # below, and only when this IS a write with nothing pre-existing to
    # grandfather onto.
    promoted = f"{ct}__{owner_segment}__{_resolve_promoted_model_token(ct, for_write=False)}__v1"

    if t3 is None:
        if for_write:
            # Pure write-shaped call with no t3 to probe against (e.g.
            # the indexers) — no legacy collection could possibly be
            # grandfathered onto without a live probe, so the identity
            # must be the STRICT one: raises loud if local.embed_model
            # is voyage-shaped with no key configured.
            return f"{ct}__{owner_segment}__{effective_embedding_model_for_writes(ct)}__v1"
        return promoted
    if user_arg == promoted:
        return promoted
    try:
        if not t3.collection_exists(promoted):  # type: ignore[attr-defined]
            # nexus-9n485 observability: collection_exists() alone can't
            # tell "promoted never had any chunks" from "every chunk under
            # promoted belongs to a trashed document" (HttpVectorClient
            # reads the tombstone-filtered stats view). This resolver
            # already WANTS live semantics here — falling through to a
            # candidate with actual queryable content is correct, not a
            # bug — but the fallthrough used to be silent. Name the
            # skipped candidate so an operator can tell why.
            from nexus.db.collection_state import CollectionState, probe_collection_state  # noqa: PLC0415 — deferred to avoid a module-load-time import cycle (nexus.db.collection_state)

            if probe_collection_state(t3, promoted) is CollectionState.TOMBSTONED:
                _log.debug(
                    "t3_collection_name_promoted_candidate_tombstoned",
                    promoted=promoted, user_arg=user_arg,
                    detail=(
                        "promoted target has physical chunk rows but every "
                        "one belongs to a trashed document; falling through "
                        "to the next candidate rather than treating it as "
                        "queryable."
                    ),
                )
            if t3.collection_exists(user_arg):  # type: ignore[attr-defined]
                return user_arg
            # Bare-prefix legacy fallback (#535 / nexus-6mr0): when the
            # operator typed only the content_type (``"knowledge"``)
            # and the conformant target is absent, bridge to the
            # documented 2-segment legacy shape ``f"{ct}__{owner_segment}"``
            # if it exists. Without this, the bare-prefix shorthand on
            # installs with pre-RDR-103 collections (e.g.
            # ``knowledge__knowledge``) reads from a missing conformant
            # name and operators see "No entries" while the data is
            # right there. Symmetric with the nexus-hmxi grandfathering
            # design intent ("pre-existing legacy collections remain
            # readable") extended to the shorthand form.
            legacy_two_segment = f"{ct}__{owner_segment}"
            if (
                legacy_two_segment != user_arg
                and t3.collection_exists(legacy_two_segment)  # type: ignore[attr-defined]
            ):
                return legacy_two_segment
            # nexus-35ok4 (GH #1461 round 2, gated round 3, delegated
            # round 4 / nexus-o5x2c): local.embed_model may have MOVED to
            # voyage-* since this corpus was last indexed under a local
            # bge/minilm token — probe the other known local tokens too,
            # so a read finds a pre-existing local-model collection
            # regardless of what local.embed_model CURRENTLY says.
            # Bounded (LOCAL_EMBEDDING_MODELS is a 2-entry frozenset) and
            # scoped tightly to the voyage-switch scenario (never fires
            # for a plain bge<->minilm install, which keeps its own
            # deliberate `nx init` migration UX unchanged).
            #
            # TRUTH TABLE (mode: local + local.embed_model voyage-shaped;
            # all other modes/configs never reach this line — full table
            # incl. the read-path row shared with docs/cli-reference.md
            # "Local mode with Voyage"):
            #
            #   for_write=False (read), key ABSENT or PRESENT  -> PROBE.
            #       Reads must always find whatever exists — a credential
            #       is never required just to look at data. (This is a
            #       genuinely different row from resolve_write_embedding_
            #       model's table below — reads probe unconditionally,
            #       writes only when keyless — so this branch keeps its
            #       own call rather than delegating.)
            #   for_write=True (write)  -> delegates to
            #       resolve_write_embedding_model() (nexus-o5x2c), THE
            #       single chokepoint every other write-path caller
            #       (catalog registration, ad-hoc corpus fallbacks, dt
            #       import, ...) also goes through — see its docstring
            #       for the full key-present/absent truth table. Kept
            #       here, not just re-implemented, so this function's
            #       write branch and every external caller are
            #       PROVABLY the same decision, not two copies that
            #       happen to agree today.
            def _bge_candidate_exists(local_token: str) -> bool:
                candidate = f"{ct}__{owner_segment}__{local_token}__v1"
                return candidate != promoted and t3.collection_exists(candidate)  # type: ignore[attr-defined]

            from nexus.config import is_local_mode, local_embed_model_is_voyage  # noqa: PLC0415 — circular-dep avoidance (config)
            if is_local_mode() and local_embed_model_is_voyage():
                if not for_write:
                    found_token = _probe_local_token_collections(_bge_candidate_exists)
                    if found_token is not None:
                        return f"{ct}__{owner_segment}__{found_token}__v1"
                else:
                    resolved_token = resolve_write_embedding_model(
                        ct, collection_exists=_bge_candidate_exists,
                    )
                    if resolved_token in LOCAL_EMBEDDING_MODELS:
                        return f"{ct}__{owner_segment}__{resolved_token}__v1"
                    # Not a local token: either the key IS configured
                    # (resolve_write_embedding_model returned the voyage
                    # token — identical to `promoted`, already computed
                    # above, so nothing more to do here) or nothing to
                    # grandfather onto (it re-raised
                    # LocalVoyageCredentialMissingError from its own
                    # strict fallback — caught by this function's
                    # best-effort except-block below and re-raised
                    # cleanly by the for_write recompute at the bottom
                    # of this function, so the caller sees ONE raise,
                    # not a probe-time one).
    except Exception:  # noqa: BLE001 — best-effort collection_exists probe; falls through to auto-promoted shape on any backend failure
        # collection_exists probe is best-effort. On failure (cloud
        # quota error, transient network) fall through to the
        # auto-promoted shape; legacy reads still work via T3's
        # existing-collection bypass on read paths.
        pass
    if for_write:
        # Nothing pre-existing to grandfather onto: this IS a brand-new
        # mint. Recompute strictly — raises loud if local.embed_model is
        # voyage-shaped with no key configured, never silently falls
        # back to bge.
        return f"{ct}__{owner_segment}__{effective_embedding_model_for_writes(ct)}__v1"
    return promoted


def collection_registration_kwargs(name: str) -> dict[str, str]:
    """Derive ``register_collection`` kwargs for *name*.

    RDR-204 Phase 1 (nexus-f5wwx): the engine no longer auto-registers a
    collection on first write (bead .7 deleted the seven stub-insert
    paths) — a client write path that used to rely on that must
    register first. This is the ONE derivation every such call site
    reuses (T3 chunk writes, the aspects store, taxonomy persistence),
    so content_type/owner_id/embedding_model never diverge between them.

    A conformant 4-segment name (``<content_type>__<owner_id>__
    <embedding_model>__v<n>``) is decomposed via
    :func:`parse_conformant_collection_name` for ``content_type``,
    ``owner_id`` and ``model_version``. A legacy 2-segment name (e.g. a
    grandfathered ``knowledge__distributed-systems``, RDR-101) is split
    on the first ``__``: ``content_type`` is the first segment,
    ``owner_id`` is everything after it, and ``model_version`` is
    ``"v1"`` (the ``register_collection`` default — legacy names carry
    no version segment to preserve). A name with NO ``__`` at all
    (``"my-notes"``) mirrors :func:`t3_collection_name`'s own existing
    bare-name convention (a bare content-type-less string promotes to
    ``knowledge__<name>__...``): ``content_type`` is ``"knowledge"``,
    ``owner_id`` is the whole string.

    ``embedding_model`` is ALWAYS :func:`effective_embedding_model_for_writes`
    for the derived ``content_type`` — never a token read back out of
    the name — mirroring nexus-ft04v.34's ``commands/index.py`` fix
    exactly: an engine older than Phase 1 (which ignores the field)
    still stores the right model, and a Phase-1+ engine's
    profile-mismatch 422 fires on a real drift instead of being masked
    by parroting whatever the name happened to say.

    Raises :class:`ValueError` only when *name* carries no
    recognisable ``<content_type>__<owner_id>`` shape at all (an
    empty segment on either side of a ``__``) — a MINT-time decision,
    a name a client is about to write into for the first time, never
    a backfill-time salvage; there is no "unknown"/"disputed"
    fallback here the way the engine's one-time backfill has one for
    pre-existing garbage rows. The derived ``content_type`` itself is
    NOT restricted to the four canonical types here: the engine's own
    constraint is NOT NULL + non-empty, not an enum
    (hygiene-002-collection-attributes-walk.xml carries no CHECK on
    the column), and a large pre-existing test surface uses
    non-canonical placeholder segments (``test__coll``) as opaque
    collection identifiers that were never meant to name a real T3
    content type. :func:`effective_embedding_model_for_writes` still
    validates on its cloud/voyage branch via
    :func:`canonical_embedding_model` — unchanged, and the only
    validation that ever fired here in production (plain local mode
    never validates content_type either, an existing property of that
    function this one does not alter).
    """
    # RDR-204 Phase 3 repoint (nexus-ft04v.26): *name* may not have a row
    # yet at all -- registering IS what creates one, so this candidate
    # derivation from the STRING SHAPE (split_candidate_collection_name, the
    # same primitive t3_collection_name's own candidate-parsing sites use)
    # stays, unlike collection_content_type/collection_owner which now
    # read the row and fail loud on a name with none.
    #
    # split_candidate_collection_name(name)[1] == name is the string-shape
    # way to ask "does name have no '__' at all", kept as its own branch
    # (rather than folded into a single `or "knowledge"` expression) for
    # the same reason as t3_collection_name's ct/rest split above -- the
    # has-"__"-but-empty-first-segment case must still raise below, not
    # silently default to "knowledge".
    if is_conformant_collection_name(name):
        segments = parse_conformant_collection_name(name)
        content_type = segments["content_type"]
        owner_id = segments["owner_id"]
        model_version = segments["model_version"]
    else:
        _ct_probe, _owner_probe = split_candidate_collection_name(name)
        if _owner_probe == name:
            content_type = "knowledge"
            owner_id = name
        else:
            content_type = _ct_probe
            owner_id = _owner_probe
            # Equivalent to the historical `parts = name.split("__"); len(parts) < 2
            # or not parts[0] or not parts[1]` guard: `len(parts) < 2` can never
            # fire here (a "__" is already known present), `not parts[0]` is
            # exactly `not content_type`, and `not parts[1]` is exactly
            # `not owner_id or owner_id.startswith("__")` -- parts[1] is the
            # first joined element of owner_id, which is empty iff owner_id
            # itself is empty or begins with a second, immediately-adjacent
            # "__" (a plain str.split("__") can never leave "__" inside a
            # single part, so owner_id cannot start with "__" for any other
            # reason).
            if not content_type or not owner_id or owner_id.startswith("__"):
                raise ValueError(
                    f"collection_registration_kwargs: {name!r} has no "
                    "<content_type>__<owner_id> shape to register with"
                )
        model_version = "v1"

    # RDR-204 Phase 3 (nexus-ft04v.26): deliberately NOT repointed to read
    # the row. *name* here may have NO row at all -- registering IS what
    # creates one (the seven bare `register_collection(name)` call sites
    # this derivation exists for are precisely the mint case) -- and
    # calling nexus.mcp_infra.get_collection_row unconditionally on every
    # registration would add a real round trip (a cold collections-cache
    # miss fetches the FULL tenant list) to a write path this bead's own
    # §Performance Expectations promises adds no new hot-path query, plus
    # break every unit test of this function that mocks no T3 substrate.
    # embedding_model already comes from the profile, never the row or the
    # name (nexus-ft04v.34) -- a genuine drift 422s. content_type/owner_id
    # staying name-derived here is the one helper in this module's item-1
    # list that keeps its string-shape contract; see this bead's hand-off
    # report for the fuller design note.
    return {
        "content_type": content_type,
        "owner_id": owner_id,
        "embedding_model": effective_embedding_model_for_writes(content_type),
        "model_version": model_version,
    }


#: Per-process cache of collection names already registered by
#: :func:`ensure_collection_registered` — see that function's docstring.
_REGISTERED_COLLECTIONS: set[str] = set()
_REGISTERED_COLLECTIONS_LOCK = threading.Lock()


def ensure_collection_registered(
    name: str, *, registrar: "Callable[[], object] | None" = None,
) -> None:
    """Idempotently register *name* before its first write in this process.

    RDR-204 Phase 1 client half (nexus-f5wwx). Call this from every
    write path that used to rely on the engine's now-retired
    auto-registration on first write: the T3 chunk write path
    (``HttpVectorClient.put`` / ``.upsert_chunks``, which covers
    ``nx store put``, MCP ``store_put``, the indexer, ``nx index md``/
    ``pdf``/``rdr``, ``nx dt import`` and ``nx memory promote`` in one
    place), the aspects store (``HttpDocumentAspectsStore.upsert``),
    and taxonomy persistence (``HttpTaxonomyStore.persist_discovered_topics``
    / ``.persist_rebuild_topics`` / ``.import_topic``).

    Cheap after the first call: a per-process cache means a hot
    per-chunk write path pays one HTTP round trip per NEW collection,
    never one per write. The cache is name-keyed only (no tenant
    dimension) — matching every other ambient-tenant catalog write in
    this codebase (``make_catalog_writer()`` itself resolves tenant
    from config, not from a caller-supplied value).

    *registrar* is a zero-arg factory returning a catalog writer (an
    object with ``register_collection`` and ``close``) — injectable so
    a low-level caller (:class:`~nexus.db.http_vector_client.
    HttpVectorClient`) is never hard-coupled to
    ``nexus.catalog.factory``, and so a unit test can pass a fake
    writer with no service running. Defaults to
    :func:`nexus.catalog.factory.make_catalog_writer`, imported here
    (not at module level) to keep this module free of a catalog
    import cycle.

    A 409 from the register call is treated as already-registered
    (idempotent-upsert semantics, RDR-204 Technical Design step 2) —
    another process may have won the race to register the same name.
    Any other failure propagates uncaught: a registration failure here
    means the write that follows would 422/4xx anyway, and failing at
    this boundary names the real cause instead of the write's more
    confusing downstream error.

    RDR-204 Phase 3 item 3 (nexus-ft04v.26; coordinator design
    correction 2026-09-09): immediately BEFORE the register call, reads
    the engine's ``nexus.embedding_profile`` for *kwargs*'s
    ``content_type`` and compares it against the ``embedding_model``
    ``collection_registration_kwargs`` just derived (the client's local
    intent) — THE REGISTRATION SEAM, chosen because a catalog client is
    already about to be used here and the model is about to be
    committed, unlike :func:`effective_embedding_model_for_writes`
    (reverted to pure local computation after a first attempt at this
    same check there broke 155 unit tests that reach it with only the
    db/T3 layer mocked — a network call in that chokepoint was wrong,
    not under-fixtured). Profile disagrees -> :class:`EmbeddingProfileMismatchError`
    naming the restart, registration refused before the wire call. No
    profile row for this content_type yet -> proceeds with intent
    unchanged (the bootstrap case: this registration is what seeds the
    row every later comparison reads, verified against
    ``CatalogRepository.upsertCollection`` — see
    :func:`effective_embedding_model_for_writes`'s prior docstring
    history for the full engine citation). Against a pre-Phase-2
    engine, :class:`~nexus.catalog.http_catalog_client.
    EmbeddingProfileRouteMissingError` propagates uncaught. This is an
    EARLY, more actionable diagnostic layered on top of the engine's
    own register-time 422 on a mismatch, which remains the correctness
    guard on its own for every registration call site OUTSIDE this
    funnel (``commands/index.py``, ``commands/collection.py``'s
    ``reindex_cmd``, ``commands/catalog_cmds/collections.py``'s
    backfill/rename, ``db/t3.py``'s row synthesis — all call
    :func:`collection_registration_kwargs` directly and register
    without going through this function) until nexus-ft04v.27
    consolidates them through one funnel.
    """
    if name in _REGISTERED_COLLECTIONS:
        return
    with _REGISTERED_COLLECTIONS_LOCK:
        if name in _REGISTERED_COLLECTIONS:
            return
        kwargs = collection_registration_kwargs(name)
        profile_model = _profile_model_for_content_type(kwargs["content_type"])
        if profile_model is not None and profile_model != kwargs["embedding_model"]:
            raise EmbeddingProfileMismatchError(
                f"content_type={kwargs['content_type']!r}: this install's "
                f"configured intent is {kwargs['embedding_model']!r}, but the "
                f"engine's embedding_profile still says {profile_model!r}. "
                "The engine reads local.embed_model and voyage_api_key only "
                "at spawn, so a config change after the service started "
                "leaves the two disagreeing until it restarts. A restart is "
                f"required for the engine to adopt this: `{_SERVICE_RESTART_COMMAND}`."
            )
        if registrar is None:
            from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — circular-dep avoidance (catalog)
            registrar = make_catalog_writer
        writer = registrar()
        try:
            import httpx  # noqa: PLC0415 — deferred: keeps this module httpx-free at import time
            try:
                writer.register_collection(name, **kwargs)
            except httpx.HTTPStatusError as exc:
                if exc.response is None or exc.response.status_code != 409:
                    raise
                _log.debug(
                    "collection_already_registered_race",
                    name=name,
                )
        finally:
            writer.close()
        _REGISTERED_COLLECTIONS.add(name)
        # RDR-204 Phase 3 (nexus-ft04v.26, fixture-seam round 2):
        # nexus.mcp_infra's collection-row cache (_collections_cache,
        # 60s TTL) is the row source resolve_corpus's bare-corpus fan-out
        # reads. store_put/store_delete already invalidate it on write
        # (mcp/core.py); this registration path -- the one EVERY write
        # path this function documents (T3 chunks, aspects, taxonomy,
        # the doc indexer) funnels through -- did not, so a NEWLY
        # registered collection could stay invisible to a search moments
        # later in the SAME process, for the cache's remaining TTL
        # window. Real impact: any long-lived process (the MCP server;
        # an in-process CliRunner test chaining index-then-search calls)
        # that writes a brand-new collection and searches it within the
        # TTL window -- found live via test_index_repo_routes_code_to_
        # code_corpus, whose `nx search --corpus code --json` returned
        # empty stdout (resolve_corpus dropped the just-registered
        # code__ collection; both diagnostics this branch prints go to
        # stderr, never stdout) immediately after `nx index repo`
        # registered it in the SAME process. A real, separate-OS-process
        # CLI invocation never hit this (mcp_infra's cache always starts
        # cold), which is why it stayed invisible until an in-process
        # test chained the two calls.
        from nexus.mcp_infra import invalidate_collections_cache  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)
        invalidate_collections_cache()


def _looks_like_stale_registration_error(exc: BaseException) -> bool:
    """True when *exc* is the ONE 422 :func:`write_with_registration_retry`
    retries: the engine's per-tenant, once-per-boot ghost sweep (RDR-204
    Technical Design step 3, bead nexus-ft04v.3) deleted a
    registered-but-chunkless collection between this process's own
    :func:`ensure_collection_registered` call and the write that
    followed it — a real race only across an intervening engine
    restart (the sweep runs once per tenant at that tenant's FIRST
    request after boot), never within one call. Any OTHER 422 (most
    notably the profile-mismatch "names a different model" refusal,
    RDR-204 Technical Design step 2) must propagate unretried — this
    check is deliberately narrowed to the "not registered" wording so
    it can never mask that different failure as a transient one.

    Two HTTP-error families reach here, mirroring
    :func:`nexus.retry._extract_status_and_retry_after`'s own
    precedent for this exact split: ``httpx.HTTPStatusError`` (the
    aspects and taxonomy stores) carries the status and body on
    ``.response``; ``nexus.db.http_vector_client.VectorServiceError``
    (the T3 vector client, urllib-based) is matched by DUCK TYPE — a
    plain ``.code`` int — rather than an import, so this module never
    takes a dependency on ``http_vector_client`` (which already
    deferred-imports THIS module, to avoid exactly that cycle);
    ``VectorServiceError.__init__`` folds the engine's error body into
    its message, so ``str(exc)`` is where the text lives for that
    family.
    """
    import httpx  # noqa: PLC0415 — deferred: keeps this module httpx-free at import time

    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        if resp is None or resp.status_code != 422:
            return False
        try:
            body_text = resp.text
        except Exception:  # noqa: BLE001 — a response with no readable body is never this specific error
            return False
        return "not registered" in body_text.lower()
    code = getattr(exc, "code", None)
    if code == 422:
        return "not registered" in str(exc).lower()
    return False


_T = TypeVar("_T")


def write_with_registration_retry(
    name: str,
    write_fn: "Callable[[], _T]",
    *,
    registrar: "Callable[[], object] | None" = None,
) -> "_T":
    """Ensure *name* is registered, run *write_fn*, and retry exactly
    once on the one known post-registration race.

    RDR-204 Phase 1 client half (nexus-f5wwx), refinement after bead
    .3 landed: the engine's boot-sweep can delete a registered but
    still-chunkless collection at the next engine restart, so a
    collection this PROCESS registered earlier (and cached as known)
    can be gone by the time a write for it finally happens, and that
    write 422s "collection ... is not registered". Catch exactly that
    shape (:func:`_looks_like_stale_registration_error`), evict the
    cache entry, register once more, and retry *write_fn* ONE time.
    Any second failure, or any OTHER exception on the first attempt
    (including a different-shaped 422, e.g. a profile mismatch),
    propagates immediately — this is a narrow one-shot repair, not a
    general retry loop.

    *write_fn* is a zero-arg callable performing the actual HTTP
    write (and returning whatever the caller needs back) — callers
    wrap their POST in a closure so this helper stays write-shape
    agnostic across the T3 chunk write path, the aspects store, and
    taxonomy persistence.

    The normal path (registration and the write both succeed on the
    first attempt) pays nothing extra beyond
    :func:`ensure_collection_registered`'s own per-process cache: the
    registration call is itself authenticated the same way as every
    other catalog write, so it always completes strictly BEFORE the
    write that follows in the SAME call — the sweep can only ever
    catch a collection that sat registered-but-unwritten across an
    intervening restart, never the write this function itself just
    triggered registration for.
    """
    ensure_collection_registered(name, registrar=registrar)
    try:
        return write_fn()
    except Exception as exc:  # noqa: BLE001 — narrowed immediately below; anything else re-raised unchanged
        if not _looks_like_stale_registration_error(exc):
            raise
        _log.info(
            "collection_registration_stale_after_boot_sweep_retry",
            name=name,
        )
        with _REGISTERED_COLLECTIONS_LOCK:
            _REGISTERED_COLLECTIONS.discard(name)
        ensure_collection_registered(name, registrar=registrar)
        return write_fn()


def resolve_corpus(corpus: str, all_collections: list[str]) -> list[str]:
    """Resolve a --corpus argument to a list of matching collection names.

    Three-stage match:

    1. Exact match (covers fully-qualified conformant names from RDR-103,
       e.g. ``knowledge__foo__voyage-context-3__v1``).
    2. BARE CANONICAL CONTENT-TYPE fan-out (*corpus* is exactly one of
       :data:`CONTENT_TYPES` -- ``code``/``docs``/``rdr``/``knowledge``,
       no ``__`` at all): RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26,
       Gap 4). Each candidate's catalog row (:func:`nexus.mcp_infra.get_collection_row`,
       the SAME collection list this function's caller already fetched)
       is checked for ``content_type == corpus`` AND ``lifecycle_state ==
       "live"`` -- a candidate with no row, or a non-live row (quarantine,
       dormant, disputed) is DROPPED, never included and never a hard
       failure (a bare-corpus fan-out silently skipping an unregistered
       or non-live name is the documented RDR §Failure Modes behaviour).
       This retires the ``quarantine-`` NAME PREFIX as the exclusion
       mechanism -- a quarantine sibling's row carries its ORIGIN content
       type with ``lifecycle_state="quarantine"``, so it is excluded by
       the column here even though its physical name never matched a
       ``{corpus}__`` string prefix in the first place.
    3. Legacy STRING-PREFIX recovery, for every *corpus* value stage 2
       does not apply to (contains ``__``, e.g. a human-typed short form
       like ``knowledge__foo`` recovering the auto-promoted on-disk
       ``knowledge__foo__voyage-context-3__v1``; or a bare, non-canonical
       word that is not a content type to begin with). There is no
       catalog column to filter such a value BY -- it may not even be a
       real content type -- so this stage keeps the original pure string
       match unchanged: it is not what Gap 4 is about, and three of this
       function's four callers (``nx collection verify``'s legacy-name
       recovery, the CLI ``--corpus`` resolver, the doctor corpus probe)
       depend on exactly this shape surviving untouched.

    The structlog debug record reports which stage matched, useful when
    tracing why a corpus argument resolved to a particular collection.
    """
    # Stage 1: exact match.
    matches = [c for c in all_collections if c == corpus]
    if matches:
        return matches

    # Stage 2: bare canonical content-type fan-out, row-filtered.
    if corpus in CONTENT_TYPES:
        from nexus.mcp_infra import get_collection_row  # noqa: PLC0415 — circular-dep avoidance (mcp_infra)
        matches = []
        for c in all_collections:
            row = get_collection_row(c)
            if row is None:
                _log.debug(
                    "resolve_corpus_candidate_dropped_no_row",
                    corpus=corpus, collection=c,
                )
                continue
            if row["content_type"] != corpus:
                continue
            if row.get("lifecycle_state") != "live":
                _log.debug(
                    "resolve_corpus_candidate_excluded_lifecycle",
                    corpus=corpus, collection=c, lifecycle_state=row.get("lifecycle_state"),
                )
                continue
            matches.append(c)
        if not matches:
            _log.debug("resolve_corpus_no_collections_matched", corpus=corpus, stage="content_type_fanout")
        return matches

    # Stage 3: legacy string-prefix recovery (unchanged pure string match).
    # The conformant name shape always introduces ``__`` between segments,
    # so ``{corpus}__`` is the invariant boundary.
    prefix = f"{corpus}__"
    matches = [c for c in all_collections if c.startswith(prefix)]
    if not matches:
        _log.debug("resolve_corpus_no_collections_matched", corpus=corpus, stage="legacy_prefix")
    return matches
