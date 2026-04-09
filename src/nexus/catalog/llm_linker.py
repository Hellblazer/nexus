# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""LLM-hybrid entity resolution for knowledge documents (RDR-061 E3 Phase 2b).

Pipeline: heuristic_pass → llm_verify → link creation.
Only runs on knowledge__* collections. Non-fatal by design.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from nexus.catalog.catalog import Catalog, CatalogEntry
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger()


def _normalize_name(name: str) -> list[str]:
    """CamelCase, snake_case, kebab-case -> list of lowercase tokens (len > 2)."""
    camel = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    flat = re.sub(r"[_\-\s]+", " ", camel).strip().lower()
    return [t for t in flat.split() if len(t) > 2]


_CONFIDENCE_THRESHOLD = 0.7
_HEURISTIC_AUTO_LINK_THRESHOLD = 0.8
_MAX_HEURISTIC_CANDIDATES = 10
_VALID_RELATIONS = frozenset({"cites", "implements", "supersedes", "relates"})

RELATION_PROMPT = """\
Given two documents:
Document A: {title_a}
{excerpt_a}

Document B: {title_b}
{excerpt_b}

Classify their relationship as one of:
- cites: A references B as a source
- implements: A implements the design in B
- supersedes: A replaces or improves upon B
- relates: A and B discuss the same concept
- none: no meaningful relationship

Respond with JSON only: {{"relation": "<type>", "confidence": 0.0-1.0}}
"""


class LLMClient(Protocol):
    """Protocol for LLM verification calls."""

    def classify_relation(self, prompt: str) -> dict[str, Any]:
        """Return {"relation": str, "confidence": float}."""
        ...


@dataclass
class CandidatePair:
    """A pair of catalog entries that may be related."""

    source: CatalogEntry
    target: CatalogEntry
    heuristic_score: float


def heuristic_pass(
    new_entry: CatalogEntry,
    all_entries: list[CatalogEntry],
) -> list[CandidatePair]:
    """Find candidate pairs via title token overlap (Jaccard >= 0.2).

    Only considers knowledge and RDR entries as targets (not code).
    Skips self-matches and same-tumbler matches.
    """
    source_tokens = set(_normalize_name(new_entry.title))
    if not source_tokens:
        return []

    candidates: list[CandidatePair] = []
    for entry in all_entries:
        if str(entry.tumbler) == str(new_entry.tumbler):
            continue
        if entry.content_type not in ("knowledge", "rdr"):
            continue
        target_tokens = set(_normalize_name(entry.title))
        if not target_tokens:
            continue
        intersection = source_tokens & target_tokens
        union = source_tokens | target_tokens
        jaccard = len(intersection) / len(union) if union else 0.0
        if jaccard >= 0.2:
            candidates.append(CandidatePair(
                source=new_entry, target=entry, heuristic_score=jaccard,
            ))

    # Sort by score descending, cap at limit
    candidates.sort(key=lambda c: c.heuristic_score, reverse=True)
    return candidates[:_MAX_HEURISTIC_CANDIDATES]


def llm_verify_candidates(
    candidates: list[CandidatePair],
    llm: LLMClient,
    source_excerpt: str = "",
) -> list[tuple[CandidatePair, str, float]]:
    """Verify uncertain candidates via LLM. Returns (pair, relation, confidence).

    High-confidence heuristic matches (score >= 0.8) are auto-linked as 'relates'
    without LLM verification (cost containment — EvidenceNet approach).
    """
    verified: list[tuple[CandidatePair, str, float]] = []

    for pair in candidates:
        if pair.heuristic_score >= _HEURISTIC_AUTO_LINK_THRESHOLD:
            verified.append((pair, "relates", pair.heuristic_score))
            continue

        prompt = RELATION_PROMPT.format(
            title_a=pair.source.title,
            excerpt_a=source_excerpt[:200] if source_excerpt else "(no excerpt)",
            title_b=pair.target.title,
            excerpt_b="(catalog entry)",
        )
        try:
            result = llm.classify_relation(prompt)
        except Exception:
            _log.debug("llm_verify_failed", source=pair.source.title, target=pair.target.title)
            continue

        relation = result.get("relation", "none")
        confidence = float(result.get("confidence", 0.0))

        if relation == "none" or relation not in _VALID_RELATIONS:
            continue
        if confidence < _CONFIDENCE_THRESHOLD:
            continue

        verified.append((pair, relation, confidence))

    return verified


def run_hybrid_pipeline(
    cat: Catalog,
    new_tumbler: Tumbler,
    llm: LLMClient | None = None,
    source_excerpt: str = "",
) -> int:
    """Full hybrid extraction pipeline for a newly stored knowledge document.

    1. Heuristic pass: find candidates via title token overlap
    2. LLM verify: classify uncertain candidates (if llm provided)
    3. Create links

    Returns number of links created.
    """
    all_entries = cat.all_documents()

    # Find the new entry
    new_entry = None
    for e in all_entries:
        if str(e.tumbler) == str(new_tumbler):
            new_entry = e
            break
    if new_entry is None:
        return 0

    candidates = heuristic_pass(new_entry, all_entries)
    if not candidates:
        return 0

    if llm is not None:
        verified = llm_verify_candidates(candidates, llm, source_excerpt)
    else:
        # No LLM — only auto-link high-confidence heuristic matches
        verified = [
            (c, "relates", c.heuristic_score)
            for c in candidates
            if c.heuristic_score >= _HEURISTIC_AUTO_LINK_THRESHOLD
        ]

    count = 0
    for pair, relation, confidence in verified:
        try:
            created = cat.link_if_absent(
                pair.source.tumbler,
                pair.target.tumbler,
                relation,
                created_by="llm-extractor",
            )
        except ValueError:
            continue
        if created:
            count += 1
            _log.debug(
                "hybrid_link_created",
                source=str(pair.source.tumbler),
                target=str(pair.target.tumbler),
                relation=relation,
                confidence=confidence,
            )

    return count
