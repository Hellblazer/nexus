# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re
from collections.abc import Callable

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
    """
    # Length check fires first for <3 chars; regex rejects other invalid patterns.
    # Both gates are needed: length for clear error messages, regex for charset/boundary validation.
    if len(name) > 63:
        # Overflow: derive an actionable hint.  The canonical remedy is to
        # rename to the repo-id-conformant name (e.g. ``code__1-36__bge-base-en-v15-768__v1``,
        # 35 chars) which fits under the cap and preserves vectors (no reindex).
        # Derive content_type from the name prefix (before the first ``__``) when
        # present so the hint can be concrete; fall back to a generic flag otherwise.
        _ct_hint = ""
        if "__" in name:
            _prefix = name.split("__", 1)[0]
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
_CONFORMANT_COLLECTION_RE = re.compile(
    rf"^(?P<ct>{_CT_ALTERNATION})"
    r"__(?P<owner>[a-zA-Z0-9-]+)"
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


def effective_embedding_model_for_writes(content_type: str) -> str:
    """Return the embedding-model token to write into NEW collection
    names and per-chunk metadata for ``content_type``.

    RDR-109 Phase 2. Cloud mode delegates verbatim to
    :func:`canonical_embedding_model` so the RDR-103 canonical-set
    invariant is preserved. Local mode returns the active local
    embedder's normalized token (``minilm-l6-v2-384`` or
    ``bge-base-en-v15-768``) so a fresh local-mode index produces
    collection names that match the bytes inside — UNLESS the user has
    opted local mode into Voyage via ``local.embed_model=voyage-*``
    (nexus-35ok4 / GH #1461), in which case this mirrors cloud mode
    exactly: it delegates to :func:`canonical_embedding_model` so the
    per-content-type voyage-code-3/voyage-context-3 split matches what
    the engine's ``EmbedderRouter`` actually does once
    ``NX_VOYAGE_API_KEY`` is plumbed (Main.java boots a PURE-voyage
    router — no per-content-type choice on the engine side either, so
    delegating here is not a guess, it is the same policy the engine
    already applies).

    ``local_embed_model_is_voyage()`` (:mod:`nexus.config`) is the SAME
    predicate the storage-service supervisor uses to decide whether to
    plumb ``NX_VOYAGE_API_KEY`` into the engine at spawn
    (:mod:`nexus.daemon.storage_service_daemon`) — the two conditions
    are structurally incapable of disagreeing now.

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
                    "engine re-reads it: `nx daemon service stop && nx daemon "
                    "service start`."
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


def _promoted_model_token_for_read(content_type: str) -> str:
    """The read-path counterpart of :func:`effective_embedding_model_for_writes`.

    Computing a CANDIDATE collection name to probe with
    ``collection_exists()`` is not the same as committing to write under
    it — a read must never need a Voyage credential just to construct a
    string to check for existence (code-review-expert CRITICAL,
    nexus-35ok4 round 2). When ``local.embed_model`` is voyage-shaped
    this returns :func:`canonical_embedding_model` directly, BYPASSING
    the credential gate entirely — deliberately, regardless of whether
    ``voyage_api_key`` is currently configured, so a pre-existing
    voyage-named collection (created back when the key WAS present) is
    still a probeable candidate on a keyless read. When
    ``local.embed_model`` is not voyage-shaped this delegates to
    :func:`effective_embedding_model_for_writes`, which never raises on
    that branch (unaffected by this nexus-35ok4 change).
    """
    from nexus.config import is_local_mode, local_embed_model_is_voyage  # noqa: PLC0415 — circular-dep avoidance (config)
    if is_local_mode() and local_embed_model_is_voyage():
        return canonical_embedding_model(content_type)
    return effective_embedding_model_for_writes(content_type)


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
                message=(
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
                    message=(
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


def voyage_model_for_collection(collection_name: str) -> str:
    """Return the Voyage AI model for a T3 collection (index and query).

    The same model MUST be used at both index and query time —
    mismatched models yield random noise (RDR-059).

    docs__/knowledge__/rdr__ → voyage-context-3 (CCE)
    code__ and all others    → voyage-code-3

    In local mode, callers bypass this and use ``LocalEmbeddingFunction``.
    """
    if collection_name.startswith(("docs__", "knowledge__", "rdr__")):
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
    """
    if collection_name.startswith("code__"):
        return 0.70
    if collection_name.startswith("knowledge__"):
        return 0.50
    if collection_name.startswith(("docs__", "rdr__")):
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


def t3_collection_name(
    user_arg: str, *, t3: object | None = None, for_write: bool = False,
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
    if t3 is not None and "__" not in user_arg and user_arg in CONTENT_TYPES:
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

    if "__" in user_arg:
        ct, _, rest = user_arg.partition("__")
    else:
        ct, rest = "knowledge", user_arg

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
                    message=(
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


def resolve_corpus(corpus: str, all_collections: list[str]) -> list[str]:
    """Resolve a --corpus argument to a list of matching collection names.

    Three-stage match:

    1. Exact match (covers fully-qualified conformant names from RDR-103,
       e.g. ``knowledge__foo__voyage-context-3__v1``).
    2. Prefix match if *corpus* does not contain ``__`` (covers the
       short-form ``knowledge__foo`` typed by humans -- wait, this case
       has ``__`` -- so see step 3).
    3. Prefix match if *corpus* DOES contain ``__`` but exact returned
       nothing. This is the post-RDR-103 case: a user types
       ``knowledge__foo`` expecting the legacy name; the on-disk
       collection is now ``knowledge__foo__voyage-context-3__v1``.
       Treating the partial form as a prefix recovers the intent
       without forcing users to know the embedder + version suffix.

    The structlog debug record reports which stage matched, useful when
    tracing why a corpus argument resolved to a particular collection.
    """
    # Stage 1: exact match.
    matches = [c for c in all_collections if c == corpus]
    if matches:
        return matches

    # Stage 2 + 3: prefix match. The conformant name shape always
    # introduces ``__`` between segments, so ``{corpus}__`` is the
    # invariant boundary whether *corpus* itself contains ``__`` or not.
    prefix = f"{corpus}__"
    matches = [c for c in all_collections if c.startswith(prefix)]
    if not matches:
        structlog.get_logger().debug("resolve_corpus: no collections matched", corpus=corpus)
    return matches
