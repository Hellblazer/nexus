# SPDX-License-Identifier: AGPL-3.0-or-later
"""The write side of ``--sensitive`` (RDR-243 Technical Design items 2, 3 and 8; nexus-rfvyy.6.9).

Every ``--sensitive`` write path (``nx index pdf``, ``nx index md``, ``nx dt index``, ``nx dt
capture``, ``nx store put`` and MCP ``store_put``) goes through here for the three things they share:

* :func:`require_sensitive_engine` refuses, before any register call, an engine that cannot hold
  the flag (an engine without the column would drop it silently).
* :func:`raise_flag` moves the flag on a document that already exists. A fresh document carries it
  in its register request; a document the verb resolves instead of minting does not register, so
  the audited route (``set_sensitivity``) raises the flag before any chunk is written.
* The exceptions the verbs turn into a clean message.

Imports no heavy module at load: ``nexus.catalog.http_catalog_client`` is loaded when a flagged
write runs.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "BGE_TOKEN", "ExternalEmbedderRefusedError", "SENSITIVE_REASON", "SensitiveEnrichRefusedError",
    "SensitiveFlagNotSetError", "ack_help", "bge_sibling", "catalog_sensitive", "cloud_disclosure",
    "cloud_refusal_text", "external_legs_gate", "is_external_embedder", "raise_flag", "refuse_external_existing",
    "refuse_external_existing_note", "require_sensitive_engine", "resolve_sensitive_collection", "sensitive_help", "route_for_index",
    "sensitive_write_kwargs",
]

#: The audit reason recorded when a verb raises the flag on an existing document.
SENSITIVE_REASON = "--sensitive write"


def sensitive_help() -> str:
    """The ``--sensitive`` option's help text: generated from the registry rows enforced in this release."""
    from nexus.tables import llm_surfaces as ls  # noqa: PLC0415 — deferred: read at command definition, not at module load

    return ls.render_sensitive_help(ls.RELEASE_ENFORCED_PHASE)


def _bge_token() -> str:
    from nexus.db.local_ef import _MODEL_TOKENS, _TIER1_MODEL  # noqa: PLC0415 — deferred: leaf module, read once

    return _MODEL_TOKENS[_TIER1_MODEL]


#: The bge collection-name token a sensitive document is routed to in local mode, read from the one table the
#: write path reads, so the sibling name cannot drift from the collection the engine embeds.
BGE_TOKEN: str = _bge_token()


def ack_help() -> str:
    """Help for ``--ack-external-embedder`` (RDR-243 Q3). Names the bge sibling of every prefix and the cloud rule."""
    return (
        "With --sensitive: accept that this document's text is sent to an external embedder. Without it, a "
        "sensitive document in local mode is written to the bge collection of the same subject, which "
        "embeds on this machine: knowledge__S, docs__S, rdr__S and code__S go to "
        f"<prefix>__S__{BGE_TOKEN}__v1. A sensitive document already registered in a Voyage collection is "
        "not moved; the run refuses (delete the document and index it again, or pass this flag to index "
        "it in place). In cloud mode every collection embeds through Voyage and the tenant stores the "
        "text, so --sensitive refuses without this flag; with it, a one-line disclosure names the endpoint "
        "and the embedder. MCP store_put has no way to pass it, so it refuses in cloud mode."
    )


class SensitiveFlagNotSetError(RuntimeError):
    """A ``--sensitive`` write could not put the flag on the document, so nothing was written to it."""


class ExternalEmbedderRefusedError(click.ClickException, RuntimeError):
    """A ``--sensitive`` write would send the document to an external embedder without the acknowledgement.

    Both a ``ClickException`` (a verb prints it and exits non-zero) and a ``RuntimeError`` (a batch loop
    that survives a per-record ``RuntimeError`` survives this one, and MCP words it with ``_fail``)."""


class SensitiveEnrichRefusedError(click.ClickException, RuntimeError):
    """``--enrich`` on a sensitive document: its title would go to Semantic Scholar or CrossRef (rows 7 and 8).

    A ``ClickException`` (a verb prints it) and a ``RuntimeError`` (a ``--dir`` batch isolates it to the file)."""


def _parse_collection(collection_name: str) -> Any:
    """The :class:`~nexus.catalog.collection_name.CollectionName` of a name, or ``None`` when it has none
    (a legacy two-segment name, or an embedder outside the canonical sets). The value object is the one
    sanctioned parse of a candidate name; a row lookup would need the collection to be registered."""
    from nexus.catalog.collection_name import CollectionName  # noqa: PLC0415 — deferred: leaf value object

    try:
        return CollectionName.parse(collection_name)
    except ValueError:
        return None


def is_external_embedder(collection_name: str) -> bool:
    """True when *collection_name* is embedded off this machine, or when that cannot be told.

    A conformant name carries its embedder token: the local tokens (bge, MiniLM) embed here, anything
    else (Voyage) does not. A name without a token (a legacy two-segment name) is unknown, and an
    unknown embedder reads as external, never as safe.
    """
    from nexus.corpus import LOCAL_EMBEDDING_MODELS  # noqa: PLC0415 — deferred: corpus is heavy

    parsed = _parse_collection(collection_name)
    return parsed is None or parsed.embedding_model not in LOCAL_EMBEDDING_MODELS


def bge_sibling(collection_name: str) -> str | None:
    """``<prefix>__<subject>__bge-base-en-v15-768__v1`` for a conformant *collection_name*, else ``None``.

    Works for every prefix (``knowledge__``, ``docs__``, ``rdr__``, ``code__``). The version segment is
    ``v1``: the sibling is a different collection, not a newer version of the Voyage one.
    """
    from nexus.catalog.collection_name import CollectionName  # noqa: PLC0415 — deferred: leaf value object

    parsed = _parse_collection(collection_name)
    if parsed is None:
        return None
    return CollectionName(parsed.content_type, parsed.owner_id, BGE_TOKEN, 1).render()


def _cloud_endpoint() -> str:
    try:
        from urllib.parse import urlsplit  # noqa: PLC0415 — stdlib, branch-local

        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deferred: endpoint resolution reads config

        host = urlsplit(resolve_service_endpoint()[0]).hostname
        return host or "the managed service"
    except Exception:  # noqa: BLE001 — a disclosure line must never fail the write it describes
        return "the managed service"


def cloud_refusal_text(what: str) -> str:
    """Why *what* refuses in cloud mode without the acknowledgement. One sentence for the CLI and MCP alike."""
    return (
        f"{what} refused: in cloud mode every collection is embedded by Voyage AI and stored in the managed "
        f"tenant, so this document's text would leave this machine. Nothing was written. "
        f"Use local mode, where a sensitive document is embedded on this machine, or pass "
        f"--ack-external-embedder to accept the cloud tenant and Voyage."
    )


def cloud_disclosure() -> str:
    """The one-line disclosure for census row 22 (cloud tenant storage), shown with the acknowledgement."""
    return (
        f"sensitive: acknowledged. This document's chunk text and its Voyage embeddings are stored in the "
        f"managed cloud tenant at {_cloud_endpoint()} and sent to Voyage AI to be embedded. It stays out of "
        f"model-bound surfaces on this machine; the tenant and Voyage hold a copy."
    )


def route_for_index(collection: str | None, corpus: str, *, ack: bool, what: str) -> str:
    """:func:`resolve_sensitive_collection` for ``nx index pdf`` / ``md``: with no ``--collection`` the
    collection is the docs leaf of *corpus*, resolved here so the decision sees the name it will write."""
    if collection is None:
        from nexus.corpus import docs_leaf_fallback_collection_name  # noqa: PLC0415 — deferred: corpus is heavy

        collection = docs_leaf_fallback_collection_name(corpus)
    return resolve_sensitive_collection(collection, ack=ack, what=what)


def resolve_sensitive_collection(name: str, *, ack: bool, what: str) -> str:
    """Decide where a sensitive document goes, given the collection its verb resolved (RDR-243 item 8, Q3).

    * an embedder on this machine: *name* unchanged;
    * a Voyage collection with *ack*: *name* unchanged, with a one-line notice;
    * a Voyage collection without *ack*, local mode: the bge sibling of the same subject, with a notice;
      refused when the install embeds with Voyage this boot (no bge route exists) or *name* has no sibling;
    * cloud mode without *ack*: refused (every collection is Voyage there).
    """
    from nexus.config import is_local_mode, local_embed_profile  # noqa: PLC0415 — deferred: config is heavy

    if not is_external_embedder(name):
        return name
    if ack:
        if is_local_mode():
            click.echo(
                f"sensitive: acknowledged. This document's text is sent to Voyage AI to be embedded "
                f"(collection {name}).", err=True)
        return name
    if not is_local_mode():
        raise ExternalEmbedderRefusedError(cloud_refusal_text(what))
    if local_embed_profile() == "voyage":
        raise ExternalEmbedderRefusedError(
            f"{what} refused: collection {name} embeds with Voyage AI, and this install's engine embeds with "
            f"Voyage this boot, so there is no local bge collection to route a sensitive document to. "
            f"Nothing was written. Pass --ack-external-embedder to accept Voyage, or start the engine on "
            f"the bge profile.")
    sibling = bge_sibling(name)
    if sibling is None:
        raise ExternalEmbedderRefusedError(
            f"{what} refused: cannot tell which embedder collection {name} uses, and it has no bge sibling "
            f"to route to. Nothing was written. Use a conformant collection name, or pass "
            f"--ack-external-embedder.")
    click.echo(f"sensitive: {name} embeds with Voyage AI; this document goes to {sibling}, which embeds on "
               f"this machine.", err=True)
    return sibling


def refuse_external_existing(existing: object, *, ack: bool, what: str) -> None:
    """Refuse a ``--sensitive`` index of a document already registered in a Voyage collection (residual S-B).

    The register call matches a live document on its source URI or file path and writes none of its
    request fields, so routing the new chunks to the bge sibling would split the document: the catalog
    row would still name the Voyage collection and the Voyage embedding would stay. This refuses
    instead of re-routing, before any chunk or catalog write. With *ack* the run indexes in place.
    """
    if ack:
        return
    collection = getattr(existing, "physical_collection", "") or ""
    if not collection or not is_external_embedder(collection):
        return
    tumbler = getattr(existing, "tumbler", "<tumbler>")
    raise ExternalEmbedderRefusedError(
        f"{what} refused: document {tumbler} is already registered in the Voyage collection {collection}, "
        f"and a --sensitive index does not move it. Nothing was written. Either pass "
        f"--ack-external-embedder to index it in place on Voyage, or delete the document "
        f"(nx catalog delete {tumbler}) and index it again."
    )


def refuse_external_existing_note(collection: str, title: str, *, ack: bool, what: str) -> None:
    """Refuse a ``--sensitive`` note put whose title already names a live note in the Voyage *collection*.

    The store verbs route a sensitive put to the bge sibling in local mode. When the title already has a
    live note in the Voyage collection, the re-route writes a new note there and leaves the original,
    unflagged and Voyage-embedded, beside it. Same rule as :func:`refuse_external_existing`: refuse before
    anything is written; with *ack* the put goes to the Voyage collection and flags the existing note in
    place. A blank title, a local-embedder collection and the ack never ask the catalog. A catalog that
    cannot answer refuses (it cannot be told there is no twin).
    """
    if ack or not title or not is_external_embedder(collection):
        return
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: the factory pulls the catalog client

    try:
        cat = make_catalog_reader()
        try:
            existing = [e for e in cat.find_by_title_exact(title) if e.physical_collection == collection]
        finally:
            close = getattr(cat, "close", None)
            if callable(close):
                close()
    except Exception as exc:  # noqa: BLE001 — boundary: an unanswerable twin check refuses, it never guesses
        raise ExternalEmbedderRefusedError(
            f"{what} refused: could not check whether a note titled {title!r} already exists in the Voyage "
            f"collection {collection} ({type(exc).__name__}: {exc}). Nothing was written."
        ) from exc
    if existing:
        refuse_external_existing(existing[0], ack=False, what=what)


def catalog_sensitive(doc_id: str) -> bool:
    """True when the catalog row of *doc_id* is flagged sensitive, or when the catalog cannot say.

    The index paths call this after they resolved the document, so a document flagged earlier (``nx
    catalog sensitive``, or a previous ``--sensitive`` run) is protected on a run that does not pass the
    flag. A read that fails reads as sensitive: the external legs refuse rather than guess. A blank
    *doc_id* (no catalog row to ask about) is not flagged.
    """
    if not doc_id:
        return False
    from nexus.doc_sensitivity import SensitivityUnknown, is_sensitive  # noqa: PLC0415 — deferred: reads the catalog client

    try:
        return is_sensitive(doc_id)
    except SensitivityUnknown as exc:
        _log.warning("sensitive_flag_unreadable_treated_as_sensitive", doc_id=doc_id, detail=exc.detail)
        return True


#: The event each kind of skip logs (literal names, so a grep for one finds its site).
_SKIP_EVENTS: dict[str, str] = {
    "md": "sensitive_md_skipped_external_embedder",
    "prose": "sensitive_prose_skipped_external_embedder",
    "code": "sensitive_code_skipped_external_embedder",
}


class SensitiveDocs:
    """The flagged subset of one batch of catalog documents, read once (a membership test, no I/O).

    ``unknown`` is the catalog that could not answer: every non-blank id then reads as sensitive, the
    same rule :func:`catalog_sensitive` applies to a single document. A blank id has no catalog row and
    is never flagged.
    """

    __slots__ = ("ids", "unknown")

    def __init__(self, ids: frozenset[str] = frozenset(), *, unknown: bool = False) -> None:
        self.ids = ids
        self.unknown = unknown

    def __contains__(self, doc_id: object) -> bool:
        return bool(doc_id) and (self.unknown or doc_id in self.ids)


def sensitive_docs(doc_ids: Iterable[str]) -> SensitiveDocs:
    """One batched catalog read: which of *doc_ids* are flagged. A read that fails reads as all sensitive."""
    from nexus.doc_sensitivity import SensitivityUnknown, flagged  # noqa: PLC0415 — deferred: reads the catalog client

    try:
        return SensitiveDocs(frozenset(flagged(doc_ids)))
    except SensitivityUnknown as exc:
        _log.warning("sensitive_flag_unreadable_treated_as_sensitive", detail=exc.detail, batch=True)
        return SensitiveDocs(unknown=True)


def skip_sensitive_for_external(
    doc_id: str, collection: str, path: object, kind: str, known: SensitiveDocs | None = None,
) -> bool:
    """True (after a loud warning) when a repo or batch run must not embed *path* into *collection*.

    The document is flagged sensitive in the catalog (or the catalog cannot say) and *collection* embeds
    with Voyage. Such a run has no ``--ack-external-embedder``, so the document is skipped, not refused:
    ``nx index pdf --sensitive --ack-external-embedder`` is the way to accept Voyage for it. *known* is
    the batch's flag read; without it the document is read on its own. A local-embedder collection is
    never gated, and costs no read. *kind* (``md``, ``prose`` or ``code``) picks the event, ``sensitive_<kind>_skipped_external_embedder``.
    """
    if not doc_id or not is_external_embedder(collection):
        return False
    if not (doc_id in known if known is not None else catalog_sensitive(doc_id)):
        return False
    event = _SKIP_EVENTS[kind]
    _log.warning(
        event, path=str(path), doc_id=doc_id, collection=collection,
        note="a sensitive document is not written to a Voyage collection from a repo or batch run; "
             "index it with nx index pdf --sensitive --ack-external-embedder to accept Voyage")
    return True


def external_legs_gate(
    doc_id: str, collection: str, *, sensitive: bool, enrich: bool, ack: bool, what: str,
) -> bool:
    """Decide whether this index run treats the document as sensitive, and refuse its external legs if so.

    The document is sensitive when the verb said so (*sensitive*) or when its catalog row is flagged (or
    unreadable). For a sensitive document: ``--enrich`` refuses (no title to Semantic Scholar or
    CrossRef); a write into a Voyage collection refuses without *ack* when the verb did not already
    route it (the verb's own ``--sensitive`` path chose *collection* through
    :func:`resolve_sensitive_collection`); and the caller builds a local-only extractor from the return
    value. Returns that decision. Nothing has been written when this raises.
    """
    from_catalog = not sensitive and catalog_sensitive(doc_id)
    if not (sensitive or from_catalog):
        return False
    if enrich:
        raise SensitiveEnrichRefusedError(
            f"{what}: --enrich sends the title and identifiers to Semantic Scholar or CrossRef, and "
            f"document {doc_id or '<new>'} is sensitive"
            f"{' in the catalog' if from_catalog else ''}. Nothing was written. Drop --enrich."
        )
    if from_catalog:
        if not ack and is_external_embedder(collection):
            raise ExternalEmbedderRefusedError(
                f"{what} refused: document {doc_id} is sensitive in the catalog and {collection} embeds "
                f"with Voyage AI, which would send its text off this machine. Nothing was written. Pass "
                f"--sensitive --ack-external-embedder to accept Voyage, or index into a local-embedder "
                f"collection."
            )
        click.echo(f"sensitive: document {doc_id} is flagged in the catalog and is handled as a sensitive "
                   f"document: MinerU runs as a local subprocess (no MinerU server is contacted) and no "
                   f"title lookup is made.", err=True)
    return True


def require_catalog_sensitivity(cat: Any, *, what: str = "a sensitive write") -> None:
    """The refusal every ``--sensitive`` write calls (a name here so the module a verb imports is the one a pin patches)."""
    from nexus.catalog.http_catalog_client import require_catalog_sensitivity as _require  # noqa: PLC0415 — deferred: the client module is heavy

    _require(cat, what=what)


def require_sensitive_engine(what: str) -> None:
    """Refuse *what* unless the engine advertises ``catalog_sensitivity_supported``.

    Raises :class:`~nexus.catalog.http_catalog_client.CatalogSensitivityUnsupportedError` (a
    ``RuntimeError``) with "Nothing was written" in the text. Called before the register call, and
    before the T3 handle and any extraction, embedding or catalog write of the verb.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: the factory pulls the catalog client

    cat = make_catalog_reader()
    try:
        require_catalog_sensitivity(cat, what=what)
    finally:
        close = getattr(cat, "close", None)
        if callable(close):
            close()


def sensitive_write_kwargs(
    sensitive: bool, what: str, *, ack: bool = False, dry_run: bool = False, enrich: bool = False,
) -> dict[str, bool]:
    """The CLI front door of a ``--sensitive`` verb: ``{}`` when the flag is off, else the refusals and
    the keywords to splat into the indexer calls (``sensitive``, and ``ack_external_embedder`` when given).

    Order: usage errors first (``--ack-external-embedder`` means nothing without ``--sensitive``;
    ``--enrich`` sends the title to Semantic Scholar or CrossRef, census rows 7 and 8, which a
    sensitive document must not), then the cloud rule
    (every cloud collection is Voyage: refuse without *ack*, disclose with it, census row 22), then the
    engine refusal. A dry run writes nothing, so it skips the last two. The keyword rides only when
    set, so a test double or an older indexer signature is untouched when the flag is off.
    """
    if ack and not sensitive:
        raise click.UsageError("--ack-external-embedder only means something with --sensitive.")
    if not sensitive:
        return {}
    if enrich:
        raise click.UsageError(
            f"{what}: --enrich sends the title and identifiers to Semantic Scholar or CrossRef; "
            f"a sensitive document is not sent there. Drop one of the two flags."
        )
    if not dry_run:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred: config is heavy

        if not is_local_mode():
            if not ack:
                raise click.ClickException(cloud_refusal_text(what))
            click.echo(cloud_disclosure(), err=True)
        try:
            require_sensitive_engine(what)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
    return {"sensitive": True, **({"ack_external_embedder": True} if ack else {})}


def raise_flag(writer: Any, tumbler: str, *, what: str) -> None:
    """Put ``sensitive = true`` on the existing document *tumbler* through the audited route.

    A document that is already sensitive is a no-op. Any failure raises
    :class:`SensitiveFlagNotSetError`: the caller has not written a chunk yet and must not.
    """
    try:
        out = writer.set_sensitivity([tumbler], True, reason=SENSITIVE_REASON)
    except Exception as exc:  # noqa: BLE001 — boundary: any failure of the audited route means the document is not protected
        raise SensitiveFlagNotSetError(
            f"{what} refused: could not mark {tumbler} sensitive ({type(exc).__name__}: {exc}). "
            f"Nothing was written to it."
        ) from exc
    _log.debug("sensitive_flag_raised", tumbler=tumbler, updated=(out or {}).get("updated") if isinstance(out, dict) else out)
