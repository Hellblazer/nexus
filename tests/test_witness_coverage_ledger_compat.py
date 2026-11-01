# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-232 coverage ledger reads the Witness Schema v1 payload (RDR-088 amendment 2026-11-01).

The ledger sends ``[{"id","text"}]`` items through the run's dispatcher to ``operator_check``, instructs the
model to open each quote with a facet tag (``[f1] ...``), and reads ``item_id`` / ``quote`` / ``role`` from the
reply. Binding must keep all three working: the tag is stripped for the containment test only, the returned
quote keeps it, and a reply the ledger reads is the bound payload.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import nexus.operators.dispatch as dmod
from nexus.plans import coverage_ledger, runner

TEXT_1 = "The identity gap is that a chash is shared by two documents."
TEXT_2 = "Options weighed: keep one row per chash, or one row per document."


def _items() -> str:
    return json.dumps([{"id": "e1", "text": TEXT_1}, {"id": "e2", "text": TEXT_2}])


@pytest.mark.asyncio
async def test_a_facet_tagged_quote_binds_and_the_ledger_reads_the_bound_payload() -> None:
    async def fake_dispatch(prompt, schema, **kw):
        return {"ok": True, "evidence": [
            {"item_id": "e1", "quote": "[f1] chash is shared by two documents", "role": "supports"},
            {"item_id": "e2", "quote": "[f2] one row per document", "role": "supports"},
        ]}

    with patch.object(dmod, "claude_dispatch", fake_dispatch):
        reply = await runner._default_dispatcher(
            coverage_ledger.ASSESSMENT_OPERATOR, {"items": _items(), "check_instruction": "[f1] gap\n[f2] options"},
        )
    assert reply["schema_version"] == 1
    # direct-style call: the items carry no source, so a contained quote is unattributed (never bound)
    assert [i["state"] for i in reply["evidence"]] == ["unattributed", "unattributed"]
    # the returned quote keeps its tag, which is what the ledger's opens-with-a-facet-tag check reads
    records = coverage_ledger._records(reply)
    assert [(r["item_id"], r["quote"], r["role"]) for r in records] == [
        ("e1", "[f1] chash is shared by two documents", "supports"),
        ("e2", "[f2] one row per document", "supports"),
    ]
    assert all(coverage_ledger._TAG.match(r["quote"]) for r in records)
    # the host rejected nothing and the span points into the item text, past the stripped tag
    assert reply["rejected"] == []
    start, end = reply["evidence"][0]["span"]["start"], reply["evidence"][0]["span"]["end"]
    assert TEXT_1[start:end] == "chash is shared by two documents"


@pytest.mark.asyncio
async def test_the_ledger_still_rejects_an_id_the_call_did_not_carry_and_the_host_agrees() -> None:
    async def fake_dispatch(prompt, schema, **kw):
        return {"ok": False, "evidence": [{"item_id": "e9", "quote": "[f1] anything", "role": "supports"}]}

    with patch.object(dmod, "claude_dispatch", fake_dispatch):
        reply = await runner._default_dispatcher(
            coverage_ledger.ASSESSMENT_OPERATOR, {"items": _items(), "check_instruction": "[f1] gap"},
        )
    assert reply["evidence"] == [] and reply["rejected"] == ["e9"]
    assert coverage_ledger._records(reply) == []
