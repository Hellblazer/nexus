# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10, critique S3 e): the blind drafting packet.

Independence of drafted labels rests on the drafting agent never seeing the assessor's ledger. The envelope holds the
ledger, the anchors' evidence ids, the findings and an answer generated from the ledger, so "the agent never looks" is
discipline. The packet extractor makes it construction: it reads one thing from an envelope (the anchors' ``item_id``,
``chash`` and ``collection``) and writes qid, question, facet slugs and per item its id, chash and text. These tests prove
that by spying on every key read, and by planting a sentinel in every other field and finding none in the packet.

Model-free and tenant-free: texts come from the G06 fixture pieces, keyed by their chunk hash.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from bench import rdr232_oracle_packet as packet  # noqa: E402
from bench.rdr232_envelope import EnvelopeRecord, write_envelopes_file  # noqa: E402
from bench.schema import load_golden_set  # noqa: E402

from nexus.mcp import core  # noqa: E402
from tests._rdr232_golden import ART_RDR_PIECES  # noqa: E402

GOLDEN = load_golden_set(packet.GOLDEN_PATH)
G06 = next(q for q in GOLDEN if q.qid == "G06-synthesis-art-rdr084")
FIXTURE = REPO / "tests" / "fixtures" / "rdr232_envelope_g06_stub.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
TEXT_BY_CHASH = {hashlib.sha256(text.encode("utf-8")).hexdigest(): text for _f, text in ART_RDR_PIECES}


def _text_of(chash: str, collection: str) -> str | None:  # noqa: ARG001
    return TEXT_BY_CHASH.get(chash)


SENTINELS = (
    "SENTINEL_FINDING_REASON", "SENTINEL_EVIDENCE_ID", "SENTINEL_ANSWER_TEXT", "SENTINEL_LEDGER_STATE", "SENTINEL_REPAIR",
    "SENTINEL_STREAM", "SENTINEL_TUMBLER", "SENTINEL_COST", "SENTINEL_CALL_LOG", "SENTINEL_FACET_TEXT",
)


def _poisoned() -> dict[str, Any]:
    """The recorded envelope with a sentinel planted in every field the packet must not carry."""
    env = json.loads(json.dumps(RECORDED))
    env["final_text"] = "SENTINEL_ANSWER_TEXT " + env["final_text"]
    cov = env["coverage"]
    cov["state"], cov["reason"] = "SENTINEL_LEDGER_STATE", "SENTINEL_LEDGER_STATE"
    for f in cov["findings"]:
        f["reason"], f["evidence_ids"] = "SENTINEL_FINDING_REASON", ["SENTINEL_EVIDENCE_ID"]
    for a in cov["anchors"]:
        a["stream"], a["tumbler"], a["t2_record_id"], a["kind"] = "SENTINEL_STREAM", "SENTINEL_TUMBLER", "SENTINEL_TUMBLER", "SENTINEL_STREAM"
    cov["assessment"]["call_log"] = [["SENTINEL_CALL_LOG", 1, "support", 1, 0]]
    cov["repair"] = {"outcome": "SENTINEL_REPAIR", "facet_ids": ["SENTINEL_REPAIR"]}
    env["cost_usd"] = "SENTINEL_COST"
    env["request_coverage"]["facets"] = [{"id": "f1", "text": "SENTINEL_FACET_TEXT", "origin": "SENTINEL_FACET_TEXT"}]
    env["chunks"] = [{"id": "SENTINEL_EVIDENCE_ID", "chash": "SENTINEL_EVIDENCE_ID", "collection": "x", "distance": 0.1}]
    return env


# ── the shape ────────────────────────────────────────────────────────────────────────────────────────────


def test_the_packet_carries_the_qid_the_question_the_slugs_and_each_items_id_chash_and_text() -> None:
    out = packet.build_packet([G06], {G06.qid: RECORDED}, _text_of)
    assert set(out) == {"packet_version", "requests", "meta"}
    (req,) = out["requests"]
    assert set(req) == {"qid", "question", "facet_slugs", "items"}
    assert req["qid"] == G06.qid and req["question"] == G06.text and req["facet_slugs"] == list(G06.required_facets)
    assert len(req["items"]) == len(RECORDED["coverage"]["anchors"]) == 12
    for item in req["items"]:
        assert set(item) == {"item_id", "chash", "text"}
        assert hashlib.sha256(item["text"].encode("utf-8")).hexdigest() == item["chash"], "the text is the chunk the hash names"
    assert [i["item_id"] for i in req["items"]] == [f"e{n}" for n in range(1, 13)]
    assert out["meta"] == {"requests": 1, "items": 12, "unresolved_text": 0, "chash_mismatch": 0}


# ── blindness by construction ────────────────────────────────────────────────────────────────────────────


def test_no_ledger_finding_evidence_id_or_answer_field_reaches_the_packet() -> None:
    out = packet.build_packet([G06], {G06.qid: _poisoned()}, _text_of)
    text = json.dumps(out)
    for s in SENTINELS:
        assert s not in text, f"{s} reached the packet"
    # non-vacuity: the poison is really in the envelope the extractor was given
    assert all(s in json.dumps(_poisoned()) for s in SENTINELS)


class _Spy(Mapping):
    """A mapping that records every key read, at every depth."""

    def __init__(self, data: Any, path: str, log: set[str]) -> None:
        self._data, self._path, self._log = data, path, log

    def __getitem__(self, key: str) -> Any:
        self._log.add(f"{self._path}/{key}")
        return _wrap(self._data[key], f"{self._path}/{key}", self._log)

    def __iter__(self) -> Iterator[str]:
        self._log.add(f"{self._path}/*")  # iterating the keys is itself a read of all of them
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _wrap(value: Any, path: str, log: set[str]) -> Any:
    if isinstance(value, dict):
        return _Spy(value, path, log)
    if isinstance(value, list):
        return [_wrap(v, path + "[]", log) for v in value]
    return value


def test_the_extractor_reads_only_the_anchors_identity_fields_of_an_envelope() -> None:
    read: set[str] = set()
    packet.build_packet([G06], {G06.qid: _wrap(RECORDED, "env", read)}, _text_of)
    assert read <= {"env/coverage", "env/coverage/anchors", "env/coverage/anchors[]/item_id", "env/coverage/anchors[]/chash",
                    "env/coverage/anchors[]/collection"}, sorted(read)
    assert "env/coverage/anchors[]/chash" in read, "non-vacuity: the spy sees the reads the extractor does make"


def test_an_envelope_with_no_coverage_or_no_anchors_gives_an_empty_item_list_not_a_guess() -> None:
    out = packet.build_packet([G06], {G06.qid: {"final_text": "SENTINEL_ANSWER_TEXT"}}, _text_of)
    assert out["requests"][0]["items"] == [] and "SENTINEL_ANSWER_TEXT" not in json.dumps(out)


# ── text that cannot be had ──────────────────────────────────────────────────────────────────────────────


def test_an_item_whose_text_cannot_be_resolved_is_kept_with_null_text_and_counted() -> None:
    out = packet.build_packet([G06], {G06.qid: RECORDED}, lambda c, col: None if c.startswith("a") else _text_of(c, col))
    nulls = [i for i in out["requests"][0]["items"] if i["text"] is None]
    assert nulls and out["meta"]["unresolved_text"] == len(nulls)


def test_a_text_that_does_not_hash_to_its_chash_is_counted() -> None:
    out = packet.build_packet([G06], {G06.qid: RECORDED}, lambda c, col: "some other text")  # noqa: ARG005
    assert out["meta"]["chash_mismatch"] == 12 and out["meta"]["unresolved_text"] == 0


def test_requests_without_an_envelope_are_skipped_and_named_in_the_meta_by_count() -> None:
    other = GOLDEN[0]
    out = packet.build_packet([G06, other], {G06.qid: RECORDED}, _text_of)
    assert [r["qid"] for r in out["requests"]] == [G06.qid] and out["meta"]["requests"] == 1 and out["meta"]["without_envelope"] == 1


# ── the command line ─────────────────────────────────────────────────────────────────────────────────────


def test_the_cli_writes_the_packet_from_an_envelopes_file_and_a_text_map(tmp_path: Path) -> None:
    envs = tmp_path / "env.json"
    write_envelopes_file(envs, {"off": [EnvelopeRecord(G06.qid, 1, "", _poisoned())], "on": [EnvelopeRecord(G06.qid, 1, "", _poisoned())]})
    texts = tmp_path / "texts.json"
    texts.write_text(json.dumps(TEXT_BY_CHASH), encoding="utf-8")
    out = tmp_path / "packet.json"
    rc = packet.main(["--envelopes", str(envs), "--set", "golden", "--qid", G06.qid, "--texts", str(texts), "--out", str(out)])
    assert rc == 0
    body = out.read_text(encoding="utf-8")
    assert not any(s in body for s in SENTINELS)
    assert json.loads(body)["requests"][0]["qid"] == G06.qid


def test_the_cli_reads_the_on_arm_by_default_and_refuses_an_unknown_arm(tmp_path: Path, capsys) -> None:
    envs = tmp_path / "env.json"
    write_envelopes_file(envs, {"on": [EnvelopeRecord(G06.qid, 1, "", RECORDED)]})
    texts = tmp_path / "texts.json"
    texts.write_text(json.dumps(TEXT_BY_CHASH), encoding="utf-8")
    assert packet.main(["--envelopes", str(envs), "--set", "golden", "--texts", str(texts), "--arm", "sideways"]) == 2
    assert "arm" in capsys.readouterr().err
    assert packet.main(["--envelopes", str(envs), "--set", "golden", "--texts", str(texts), "--out", str(tmp_path / "p.json")]) == 0


def test_the_cli_needs_a_text_source(tmp_path: Path, capsys) -> None:
    envs = tmp_path / "env.json"
    write_envelopes_file(envs, {"on": [EnvelopeRecord(G06.qid, 1, "", RECORDED)]})
    assert packet.main(["--envelopes", str(envs), "--set", "golden"]) == 2
    err = capsys.readouterr().err
    assert "--texts" in err and "--live" in err


def test_the_live_resolver_matches_each_body_to_its_chash_by_hashing_never_by_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch may return fewer bodies, in another order, with a ``[source: ...]`` line: the text an item gets is the
    body that hashes to its chash."""
    calls: list[tuple[list[str], str]] = []

    def fake(ids, collections, *, structured, max_chars_per_doc):  # noqa: ARG001
        calls.append((list(ids), collections))
        bodies = [f"[source: x.md]\n{TEXT_BY_CHASH[c]}" for c in reversed(ids) if c in TEXT_BY_CHASH]
        return {"contents": bodies[:-1], "missing": []}  # drops one body and reverses the rest

    monkeypatch.setattr(core, "store_get_many", fake)
    refs = packet.anchor_refs(RECORDED)
    text_of = packet.live_text_resolver(refs)
    resolved = {chash: text_of(chash, col) for _i, chash, col in refs}
    assert len(calls) == 1 and sorted(calls[0][0]) == sorted({c for _i, c, _col in refs}), "one batch per collection"
    assert sum(t is None for t in resolved.values()) == 1, "the dropped body is unresolved, not misassigned"
    assert all(t is None or hashlib.sha256(t.encode("utf-8")).hexdigest() == c for c, t in resolved.items())
