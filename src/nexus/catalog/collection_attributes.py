# SPDX-License-Identifier: AGPL-3.0-or-later
"""A collection's registry attributes, read from its catalog row (RDR-229 item 1).

The name a collection is registered under is its *label*: an existing
collection's name becomes its label unchanged, whatever shape it has. Callers
that hold a :class:`CollectionAttributes` read the label and the attributes
from the row and never parse them out of the name.
"""
from __future__ import annotations

from dataclasses import dataclass

from nexus.catalog.collection_name import version_from_for_tuple_row


@dataclass(frozen=True)
class CollectionAttributes:
    """One catalog collection row.

    ``collection_id`` is ``None`` until the engine carries the surrogate key
    (RDR-229 P3.1), and for a name that is about to be minted. ``label`` is the
    registered name; until the engine sends a separate label field it is the
    row's ``name``.
    """

    collection_id: int | None
    label: str
    content_type: str
    owner_id: str
    embedding_model: str
    model_version: int
    dimension: int | None
    lifecycle_state: str | None

    @classmethod
    def from_for_tuple(
        cls, row: dict, *, content_type: str, owner_id: str, embedding_model: str,
    ) -> "CollectionAttributes":
        """The attributes of a ``/collections/for_tuple`` row for the requested tuple.

        Raises ``ValueError`` when the row is for another tuple or carries a
        malformed version (:func:`version_from_for_tuple_row`), or has no name.
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
