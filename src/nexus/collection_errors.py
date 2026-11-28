# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection errors the CLI boundary renders cleanly.

Kept apart from ``nexus.corpus`` so ``nexus.cli`` can catch them without
importing the corpus module on every invocation.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
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

    A collection renamed away from the label by the one-row rename is not freed of it while a name-keyed table still
    holds a row under that label for another collection id; the label frees when the engine drops the name columns
    (RDR-229 P4.1). A register, a supersede's successor, a rename's target
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
                f"(`nx collection rename <its current name> {shlex.quote(label)}`; it is the collection that was "
                f"named {label!r} before, and no command lists former names, so find it by what it holds in "
                f"`nx collection list`)")
        advice = f"Pick another label, or {back}." if user_chooses_label else f"{back[0].upper()}{back[1:]}."
        super().__init__(f"{self.explanation} Nothing was written. {advice}")

    @property
    def explanation(self) -> str:
        return (f"the collection label {self.label!r} is reserved: a collection that was renamed away from it still "
                f"has rows that carry it, and the label can be used again once the engine drops the name columns that hold them.")

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


@dataclass(frozen=True)
class RefusedChunkKey:
    """One entry of the engine's ``refused`` list: a metadata ``key``, its ``cls`` (DOCUMENT, POSITION, LEGACY or
    UNKNOWN; empty when the engine sent none) and the ``home`` where the value lives instead (empty when none)."""

    key: str
    cls: str = ""
    home: str = ""

    def render(self) -> str:
        detail = ", ".join(part for part in (self.cls, f"belongs in: {self.home}" if self.home else "") if part)
        return f"{self.key!r} ({detail})" if detail else repr(self.key)


class ChunkMetadataKeyRefusedError(RuntimeError):
    """The engine refused a chunk write because its metadata names a key a chunk row does not carry
    (422 ``chunk_metadata_key_refused``, RDR-236 Phase 4, nexus-6pfgb.66).

    Answered on ``upsert-chunks``, ``update-metadata`` and the combined manifest writes (``append``, ``append_many``,
    ``write_many``) for a collection outside the ``quarantine-`` and ``taxonomy__`` prefixes. It is a defect in the
    writer, which put a document or position field (or an unknown one) on a chunk, so resending can only repeat it:
    never retried, never turned into a registration. Not a ``VectorServiceError`` on purpose, as with
    :class:`SupersededCollectionWriteError`, so no degrade path or retry ladder treats it as a transient engine failure.

    Exposes ``refused`` (a tuple of :class:`RefusedChunkKey`; empty when the body named none), ``route``,
    ``collection``, ``collection_id``, ``refused_chunks`` and ``requested_count`` (each ``None`` when the engine
    did not send it) and ``engine_message`` (the engine's own prose).
    """

    def __init__(
        self, *, refused: tuple[RefusedChunkKey, ...] = (), route: str | None = None, collection: str | None = None,
        collection_id: int | None = None, refused_chunks: int | None = None, requested_count: int | None = None,
        engine_message: str | None = None,
    ) -> None:
        self.refused = refused
        self.route = route
        self.collection = collection
        self.collection_id = collection_id
        self.refused_chunks = refused_chunks
        self.requested_count = requested_count
        self.engine_message = engine_message
        where = " on ".join(part for part in (
            f"collection {collection!r}" if collection else "", route or "") if part)
        head = "the engine refused a chunk write: its metadata names keys a chunk row does not carry"
        if where:
            head += f" ({where})"
        if refused:
            keys = "\n".join(f"  {k.render()}" for k in refused)
            body = f"{head}:\n{keys}"
        else:
            body = f"{head}. {engine_message}" if engine_message else f"{head}."
        if refused_chunks is not None and requested_count is not None:
            body += f"\n{refused_chunks} of {requested_count} chunks carried one."
        super().__init__(body + "\nThe writer must stop sending these keys; the same write is refused again, so it is not retried.")


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def chunk_key_refused_error_from_body(status: int | None, body: Any) -> ChunkMetadataKeyRefusedError | None:
    """The typed error for the engine's ``chunk_metadata_key_refused`` body, else None.

    Keyed on the body's ``reason`` (the engine's stable discriminator) and read defensively: a 4xx or an absent
    status is accepted, ``refused`` is optional and a malformed entry is skipped, and a body with no usable
    ``refused`` falls back to the engine's ``error`` prose. Never raises."""
    from nexus.db.engine_reasons import CHUNK_METADATA_KEY_REFUSED_REASON, error_reason  # noqa: PLC0415 — deferred: this module stays import-free for the CLI boundary

    if status is not None and not 400 <= status < 500:
        return None
    if not isinstance(body, dict) or error_reason(body) != CHUNK_METADATA_KEY_REFUSED_REASON:
        return None
    raw = body.get("refused")
    refused = tuple(
        RefusedChunkKey(key=e["key"], cls=_opt_str(e.get("class")) or "", home=_opt_str(e.get("home")) or "")
        for e in (raw if isinstance(raw, list) else ())
        if isinstance(e, dict) and isinstance(e.get("key"), str) and e["key"]
    )
    return ChunkMetadataKeyRefusedError(
        refused=refused, route=_opt_str(body.get("route")), collection=_opt_str(body.get("collection")),
        collection_id=_opt_int(body.get("collection_id")), refused_chunks=_opt_int(body.get("refused_chunks")),
        requested_count=_opt_int(body.get("requested_count")), engine_message=_opt_str(body.get("error")),
    )
