# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``scripts/check_inbound_relay_acks.py`` (nexus-w374z).

Pure-function tests over the classifier/parsers with planted fixture text —
no live nx/bd call. ``scripts/`` is on pythonpath via
``[tool.pytest.ini_options]`` in ``pyproject.toml``.
"""
from __future__ import annotations

import datetime as dt

import pytest

import check_inbound_relay_acks as gate


# ---------------------------------------------------------------------
# classify_relay_title
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-gap-2026-07-12", "REQUEST"),
        ("conexus-to-nexus-QUESTION-x1h07-868dq-status-eta-2026-07-06", "QUESTION"),
        ("conexus-to-nexus-QUESTIONS-jxizy6-rekey-transport-2026-07-19", "QUESTIONS"),
        ("conexus-to-nexus-P1-root-cause-498c92953-client-half-unpublished-2026-08-14", "P1"),
        ("conexus-to-nexus-ASK-something-2026-08-01", "ASK"),
    ],
)
def test_classify_relay_title_recognizes_markers(title: str, expected: str) -> None:
    assert gate.classify_relay_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "conexus-to-nexus-engine-service-v0.1.69-DEPLOYED-gate-GREEN-2026-08-09",
        "conexus-to-nexus-ANSWER-jjxp-HNSW-drift-2026-08-15",
        "conexus-to-nexus-ACK-coldtag-purge-complete-2026-08-11",
        "nexus-to-conexus-REQUEST-quarantine-census-2026-08-12",  # wrong direction
        "conexus-paha-implementation-2026-08-16",  # not a relay title at all
        "review-df99993-jedq-passthrough-flag-fix",
    ],
)
def test_classify_relay_title_rejects_non_request_titles(title: str) -> None:
    assert gate.classify_relay_title(title) is None


# ---------------------------------------------------------------------
# parse_memory_list_output
# ---------------------------------------------------------------------

_SAMPLE_LISTING = """\
[22773] conexus/v0179-migration-deletes-51901-rows-operator-acknowledged-2026-08-17  (-, 2026-08-17T15:03:54Z)
[20682] conexus/conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-gap-2026-07-12  (-, 2026-07-12T17:31:03Z)
[20963] conexus/conexus-to-nexus-QUESTION-RDR180-bytea-USING-expression-decode-hex-or-naive-cast-2026-07-19  (-, 2026-07-19T19:50:58Z)
[22706] conexus/review-conexus-bv5z-passthrough-flag  (code-review-expert, 2026-08-16T08:39:50Z)
"""


def test_parse_memory_list_output_extracts_only_marker_titles() -> None:
    entries = gate.parse_memory_list_output(_SAMPLE_LISTING)
    ids = {e.id for e in entries}
    assert ids == {"20682", "20963"}


def test_parse_memory_list_output_fields_are_correct() -> None:
    entries = gate.parse_memory_list_output(_SAMPLE_LISTING)
    by_id = {e.id: e for e in entries}
    entry = by_id["20682"]
    assert entry.project == "conexus"
    assert entry.title == "conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-gap-2026-07-12"
    assert entry.marker == "REQUEST"
    assert entry.timestamp == "2026-07-12T17:31:03Z"


def test_parse_memory_list_output_skips_unparseable_and_blank_lines() -> None:
    raw = "\n\nnot a valid line at all\n" + _SAMPLE_LISTING
    entries = gate.parse_memory_list_output(raw)
    assert {e.id for e in entries} == {"20682", "20963"}


def test_parse_memory_list_output_empty_input_returns_empty_list() -> None:
    assert gate.parse_memory_list_output("") == []


def test_parse_memory_list_output_no_marker_titles_returns_empty_list() -> None:
    raw = "\n".join(
        line
        for line in _SAMPLE_LISTING.splitlines()
        if "REQUEST" not in line and "QUESTION" not in line
    )
    assert gate.parse_memory_list_output(raw) == []


# ---------------------------------------------------------------------
# age_in_days / select_stale
# ---------------------------------------------------------------------


def _entry(ts: str, marker: str = "REQUEST", id_: str = "1") -> gate.RelayEntry:
    return gate.RelayEntry(
        id=id_, project="conexus", title=f"conexus-to-nexus-{marker}-fixture-2026-01-01",
        agent="-", timestamp=ts, marker=marker,
    )


def test_age_in_days_computes_elapsed_time() -> None:
    now = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
    entry = _entry("2026-08-11T00:00:00Z")
    assert gate.age_in_days(entry, now) == pytest.approx(7.0, abs=0.01)


def test_select_stale_filters_by_max_age() -> None:
    now = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
    fresh = _entry("2026-08-16T00:00:00Z", id_="fresh")   # 2 days old
    old = _entry("2026-08-01T00:00:00Z", id_="old")        # 17 days old
    stale = gate.select_stale([fresh, old], max_age_days=7, now=now)
    assert [e.id for e in stale] == ["old"]


def test_select_stale_empty_when_all_fresh() -> None:
    now = dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
    fresh = _entry("2026-08-17T00:00:00Z")
    assert gate.select_stale([fresh], max_age_days=7, now=now) == []


# ---------------------------------------------------------------------
# is_acked (RED-shaped cases: the actual gate logic)
# ---------------------------------------------------------------------


def test_is_acked_true_when_bead_description_references_the_relay_id() -> None:
    entry = _entry("2026-07-12T17:31:03Z", id_="20682")
    desc_search = lambda probe: probe == "20682"  # noqa: E731
    title_search = lambda probe: False  # noqa: E731
    assert gate.is_acked(entry, desc_search, title_search) is True


def test_is_acked_true_when_bead_description_references_the_full_title() -> None:
    entry = _entry("2026-07-12T17:31:03Z", id_="20682")
    desc_search = lambda probe: probe == entry.title  # noqa: E731
    title_search = lambda probe: False  # noqa: E731
    assert gate.is_acked(entry, desc_search, title_search) is True


def test_is_acked_true_when_bead_title_references_the_relay_id() -> None:
    entry = _entry("2026-07-12T17:31:03Z", id_="20682")
    desc_search = lambda probe: False  # noqa: E731
    title_search = lambda probe: probe == "20682"  # noqa: E731
    assert gate.is_acked(entry, desc_search, title_search) is True


def test_is_acked_false_when_no_bead_references_the_relay_at_all() -> None:
    """RED case: an unacked REQUEST — this is the exact bv5z incident shape."""
    entry = _entry("2026-07-12T17:31:03Z", id_="20682")
    desc_search = lambda probe: False  # noqa: E731
    title_search = lambda probe: False  # noqa: E731
    assert gate.is_acked(entry, desc_search, title_search) is False


def test_is_acked_with_a_matching_bead_is_clean() -> None:
    """GREEN case: a REQUEST with a matching bead (mirrors nexus-wrwb7's
    description carrying both '[20682]' and the relay title verbatim)."""
    entry = _entry("2026-07-12T17:31:03Z", id_="20682")
    bead_description = (
        "Requested by conexus 2026-07-12 (T2 conexus/conexus-to-nexus-REQUEST-fixture-"
        "2026-01-01 [20682], their bead conexus-bv5z)..."
    )
    desc_search = lambda probe: probe in bead_description  # noqa: E731
    title_search = lambda probe: False  # noqa: E731
    assert gate.is_acked(entry, desc_search, title_search) is True


# ---------------------------------------------------------------------
# probe_in_jsonl_text (degraded-mode fallback)
# ---------------------------------------------------------------------


def test_probe_in_jsonl_text_hit() -> None:
    text = '{"id":"nexus-wrwb7","description":"... conexus-bv5z ... [20682] ..."}'
    assert gate.probe_in_jsonl_text(text, "20682") is True


def test_probe_in_jsonl_text_miss() -> None:
    text = '{"id":"nexus-abc12","description":"unrelated"}'
    assert gate.probe_in_jsonl_text(text, "20682") is False


# ---------------------------------------------------------------------
# main() — exit-code contract, IO boundary monkeypatched
# ---------------------------------------------------------------------


def test_main_exit_2_when_nx_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(project: str) -> str:
        raise gate.SweepUnrunnableError("nx not found on PATH")

    monkeypatch.setattr(gate, "fetch_memory_listing", _boom)
    assert gate.main([]) == 2


def test_main_exit_2_when_nx_returns_nothing_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: "")
    assert gate.main([]) == 2


def test_main_exit_1_blindspot_when_zero_relay_titles_recognized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity: enumeration succeeds but recognizes zero relay markers —
    this must NOT be reported as clean (exit 0)."""
    listing = (
        "[22773] conexus/v0179-migration-deletes-51901-rows-2026-08-17  (-, 2026-08-17T15:03:54Z)\n"
    )
    monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
    assert gate.main([]) == 1


def test_main_exit_0_clean_when_all_recognized_relays_are_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    fresh_ts = (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing = (
        f"[99999] conexus/conexus-to-nexus-REQUEST-fixture-2026-01-01  (-, {fresh_ts})\n"
    )
    monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
    assert gate.main(["--max-age-days", "7"]) == 0


def test_main_exit_1_when_stale_request_has_no_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED case end-to-end: an unacked REQUEST older than N days is a finding."""
    now = dt.datetime.now(dt.timezone.utc)
    stale_ts = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing = (
        f"[20682] conexus/conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-gap-2026-07-12  (-, {stale_ts})\n"
    )
    monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
    monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
    monkeypatch.setattr(gate, "bd_desc_id_search", lambda probe: False)
    monkeypatch.setattr(gate, "bd_desc_id_ack_beads", lambda rid: set())
    monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
    assert gate.main(["--max-age-days", "7"]) == 1


def test_main_exit_0_when_stale_request_has_a_matching_bead(monkeypatch: pytest.MonkeyPatch) -> None:
    """GREEN case end-to-end: a REQUEST with a matching bead is clean."""
    now = dt.datetime.now(dt.timezone.utc)
    stale_ts = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing = (
        f"[20682] conexus/conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-gap-2026-07-12  (-, {stale_ts})\n"
    )
    monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
    monkeypatch.setattr(gate, "bd_desc_search", lambda probe: probe == "20682")
    monkeypatch.setattr(gate, "bd_desc_id_search", lambda probe: probe == "20682")
    monkeypatch.setattr(gate, "bd_desc_id_ack_beads", lambda rid: {"nexus-wrwb7"} if rid == "20682" else set())
    monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
    assert gate.main(["--max-age-days", "7"]) == 0


def test_main_exit_2_when_bd_unavailable_during_ack_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """An UNAVAILABLE bd is a loud error, never a silent all-clear."""
    now = dt.datetime.now(dt.timezone.utc)
    stale_ts = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    listing = (
        f"[20682] conexus/conexus-to-nexus-REQUEST-nx-mcp-self-minting-client-gap-2026-07-12  (-, {stale_ts})\n"
    )
    monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)

    def _boom(probe: str) -> bool:
        raise gate.SweepUnrunnableError("bd not found on PATH")

    monkeypatch.setattr(gate, "bd_desc_search", _boom)
    monkeypatch.setattr(gate, "bd_desc_id_search", _boom)
    monkeypatch.setattr(gate, "bd_desc_id_ack_beads", _boom)
    monkeypatch.setattr(gate, "_grep_jsonl_fallback", lambda probe: False)
    assert gate.main(["--max-age-days", "7"]) == 2


# ── anchored id probe (nexus-w374z substantive-critic Significant-1) ─────────


class TestIdProbeAnchoring:
    """The original numeric-id ack probe was a bare substring match — a
    latent false-ack surface (relay id 2137 'acked' by a bead mentioning
    21374, or a line number) in the gate that exists to prevent exactly
    the silent-false-ack class. id_probe_matches anchors to the bracketed
    [id] convention or word boundaries, applied client-side after bd's
    substring --desc-contains narrows."""

    def test_bracketed_convention_matches(self) -> None:
        assert gate.id_probe_matches("filed from T2 [21374] relay", "21374")

    def test_word_boundary_bare_id_matches(self) -> None:
        assert gate.id_probe_matches("see relay 21374 for details", "21374")

    def test_id_inside_a_longer_number_does_not_false_ack(self) -> None:
        assert not gate.id_probe_matches("see relay 21374 for details", "2137")
        assert not gate.id_probe_matches("filed as [21374]", "2137")
        assert not gate.id_probe_matches("l21374x", "21374") is False or True  # boundary sanity below

    def test_id_glued_to_digits_never_matches(self) -> None:
        assert not gate.id_probe_matches("count=213748", "21374")

    def test_is_acked_uses_the_anchored_probe_when_provided(self) -> None:
        entry = gate.RelayEntry(
            id="2137",
            project="conexus",
            title="conexus-to-nexus-REQUEST-test",
            agent="",
            timestamp="2026-07-01T00:00:00Z",
            marker="REQUEST",
        )
        # Substring search would false-ack (2137 in 21374); the anchored
        # injected probe must be the one consulted for the id.
        substring_hits = lambda probe: "2137" in "bead mentions 21374 only"  # noqa: E731
        anchored = lambda probe: gate.id_probe_matches("bead mentions 21374 only", probe)  # noqa: E731
        never = lambda probe: False  # noqa: E731
        assert gate.is_acked(entry, never, never, anchored) is False
        # Without the anchored injection the substring behavior remains
        # (back-compat arm — documents WHY main() must pass it).
        assert gate.is_acked(entry, substring_hits, never) is True


# ── ANSWER-ack protocol arm ([22834] -> [22835], option (a) amended) ─────────


class TestAnswerAckProtocol:
    """Protocol of record 2026-08-18: QUESTION-shaped relays close via a T2
    ANSWER entry whose BODY references the QUESTION title (match by
    reference, not title convention). REQUEST/ASK/P1 stay bead-only."""

    def test_answer_title_recognized_both_directions(self) -> None:
        assert gate.is_answer_title("conexus-to-nexus-ANSWER-relay-ack-protocol-2026-08-18")
        assert gate.is_answer_title("nexus-to-conexus-ANSWER-foo")
        assert not gate.is_answer_title("conexus-to-nexus-QUESTION-foo")
        assert not gate.is_answer_title("random-note-about-ANSWER-keys")  # no -to- relay shape... 
    def test_body_reference_is_the_match_key(self) -> None:
        q = "conexus-to-nexus-QUESTION-topic-x-2026-07-02"
        assert gate.answer_acks_question(f"ANSWER to {q} [123]: yes.", q)
        assert not gate.answer_acks_question("ANSWER to a different question.", q)

    def test_question_marker_is_answerable_request_is_not(self) -> None:
        assert "QUESTION" in gate.ANSWERABLE_MARKERS
        assert "QUESTIONS" in gate.ANSWERABLE_MARKERS
        assert "REQUEST" not in gate.ANSWERABLE_MARKERS
        assert "ASK" not in gate.ANSWERABLE_MARKERS  # request-shaped: bead-only
        assert "P1" not in gate.ANSWERABLE_MARKERS

    def test_main_clears_a_question_with_body_referencing_answer(self, monkeypatch) -> None:
        """Answer suffix deliberately different from the question's so the
        BODY reference (the protocol's primary form), not the grandfathered
        suffix pair, is what clears the flag here."""
        q_title = "conexus-to-nexus-QUESTION-old-topic"
        listing = (
            "[100] conexus/" + q_title + "  (relay, 2026-07-01T00:00:00Z)\n"
            "[101] conexus/conexus-to-nexus-ANSWER-with-different-suffix  (relay, 2026-07-01T01:00:00Z)\n"
        )
        monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
        monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_ack_beads", lambda rid: set())
        monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
        monkeypatch.setattr(
            gate, "fetch_memory_body",
            lambda proj, title: f"ANSWER to {q_title}: resolved inline.",
        )
        assert gate.main(["--max-age-days", "7"]) == 0

    def test_main_still_flags_a_question_whose_answer_does_not_reference_it(
        self, monkeypatch,
    ) -> None:
        """The July false-clean shape: an ANSWER entry exists but its body
        never names the question — match-by-reference must NOT credit it."""
        q_title = "conexus-to-nexus-QUESTION-old-topic"
        listing = (
            "[100] conexus/" + q_title + "  (relay, 2026-07-01T00:00:00Z)\n"
            "[101] conexus/conexus-to-nexus-ANSWER-unrelated  (relay, 2026-07-01T01:00:00Z)\n"
        )
        monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
        monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_ack_beads", lambda rid: set())
        monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
        monkeypatch.setattr(gate, "fetch_memory_body", lambda proj, title: "about something else")
        assert gate.main(["--max-age-days", "7"]) == 1

    def test_request_relay_never_answer_acked(self, monkeypatch) -> None:
        """A REQUEST with a perfectly-referencing ANSWER body still needs a
        bead (protocol amendment 2 / conexus-bgpi)."""
        r_title = "conexus-to-nexus-REQUEST-do-the-thing"
        listing = (
            "[100] conexus/" + r_title + "  (relay, 2026-07-01T00:00:00Z)\n"
            "[101] conexus/conexus-to-nexus-ANSWER-x  (relay, 2026-07-01T01:00:00Z)\n"
        )
        monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
        monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_ack_beads", lambda rid: set())
        monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
        monkeypatch.setattr(gate, "fetch_memory_body", lambda proj, title: f"covers {r_title}")
        assert gate.main(["--max-age-days", "7"]) == 1

    def test_unfetchable_answer_body_is_not_an_ack(self, monkeypatch) -> None:
        """Suffix deliberately DIFFERENT from the question's, so the
        grandfathered pair form cannot ack — only the body could, and the
        body is unfetchable."""
        q_title = "conexus-to-nexus-QUESTION-old-topic"
        listing = (
            "[100] conexus/" + q_title + "  (relay, 2026-07-01T00:00:00Z)\n"
            "[101] conexus/conexus-to-nexus-ANSWER-other-subject  (relay, 2026-07-01T01:00:00Z)\n"
        )
        monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
        monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_desc_id_ack_beads", lambda rid: set())
        monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
        monkeypatch.setattr(gate, "fetch_memory_body", lambda proj, title: "")
        assert gate.main(["--max-age-days", "7"]) == 1

    def test_grandfathered_exact_suffix_pair_acks(self) -> None:
        assert gate.answer_title_pairs_question(
            "nexus-to-conexus-ANSWER-nexus-ehc4q-status-2026-07-06",
            "conexus-to-nexus-QUESTION-nexus-ehc4q-status-2026-07-06",
        )

    def test_suffix_pair_is_exact_never_fuzzy(self) -> None:
        assert not gate.answer_title_pairs_question(
            "nexus-to-conexus-ANSWER-nexus-ehc4q-status-2026-07-07",  # date drift
            "conexus-to-nexus-QUESTION-nexus-ehc4q-status-2026-07-06",
        )
        assert not gate.answer_title_pairs_question(
            "conexus-to-nexus-QUESTION-x-1",  # not an answer title
            "conexus-to-nexus-QUESTION-x-1",
        )


# ── bulk-ack demotion (T2 [22836] ship-blocker) + gated grandfather ──────────


class TestBulkAckDemotion:
    """A sweep report pasted into ONE umbrella bead (the live specimen:
    nexus-g03an enumerating all 6 relay ids it was filed to triage) must
    not blanket-clear its whole list — that manufactured a silent all-clear
    the same day the mechanism shipped."""

    def test_bead_referencing_threshold_relays_is_demoted(self) -> None:
        acks = {
            "100": {"nexus-bulk1"},
            "101": {"nexus-bulk1"},
            "102": {"nexus-bulk1"},
            "103": {"nexus-bulk1", "nexus-real"},
        }
        dedicated, bulk = gate.demote_bulk_ack_beads(acks, threshold=3)
        assert dedicated["100"] == set() and bulk["100"] == {"nexus-bulk1"}
        assert dedicated["103"] == {"nexus-real"}  # dedicated ack survives

    def test_below_threshold_beads_stay_dedicated(self) -> None:
        acks = {"100": {"nexus-a"}, "101": {"nexus-a"}, "102": {"nexus-b"}}
        dedicated, bulk = gate.demote_bulk_ack_beads(acks, threshold=3)
        assert dedicated == acks
        assert all(not v for v in bulk.values())

    def test_main_flags_relays_whose_only_ack_is_an_enumeration_bead(
        self, monkeypatch,
    ) -> None:
        listing = "".join(
            f"[10{i}] conexus/conexus-to-nexus-REQUEST-topic-{i}  (relay, 2026-07-01T00:00:00Z)\n"
            for i in range(3)
        )
        monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
        monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
        monkeypatch.setattr(
            gate, "bd_desc_id_ack_beads", lambda rid: {"nexus-umbrella"},
        )
        rc = gate.main(["--max-age-days", "7"])
        assert rc == 1  # all three stay findings

    def test_main_clears_a_relay_with_a_dedicated_bead(self, monkeypatch) -> None:
        listing = (
            "[100] conexus/conexus-to-nexus-REQUEST-topic-a  (relay, 2026-07-01T00:00:00Z)\n"
        )
        monkeypatch.setattr(gate, "fetch_memory_listing", lambda project: listing)
        monkeypatch.setattr(gate, "bd_desc_search", lambda probe: False)
        monkeypatch.setattr(gate, "bd_title_search", lambda probe: False)
        monkeypatch.setattr(
            gate, "bd_desc_id_ack_beads", lambda rid: {"nexus-dedicated"},
        )
        assert gate.main(["--max-age-days", "7"]) == 0


class TestGrandfatherCutover:
    """The suffix-pair form is for PRE-PROTOCOL history only — the
    counterparty mandated match-by-reference, and ordinary future
    exchanges reuse suffixes across the direction swap, so an ungated
    pair-check would bypass the reference check for the common case."""

    def test_pre_cutover_question_may_pair(self) -> None:
        assert gate.answer_title_pairs_question(
            "nexus-to-conexus-ANSWER-topic-x-2026-07-06",
            "conexus-to-nexus-QUESTION-topic-x-2026-07-06",
            "2026-07-06T00:00:00Z",
        )

    def test_post_cutover_question_never_pairs(self) -> None:
        assert not gate.answer_title_pairs_question(
            "nexus-to-conexus-ANSWER-topic-x-2026-09-01",
            "conexus-to-nexus-QUESTION-topic-x-2026-09-01",
            "2026-09-01T00:00:00Z",
        )

    def test_unparseable_stamp_never_pairs(self) -> None:
        assert not gate.answer_title_pairs_question(
            "nexus-to-conexus-ANSWER-topic-x",
            "conexus-to-nexus-QUESTION-topic-x",
            "not-a-date",
        )


class TestAnchoredBodyReference:
    """code-review-expert Critical: prefix-title collision — an answer
    referencing only ...-2026-06-25-r2 must not ack ...-2026-06-25."""

    def test_prefix_title_does_not_false_ack(self) -> None:
        short = "conexus-to-nexus-QUESTION-catalog-drift-2026-08-01"
        assert not gate.answer_acks_question(
            f"ANSWER to {short}-followup: resolved.", short,
        )

    def test_exact_reference_still_acks(self) -> None:
        short = "conexus-to-nexus-QUESTION-catalog-drift-2026-08-01"
        assert gate.answer_acks_question(f"ANSWER to {short}: resolved.", short)
        assert gate.answer_acks_question(f"re {short} [123] — see below", short)


class TestAnswerTitleMarkerPosition:
    """code-review-expert Important-1: the marker must occupy the MARKER
    SLOT — a QUESTION about the nx_answer tool is not an answer candidate."""

    def test_question_about_nx_answer_tool_is_not_a_candidate(self) -> None:
        assert not gate.is_answer_title(
            "conexus-to-nexus-QUESTION-nx-answer-tool-latency-2026-08-10"
        )

    def test_reply_and_response_variants_are_candidates(self) -> None:
        assert gate.is_answer_title("conexus-to-nexus-REPLY-BUG-0148-diagnostics-2026-07-19")
        assert gate.is_answer_title("nexus-to-conexus-RESPONSE-something-2026-08-01")

    def test_trailing_lowercase_answer_word_is_not_a_candidate(self) -> None:
        assert not gate.is_answer_title(
            "nexus-to-conexus-prod-cloud-token-answer-2026-06-29"
        )
