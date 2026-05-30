# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DEVONthink bidirectional write-back (RDR-139 Layer F).

After nexus indexes + enriches a DEVONthink record, ``nx dt index --writeback``
stamps the nexus identity back onto the DT record so the knowledge graph is
navigable from inside DEVONthink. This module holds the pure orchestration; the
CLI wires it after a successful index.

Authoritative-source contract (RDR §Approach Layer F, §Risks):

- **nexus-owned namespace only**: every tag nexus writes is ``nx-*`` prefixed.
  Custom-metadata keys are ``nxtumbler`` / ``nxindexed`` (DT strips separators
  from metadata identifiers, so a hyphen/dot is not possible — ``nx``-prefixed
  is the maximally-namespaced legal form). A later audit/revoke pass matches
  ``nx-*`` tags and the ``nx``-prefixed metadata keys.
- **never edits user content**: only tags, an appended annotation, and custom
  metadata fields — the record body is never touched.
- **no-clobber**: tags add-mode, annotation append-mode, metadata merge-mode
  (verified live, CA5 / nexus-ymcrt).
- **honours Exclude-from-AI&MCP**: the DT server rejects writes to excluded
  records; each helper is fail-soft (``None`` → ``False``) so an excluded record
  yields a clean skip, never a silent partial write.
- **opt-in**: the CLI flag defaults off; this function is only called when the
  user asks for it.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

from nexus.mcp_client import devonthink as _devonthink

log = structlog.get_logger(__name__)


def _nx_keyword_tags(aspect_keywords: Iterable[str]) -> list[str]:
    """Namespace + normalise aspect keywords into ``nx-kw:<k>`` tags (deduped)."""
    seen: set[str] = set()
    out: list[str] = []
    for kw in aspect_keywords:
        norm = str(kw).strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(f"nx-kw:{norm}")
    return out


def writeback_record(
    dt_uuid: str,
    tumbler: str,
    *,
    aspect_keywords: Iterable[str] = (),
    dt_client=_devonthink,
) -> dict[str, bool]:
    """Stamp the nexus identity onto a DEVONthink record (Layer F).

    Writes (all nexus-owned, no-clobber):
    - tags ``nx-indexed``, ``nx-tumbler:<t>``, and ``nx-kw:<k>`` per aspect keyword
    - an appended annotation backlink to the tumbler
    - custom metadata ``nxtumbler`` / ``nxindexed``

    Returns ``{"tags", "annotation", "metadata", "skipped"}``. When DT is
    unavailable returns ``{"skipped": True, ...all False}`` — the tested
    fallback (index still succeeds, no DT mutation; Gap 0). Per-write failures
    (e.g. an excluded record) are fail-soft: the individual flag is ``False``
    and the others still attempt, mirroring the helpers' clean-error contract.
    """
    result = {"tags": False, "annotation": False, "metadata": False, "skipped": False}
    if not dt_uuid or not tumbler:
        result["skipped"] = True
        return result
    if not dt_client.available():
        log.info("dt_writeback_skipped_unavailable", dt_uuid=dt_uuid, tumbler=tumbler)
        result["skipped"] = True
        return result

    tags = ["nx-indexed", f"nx-tumbler:{tumbler}", *_nx_keyword_tags(aspect_keywords)]
    result["tags"] = dt_client.dt_set_tags(dt_uuid, tags, mode="add")

    # Annotation append is NOT idempotent server-side (DT has no dedup on append),
    # so re-indexing would accumulate duplicate backlink lines and pollute the
    # user's annotation. Guard: only append when the backlink is absent. Treat an
    # already-present backlink as success (idempotent no-op).
    backlink = f"nexus: indexed as tumbler {tumbler}"
    existing = dt_client.dt_annotation_text(dt_uuid) or ""
    if backlink in existing:
        result["annotation"] = True
    else:
        result["annotation"] = dt_client.dt_set_annotation(dt_uuid, backlink, mode="append")

    # Custom metadata is best-effort: DT strips separators from identifiers and
    # only accepts PRE-DEFINED custom-metadata fields, so ``nxtumbler`` is the
    # maximally-namespaced legal key and the write is a no-op (honestly reported
    # as metadata=False) unless the user has defined nexus fields in DT. The
    # tumbler is always recoverable from the ``nx-tumbler:<t>`` tag regardless,
    # so this is additive, not load-bearing. (Live finding, CA5 / 139-research-CA5.)
    result["metadata"] = dt_client.dt_set_custom_metadata(
        dt_uuid, {"nxtumbler": str(tumbler), "nxindexed": "true"}, mode="merge"
    )

    log.info(
        "dt_writeback_done",
        dt_uuid=dt_uuid,
        tumbler=tumbler,
        tags=result["tags"],
        annotation=result["annotation"],
        metadata=result["metadata"],
    )
    return result
