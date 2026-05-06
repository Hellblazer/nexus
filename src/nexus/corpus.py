# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re

import structlog

_log = structlog.get_logger(__name__)

# ChromaDB collection name constraints:
# - 3–63 characters
# - Must start and end with an alphanumeric character
# - May contain alphanumeric characters, hyphens, or underscores in the middle
_COLLECTION_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,61}[a-zA-Z0-9]$")


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
    if not (3 <= len(name) <= 63):
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


_CONTENT_TYPES = ("code", "docs", "rdr", "knowledge")
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
    See ``docs/taxonomy-projection-tuning.md`` for calibration methodology.
    """
    if collection_name.startswith("code__"):
        return 0.70
    if collection_name.startswith("knowledge__"):
        return 0.50
    if collection_name.startswith(("docs__", "rdr__")):
        return 0.55
    return 0.70


# Backward-compatible aliases — callers don't need to distinguish index vs query.
embedding_model_for_collection = voyage_model_for_collection
index_model_for_collection = voyage_model_for_collection


def t3_collection_name(user_arg: str, *, t3: object | None = None) -> str:
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
        except Exception:
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
            preferred_4seg = (
                f"{user_arg}__{user_arg}__"
                f"{canonical_embedding_model(user_arg)}__v1"
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
    promoted = f"{ct}__{owner_segment}__{canonical_embedding_model(ct)}__v1"

    if t3 is None or user_arg == promoted:
        return promoted
    try:
        if not t3.collection_exists(promoted):  # type: ignore[attr-defined]
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
    except Exception:
        # collection_exists probe is best-effort. On failure (cloud
        # quota error, transient network) fall through to the
        # auto-promoted shape; legacy reads still work via T3's
        # existing-collection bypass on read paths.
        pass
    return promoted


def resolve_corpus(corpus: str, all_collections: list[str]) -> list[str]:
    """Resolve a --corpus argument to a list of matching collection names.

    If *corpus* contains ``__`` it is treated as an exact collection name.
    Otherwise it is treated as a prefix — all collections starting with
    ``{corpus}__`` are returned.
    """
    if "__" in corpus:
        matches = [c for c in all_collections if c == corpus]
    else:
        prefix = f"{corpus}__"
        matches = [c for c in all_collections if c.startswith(prefix)]
    if not matches:
        structlog.get_logger().debug("resolve_corpus: no collections matched", corpus=corpus)
    return matches
