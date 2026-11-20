# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 1: ``CollectionName`` value object.

The collection name is a four-segment tuple
``(content_type, owner_id, embedding_model, model_version)``
rendered to ``<content_type>__<owner_id>__<embedding_model>__v<n>``.

The catalog renders ``CollectionName`` instances; the indexer asks the
catalog for a collection rather than constructing one and asking the
catalog to record it. ``CollectionName.parse`` is strict (pinned
decision #4): legacy 2-segment names and non-canonical embedding models
raise ``ValueError`` and remain string-only identifiers per RDR-101's
grandfathering invariant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from nexus.corpus import (
    CANONICAL_EMBEDDING_MODELS,
    CONTENT_TYPES,
    LOCAL_EMBEDDING_MODELS,
    parse_conformant_collection_name,
)

if TYPE_CHECKING:
    from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)

#: The ``lifecycle_state`` values a ``for_tuple`` row may carry and still be written to (RDR-229
#: P2.5). The engine's CHECK allows these and ``quarantine`` (hygiene-002); its tuple filter is
#: ``<> 'quarantine'`` on purpose, so ``dormant`` and ``disputed`` keep resolving.
RESOLVABLE_LIFECYCLE_STATES: frozenset[str] = frozenset({"live", "dormant", "disputed"})


def owner_segment_for_tumbler(tumbler: str | Tumbler) -> str:
    """Return the collection-name owner segment for a tumbler.

    ``1.7.42`` to ``1-7`` (the first two dot-segments joined with a
    hyphen so the result fits ChromaDB's collection-name regex).
    Tumbler instances are converted via ``str()``.

    Returns the empty string for malformed input (single-segment
    tumblers, empty strings) rather than raising; the ``migrate`` verb
    relies on the empty string to skip the row with a warning instead
    of aborting the loop. ``Catalog.collection_for`` then promotes the
    empty-segment case into a ``ValueError`` at the public boundary.
    """
    s = str(tumbler) if not isinstance(tumbler, str) else tumbler
    if not s:
        return ""
    parts = s.split(".")
    if len(parts) < 2:
        return ""
    return "-".join(parts[:2])


@dataclass(frozen=True, slots=True)
class CollectionName:
    """The canonical collection-name tuple."""

    content_type: str
    owner_id: str
    embedding_model: str
    model_version: int

    def __post_init__(self) -> None:
        """Validate against the same canonical sets ``parse()`` uses.

        nexus-fm3th: this was a bare frozen dataclass — only ``.parse()``
        validated content_type/embedding_model, so a direct construction
        (e.g. ``http_catalog_client.py``'s ``collection_for``) could mint
        a non-conformant ``CollectionName`` from a bogus content_type or
        embedding_model. Both branches import the SAME sets ``parse()``
        checks against (``CONTENT_TYPES``, ``CANONICAL_EMBEDDING_MODELS``,
        ``LOCAL_EMBEDDING_MODELS``) — no separate literal list to drift.
        """
        if self.content_type not in CONTENT_TYPES:
            raise ValueError(
                f"CollectionName: unknown content_type {self.content_type!r}; "
                f"expected one of {sorted(CONTENT_TYPES)}"
            )
        if (
            self.embedding_model not in CANONICAL_EMBEDDING_MODELS
            and self.embedding_model not in LOCAL_EMBEDDING_MODELS
        ):
            allowed = sorted(CANONICAL_EMBEDDING_MODELS | LOCAL_EMBEDDING_MODELS)
            raise ValueError(
                f"CollectionName: non-canonical embedding_model "
                f"{self.embedding_model!r}; expected one of {allowed}"
            )

    def render(self) -> str:
        """Render to the physical T3 collection name."""
        return (
            f"{self.content_type}__{self.owner_id}"
            f"__{self.embedding_model}__v{self.model_version}"
        )

    @classmethod
    def parse(cls, name: str) -> CollectionName:
        """Parse a conformant T3 collection name into a ``CollectionName``.

        Raises ``ValueError`` for legacy 2-segment names, fallback names,
        non-canonical embedding models, or any other shape that does not
        match the RDR-103 schema. Generic callers receiving arbitrary
        physical-collection strings must gate with
        ``nexus.corpus.is_conformant_collection_name`` before calling
        this constructor.

        The conformance regex (``_CONFORMANT_COLLECTION_RE`` in
        ``nexus.corpus``) is derived from ``CONTENT_TYPES``, so the
        content_type group already enforces the closed set. The redundant
        check below catches a future divergence (e.g. someone widens the
        regex without updating ``CONTENT_TYPES``) without weakening today's
        guarantees.
        """
        parsed = parse_conformant_collection_name(name)
        content_type = parsed["content_type"]
        if content_type not in CONTENT_TYPES:
            raise ValueError(
                f"Collection name {name!r} has unknown content_type "
                f"{content_type!r}; expected one of {CONTENT_TYPES}"
            )
        embedding_model = parsed["embedding_model"]
        # RDR-109 Phase 2 widens the closed set to include local-mode
        # tokens. Both cloud (CANONICAL) and local (LOCAL) names are
        # truthful identities under the four-segment schema.
        if (
            embedding_model not in CANONICAL_EMBEDDING_MODELS
            and embedding_model not in LOCAL_EMBEDDING_MODELS
        ):
            allowed = sorted(
                CANONICAL_EMBEDDING_MODELS | LOCAL_EMBEDDING_MODELS
            )
            raise ValueError(
                f"Collection name {name!r} has non-canonical embedding_model "
                f"{embedding_model!r}; expected one of {allowed}"
            )
        return cls(
            content_type=content_type,
            owner_id=parsed["owner_id"],
            embedding_model=embedding_model,
            model_version=int(parsed["model_version"][1:]),
        )


def version_from_for_tuple_row(
    row: dict, *, content_type: str, owner_id: str, embedding_model: str,
) -> int:
    """The existing collection version of a ``/collections/for_tuple`` row.

    RDR-229 P1.4: the version is the ROW's ``model_version`` (``"v<N>"``,
    N >= 1), not a parse of its name, so a legacy-slug name that carries no
    version still resolves. Raises ``ValueError`` when

    - the row's ``content_type``, ``owner_id`` or ``embedding_model`` differ
      from the requested tuple (the engine resolved some other tuple),
    - ``model_version`` is missing or non-empty and malformed (``"v0"`` included),
    - the row's ``lifecycle_state`` is ``quarantine`` or a state the client does
      not know (RDR-229 P2.5). The engine already filters quarantine out; this is
      the client's own check, since P1.4 stopped parsing the name whose failure
      used to refuse a quarantine sibling (nexus-n9xjy). A row with no
      ``lifecycle_state`` (an engine older than P2.5) is accepted as before.

    An EMPTY ``model_version`` (the column is ``NOT NULL DEFAULT ''`` and an
    upsert never rewrites it, so such rows exist and cannot be repaired by
    re-registering) falls back to the returned conformant name, with a warning;
    a row whose name cannot parse either raises.

    The fallback is temporary: P1.2's production census (nexus-p2m1x.2) counts
    rows with an empty ``model_version``, and the fallback comes out once that
    count reads zero.
    """
    got = (row.get("content_type"), row.get("owner_id"), row.get("embedding_model"))
    want = (content_type, owner_id, embedding_model)
    if got != want:
        raise ValueError(
            f"for_tuple answered {row.get('name')!r} for tuple {want!r}, but the row "
            f"carries (content_type, owner_id, embedding_model) = {got!r}"
        )
    state = row.get("lifecycle_state")
    if state is not None and state not in RESOLVABLE_LIFECYCLE_STATES:
        raise ValueError(
            f"for_tuple answered {row.get('name')!r} for tuple {want!r} in lifecycle_state "
            f"{state!r}; only a {'/'.join(sorted(RESOLVABLE_LIFECYCLE_STATES))} row is a write target"
        )
    raw = row.get("model_version")
    if raw == "":
        name = row.get("name")
        try:
            version = CollectionName.parse(name).model_version  # type: ignore[arg-type]
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                f"collection row {name!r} has an empty model_version and a name that "
                "does not carry one"
            ) from exc
        if version < 1:
            raise ValueError(f"collection row {name!r} has an empty model_version and name version v0")
        _log.warning("collection_for_row_model_version_empty", collection=name)
        return version
    digits = raw[1:] if isinstance(raw, str) and raw.startswith("v") else ""
    if not (digits.isascii() and digits.isdigit()) or int(digits) < 1:
        raise ValueError(
            f"collection row {row.get('name')!r} has a malformed model_version {raw!r}; "
            "expected 'v<N>' with N >= 1"
        )
    return int(digits)
