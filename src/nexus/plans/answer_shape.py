"""Answer-shape classification for ``nx_answer`` results (nexus-90gyo).

One deterministic classifier shared by the WRITE side (``nx_answer``'s
plan path: the envelope field, the success/failure record, and the
RDR-084 plan-grow gate) and the READ side (``nx answer-runs``'s
degenerate split), so both agree on what counts as an answer.

Origin: the 2026-09-06 live probe (nexus-zy0kj). A grown plan of six
retrieval steps returned the ``store_get_many`` JSON payload as the
answer; a grown compare plan terminated at ``extract`` and returned bare
extractions. Both were recorded executed-ok and both were grown as
successes, so the library compounded non-answers that then matched
paraphrases at zero cost. Nothing in the envelope told the caller.

Every rule here is anchored to a code-templated shape, never to prose
heuristics: the retrieval and operator schemas' terminal keys (``ids``/
``tumblers`` from ``search``, ``contents`` from ``store_get_many``,
``extractions`` from ``operator_extract``, ``ranked`` from
``operator_rank``, and so on) and the ``query()`` renderer's listing
header. A synthesized answer cannot trip any of them.

Known limit (code review, 2026-09-06): the payload rule is a key-SUBSET
test against each operator's declared schema keys. The schemas do not set
``additionalProperties: false``, so a dispatch response carrying an extra
top-level key is not recognised as that payload and classifies by the
remaining rules (a false negative, never a false positive).
"""

from __future__ import annotations

import json
import re
from enum import Enum

__all__ = [
    "AnswerShape",
    "NON_ANSWER_SHAPES",
    "classify_answer_shape",
    "render_non_answer_notice",
]


class AnswerShape(str, Enum):
    """What ``final_text`` structurally IS.

    ``ANSWERED`` is the only shape a caller should read as prose. The rest
    are non-answers the tool can name without a model call.
    """

    #: A terminal operator step ran and produced text that is not one of
    #: the raw payload shapes below.
    ANSWERED = "answered"
    #: ``final_text`` is a retrieval payload: the structured ``search`` /
    #: ``query`` / ``traverse`` result (``ids``/``tumblers``/``distances``/
    #: ``collections``...). The plan's last step fetched and nothing ever
    #: reduced the evidence (plan 488 class, search-terminal variant).
    RETRIEVAL_ONLY = "retrieval_only"
    #: ``final_text`` is a ``store_get_many`` payload (``{"contents": [...]}``).
    HYDRATION_DUMP = "hydration_dump"
    #: ``final_text`` is a bare ``operator_extract`` payload
    #: (``{"extractions": [...]}``): fields pulled per item, never composed
    #: into an answer (plan 487 class).
    EXTRACTIONS_ONLY = "extractions_only"
    #: ``final_text`` is a bare ``operator_rank`` payload (``{"ranked": [...]}``).
    RANKING_ONLY = "ranking_only"
    #: ``final_text`` is a bare structured payload of one of the other
    #: non-prose operators: ``filter`` (``items``/``rationale``), ``check``
    #: (``ok``/``evidence``), ``verify`` (``verified``/``reason``/
    #: ``citations``), ``groupby`` (``groups``). A plan terminating in any
    #: of these has partitioned or judged the evidence, not answered.
    OPERATOR_PAYLOAD = "operator_payload"
    #: A ``query()`` document listing (``Found N documents (from ...``), the
    #: single-step reroute's renderer header — nexus-x79ne's
    #: ``query_listing_reroute`` class, named here so both sides share one
    #: vocabulary.
    LISTING = "listing"
    #: Empty or whitespace-only ``final_text``.
    EMPTY = "empty"


#: Every shape that is not an answer. ``nx_answer`` records these as
#: ``success=False`` and never grows a plan from them; ``nx answer-runs``
#: diverts rows carrying them into ``degenerate``.
NON_ANSWER_SHAPES: frozenset[AnswerShape] = frozenset({
    AnswerShape.RETRIEVAL_ONLY,
    AnswerShape.HYDRATION_DUMP,
    AnswerShape.EXTRACTIONS_ONLY,
    AnswerShape.RANKING_ONLY,
    AnswerShape.OPERATOR_PAYLOAD,
    AnswerShape.LISTING,
    AnswerShape.EMPTY,
})

#: The ``query()`` plain-corpus renderer's line-1 header, verbatim
#: (mirrors ``commands/answer_runs._QUERY_LISTING_REROUTE_HEADER_RE``).
_LISTING_HEADER_RE = re.compile(r"^Found \d+ documents \(from ")

#: Raw operator/retrieval payload key -> shape. Matched only when the
#: parsed JSON is a dict whose keys are a subset of the payload's own
#: schema keys, so a synthesized answer that happens to be JSON with one
#: of these words inside it does not trip the rule.
_PAYLOAD_SHAPES: tuple[tuple[frozenset[str], AnswerShape], ...] = (
    # ``search``/``query`` structured result and ``traverse``'s result:
    # every key a retrieval step can emit (core.py's structured branches).
    # ``also_in`` joined both query() envelopes at GH #1524 (nexus-20uv3);
    # a key missing here misclassifies every query()-terminal plan as
    # ANSWERED (review of that landing, Critical) -- test_answer_shape_
    # matches_the_real_query_envelope pins the two against each other.
    (frozenset({"ids", "tumblers", "distances", "collections",
                "chunk_collections", "chunk_text_hash", "also_in"}), AnswerShape.RETRIEVAL_ONLY),
    (frozenset({"contents", "missing", "section_types"}), AnswerShape.HYDRATION_DUMP),
    (frozenset({"extractions"}), AnswerShape.EXTRACTIONS_ONLY),
    (frozenset({"ranked"}), AnswerShape.RANKING_ONLY),
    # The remaining structured operators (``nexus.mcp.operator_requests``
    # build_filter/check/verify/groupby_request schemas, verbatim keys).
    (frozenset({"items", "rationale"}), AnswerShape.OPERATOR_PAYLOAD),
    (frozenset({"ok", "evidence"}), AnswerShape.OPERATOR_PAYLOAD),
    (frozenset({"verified", "reason", "citations"}), AnswerShape.OPERATOR_PAYLOAD),
    (frozenset({"groups"}), AnswerShape.OPERATOR_PAYLOAD),
)


def _payload_shape(text: str) -> AnswerShape | None:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    keys = frozenset(parsed.keys())
    for schema_keys, shape in _PAYLOAD_SHAPES:
        # Every key must belong to the payload's own schema, and at least
        # one must be present: nothing outside the schema, nothing empty.
        if keys <= schema_keys:
            return shape
    return None




def classify_answer_shape(final_text: str | None) -> AnswerShape:
    """Classify *final_text* by what the caller actually received.

    Text-only on purpose. An earlier cut also refused any plan with no
    known operator step; that called a plan naming an unknown tool, or a
    fixture whose search step returned prose, a non-answer, which is a
    refusal of a real answer. The payload table already covers every
    fetch-terminal shape (a search/query/traverse result, a hydration
    payload), so the plan's steps add nothing the text does not say.

    Order matters and is deliberate:

    1. ``EMPTY`` — nothing to read.
    2. Payload shapes — the text IS a raw tool payload (retrieval result,
       hydration, extractions, ranking, filter/check/verify/groupby).
    3. ``LISTING`` — the ``query()`` renderer header.
    4. ``ANSWERED`` otherwise.
    """
    text = final_text or ""
    if not text.strip():
        return AnswerShape.EMPTY
    payload = _payload_shape(text)
    if payload is not None:
        return payload
    if _LISTING_HEADER_RE.match(text) is not None:
        return AnswerShape.LISTING
    return AnswerShape.ANSWERED


#: Text-mode notice prefix for a non-answer. ``nx answer-runs`` keys on
#: this prefix on the read side (a code-templated marker, the same
#: convention as the budget-exhausted and continuation markers), so keep
#: it stable.
NON_ANSWER_NOTICE_PREFIX: str = "[non-answer:"


#: Max chunk references listed in the text-mode notice.
NON_ANSWER_MAX_CHUNK_REFS: int = 10


def render_non_answer_notice(
    shape: AnswerShape,
    *,
    plan_id: int,
    step_count: int,
    chunk_refs: list[str] | None = None,
) -> str:
    """Text notice explaining why there is no prose answer.

    The raw payload is NOT echoed. What the caller does get, in BOTH
    modes, is the list of retrieved chunk references (``collection
    chash:<hex>``, capped at :data:`NON_ANSWER_MAX_CHUNK_REFS`) so a
    text-mode caller, who never sees the structured envelope's ``chunks``
    list, can still hydrate what was found rather than being pointed at
    something it does not receive (substantive critique, 2026-09-06).
    """
    head = (
        f"{NON_ANSWER_NOTICE_PREFIX} {shape.value}] The plan (plan_id={plan_id}, "
        f"{step_count} steps) retrieved evidence but produced no synthesized "
        "answer. Nothing was reduced."
    )
    refs = [r for r in (chunk_refs or []) if r]
    if not refs:
        return f"{head} Use search/query directly."
    shown = refs[:NON_ANSWER_MAX_CHUNK_REFS]
    more = len(refs) - len(shown)
    lines = [head, f"Retrieved {len(refs)} chunk(s); hydrate with store_get_many:"]
    lines.extend(f"  {r}" for r in shown)
    if more > 0:
        lines.append(f"  ... and {more} more (structured=True carries the full list)")
    return "\n".join(lines)
