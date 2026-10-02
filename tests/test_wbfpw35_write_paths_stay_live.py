# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.35 fix round 2: the maintenance verb that WRITES reads live rows.

``nx collection re-embed`` pages a collection and re-upserts chunks. A client
re-write refreshes ``nexus.chunks.last_written_at`` (vectors-020), the anchor the
reaper's grace window keys on. Two kinds of hidden chunk, two reasons: a
never-owned chunk would get a fresh grace window (the grace argument holds for
those only), and a chunk owned only by a tombstoned document gains nothing from a
refresh under the final design (nexus-wbfpw.15: ``nexus.chunk_orphaned_at``,
reapable keyed on ``GREATEST(last_written_at, orphaned_at)``), so for it the
reasons are the billed Voyage embed and the pointlessness of patching a chunk of a
deleted document. The readers that only discover or guard (backfill, the reindex
pre-delete scan) read stored chunks; this one must not.

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


def test_re_embed_summary_says_how_many_chunks_stayed_on_the_old_model() -> None:
    """The walk reads live rows only, so "re-embedded 2" on a collection that
    stores 4 would read as the whole collection. The summary names the rest."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.cli import main

    col = _LiveAwareCollection()
    db = MagicMock()
    db.get_collection.return_value = col

    with patch("nexus.commands.collection._t3", return_value=db), \
         patch("nexus.hook_registry.install_default_hooks"):
        result = CliRunner().invoke(
            main, ["collection", "re-embed", col.name, "--to", "voyage-3",
                   "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert f"re-embedded {len(LIVE_IDS)} chunk(s)" in result.output
    assert "2 of 4 stored chunk(s) were not re-embedded" in result.output


def test_re_embed_prompt_does_not_claim_every_chunk() -> None:
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.cli import main

    col = _LiveAwareCollection()
    db = MagicMock()
    db.get_collection.return_value = col

    with patch("nexus.commands.collection._t3", return_value=db):
        result = CliRunner().invoke(
            main, ["collection", "re-embed", col.name, "--to", "voyage-3", "--no-dry-run"],
            input="n\n",
        )

    assert "every chunk's vector" not in result.output
    assert "live catalog document" in result.output
