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

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from nexus.catalog.rows.legacy_names import candidate_attributes, version_of_blank_row_label
from nexus.corpus import CANONICAL_EMBEDDING_MODELS, CONTENT_TYPES, LOCAL_EMBEDDING_MODELS

if TYPE_CHECKING:
    from pathlib import Path

    from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)

#: The embedding-model tokens a conformant collection name may carry, cloud and local: the
#: closed set ``CollectionName.parse`` validates against, and the one the blank-version
#: fallback keeps validating (``legacy_names`` cannot import it from ``nexus.corpus``).
KNOWN_EMBEDDING_MODELS: frozenset[str] = CANONICAL_EMBEDDING_MODELS | LOCAL_EMBEDDING_MODELS

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
        ``nexus.catalog.rows.legacy_names.is_conformant_collection_name`` before calling
        this constructor.

        The conformance regex (``_CONFORMANT_COLLECTION_RE`` in
        ``nexus.catalog.rows.legacy_names``, the one name parser left, read
        through ``candidate_attributes``) is derived from the same content types, so the
        content_type group already enforces the closed set. The redundant
        check below catches a future divergence (e.g. someone widens the
        regex without updating ``CONTENT_TYPES``) without weakening today's
        guarantees.
        """
        parsed = candidate_attributes(name)
        if parsed is None or not parsed.conformant:
            raise ValueError(
                f"Collection name {name!r} is not conformant: "
                f"expected <content_type>__<owner_id>__<embedding_model>__v<n>"
            )
        content_type = parsed.content_type
        if content_type not in CONTENT_TYPES:
            raise ValueError(
                f"Collection name {name!r} has unknown content_type "
                f"{content_type!r}; expected one of {CONTENT_TYPES}"
            )
        embedding_model = parsed.embedding_model
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
            owner_id=parsed.owner_id,
            embedding_model=embedding_model,
            model_version=parsed.model_version,
        )


def row_version_number(raw: object) -> int | None:
    """The version number of a registry row's ``model_version`` text (``"v<N>"``, N >= 1), else ``None``.

    The one parser of that text: :func:`version_from_for_tuple_row` raises on ``None`` and
    :func:`nexus.catalog.rows.resolve.resolve_collection` reads it as an unreadable version.
    """
    digits = raw[1:] if isinstance(raw, str) and raw.startswith("v") else ""
    return int(digits) if digits.isascii() and digits.isdigit() and int(digits) >= 1 else None


def version_from_for_tuple_row(
    row: dict, *, content_type: str, owner_id: str, embedding_model: str,
) -> int | None:
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
    re-registering) falls back to the returned conformant name, with a warning.
    When the name carries no version either (a legacy slug or a non-conformant
    label: ``for_tuple`` answers such a ``legacy_grandfathered`` row since engine
    0.1.200, RDR-229 P2.8) the answer is ``None``, a row with no readable version:
    the caller answers the row as it is and refuses only a bump. A name that
    parses to version 0 raises.

    The fallback is temporary and lives in
    :func:`nexus.catalog.rows.legacy_names.version_of_blank_row_label` (a migration
    source, RDR-229 P2.2): P1.2's production census (nexus-p2m1x.2) counts rows with an
    empty ``model_version``, and it comes out once D2's cloud run completes (no bead owns
    the removal). It refuses a model token outside :data:`KNOWN_EMBEDDING_MODELS`, as
    ``CollectionName.parse`` did.
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
        return version_of_blank_row_label(row.get("name"), models=KNOWN_EMBEDDING_MODELS)
    version = row_version_number(raw)
    if version is None:
        raise ValueError(
            f"collection row {row.get('name')!r} has a malformed model_version {raw!r}; "
            "expected 'v<N>' with N >= 1"
        )
    return version


@dataclass(frozen=True)
class CollectionAttributes:
    """One catalog collection row (RDR-229 item 1).

    The name a collection is registered under is its *label*: an existing
    collection's name becomes its label unchanged, whatever shape it has.
    Callers that hold a :class:`CollectionAttributes` read the label and the
    attributes from the row and never parse them out of the name.

    ``collection_id`` is ``None`` until the engine carries the surrogate key
    (RDR-229 P3.1), and for a name that is about to be minted. ``label`` is the
    registered name; until the engine sends a separate label field it is the
    row's ``name``. ``model_version`` is ``None`` for a registered row whose
    ``model_version`` is blank and whose name carries none (a grandfathered slug):
    nothing can be bumped from it.
    """

    collection_id: int | None
    label: str
    content_type: str
    owner_id: str
    embedding_model: str
    model_version: int | None
    dimension: int | None
    lifecycle_state: str | None

    def render(self) -> str:
        """The registered name, unchanged (a label of any shape)."""
        return self.label

    @classmethod
    def from_for_tuple(
        cls, row: dict, *, content_type: str, owner_id: str, embedding_model: str,
    ) -> "CollectionAttributes":
        """The attributes of a ``/collections/for_tuple`` row for the requested tuple.

        Raises ``ValueError`` when the row is for another tuple or carries a
        malformed version (:func:`version_from_for_tuple_row`), or has no name.
        A blank version the name cannot supply answers ``model_version=None``.
        """
        label = row.get("label") or row.get("name")
        if not isinstance(label, str) or not label:
            raise ValueError(f"for_tuple answered a row with no name: {row!r}")
        version = version_from_for_tuple_row(
            row, content_type=content_type, owner_id=owner_id, embedding_model=embedding_model,
        )
        cid = row.get("collection_id")
        dim = row.get("dimension")
        state = row.get("lifecycle_state")
        return cls(
            collection_id=cid if isinstance(cid, int) and not isinstance(cid, bool) else None,
            label=label,
            content_type=content_type,
            owner_id=owner_id,
            embedding_model=embedding_model,
            model_version=version,
            dimension=dim if isinstance(dim, int) and not isinstance(dim, bool) else None,
            lifecycle_state=state if isinstance(state, str) and state else None,
        )

    @classmethod
    def minted(
        cls, *, label: str, content_type: str, owner_id: str, embedding_model: str, model_version: int,
    ) -> "CollectionAttributes":
        """Attributes for a name about to be registered: no row backs it yet."""
        return cls(
            collection_id=None, label=label, content_type=content_type, owner_id=owner_id,
            embedding_model=embedding_model, model_version=model_version,
            dimension=None, lifecycle_state=None,
        )


def version_unreadable_message(label: str) -> str:
    """The refusal for a bump asked on a registered row that has no readable version."""
    return (
        f"collection {label!r} is registered with no readable model_version, so no next version "
        "can be minted from it: set the row's model_version, or re-register the collection"
    )


def find_collection_row(
    get: Callable[..., Any], content_type: str, owner_id: str, embedding_model: str,
) -> "CollectionAttributes | None":
    """The registered collection for ``(content_type, owner_id, embedding_model)``.

    RDR-229 item 1, through ``GET /collections/for_tuple`` read with *get* (the
    catalog client's ``_get``). *owner_id* is the row's owner segment (a
    knowledge collection's subject). ``None`` on a 404: no row backs the tuple.
    Any other failure raises, so a caller never takes an unreadable registry for
    an empty one and mints over it. A row for a different tuple, or with a
    malformed version, raises ``ValueError``.
    """
    try:
        row = get(
            "/collections/for_tuple",
            content_type=content_type,
            owner_id=owner_id,
            embedding_model=embedding_model,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    if not row:
        return None
    return CollectionAttributes.from_for_tuple(
        row, content_type=content_type, owner_id=owner_id, embedding_model=embedding_model,
    )


def collection_for_repo_row(
    repo: "Path",
    content_type: str,
    *,
    bump: bool,
    owner_for_repo: Callable[[str], Any],
    tuple_registered: Callable[[str, Any, str], bool],
    find_collection: Callable[[str, str, str], "CollectionAttributes | None"],
) -> "CollectionAttributes":
    """The collection *repo*'s *content_type* chunks go to, as its catalog row.

    RDR-229 P2.8 (item 5): a registered tuple answers its row, so a repo whose
    collection is registered under a legacy slug gets that label back instead of
    the conformant name a render would produce. A new tuple, or *bump*, has no
    row for the name it needs, so it renders one (the mint class; the engine
    mints labels from P3.8). The catalog client passes its ``owner_for_repo``,
    ``_tuple_registered`` and ``find_collection``.

    Mirrors canonical ``_DocumentOps.collection_for_repo``: look up the owner by
    repo_hash, then resolve the embedding model.
    """
    from nexus.corpus import resolve_write_embedding_model  # noqa: PLC0415 — circular-dep avoidance
    from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — circular-dep avoidance

    _, repo_hash = _repo_identity(repo)
    owner = owner_for_repo(repo_hash)
    if owner is None:
        raise LookupError(
            f"collection_for_repo: no owner registered for repo_hash {repo_hash!r} "
            f"(repo {repo!s}). Call ensure_owner_for_repo() first."
        )
    # nexus-o5x2c: THE chokepoint (see resolve_write_embedding_model's docstring
    # for the full truth table) -- grandfathers onto a pre-existing bge/minilm
    # registration when local.embed_model is voyage-shaped and no key is
    # configured, instead of crashing. The hot path for `nx index repo` on an
    # ALREADY catalog-registered repo (live-repro'd bug).
    embedding_model = resolve_write_embedding_model(
        content_type,
        collection_exists=lambda token: tuple_registered(content_type, owner, token),
    )
    owner_id = owner_segment_for_tumbler(owner)
    if not owner_id:
        raise ValueError(
            f"collection_for_repo: cannot derive owner_id segment from owner {owner!r}"
        )
    row = find_collection(content_type, owner_id, embedding_model)
    if row is not None and not bump:
        return row
    if row is not None and row.model_version is None:
        raise ValueError(version_unreadable_message(row.label))
    version = row.model_version + 1 if row is not None else 1
    return CollectionAttributes.minted(
        label=CollectionName(
            content_type=content_type, owner_id=owner_id,
            embedding_model=embedding_model, model_version=version,
        ).render(),
        content_type=content_type, owner_id=owner_id,
        embedding_model=embedding_model, model_version=version,
    )
