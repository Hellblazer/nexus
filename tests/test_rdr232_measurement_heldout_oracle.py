# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10): oracle labels for the HELD-OUT set, and the drafter-JSON converter.

The held-out set (``bench/queries/golden_heldout_v1.yaml``, 29 requests) has its own hand file (all ``todo``: no hand
labels exist) and its own drafted file. ``--set heldout`` reads those, ``--set golden`` the golden pair, ``--set both``
merges the two. ``bench.rdr232_oracle_drafts`` turns a drafter's JSON plus the blind packet into the drafted YAML. A
request's chunks are keyed by chash, never by item id: the held-out packet listed one request's ``e24``..``e33`` twice,
each with another chunk. Model-free and tenant-free: every envelope and packet here is built by hand or is the recorded
G06 stub.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from bench import rdr232_oracle as om  # noqa: E402
from bench import rdr232_oracle_drafts as drafts_mod  # noqa: E402
from bench import rdr232_phase3_measure as driver  # noqa: E402
from bench.rdr232_envelope import EnvelopeRecord, write_envelopes_file  # noqa: E402
from bench.rdr232_facet_map import FacetResolution  # noqa: E402
from bench.schema import GOLDEN_SET_VERSION, load_golden_set  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "rdr232_envelope_g06_stub.json"
GOLDEN = load_golden_set(om.GOLDEN_PATH)
HELDOUT = load_golden_set(om.HELDOUT_PATH)
BY_QID = {q.qid: q for q in [*GOLDEN, *HELDOUT]}
H01 = BY_QID["H01"]  # one facet: introducing-rdr
H14 = BY_QID["H14"]  # two facets: storage-tiers-claims, stale-statements
S1, S2 = H14.required_facets


def _h(n: int) -> str:
    return hashlib.sha256(f"heldout-chunk-{n}".encode()).hexdigest()


# ── the committed files ──────────────────────────────────────────────────────────────────────────────────


def test_the_heldout_hand_file_labels_all_29_requests_todo_and_checks_the_golden_set_version() -> None:
    o = om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT)
    assert len(HELDOUT) == 29 and o.summary()["requests"] == 29 and o.summary()["requests_todo"] == 29
    assert o.summary()["requests_labelled"] == 0 and o.summary()["pairs_labelled"] == 0
    data = yaml.safe_load(om.HELDOUT_ORACLE_PATH.read_text(encoding="utf-8"))
    assert data["golden_set_version"] == GOLDEN_SET_VERSION == 1, (
        "the held-out set versions itself with the golden set's golden_set_version")
    assert all(r["todo"] is True and r["items"] == [] for r in data["requests"].values())


def test_the_heldout_hand_file_is_the_scaffold_derivation() -> None:
    derived = om.scaffold(HELDOUT)["requests"]
    assert yaml.safe_load(om.HELDOUT_ORACLE_PATH.read_text(encoding="utf-8"))["requests"] == derived


def test_the_heldout_drafted_skeleton_loads_and_names_its_own_files() -> None:
    assert om.HELDOUT_DRAFTED_PATH.name == "golden_heldout_oracle_labels_drafted_v1.yaml" and om.HELDOUT_DRAFTED_PATH.is_file()
    plain = om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT).summary()
    drafted = om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT, drafted_path=om.HELDOUT_DRAFTED_PATH).summary()
    # The committed drafted file holds the 2026-10-29 drafts (21 requests); it only fills hand-todo requests.
    assert plain["pairs_labelled"] == 0 and drafted["requests"] == plain["requests"] == 29
    assert drafted["requests_labelled"] == 21 and drafted["pairs_labelled"] > 0


def test_each_set_has_its_own_files_and_the_other_sets_qids_are_unknown_to_them() -> None:
    with pytest.raises(ValueError, match="unknown requests"):
        om.load_oracle(om.HELDOUT_ORACLE_PATH, GOLDEN)
    with pytest.raises(ValueError, match="unknown requests"):
        om.load_oracle(om.DEFAULT_ORACLE_PATH, HELDOUT)
    with pytest.raises(ValueError, match="without an entry"):
        om.load_oracle(om.HELDOUT_ORACLE_PATH, [*HELDOUT, *GOLDEN[:1]])
    assert om.load_oracle(om.DEFAULT_ORACLE_PATH, GOLDEN).summary()["requests"] == 24


def test_load_oracle_for_set_picks_golden_heldout_or_both() -> None:
    golden, _ = om.load_oracle_for_set("golden")
    held, _ = om.load_oracle_for_set("heldout")
    both, _ = om.load_oracle_for_set("both")
    assert set(golden.requests) == {q.qid for q in GOLDEN} and set(held.requests) == {q.qid for q in HELDOUT}
    assert set(both.requests) == set(golden.requests) | set(held.requests) and len(both.requests) == 53
    assert both.requests["G06-synthesis-art-rdr084"] == golden.requests["G06-synthesis-art-rdr084"]
    for extra in ("compound", "near_miss", "negative"):
        assert set(om.load_oracle_for_set(extra)[0].requests) == set(golden.requests), "the in-sample extras read the golden files"


def test_the_heldout_loader_refuses_a_drafted_file_with_an_unknown_qid_or_slug(tmp_path: Path) -> None:
    def write(requests: dict[str, Any]) -> Path:
        p = tmp_path / "d.yaml"
        p.write_text(yaml.safe_dump({"oracle_version": 1, "golden_set_version": 1, "requests": requests}), encoding="utf-8")
        return p

    def entry(slug: str) -> dict[str, Any]:
        return {"provenance": "drafted", "items": [{"chash": _h(1), "closed": True, "facets": {slug: "supports"}}]}

    ok, _ = om.load_oracle_for_set("heldout", overrides={"heldout": (None, write({"H01": entry("introducing-rdr")}))})
    assert ok.requests["H01"].provenance == "drafted" and ok.requests["H02"].todo
    with pytest.raises(ValueError, match="unknown requests"):
        om.load_oracle_for_set("heldout", overrides={"heldout": (None, write({"G04-lookup-min-confidence": entry("x")}))})
    with pytest.raises(ValueError, match="not a required facet"):
        om.load_oracle_for_set("heldout", overrides={"heldout": (None, write({"H01": entry("no-such-slug")}))})


# ── the converter ────────────────────────────────────────────────────────────────────────────────────────


def _packet() -> dict[str, Any]:
    """H14's request carries item id e1 TWICE with two chashes (as the held-out packet did for e24..e33), plus e2 with
    no text and e3 repeating e1's first chash; H01 has two plain items."""
    return {
        "packet_version": 1,
        "requests": [
            {"qid": "H01", "question": H01.text, "facet_slugs": list(H01.required_facets), "items": [
                {"item_id": "e1", "chash": _h(1), "text": "RDR-064 introduces the console web ui"},
                {"item_id": "e2", "chash": _h(2), "text": "unrelated"},
            ]},
            {"qid": "H14", "question": H14.text, "facet_slugs": list(H14.required_facets), "items": [
                {"item_id": "e1", "chash": _h(10), "text": "tier claim in the first chunk"},
                {"item_id": "e1", "chash": _h(11), "text": "stale statement in the second chunk"},
                {"item_id": "e2", "chash": _h(12), "text": None},
                {"item_id": "e3", "chash": _h(10), "text": "tier claim in the first chunk"},
            ]},
        ],
        "meta": {},
    }


def _convert(drafts: dict[str, Any], hand: om.OracleSet | None = None, packet: dict[str, Any] | None = None):
    return drafts_mod.convert(
        packet or _packet(), drafts_mod.merge_drafts([("d", drafts)]), {q.qid: q for q in HELDOUT},
        hand or om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT),
    )


def test_the_converter_keeps_two_chunks_that_share_an_item_id_apart_and_round_trips(tmp_path: Path) -> None:
    body, counts = _convert({"H14": {_h(10): {S1: "supports"}, _h(11): {S2: "supports", S1: "contradicts"}}, "H01": {_h(1): {"introducing-rdr": "supports"}}})
    items = {i["chash"]: i for i in body["requests"]["H14"]["items"]}
    assert list(items) == [_h(10), _h(11)], "two chunks under one item id are two items; the repeated chash and the unread chunk are not"
    assert items[_h(10)]["facets"] == {S1: "supports"} and items[_h(11)]["facets"] == {S1: "contradicts", S2: "supports"}
    assert all(i["closed"] for r in body["requests"].values() for i in r["items"])
    assert body["requests"]["H01"]["items"][1] == {"chash": _h(2), "closed": True, "note": "unrelated", "facets": {}}, "an unlabelled item is present, neutral"
    assert counts == {"requests": 2, "items": 4, "items_labelled": 3, "items_unlabelled": 1, "items_unread_omitted": 1,
                      "items_duplicate_chash": 1, "pairs_supports": 3, "pairs_contradicts": 1}
    path = tmp_path / "drafted.yaml"
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    loaded = om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT, drafted_path=path)
    r = loaded.requests["H14"]
    assert r.provenance == "drafted" and not r.todo and loaded.requests["H02"].todo
    assert r.label_of(_h(10), "", S1)[0] == "supports" and r.label_of(_h(10), "", S2)[0] == "neutral"
    assert r.label_of(_h(11), "", S1)[0] == "contradicts" and r.label_of(_h(99), "", S1)[0] is None
    assert loaded.requests["H01"].label_of(_h(2), "", "introducing-rdr")[0] == "neutral"


def test_neutral_may_be_written_and_is_the_same_as_leaving_it_out() -> None:
    body, counts = _convert({"H01": {_h(1): {"introducing-rdr": "neutral"}}})
    assert body["requests"]["H01"]["items"][0]["facets"] == {} and counts["items_unlabelled"] == 2


@pytest.mark.parametrize(
    ("drafts", "message"),
    [
        ({"H01": {_h(77): {"introducing-rdr": "supports"}}}, "not one of the request's packet items"),
        ({"H14": {_h(1): {S1: "supports"}}}, "not one of the request's packet items"),
        ({"H01": {_h(1): {"no-such-slug": "supports"}}}, "not a required facet"),
        ({"H14": {_h(10): {"introducing-rdr": "supports"}}}, "not a required facet"),
        ({"H01": {_h(1): {"introducing-rdr": "maybe"}}}, "must be one of"),
        ({"H99": {_h(1): {"introducing-rdr": "supports"}}}, "packet does not hold"),
        ({"H14": {_h(12): {S1: "supports"}}}, "no text"),
    ],
    ids=["chash_not_in_packet", "chash_of_another_request", "unknown_slug", "slug_of_another_request", "bad_role", "qid_not_in_packet", "unread_chunk"],
)
def test_the_converter_refuses_what_the_packet_and_the_set_do_not_hold(drafts, message) -> None:
    with pytest.raises(ValueError, match=message):
        _convert(drafts)


def test_the_converter_refuses_a_qid_outside_the_set_a_hand_labelled_qid_and_a_packet_with_nothing_readable() -> None:
    golden_packet = {"packet_version": 1, "requests": [{"qid": "G04-lookup-min-confidence", "facet_slugs": ["default-min-confidence"],
                                                         "items": [{"item_id": "e1", "chash": _h(1), "text": "t"}]}]}
    with pytest.raises(ValueError, match="not a request of this --set"):
        _convert({"G04-lookup-min-confidence": {}}, packet=golden_packet)
    labelled = om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT)
    labelled = om.OracleSet({**labelled.requests, "H01": om.RequestOracle("H01", tuple(H01.required_facets), "human", (
        om.ItemLabel(chash=_h(1), closed=True, facets={"introducing-rdr": "supports"}),))})
    with pytest.raises(ValueError, match="never shadows a hand label"):
        _convert({"H01": {}}, hand=labelled)
    empty = _packet()
    for it in empty["requests"][0]["items"]:
        it["text"] = None
    with pytest.raises(ValueError, match="no readable packet item"):
        _convert({"H01": {}}, packet=empty)


def test_two_draft_files_may_split_a_request_but_not_disagree_about_a_chunk() -> None:
    merged = drafts_mod.merge_drafts([
        ("a", {"H14": {_h(10): {S1: "supports"}}}), ("b", {"H14": {_h(11): {S2: "supports"}}}), ("c", {"H14": {_h(10): {S1: "supports"}}}),
    ])
    assert merged == {"H14": {_h(10): {S1: "supports"}, _h(11): {S2: "supports"}}}
    with pytest.raises(ValueError, match="contradicts"):
        drafts_mod.merge_drafts([("a", {"H14": {_h(10): {S1: "supports"}}}), ("b", {"H14": {_h(10): {S1: "contradicts"}}})])


def test_the_converter_cli_writes_a_loadable_file_and_reports_counts(tmp_path: Path, capsys) -> None:
    packet, d1, d2, out = (tmp_path / n for n in ("packet.json", "d1.json", "d2.json", "out.yaml"))
    packet.write_text(json.dumps(_packet()), encoding="utf-8")
    d1.write_text(json.dumps({"H14": {_h(10): {S1: "supports"}}}), encoding="utf-8")
    d2.write_text(json.dumps({"H14": {_h(11): {S2: "supports"}}, "H01": {_h(1): {"introducing-rdr": "supports"}}}), encoding="utf-8")
    rc = drafts_mod.main(["--packet", str(packet), "--drafts", str(d1), str(d2), "--set", "heldout", "--out", str(out)])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0 and report["requests"] == 2 and report["items"] == 4 and report["pairs_supports"] == 3
    assert report["provenance"]["drafted"] == {"requests": 2, "pairs": 3}
    assert om.load_oracle(om.HELDOUT_ORACLE_PATH, HELDOUT, drafted_path=out).requests["H01"].provenance == "drafted"
    # a refusal writes nothing and exits 2
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"H01": {_h(5): {"introducing-rdr": "supports"}}}), encoding="utf-8")
    again = tmp_path / "again.yaml"
    assert drafts_mod.main(["--packet", str(packet), "--drafts", str(bad), "--set", "heldout", "--out", str(again)]) == 2
    assert "not one of the request's packet items" in capsys.readouterr().err and not again.exists()


def test_the_converter_cli_refuses_a_request_the_hand_file_labels(tmp_path: Path, capsys) -> None:
    hand = yaml.safe_load(om.HELDOUT_ORACLE_PATH.read_text(encoding="utf-8"))
    hand["requests"]["H01"] = {"provenance": "human", "items": [{"chash": _h(1), "closed": True, "facets": {"introducing-rdr": "supports"}}]}
    hand_path, packet, d, out = (tmp_path / n for n in ("hand.yaml", "packet.json", "d.json", "out.yaml"))
    hand_path.write_text(yaml.safe_dump(hand), encoding="utf-8")
    packet.write_text(json.dumps(_packet()), encoding="utf-8")
    d.write_text(json.dumps({"H01": {_h(1): {"introducing-rdr": "supports"}}}), encoding="utf-8")
    rc = drafts_mod.main(["--packet", str(packet), "--drafts", str(d), "--set", "heldout", "--hand", str(hand_path), "--out", str(out)])
    assert rc == 2 and "hand label" in capsys.readouterr().err and not out.exists()


# ── the scorer does not key anchors by item id ───────────────────────────────────────────────────────────


def _resolution() -> FacetResolution:
    return FacetResolution("H14", {"f1": (S1,), "f2": (S2,)}, {S1: ("f1",), S2: ("f2",)}, (), (), {"f1": "entry", "f2": "entry"})


def test_two_anchors_sharing_an_item_id_are_both_scored() -> None:
    """The first chunk of the shared id is the one the oracle says supports S1; keying anchors by id kept only the last."""
    oracle = om.RequestOracle("H14", (S1, S2), "drafted", (
        om.ItemLabel(chash=_h(10), closed=True, facets={S1: "supports"}), om.ItemLabel(chash=_h(11), closed=True, facets={}),
    ))
    coverage = {
        "anchors": [{"item_id": "e1", "chash": _h(10)}, {"item_id": "e1", "chash": _h(11)}],
        "findings": [{"facet_id": "f1", "state": "unsupported", "evidence_ids": [], "reason": ""},
                     {"facet_id": "f2", "state": "unsupported", "evidence_ids": [], "reason": ""}],
        "skip_generation": False,
    }
    score = om.score_run(oracle, coverage, _resolution())
    assert score.facets[0].oracle_state == "supported" and score.facets[0].false_negative is True
    assert score.facets[1].oracle_state == "not_supported"
    case = om.SpotCase(oracle, coverage, _resolution())
    sample = om.spot_check_sample([case], min_supports_multi=5, min_neutral=5)
    assert {(p.item_id, p.chash, p.slug) for p in sample.pairs} >= {("e1", _h(10), S1), ("e1", _h(11), S1), ("e1", _h(11), S2)}, (
        "the spot check lists both chunks of the shared id")


# ── the driver picks the set's files ─────────────────────────────────────────────────────────────────────


def _heldout_envelope() -> dict[str, Any]:
    """The recorded G06 envelope cut to a one-facet request: three anchors (the first two share an item id), one
    facet the assessor leaves unsupported."""
    env = json.loads(FIXTURE.read_text(encoding="utf-8"))
    env["request_coverage"]["facets"] = [{"id": "f1", "origin": "clause", "source_hints": [], "text": "the introducing rdr", "topics": []}]
    cov = env["coverage"]
    anchors = [{**cov["anchors"][0], "item_id": "e1", "chash": _h(1)}, {**cov["anchors"][1], "item_id": "e1", "chash": _h(2)},
               {**cov["anchors"][2], "item_id": "e2", "chash": _h(3)}]
    cov.update(
        anchors=anchors, findings=[{"facet_id": "f1", "state": "unsupported", "evidence_ids": [], "reason": ""}],
        facets={"contradicted": 0, "covered": 0, "unassessed": 0, "unsupported": 1}, facets_required=1, requested_facet_ids=["f1"],
        covered_facet_ids=[], facets_conflicted=0, state="uncovered", reason="unresolved_facets", skip_generation=False,
        residual_gaps=[{"facet_id": "f1", "reason": "no_supporting_evidence", "state": "unsupported"}],
    )
    return env


def _write_drafted(tmp_path: Path) -> Path:
    p = tmp_path / "drafted.yaml"
    body = {"oracle_version": 1, "golden_set_version": 1, "requests": {"H01": {"provenance": "drafted", "items": [
        {"chash": _h(1), "closed": True, "facets": {"introducing-rdr": "supports"}},
        {"chash": _h(2), "closed": True, "facets": {}},
        {"chash": _h(3), "closed": True, "facets": {}},
    ]}}}
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def test_from_envelopes_with_set_heldout_rescores_with_drafted_labels(tmp_path: Path) -> None:
    env = _heldout_envelope()
    envelopes, out, spot = tmp_path / "env.json", tmp_path / "r.json", tmp_path / "spot.yaml"
    write_envelopes_file(envelopes, {a: [EnvelopeRecord("H01", 1000, "", env)] for a in ("off", "on")})
    rc = driver.main(["--set", "heldout", "--from-envelopes", str(envelopes), "--heldout-drafted", str(_write_drafted(tmp_path)),
                      "--spot-check-out", str(spot), "--out", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    on = report["arms"]["on"]
    assert report["meta"]["mode"] == "from_envelopes" and report["meta"]["set"] == "heldout"
    assert report["oracle_labels"]["requests"] == 29 and report["oracle_labels"]["requests_labelled"] == 1
    assert report["oracle_labels_by_provenance"] == {"hand": {"requests": 0, "pairs": 0}, "drafted": {"requests": 1, "pairs": 1}}
    assert on["oracle"]["runs_scored"] == 1 and on["oracle"]["hand"]["runs"] == 0 and on["oracle"]["drafted"]["runs"] == 1
    fn = on["oracle"]["drafted"]["false_negative"]
    assert (fn["numerator"], fn["denominator"]) == (1, 1), "the drafted label says the chunk supports the facet; the assessor left it unsupported"
    assert report["meta"]["drafted_labels"] == str(tmp_path / "drafted.yaml")
    assert report["drafted_spot_check"]["sampled"] >= 1 and spot.is_file()


def test_the_default_heldout_run_reads_the_heldout_files_and_a_golden_run_does_not(tmp_path: Path) -> None:
    env = _heldout_envelope()
    envelopes = tmp_path / "env.json"
    write_envelopes_file(envelopes, {a: [EnvelopeRecord("H01", 1000, "", env)] for a in ("off", "on")})
    out = tmp_path / "r.json"
    empty = tmp_path / "drafted.yaml"
    empty.write_text("oracle_version: 1\ngolden_set_version: 1\nrequests: {}\n", encoding="utf-8")
    assert driver.main(["--set", "heldout", "--from-envelopes", str(envelopes), "--heldout-drafted", str(empty), "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["oracle_labels"]["requests"] == 29 and report["oracle_labels"]["requests_todo"] == 29
    assert report["arms"]["on"]["oracle"]["runs_scored"] == 0, "no label exists for H01: counted as todo, not scored"
    assert driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--stub", str(FIXTURE), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["oracle_labels"]["requests"] == 24


def test_set_both_merges_both_oracles_and_a_flag_for_a_set_not_read_is_refused(tmp_path: Path, capsys) -> None:
    out = tmp_path / "r.json"
    assert driver.main(["--set", "both", "--qid", "H01", "--qid", "G01-lookup-tumblers", "--stub", str(FIXTURE), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["oracle_labels"]["requests"] == 53
    rc = driver.main(["--set", "heldout", "--qid", "H01", "--stub", str(FIXTURE), "--oracle", str(om.DEFAULT_ORACLE_PATH), "--out", str(out)])
    assert rc == 2 and "does not read" in capsys.readouterr().err
    rc = driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--stub", str(FIXTURE), "--heldout-drafted", str(om.HELDOUT_DRAFTED_PATH), "--out", str(out)])
    assert rc == 2 and "does not read" in capsys.readouterr().err
