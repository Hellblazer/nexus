# SPDX-License-Identifier: AGPL-3.0-or-later
"""Witness Schema v1: the host-bound result of ``operator_check`` / ``operator_verify``.

Spec of record: RDR-088 Amendment Log 2026-11-01, § Witness Schema v1 (RDR-189 P1,
nexus-z1d0l.1.2). The model emits ``{item_id, quote, role}`` items and nothing else
about identity. The host holds one :class:`EvidenceRecord` per item it showed the model
and, after the operator returns, :func:`bind` tests each quote against its own record
and writes ``state``, ``span`` and the source fields. The host is the only writer of
``tumbler``, ``chash`` and ``collection``; a value the model puts there is never read.

Pure: no I/O, no catalog, no dispatch. Imported by ``nexus.mcp.core`` (direct calls),
``nexus.plans.runner`` (records from a retrieval step) and ``nexus.plans.bundle``
callers, so it imports nothing from them.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION: int = 1

#: The item states a v1 payload can carry. A witness is valid only in ``bound``.
STATES: tuple[str, ...] = ("bound", "unattributed", "unresolved", "ambiguous")

_ROLES = ("supports", "contradicts", "neutral")

#: One leading facet tag (``[f2]``, any case, optional whitespace after), the coverage
#: ledger's quote convention. Removed before the containment test, kept in the returned quote.
_FACET_TAG = re.compile(r"^\s*\[[fF]\d+\]\s*")


@dataclass(frozen=True)
class EvidenceRecord:
    """One item the host showed the model: its id, the body text it read, and where the
    body came from. Any source field may be ``None`` (a caller-supplied item has none).
    ``text`` never carries a ``[source: ...]`` note, so a quote of the note never binds."""

    item_id: str
    text: str
    tumbler: str | None = None
    chash: str | None = None
    collection: str | None = None


# ── quote / span normalization ──────────────────────────────────────────────────────────


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """``(normalized, mapping)``: *text* with each code point NFKC-normalized on its own,
    every whitespace run collapsed to one space, and leading/trailing whitespace dropped.
    ``mapping[i]`` is the index in *text* of the code point that produced ``normalized[i]``
    (a collapsed whitespace run maps to its first code point), so a match in the normalized
    text gives an exact range in the original. Case is not folded, and composition across
    code points is not attempted: a precomposed accent and its decomposed spelling differ,
    which errs toward review, never toward a false witness."""
    out: list[str] = []
    mapping: list[int] = []
    pending_space: int | None = None
    for idx, ch in enumerate(text):
        for nch in unicodedata.normalize("NFKC", ch):
            if nch.isspace():
                if out and pending_space is None:
                    pending_space = idx
                continue
            if pending_space is not None:
                out.append(" ")
                mapping.append(pending_space)
                pending_space = None
            out.append(nch)
            mapping.append(idx)
    return "".join(out), mapping


def _locate(quote: Any, text: str) -> dict[str, int] | None:
    """The half-open code-point range of the first occurrence of *quote* in *text* after
    normalization, or ``None`` (an empty or non-string quote never matches)."""
    if not isinstance(quote, str):
        return None
    body = _FACET_TAG.sub("", quote, count=1)
    needle, _ = normalize_with_map(body)
    if not needle:
        return None
    hay, mapping = normalize_with_map(text)
    at = hay.find(needle)
    if at < 0:
        return None
    return {"start": mapping[at], "end": mapping[at + len(needle) - 1] + 1}


# ── records from a caller's arguments ───────────────────────────────────────────────────


def records_from_check_items(items: str) -> list[EvidenceRecord]:
    """The records for a DIRECT ``operator_check`` call. A JSON array whose every element is
    an object with a string ``id`` and a string ``text`` (ids unique) gives one record per
    object with the caller's id. Anything else gives one record ``e1`` holding the whole
    string. Source fields are ``None``: a caller-supplied item has no known source."""
    try:
        parsed = json.loads(items) if isinstance(items, str) else None
    except ValueError:
        parsed = None
    if isinstance(parsed, list) and parsed and all(
        isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("text"), str) for p in parsed
    ):
        ids = [p["id"] for p in parsed]
        if len(set(ids)) == len(ids):
            return [EvidenceRecord(p["id"], p["text"]) for p in parsed]
    return [EvidenceRecord("e1", items if isinstance(items, str) else str(items))]


def records_from_verify_evidence(evidence: str) -> list[EvidenceRecord]:
    """The one record ``e1`` for a direct ``operator_verify`` call. ``evidence`` is plain text;
    it is never sniffed for JSON, so a JSON document stays one document."""
    return [EvidenceRecord("e1", evidence if isinstance(evidence, str) else str(evidence))]


def shown_items(records: Sequence[EvidenceRecord]) -> str:
    """What the model is shown for *records*: ``[{"id": ..., "text": ...}, ...]`` and nothing
    else from the record (the coverage ledger's ``_pack`` shape)."""
    return json.dumps([{"id": r.item_id, "text": r.text} for r in records], ensure_ascii=False)


def shown_check_items(items: str) -> str:
    """The ``items`` text a DIRECT check prompt carries. The caller's own id/text objects go
    in as they are; any other input is shown as the record ``e1`` so the model has an id to cite."""
    recs = records_from_check_items(items)
    if len(recs) == 1 and recs[0].item_id == "e1" and recs[0].text == items:
        return shown_items(recs)
    return items


# ── deferral: whoever holds the records binds, once ─────────────────────────────────────

_DEFERRED: contextvars.ContextVar[bool] = contextvars.ContextVar("nexus_witness_binding_deferred", default=False)


def binding_deferred() -> bool:
    """True inside :func:`defer_binding`: the operator tool returns the model's raw payload
    because its caller (the plan runner) holds the records and binds."""
    return _DEFERRED.get()


@contextlib.contextmanager
def defer_binding() -> Iterator[None]:
    token = _DEFERRED.set(True)
    try:
        yield
    finally:
        _DEFERRED.reset(token)


# ── the binding ─────────────────────────────────────────────────────────────────────────

_OWNED = ("schema_version", "rejected", "uncovered", "requires_review", "citations")


def bind(raw: Any, records: Sequence[EvidenceRecord] | None, operator: str) -> Any:
    """The v1 payload for *raw*, the model's ``check`` or ``verify`` reply.

    *records* are the items the model was shown. ``None`` means no records exist (a bundled
    terminal step whose input was an earlier step's model output): every item is then
    ``unresolved`` and nothing is rejected. A reply that is not a mapping with an
    ``evidence`` list is returned as it came (it is not the check/verify shape; nothing is
    fabricated for it). The host never changes the model's verdict (``ok`` / ``verified``).
    """
    if not isinstance(raw, Mapping) or not isinstance(raw.get("evidence"), list):
        return raw
    verb = operator.removeprefix("operator_")
    by_id = {r.item_id: r for r in records} if records is not None else {}
    items: list[dict[str, Any]] = []
    rejected: list[str] = []
    for entry in raw["evidence"]:
        if not isinstance(entry, Mapping):
            continue
        item_id = entry.get("item_id")
        quote = entry.get("quote")
        role = entry.get("role")
        if records is None:
            items.append(_item(item_id, quote, role, "unresolved", None, None))
            continue
        rec = by_id.get(item_id) if isinstance(item_id, str) else None
        if rec is None:
            rejected.append(str(item_id))
            continue
        span = _locate(quote, rec.text)
        if span is not None:
            # Contained in its own record. Rule 3/4: only a named owning document binds.
            state = "bound" if rec.tumbler else ("ambiguous" if rec.chash else "unattributed")
            items.append(_item(item_id, quote, role, state, span, rec))
            continue
        elsewhere = any(o is not rec and _locate(quote, o.text) is not None for o in (records or ()))
        items.append(_item(item_id, quote, role, "ambiguous" if elsewhere else "unresolved", None, None))

    covered = {i["item_id"] for i in items if i["role"] in ("supports", "contradicts")}
    uncovered = (
        [r.item_id for r in records if r.item_id not in covered] if (verb == "check" and records is not None) else []
    )
    verdict = raw.get("ok") if verb == "check" else raw.get("verified")
    bound_supports = {i["item_id"] for i in items if i["state"] == "bound" and i["role"] == "supports"}
    if verb == "check" and verdict is True and records is not None:
        unsupported = any(r.item_id not in bound_supports for r in records)  # ok=true needs every input item supported
    else:
        unsupported = verdict is True and not bound_supports
    review = records is None or any(i["state"] != "bound" for i in items) or bool(rejected) or bool(uncovered) or unsupported

    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    if verb == "check":
        out["ok"] = raw.get("ok")
        out["evidence"] = items
        out["rejected"] = rejected
        out["uncovered"] = uncovered
        out["requires_review"] = review
    else:
        out["verified"] = raw.get("verified")
        out["reason"] = raw.get("reason")
        out["evidence"] = items
        out["rejected"] = rejected
        out["requires_review"] = review
        out["citations"] = [i["quote"] for i in items if i["state"] in ("bound", "unattributed") and isinstance(i["quote"], str)]
    # Keys the host does not own (the runner merges truncation metadata later; a tool may add its own).
    for k, v in raw.items():
        if k not in out and k not in _OWNED and k != "evidence":
            out[k] = v
    return out


def _item(item_id: Any, quote: Any, role: Any, state: str, span: dict[str, int] | None, rec: EvidenceRecord | None) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "quote": quote,
        "role": role,
        "state": state,
        "span": span,
        "tumbler": rec.tumbler if rec is not None else None,
        "chash": rec.chash if rec is not None else None,
        "collection": rec.collection if rec is not None else None,
    }
