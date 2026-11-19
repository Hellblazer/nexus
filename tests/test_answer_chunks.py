"""GH #1667: the nx_answer structured envelope's chunk list is deduplicated and capped."""
from __future__ import annotations

import pytest

from nexus.mcp.answer_chunks import MAX_ENVELOPE_CHUNKS, cap_chunks, harvest_chunks


def _c(i: int, **kw) -> dict:
    return {"id": f"c{i}", "chash": f"h{i}", "collection": "k", "distance": 0.1, **kw}


class TestCapChunks:
    def test_cap_is_fifty(self):
        assert MAX_ENVELOPE_CHUNKS == 50

    def test_empty_and_none(self):
        assert cap_chunks(None) == ([], 0)
        assert cap_chunks([]) == ([], 0)

    def test_duplicates_keep_first_and_order(self):
        first = _c(1, distance=0.1)
        later = _c(1, distance=0.9)
        kept, truncated = cap_chunks([_c(2), first, _c(3), later, _c(2)])
        assert [c["id"] for c in kept] == ["c2", "c1", "c3"]
        assert kept[1] is first
        assert truncated == 0

    def test_cap_at_fifty_counts_distinct_dropped(self):
        kept, truncated = cap_chunks([_c(i) for i in range(120)])
        assert [c["id"] for c in kept] == [f"c{i}" for i in range(50)]
        assert truncated == 70

    def test_truncated_ignores_duplicates(self):
        # 60 distinct ids, each listed three times: 10 distinct dropped, not 130.
        chunks = [_c(i) for i in range(60)] * 3
        kept, truncated = cap_chunks(chunks)
        assert len(kept) == 50
        assert truncated == 10

    def test_exactly_at_cap_drops_nothing(self):
        kept, truncated = cap_chunks([_c(i) for i in range(50)] + [_c(0)])
        assert len(kept) == 50 and truncated == 0

    def test_custom_limit(self):
        kept, truncated = cap_chunks([_c(i) for i in range(5)], limit=2)
        assert [c["id"] for c in kept] == ["c0", "c1"] and truncated == 3


class TestHarvestChunks:
    def test_harvests_every_step_in_order_without_dedupe(self):
        steps = [
            {"ids": ["a", "b"], "chunk_text_hash": ["ha", "hb"], "collections": ["x"],
             "chunk_collections": ["x", "y"], "distances": [0.1, 0.2]},
            "not a dict",
            {"contents": ["no ids here"]},
            {"ids": ["a"], "collections": ["z"]},
        ]
        out = harvest_chunks(steps)
        assert [c["id"] for c in out] == ["a", "b", "a"]
        assert out[1] == {"id": "b", "chash": "hb", "collection": "y", "distance": 0.2}
        assert out[2] == {"id": "a", "chash": "", "collection": "z", "distance": None}

    def test_empty(self):
        assert harvest_chunks([]) == []


class TestCapChunksIdlessEntries:
    def test_an_entry_without_a_usable_id_is_kept_not_collapsed(self):
        # No producer emits these today; if one ever does, it must not lose evidence to the dedupe.
        a, b = {"chash": "h1"}, {"id": None, "chash": "h2"}
        c, d = {"id": ["unhashable"], "chash": "h3"}, "not-a-dict"
        kept, truncated = cap_chunks([a, _c(1), b, c, d, _c(1)])
        assert kept == [a, _c(1), b, c, d] and truncated == 0

    def test_idless_entries_count_toward_the_cap(self):
        kept, truncated = cap_chunks([{"chash": f"h{i}"} for i in range(52)])
        assert len(kept) == 50 and truncated == 2


class TestErrorPathEnvelope:
    @pytest.mark.asyncio
    async def test_an_error_reply_carries_chunks_truncated(self):
        from nexus.mcp.core import nx_answer  # noqa: PLC0415

        env = await nx_answer("q", budget_usd=-1, structured=True)
        assert isinstance(env, dict), env
        assert env["chunks"] == [] and env["chunks_truncated"] == 0
