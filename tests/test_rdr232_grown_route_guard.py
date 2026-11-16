# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-a4g2a: which grown plans of unknown kind a lookup or a listing still takes (none, on an untrusted route).

Moved out of ``test_rdr232_eligibility.py`` (file-size ceiling). halcyon's decisions under rule 6: cases 1 and 3 on
2026-11-11 (a grown plan that recorded no kind serves no lookup or listing; a listing refuses a grown plan that
recorded another kind), and case 2 on 2026-11-16 (a grown plan that recorded the request's own kind is refused too,
``grown_route_untrusted_label``, because that kind is the growing request's label and RDR-232 reads no kind from
steps). So a grown plan on an untrusted route serves no request kind until a reviewed decision trusts its route.
Guards, not measured effects: the library held the same five grown plans (517, 594-597) from 2026-11-07 to
2026-11-16, and none recorded lookup or discovery.
"""

from __future__ import annotations

import pytest

from nexus.plans import eligibility as el
from tests.test_rdr232_eligibility import (
    A,
    B,
    C,
    D,
    GROWN_COMPARISON,
    GROWN_DISCOVERY,
    GROWN_LOOKUP,
    GROWN_LOOKUP_HEURISTIC,
    GROWN_SYNTHESIS,
    GROWN_UNKNOWN,
    K,
    L,
    S,
    U,
    _ids,
    _match,
    _row,
)

GROWN_UNTRUSTED_CLAIM = _row(907, "grown-untrusted", kind=L, tags="ad-hoc", provenance="curated")


def test_a_grown_plan_that_recorded_no_kind_does_not_serve_a_lookup() -> None:
    out, filt = _match([GROWN_UNKNOWN], [(901, 0.05)], L)
    assert out == [] and filt.excluded_plan_ids == [901]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_unrecorded": 1}


@pytest.mark.parametrize("row", [GROWN_LOOKUP, GROWN_LOOKUP_HEURISTIC], ids=lambda r: r["name"])
def test_a_grown_plan_that_recorded_lookup_serves_no_lookup_on_an_untrusted_route(row) -> None:
    # Case 2 (halcyon, 2026-11-16): a recorded lookup is the growing request's label, not the plan's shape (run 897
    # recorded lookup on a 6-step synthesis-shaped plan), and RDR-232 reads no kind from steps.
    out, filt = _match([row], [(row["id"], 0.05)], L)
    assert out == [] and filt.excluded_plan_ids == [row["id"]]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_untrusted_label": 1}


def test_an_untrusted_curated_claim_serves_no_lookup_and_names_its_defect() -> None:
    out, filt = _match([GROWN_UNTRUSTED_CLAIM], [(907, 0.05)], L)
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"untrusted_curated_claim": 1}


def test_a_discovery_request_refuses_a_grown_plan_that_recorded_discovery_on_an_untrusted_route() -> None:
    out, filt = _match([GROWN_DISCOVERY], [(906, 0.05)], D)
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_untrusted_label": 1}


@pytest.mark.parametrize("enforce", [True, False], ids=["enforced", "shadow"])
def test_an_untrusted_label_refusal_reports_the_recorded_kind_that_caused_it(enforce) -> None:
    _, filt = _match([GROWN_LOOKUP, GROWN_LOOKUP_HEURISTIC], [(902, 0.05), (903, 0.06)], L, enforce=enforce)
    assert filt.to_wire()["refused_recorded_kinds"] == {"lookup": 2}
    _, filt = _match([GROWN_DISCOVERY], [(906, 0.05)], D, enforce=enforce)
    assert filt.to_wire()["refused_recorded_kinds"] == {"discovery/listing": 1}


@pytest.mark.parametrize(
    "row", [GROWN_UNKNOWN, GROWN_LOOKUP, GROWN_LOOKUP_HEURISTIC, GROWN_SYNTHESIS, GROWN_COMPARISON, GROWN_DISCOVERY],
    ids=lambda r: r["name"],
)
@pytest.mark.parametrize("request_kind", [L, S, D, C, A, B, U])
def test_an_untrusted_route_grown_plan_serves_no_request_kind(row, request_kind: K) -> None:
    out, _ = _match([row], [(row["id"], 0.05)], request_kind)
    assert out == []


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        pytest.param(GROWN_SYNTHESIS, "grown_route_not_discovery", id="recorded-synthesis"),
        pytest.param(GROWN_COMPARISON, "grown_route_not_discovery", id="recorded-comparison"),
        pytest.param(GROWN_LOOKUP, "grown_route_not_discovery", id="recorded-lookup"),
        pytest.param(GROWN_UNKNOWN, "grown_route_unrecorded", id="recorded-nothing"),
    ],
)
def test_a_discovery_request_refuses_a_grown_plan_that_recorded_anything_else(row, reason) -> None:
    out, filt = _match([row], [(row["id"], 0.05)], D)
    assert out == [] and filt.excluded_plan_ids == [row["id"]]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {reason: 1}


@pytest.mark.parametrize("enforce", [True, False], ids=["enforced", "shadow"])
def test_a_discovery_refusal_reports_the_recorded_kind_that_caused_it(enforce) -> None:
    _, filt = _match([GROWN_SYNTHESIS, GROWN_COMPARISON], [(904, 0.05), (905, 0.06)], D, enforce=enforce)
    assert filt.to_wire()["refused_recorded_kinds"] == {"comparison": 1, "synthesis": 1}


def test_the_new_guard_runs_in_shadow_like_the_matrix() -> None:
    out, filt = _match([GROWN_UNKNOWN], [(901, 0.05)], L, enforce=False)
    assert _ids(out) == [901]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_unrecorded": 1}


@pytest.mark.parametrize(("row", "request_kind"), [(GROWN_LOOKUP, L), (GROWN_DISCOVERY, D)], ids=["lookup", "listing"])
def test_the_untrusted_label_refusal_runs_in_shadow_like_the_matrix(row, request_kind: K) -> None:
    out, filt = _match([row], [(row["id"], 0.05)], request_kind, enforce=False)
    assert _ids(out) == [row["id"]], "shadow keeps the candidate"
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_untrusted_label": 1}
    assert filt.enforced_excluded_total == 0


def test_the_new_reasons_are_in_the_closed_vocabulary() -> None:
    assert {"grown_route_unrecorded", "grown_route_not_discovery", "grown_route_untrusted_label"} <= el.REASONS


def test_an_untrusted_curated_claim_serves_no_listing_either() -> None:
    out, filt = _match([GROWN_UNTRUSTED_CLAIM], [(907, 0.05)], D)
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"untrusted_curated_claim": 1}


@pytest.mark.parametrize(
    ("row", "request_kind"),
    [pytest.param(GROWN_LOOKUP, L, id="trusted-lookup-serves-a-lookup"),
     pytest.param(GROWN_DISCOVERY, D, id="trusted-discovery-serves-a-listing")],
)
def test_a_grown_plan_on_a_trusted_route_is_not_read_as_unrecorded(monkeypatch, row, request_kind) -> None:
    # Review r1 of 45969ecb0: a trusted route keeps the plan's known kind (recorded_kind stays None); the guard is for
    # unknown-kind grown plans only, so the matrix decides and the plan serves its own kind.
    monkeypatch.setattr(el, "TRUSTED_ROUTES", frozenset({"explicit"}))
    out, _ = _match([row], [(row["id"], 0.05)], request_kind)
    assert _ids(out) == [row["id"]]
