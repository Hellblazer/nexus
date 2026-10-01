# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.35 fix round 2: the maintenance verb that WRITES reads live rows.

``nx collection re-embed`` pages a collection and re-upserts chunks. A client
re-write refreshes ``nexus.chunks.last_written_at`` (vectors-020), the anchor
``reapable(c)``'s grace window keys on, so a verb that read HIDDEN chunks (no live
owner, or owned only by a tombstoned document) would re-write the very rows the
reaper and ``purge_trash`` are about to reclaim and hand them a fresh grace
window. The readers that only discover or guard (backfill, the reindex pre-delete
scan) read stored chunks; this one must not.

The fake hides non-live rows unless ``include_non_live=True`` is passed, as the
engine does, and records every read so the decision is asserted on the call as
well as on the outcome.
"""
from __future__ import annotations

from unittest.mock import MagicMock

LIVE_IDS = ["live-1", "live-2"]
HIDDEN_IDS = ["unowned-1", "tombstoned-1"]


class _LiveAwareCollection:
    name = "docs__wbfpw35-write-paths"

    def __init__(self) -> None:
        self._rows = {
            "live-1": ("alpha text", {"source_path": "a.md"}, True),
            "live-2": ("beta text", {"source_path": "b.md"}, True),
            "unowned-1": ("gamma text", {"source_path": "c.md"}, False),
            "tombstoned-1": ("delta text", {"source_path": "d.md"}, False),
        }
        self.reads: list[dict] = []
        self.upserted: list[str] = []

    def count(self) -> int:  # stored count, as the engine's /v1/vectors/count
        return len(self._rows)

    def get(self, *, ids=None, include=None, limit=100, offset=0, include_non_live=False, **_kw):
        self.reads.append({"ids": ids, "include_non_live": include_non_live})
        keys = [k for k in self._rows if include_non_live or self._rows[k][2]]
        if ids is not None:
            keys = [k for k in keys if k in ids]
        else:
            keys = keys[offset:offset + limit]
        return {
            "ids": keys,
            "documents": [self._rows[k][0] for k in keys],
            "metadatas": [dict(self._rows[k][1]) for k in keys],
            "embeddings": [[0.0] for _ in keys],
        }

    def upsert(self, *, ids, documents, embeddings, metadatas) -> None:
        self.upserted.extend(ids)


def test_re_embed_does_not_re_write_hidden_chunks() -> None:
    from nexus.commands.collection import _reembed_collection

    col = _LiveAwareCollection()
    db = MagicMock()
    db.get_collection.return_value = col
    hooks = MagicMock()

    processed, skipped = _reembed_collection(
        db, col.name, "voyage-3", dry_run=False, hooks=hooks,
    )

    upserted = [i for call in db.upsert_chunks.call_args_list for i in call.args[1]]
    assert sorted(upserted) == sorted(LIVE_IDS)
    assert (processed, skipped) == (len(LIVE_IDS), 0)
    assert not any(r["include_non_live"] for r in col.reads), col.reads
