# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured-agreement metrics for the RDR-196 .p2a operator quality proxy.

Pure functions only — no dispatch, no I/O. Each metric scores agreement
between two structured operator outputs (or one output and a synthetically
degraded copy of it) on a bounded ``[0.0, 1.0]`` scale, except
``spearman_rank_correlation`` which is the standard ``[-1.0, 1.0]`` scale.

Scope (see the FROZEN T2 title
``nexus_rdr/196-phase2-quality-proxy-REGISTERED-2026-08-21`` for the full
justification, recorded before any measurement; the mutable working
entry ``nexus_rdr/196-phase2-quality-proxy`` carries results/revisions
only): six of the ten ``operator_*`` MCP tools have output shapes
structured enough to score without an LLM judge — ``filter``, ``groupby``,
``rank``, ``extract``, ``check``, ``verify``. The other four
(``summarize``, ``aggregate``, ``compare``, ``generate``) return free text
as their substantive field and are explicitly OUT of this proxy; nothing
here defines a metric for them.

Conventions:

  * A metric never raises on well-formed-but-degenerate input (both sides
    empty, etc.) — degenerate cases resolve to a documented value rather
    than a ``ZeroDivisionError``, since a proxy that crashes on an edge
    case is worse than one that scores it conservatively.
  * String values feeding a set/bag comparison are normalized
    (``.strip().casefold()``) before comparison, so whitespace/case
    differences between two independent LLM completions don't register as
    disagreement.
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any


def exact_membership_jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity ``|a ∩ b| / |a ∪ b|`` over two id sets.

    Both empty is treated as perfect agreement (1.0) — two runs that both
    correctly find nothing to keep/select have not disagreed.
    """
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


_ID_TAG_RE = re.compile(r"\[([a-zA-Z0-9_-]+)\]")


def _extract_id_tag(item: str) -> str | None:
    """Pull the leading ``[id]`` tag out of a ranked-item string.

    ``operator_rank``'s schema returns plain strings with no id field
    (see ``src/nexus/mcp/core.py::operator_rank``), so two independent
    completions cannot be matched by string equality if either paraphrases.
    The proxy's fixed input set embeds an explicit ``[rdr-XXX]`` tag in
    every item string specifically so this regex can recover a stable
    identity regardless of paraphrasing elsewhere in the string.
    """
    m = _ID_TAG_RE.search(item)
    return m.group(1) if m else None


def spearman_rank_correlation(
    order_a: list[str], order_b: list[str],
) -> float | None:
    """Spearman rank correlation between two rankings of tagged item
    strings (see :func:`_extract_id_tag`).

    Only ids present in BOTH orderings are compared (an id one run
    dropped or a run that didn't preserve the tag is excluded rather than
    penalized twice — the untagged/dropped case degrades the metric
    honestly via the smaller n, not via a crash). Returns ``None`` when
    fewer than 2 common ids exist (correlation is undefined for n<2) —
    callers must treat ``None`` as "no verdict", never as 0.0 (0.0 reads
    as "actively anti-correlated", a materially different claim than
    "not enough data to know").
    """
    tags_a = [t for t in (_extract_id_tag(x) for x in order_a) if t is not None]
    tags_b = [t for t in (_extract_id_tag(x) for x in order_b) if t is not None]
    rank_a = {tag: i for i, tag in enumerate(tags_a)}
    rank_b = {tag: i for i, tag in enumerate(tags_b)}
    common = sorted(set(rank_a) & set(rank_b))
    n = len(common)
    if n < 2:
        return None

    ra = [rank_a[t] for t in common]
    rb = [rank_b[t] for t in common]
    # Standard Spearman-via-Pearson-on-ranks formula. ra/rb are already
    # 0-indexed positions within the COMMON subsequence (re-derived by
    # sort order below), not the raw full-list index, so a run that
    # dropped an id doesn't distort the correlation of the ids both runs
    # agree on.
    order_a_common = sorted(common, key=lambda t: rank_a[t])
    order_b_common = sorted(common, key=lambda t: rank_b[t])
    pos_a = {t: i for i, t in enumerate(order_a_common)}
    pos_b = {t: i for i, t in enumerate(order_b_common)}
    d2_sum = sum((pos_a[t] - pos_b[t]) ** 2 for t in common)
    return 1.0 - (6.0 * d2_sum) / (n * (n * n - 1))


def _normalize(value: Any) -> str:
    return str(value).strip().casefold()


def field_value_bag_f1(
    extractions_a: list[dict[str, Any]], extractions_b: list[dict[str, Any]],
) -> float:
    """Field-level F1 over the FLATTENED bag of ``(field, value)`` pairs
    across an entire ``extractions`` array.

    ``operator_extract``'s schema (``{"extractions": [{}...]}``) does not
    require an id field, so per-item correspondence between two
    independent completions is not guaranteed. Flattening both arrays
    into one multiset of ``(field, normalized value)`` pairs and scoring
    precision/recall on the multiset sidesteps needing that
    correspondence — it is a coarser signal than true per-item field F1
    (it can't detect "right fact, wrong item"), a documented
    simplification given the schema's own lack of an id.

    Both empty is treated as perfect agreement (1.0), matching
    :func:`exact_membership_jaccard`'s convention.
    """
    from collections import Counter

    def _bag(extractions: list[dict[str, Any]]) -> Counter:
        bag: Counter = Counter()
        for record in extractions:
            if not isinstance(record, dict):
                continue
            for field, value in record.items():
                bag[(field, _normalize(value))] += 1
        return bag

    bag_a = _bag(extractions_a)
    bag_b = _bag(extractions_b)
    if not bag_a and not bag_b:
        return 1.0

    overlap = sum((bag_a & bag_b).values())
    total_a = sum(bag_a.values())
    total_b = sum(bag_b.values())
    if total_a == 0 or total_b == 0:
        return 0.0

    precision = overlap / total_b
    recall = overlap / total_a
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def rand_index(assignment_a: dict[str, str], assignment_b: dict[str, str]) -> float:
    """Pairwise co-clustering agreement between two group assignments.

    ``assignment_*`` maps item id -> that run's own group label. Group
    LABEL STRINGS are never compared across ``a``/``b`` (two independent
    LLM completions will not reliably choose matching label text even
    when their underlying partition agrees) — only the pairwise question
    "does this run consider these two items co-grouped?" is compared,
    which is exactly the classic Rand Index over the common id universe.

    Only ids present in both assignments are scored. Fewer than 2 common
    ids returns 1.0 (vacuously agreeing — no pair exists to disagree on),
    matching the "both empty -> agree" convention used elsewhere in this
    module.
    """
    common = sorted(set(assignment_a) & set(assignment_b))
    if len(common) < 2:
        return 1.0

    agree = 0
    total = 0
    for i, j in combinations(common, 2):
        same_a = assignment_a[i] == assignment_a[j]
        same_b = assignment_b[i] == assignment_b[j]
        if same_a == same_b:
            agree += 1
        total += 1
    return agree / total


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: Filler words dropped before the Jaccard comparison (code-review finding
#: 2026-08-21): without this, two citations about UNRELATED passages that
#: happen to share only stopwords ("the", "in", "of") would register a
#: nonzero, substance-free overlap. Deliberately small and closed —
#: content words (including short ones like RDR ids' numeric parts) are
#: never filtered, only classic English function words.
_CITATION_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "and", "or", "is",
    "are", "was", "were", "that", "this", "for", "with", "from", "by",
    "as", "it", "its", "be", "which", "than", "into",
})


def citation_tokens(citations: list[Any]) -> set[str]:
    """Tokenize a ``citations``/locator string list into one bag of
    lowercased word tokens (stopwords removed), unioned across every
    citation in the list.

    Revision (2026-08-21, see T2 ``nexus_rdr/196-phase2-quality-proxy``
    "## Revision"): the original design compared whole citation STRINGS
    by exact-match Jaccard. A live strong-vs-strong run of ``verify``
    demonstrated this is too brittle — two independent completions
    describe the same grounding passage with different wording ("rdr-086
    chash section" vs "the chash index paragraph"), which exact-string
    Jaccard scores as total disagreement even when the underlying
    grounding is the same. Token-level Jaccard is forgiving of that
    phrasing variance while still requiring real lexical/topical overlap
    — two citations naming unrelated passages still won't share tokens.

    Second revision (2026-08-21, code-review round): stopwords are
    filtered before the union, so two citations that share ONLY filler
    words ("the", "of", "in") no longer register as partial agreement —
    a real methodological gap in the first revision (see
    ``TestCitationTokens`` for the non-vacuity tests proving both that a
    genuine paraphrase still overlaps and that a stopword-only overlap no
    longer does).
    """
    tokens: set[str] = set()
    for c in citations:
        if isinstance(c, str):
            tokens |= set(_TOKEN_RE.findall(c.casefold()))
    return tokens - _CITATION_STOPWORDS


def bool_plus_jaccard(
    bool_a: bool,
    bool_b: bool,
    set_a: set[str],
    set_b: set[str],
    *,
    bool_weight: float = 0.5,
) -> float:
    """Composite score for ``check``/``verify``: a boolean-agreement term
    plus a Jaccard-agreement term over a supporting evidence/citation set.

    Used with ``bool_weight=0.5`` by both operators' thresholds (see T2
    ``nexus_rdr/196-phase2-quality-proxy``) — chosen so that a boolean
    DISAGREEMENT caps the composite at ``1 - bool_weight`` (0.5), below
    both operators' 0.70 threshold regardless of evidence overlap: the
    metric cannot be rescued by evidence agreement alone when the two
    runs reached opposite verdicts.
    """
    bool_term = 1.0 if bool_a == bool_b else 0.0
    set_term = exact_membership_jaccard(set_a, set_b)
    return bool_weight * bool_term + (1.0 - bool_weight) * set_term


#: Named thresholds, fixed BEFORE any measurement — recorded in the
#: FROZEN, write-once T2 title
#: ``nexus_rdr/196-phase2-quality-proxy-REGISTERED-2026-08-21`` (never
#: upserted again after that write, so its own "Updated" timestamp is a
#: mechanically-checkable pre-registration proof; the mutable working
#: entry ``nexus_rdr/196-phase2-quality-proxy`` carries results/revisions
#: only — round-2 code-review/critic finding, 2026-08-21). Single source
#: of truth for both the harness driver and the control tests, so a
#: threshold edit here is a one-place change reflected everywhere it's
#: checked (a real edit would itself need a NEW frozen title, per the
#: doctrine above — this dict is not meant to silently drift from what
#: was registered).
THRESHOLDS: dict[str, float] = {
    "operator_filter": 0.80,
    "operator_groupby": 0.80,
    "operator_rank": 0.60,
    "operator_extract": 0.85,
    "operator_check": 0.70,
    "operator_verify": 0.70,
}

#: Operators this proxy deliberately does NOT score (see module docstring
#: + T2 entry). Free text is the substantive output field for all four.
OUT_OF_PROXY_SCOPE: frozenset[str] = frozenset({
    "operator_summarize", "operator_aggregate",
    "operator_compare", "operator_generate",
})
