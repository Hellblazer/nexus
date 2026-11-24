# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection errors the CLI boundary renders cleanly.

Kept apart from ``nexus.corpus`` so ``nexus.cli`` can catch them without
importing the corpus module on every invocation.
"""
from __future__ import annotations

import shlex
from typing import Any


class SupersededCollectionWriteError(RuntimeError):
    """A write named a collection the catalog has retired (``superseded_by``
    is set) and nothing asked for it back (nexus-wwuzp).

    Raised when a cached registration is re-validated
    (``nexus.corpus.ensure_collection_registered``), when a cold implicit
    write finds the name already retired, from the "not registered" retry in
    ``write_with_registration_retry``, and by ``nx index repo`` when the name
    it is about to index into is a tombstone. The ``/collections/upsert`` a
    registration issues clears ``superseded_by`` unconditionally, so
    proceeding would silently un-retire the collection; refusing names the
    successor instead. Exposes ``name`` and ``successor``.

    Also raised from the engine's own refusal (nexus-mp8ys, nexus-i15oc): a write
    that reaches the engine against a retired name is answered 409
    ``collection_superseded`` naming the successor, which
    :func:`superseded_write_error_from_body` turns into this error. That covers
    chunk writes (combined write, append), manifest and centroid writes, and,
    since nexus-i15oc, the taxonomy, aspect, highlight and aspect-queue writes. It
    reaches the process that did not perform the rename and whose registration
    cache still calls the name live. The ``/import/*`` verbs and a rename's
    re-home onto its target are not refused (a fidelity import; a rename onto an
    empty tombstone revives it).
    """

    def __init__(self, name: str, successor: str, *, remedy: str | None = None) -> None:
        self.name = name
        self.successor = successor
        # A caller that is not itself writing to a name of its choosing (the
        # indexer takes it from the repo's registry) says what to do instead;
        # ``{name}`` and ``{successor}`` in *remedy* are filled in, and ``{quoted_name}`` and ``{quoted_successor}``
        # with :func:`shlex.quote` for a command the remedy prints (nexus-88aj0).
        advice = (
            remedy.format(name=name, successor=successor, quoted_name=shlex.quote(name),
                          quoted_successor=shlex.quote(successor))
            if remedy is not None
            else f"Write to {successor!r} instead."
        )
        super().__init__(
            f"collection {name!r} was superseded by {successor!r}, so a write to "
            f"{name!r} is refused. {advice}"
        )


def superseded_write_error_from_body(
    status: int | None, body: Any,
) -> SupersededCollectionWriteError | None:
    """The typed error for the engine's 409 ``collection_superseded`` body, else None.

    Keyed on the status and the engine's ``reason`` (the body repeats the value
    in ``error`` for a reader of that key). A body that names no collection or
    no successor is not turned into the typed error: the caller keeps its
    generic 409, which is no more retryable.
    """
    from nexus.db.engine_reasons import COLLECTION_SUPERSEDED_REASON, error_reason  # noqa: PLC0415 — deferred: this module stays import-free for the CLI boundary

    if status != 409 or not isinstance(body, dict):
        return None
    if error_reason(body) != COLLECTION_SUPERSEDED_REASON:
        return None
    name, successor = body.get("collection"), body.get("superseded_by")
    if not (isinstance(name, str) and name and isinstance(successor, str) and successor):
        return None
    return SupersededCollectionWriteError(name, successor)


class LabelReservedError(RuntimeError):
    """The engine refused a label because it is reserved (409 ``label_reserved``, RDR-229 P3.7, engine 0.1.202).

    A collection renamed away from the label by the one-row rename still has rows that carry it, under another
    collection id, until a later engine release relabels them. A register, a supersede's successor, a rename's target
    and an import of that label are refused before anything is written, so the message is the same for each: the
    label, why, and the way out. Never retried. Exposes ``label`` and ``explanation`` (the first sentence alone, for a
    caller whose own message says what else happened: a delete that already ran, documents that already moved).

    ``user_chooses_label`` is True where the person typed the label (a rename, a register by name): they may pick
    another. It is False where the label is not theirs to change (``nx index repo``'s collection, a re-embed's target):
    renaming the other collection back is the only way out."""

    def __init__(self, label: str, *, user_chooses_label: bool = True) -> None:
        self.label = label
        self.user_chooses_label = user_chooses_label
        back = (f"rename that collection back to {label!r} "
                f"(`nx collection rename <its current name> {shlex.quote(label)}`)")
        advice = f"Pick another label, or {back}." if user_chooses_label else f"{back[0].upper()}{back[1:]}."
        super().__init__(f"{self.explanation} Nothing was written. {advice}")

    @property
    def explanation(self) -> str:
        return (f"the collection label {self.label!r} is reserved: a collection that was renamed away from it still "
                f"has rows that carry it, and the label can be used again once a later engine release relabels those rows.")

    def with_fixed_label(self) -> "LabelReservedError":
        """The same refusal worded for a caller whose label is not the person's to change."""
        return LabelReservedError(self.label, user_chooses_label=False)


def label_reserved_error_from_body(
    status: int | None, body: Any, *, user_chooses_label: bool = True,
) -> LabelReservedError | None:
    """The typed error for the engine's 409 ``label_reserved`` body, else None.

    Keyed on the status and the body's ``reason``; a body that names no label is not turned into the typed error (the
    caller keeps its generic 409, which is no more retryable)."""
    from nexus.db.engine_reasons import LABEL_RESERVED_REASON, error_reason  # noqa: PLC0415 — deferred: this module stays import-free for the CLI boundary

    if status != 409 or not isinstance(body, dict) or error_reason(body) != LABEL_RESERVED_REASON:
        return None
    label = body.get("label")
    return LabelReservedError(label, user_chooses_label=user_chooses_label) if isinstance(label, str) and label else None


class RetiredNoSuccessorError(RuntimeError):
    """The engine refused a request because it names a collection that was retired and has no live successor (409
    ``collection_superseded`` with no ``superseded_by``, RDR-229 R7, engine 0.1.202+).

    A retired collection whose successor was deleted names none. Its id was always refused; from this engine a request
    that names it by LABEL is refused too, where it used to be served from the tombstone with 200. There is nothing live
    under the label, so nothing can be read from or written to it; the way out is to use a live collection. Never
    retried. Exposes ``name`` and ``collection_id`` (None when the engine named none)."""

    def __init__(self, name: str, collection_id: int | None = None) -> None:
        self.name = name
        self.collection_id = collection_id
        super().__init__(retired_no_successor_message(name))


def retired_no_successor_message(name: str) -> str:
    """The one message for a collection retired with no successor, whichever route or command met it."""
    return (
        f"collection {name!r} was retired and has no successor, so there is nothing to read or write under that label. "
        f"`nx collection list` shows the live collections; to bring its documents back, index their files into a live "
        f"collection (`nx index md <path>` or `nx index pdf <path>`). `nx index repo` is refused while the repository's "
        f"own collection is this retired one. Nothing was changed."
    )


def retired_no_successor_error_from_body(status: int | None, body: Any) -> RetiredNoSuccessorError | None:
    """The typed error for the engine's 409 ``collection_superseded`` body that names a retired collection and no
    successor, else None.

    Keyed on the status, ``reason == "collection_superseded"``, a non-empty ``collection``, and NO ``superseded_by`` and
    NO ``successor_collection_id`` (absent, null or empty): a body that names a successor is
    :func:`superseded_write_error_from_body`'s, and every other 409 is left to its own handler."""
    from nexus.db.engine_reasons import COLLECTION_SUPERSEDED_REASON, error_reason  # noqa: PLC0415 — deferred: this module stays import-free for the CLI boundary

    if status != 409 or not isinstance(body, dict) or error_reason(body) != COLLECTION_SUPERSEDED_REASON:
        return None
    if body.get("superseded_by") or body.get("successor_collection_id") not in (None, "", 0):
        return None
    name = body.get("collection")
    if not (isinstance(name, str) and name):
        return None
    cid = body.get("collection_id")
    return RetiredNoSuccessorError(name, cid if isinstance(cid, int) and not isinstance(cid, bool) else None)
