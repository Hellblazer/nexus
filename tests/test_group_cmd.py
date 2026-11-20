# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx group`` and ``nexus.groups`` (RDR-252 Step 4 part B1, nexus-igea9.7).

Tenant-free: the client and the verbs run against ``httpx.MockTransport`` with canned engine answers, and say what each
verb SENDS (route, body, which credential, whether the identity token rides) and PRINTS. The engine-backed rows of the
same verbs (form, set, disband, override, join, leave, list, ack-times against a real engine) are in
tests/identity/test_group_journeys.py: they hold no test and mint no tenant, and one ``_journey`` entry of
tests/identity/test_identity_engine_journeys.py runs them on that suite's one tenant (the tenant ceiling,
tests/storage_touch_ceiling.toml, is Sam's and is not raised for them).
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus import groups
from nexus.cli import main
from nexus.commands.group_cmd import GroupEnv, group_group
from nexus.groups import GroupRecord, GroupsClient, GroupsError
from nexus.identity.token_header import GenerationSupersededError

TOKEN = {"X-Nexus-Identity-Token": "tok-1"}
RECORD: dict[str, Any] = {
    "name": "push-train", "leader": "i3", "purpose": None, "requested_by": "i3", "formed_at": "2026-11-19T10:00:00Z",
    "ends": None, "ended_at": None, "ack_deadline": 20, "ack_deadline_by_topic": {"nexus-develop": 30},
    "serves": ["nexus-develop"], "live": True,
}


class Wire:
    """An httpx transport that answers by ``(method, path)`` and records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[tuple[int, Any]]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, status: int = 200, body: Any = None) -> Wire:
        self.routes.setdefault((method, path), []).append((status, body if body is not None else {}))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answers = self.routes.get((request.method, request.url.path))
        if not answers:
            return httpx.Response(404, json={"error": "not_found", "message": f"no route {request.method} {request.url.path}"})
        status, body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(status, text=body) if isinstance(body, str) else httpx.Response(status, json=body)

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def body(self, method: str, path: str) -> dict[str, Any]:
        return json.loads(self.sent(method, path)[-1].content)


def _client(wire: Wire, *, headers: Callable[[], dict[str, str]] = lambda: dict(TOKEN),
            retry: Callable[[Mapping[str, str], str], dict[str, str] | None] = lambda sent, path: None) -> GroupsClient:
    return GroupsClient("http://engine", "cred", http=httpx.Client(transport=httpx.MockTransport(wire)),
                        identity_headers=headers, retry_headers=retry)


def _env(wire: Wire, *, caller: str | None = "i3", tuples: Any = None, **client_kw: Any) -> GroupEnv:
    return GroupEnv(client=_client(wire, **client_kw), caller=lambda: caller, tuples=lambda: tuples)


def _run(env: GroupEnv, *args: str) -> Any:
    return CliRunner().invoke(group_group, list(args), obj={"group_env": env})


def _view(identity: str, *, kind: str = "seat", name: str | None = None, state: str = "live", lease: str = "leased") -> dict[str, Any]:
    return {"id": identity, "kind": kind, "name": name, "state": state, "lease": lease, "liveness": state, "generation": 1}


# ── the client ──────────────────────────────────────────────────────────────


def test_form_sends_the_leader_and_the_fields_with_the_identity_token_and_the_bearer() -> None:
    wire = Wire().on("POST", "/v1/group", 200, RECORD)
    client = _client(wire)
    got = client.form("push-train", "i3", serves=["nexus-develop"], ack_deadline=20, ends="open", purpose="order",
                      ack_deadline_by_topic={"nexus-develop": 30}, requested_by="sam")
    assert got["leader"] == "i3"
    request = wire.sent("POST", "/v1/group")[0]
    assert json.loads(request.content) == {
        "name": "push-train", "leader": "i3", "serves": ["nexus-develop"], "ack_deadline": 20, "ends": "open",
        "purpose": "order", "ack_deadline_by_topic": {"nexus-develop": 30}, "requested_by": "sam"}
    assert request.headers["X-Nexus-Identity-Token"] == "tok-1" and request.headers["Authorization"] == "Bearer cred"


def test_a_401_with_a_token_resends_once_with_the_headers_the_refresh_gives() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/disband", 401, {"error": "unauthorized"}) \
                 .on("POST", "/v1/group/push-train/disband", 200, RECORD)
    client = _client(wire, retry=lambda sent, path: {**sent, "X-Nexus-Identity-Token": "tok-2"})
    client.disband("push-train")
    tokens = [r.headers.get("X-Nexus-Identity-Token") for r in wire.sent("POST", "/v1/group/push-train/disband")]
    assert tokens == ["tok-1", "tok-2"]


def test_a_401_with_no_fresher_token_is_one_attempt_and_an_error() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/disband", 401, {"error": "unauthorized"})
    with pytest.raises(GroupsError) as err:
        _client(wire).disband("push-train")
    assert err.value.status == 401 and len(wire.sent("POST", "/v1/group/push-train/disband")) == 1


def test_the_override_carries_no_identity_token() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/override", 200, RECORD)
    _client(wire).override("push-train", leader="i4")
    request = wire.sent("POST", "/v1/group/push-train/override")[0]
    assert "X-Nexus-Identity-Token" not in request.headers, "the operator override is the one write with no token"
    assert json.loads(request.content) == {"leader": "i4"}
    wire.on("POST", "/v1/group/push-train/override", 200, RECORD)
    _client(wire).override("push-train", disband=True)
    assert wire.body("POST", "/v1/group/push-train/override") == {"ended_at": True}


def test_a_superseded_writer_is_the_typed_stop_not_a_generic_error() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/set", 409, {
        "error": "generation_superseded", "identity": "i3", "token_generation": 1, "current_generation": 2,
        "holder": {"host": "h", "pid": 7}})
    with pytest.raises(GenerationSupersededError):
        _client(wire).set("push-train", ack_deadline=25)


def test_the_group_record_reader_gives_none_for_a_name_with_no_record_and_raises_on_anything_else() -> None:
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD) \
                 .on("GET", "/v1/group/nobody", 404, {"error": "unknown_group", "message": "no group record nobody"}) \
                 .on("GET", "/v1/group/broken", 500, {"error": "internal"})
    client = _client(wire)
    record = groups.read_group(client, "push-train")
    assert isinstance(record, GroupRecord)
    assert (record.name, record.leader, record.live, record.ended_at, record.ends, record.serves) == (
        "push-train", "i3", True, None, None, ("nexus-develop",))
    assert record.ack_deadline_by_topic == {"nexus-develop": 30}
    assert groups.read_group(client, "nobody") is None
    with pytest.raises(GroupsError):
        groups.read_group(client, "broken")
    assert not wire.sent("GET", "/v1/group/push-train")[0].headers.get("X-Nexus-Identity-Token"), "a read sends no token"


def test_memberships_and_the_roster_are_reads_and_join_and_leave_carry_the_token() -> None:
    wire = Wire().on("GET", "/v1/identity/i3/memberships", 200, {"identity": "i3", "memberships": [{"group": "push-train", "role": "leader"}]}) \
                 .on("GET", "/v1/identity/groups/push-train", 200, {"group": "push-train", "members": [{"identity": "i3", "role": "leader"}]}) \
                 .on("POST", "/v1/identity/i3/memberships", 200, {"identity": "i3", "group": "push-train", "role": "member"}) \
                 .on("DELETE", "/v1/identity/i3/memberships/push-train", 200, {"identity": "i3", "group": "push-train", "removed": 1})
    client = _client(wire)
    assert [m["group"] for m in client.memberships("i3")] == ["push-train"]
    assert [m["identity"] for m in client.roster("push-train")] == ["i3"]
    client.join("i3", "push-train")
    client.leave("i3", "push-train")
    assert wire.body("POST", "/v1/identity/i3/memberships") == {"group": "push-train", "role": "member"}
    assert wire.sent("POST", "/v1/identity/i3/memberships")[0].headers["X-Nexus-Identity-Token"] == "tok-1"
    assert wire.sent("DELETE", "/v1/identity/i3/memberships/push-train")[0].headers["X-Nexus-Identity-Token"] == "tok-1"
    for read in (wire.sent("GET", "/v1/identity/i3/memberships")[0], wire.sent("GET", "/v1/identity/groups/push-train")[0]):
        assert "X-Nexus-Identity-Token" not in read.headers


# ── form ────────────────────────────────────────────────────────────────────


def test_form_makes_the_caller_the_leader_and_reports_the_record() -> None:
    wire = Wire().on("POST", "/v1/group", 200, RECORD)
    result = _run(_env(wire), "form", "push-train", "--serves", "nexus-develop", "--ack-deadline", "20",
                  "--ack-deadline-for", "nexus-develop=30")
    assert result.exit_code == 0, result.output
    body = wire.body("POST", "/v1/group")
    assert (body["name"], body["leader"], body["serves"], body["ack_deadline"], body["requested_by"]) == (
        "push-train", "i3", ["nexus-develop"], 20, "i3")
    assert body["ack_deadline_by_topic"] == {"nexus-develop": 30}
    assert "push-train" in result.output and "i3" in result.output and "nexus-develop" in result.output


def test_form_refuses_a_bad_name_a_missing_serves_and_a_caller_with_no_identity_before_any_request() -> None:
    wire = Wire().on("POST", "/v1/group", 200, RECORD)
    assert _run(_env(wire), "form", "bad name", "--serves", "x").exit_code != 0
    assert _run(_env(wire), "form", "-x", "--serves", "x").exit_code != 0
    assert _run(_env(wire), "form", "push-train").exit_code != 0, "--serves is required"
    nobody = _run(_env(wire, caller=None), "form", "push-train", "--serves", "x")
    assert nobody.exit_code != 0 and "identity" in nobody.output
    assert wire.requests == []


def test_a_refusal_prints_the_engines_code_and_reason_and_exits_one() -> None:
    wire = Wire().on("POST", "/v1/group", 409, {"error": "group_exists", "message": "push-train has a live record: update it"})
    result = _run(_env(wire), "form", "push-train", "--serves", "nexus-develop")
    assert result.exit_code == 1 and "group_exists" in result.output and "live record" in result.output


# ── set ─────────────────────────────────────────────────────────────────────


def test_set_sends_only_what_was_given_and_merges_a_per_topic_deadline_into_the_stored_map() -> None:
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD).on("POST", "/v1/group/push-train/set", 200, RECORD)
    result = _run(_env(wire), "set", "push-train", "--ack-deadline", "25", "--ack-deadline-for", "other=40")
    assert result.exit_code == 0, result.output
    assert wire.body("POST", "/v1/group/push-train/set") == {
        "ack_deadline": 25, "ack_deadline_by_topic": {"nexus-develop": 30, "other": 40}}


@pytest.mark.parametrize("minutes", ["٣", "²", "１０"])
def test_a_deadline_in_non_ascii_digits_is_a_usage_error_not_a_traceback_or_a_surprise_value(minutes: str) -> None:
    """str.isdigit() is true for a superscript two (int() then raises) and for Arabic-Indic digits (int() accepts them)."""
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_env(wire), "set", "push-train", "--ack-deadline-for", f"nexus-develop={minutes}")
    assert result.exit_code == 2 and "whole minutes" in result.output, result.output
    assert not isinstance(result.exception, ValueError)
    assert not wire.sent("POST", "/v1/group/push-train/set")


def test_set_leader_resolves_a_seat_name_ends_open_and_serves_replace() -> None:
    wire = Wire().on("GET", "/v1/identity/alias/folio", 200, {"identity": "i4", "state": "live"}) \
                 .on("POST", "/v1/group/push-train/set", 200, RECORD)
    result = _run(_env(wire), "set", "push-train", "--leader", "folio", "--ends", "open", "--serves", "a", "--serves", "b")
    assert result.exit_code == 0, result.output
    assert wire.body("POST", "/v1/group/push-train/set") == {"leader": "i4", "ends": "open", "serves": ["a", "b"]}
    wire.on("POST", "/v1/group/push-train/set", 200, RECORD)
    assert _run(_env(wire), "set", "push-train", "--leader", "i9").exit_code == 0
    assert wire.body("POST", "/v1/group/push-train/set") == {"leader": "i9"}, "an id is sent as it is"


def test_set_with_nothing_to_set_is_a_usage_error() -> None:
    wire = Wire()
    result = _run(_env(wire), "set", "push-train")
    assert result.exit_code != 0 and "nothing" in result.output.lower() and wire.requests == []


def test_a_deadline_that_is_not_a_whole_number_or_a_topic_pair_without_equals_is_refused() -> None:
    wire = Wire()
    assert _run(_env(wire), "set", "push-train", "--ack-deadline", "soon").exit_code != 0
    assert _run(_env(wire), "set", "push-train", "--ack-deadline-for", "nexus-develop").exit_code != 0
    assert _run(_env(wire), "set", "push-train", "--ack-deadline-for", "nexus-develop=ten").exit_code != 0
    assert wire.requests == []


# ── disband, override ───────────────────────────────────────────────────────


def test_disband_posts_to_the_disband_route() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/disband", 200, {**RECORD, "ended_at": "2026-11-19T11:00:00Z", "live": False})
    result = _run(_env(wire), "disband", "push-train")
    assert result.exit_code == 0, result.output
    assert wire.sent("POST", "/v1/group/push-train/disband") and "disbanded" in result.output


def test_override_takes_exactly_one_of_leader_and_disband_and_says_what_it_did() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/override", 200, {**RECORD, "leader": "i4"})
    assert _run(_env(wire), "override", "push-train").exit_code != 0
    assert _run(_env(wire), "override", "push-train", "--leader", "i4", "--disband").exit_code != 0
    assert wire.requests == []
    result = _run(_env(wire), "override", "push-train", "--leader", "i4")
    assert result.exit_code == 0 and "i4" in result.output
    assert "X-Nexus-Identity-Token" not in wire.sent("POST", "/v1/group/push-train/override")[0].headers


def test_the_override_is_not_blocked_by_a_caller_with_no_identity() -> None:
    wire = Wire().on("POST", "/v1/group/push-train/override", 200, RECORD)
    assert _run(_env(wire, caller=None), "override", "push-train", "--disband").exit_code == 0


# ── join, leave ─────────────────────────────────────────────────────────────


def test_join_refuses_an_ephemeral_and_a_delegated_identity_and_sends_nothing() -> None:
    for kind in ("ephemeral", "delegated"):
        wire = Wire().on("GET", "/v1/identity/i3", 200, _view("i3", kind=kind))
        result = _run(_env(wire), "join", "push-train")
        assert result.exit_code == 1 and kind in result.output and "seat" in result.output, result.output
        assert not wire.sent("POST", "/v1/identity/i3/memberships")


def test_join_adds_the_membership_and_says_the_mcp_server_must_reconnect() -> None:
    wire = Wire().on("GET", "/v1/identity/i3", 200, _view("i3")) \
                 .on("GET", "/v1/identity/i3/memberships", 200, {"identity": "i3", "memberships": []}) \
                 .on("POST", "/v1/identity/i3/memberships", 200, {"identity": "i3", "group": "push-train", "role": "member"})
    result = _run(_env(wire), "join", "push-train")
    assert result.exit_code == 0, result.output
    assert wire.body("POST", "/v1/identity/i3/memberships") == {"group": "push-train", "role": "member"}
    assert "reconnect" in result.output.lower() and "MCP" in result.output


def test_join_of_a_group_already_held_changes_nothing_so_a_leader_keeps_its_role() -> None:
    wire = Wire().on("GET", "/v1/identity/i3", 200, _view("i3")) \
                 .on("GET", "/v1/identity/i3/memberships", 200,
                     {"identity": "i3", "memberships": [{"group": "push-train", "role": "leader"}]})
    result = _run(_env(wire), "join", "push-train")
    assert result.exit_code == 0 and "already" in result.output and "leader" in result.output
    assert not wire.sent("POST", "/v1/identity/i3/memberships")


def test_leave_removes_the_membership_says_to_reconnect_and_reports_a_leaders_refusal() -> None:
    wire = Wire().on("DELETE", "/v1/identity/i3/memberships/push-train", 200, {"identity": "i3", "group": "push-train", "removed": 1})
    result = _run(_env(wire), "leave", "push-train")
    assert result.exit_code == 0 and "reconnect" in result.output.lower()
    wire = Wire().on("DELETE", "/v1/identity/i3/memberships/push-train", 409,
                     {"error": "leader_cannot_leave", "message": "i3 leads push-train: hand the group on first"})
    refused = _run(_env(wire), "leave", "push-train")
    assert refused.exit_code == 1 and "leader_cannot_leave" in refused.output and "reconnect" not in refused.output.lower()


def test_leave_of_a_group_never_joined_says_so() -> None:
    wire = Wire().on("DELETE", "/v1/identity/i3/memberships/push-train", 200, {"identity": "i3", "group": "push-train", "removed": 0})
    result = _run(_env(wire), "leave", "push-train")
    assert result.exit_code == 0 and "not a member" in result.output


# ── list ────────────────────────────────────────────────────────────────────


def _roster_wire() -> Wire:
    return Wire().on("GET", "/v1/group/push-train", 200, RECORD) \
        .on("GET", "/v1/identity/groups/push-train", 200, {"group": "push-train", "members": [
            {"identity": "i3", "group": "push-train", "role": "leader", "joined_at": "2026-11-19T10:00:00Z"},
            {"identity": "i4", "group": "push-train", "role": "member", "joined_at": "2026-11-19T10:05:00Z"},
            {"identity": "i5", "group": "push-train", "role": "member", "joined_at": "2026-11-19T10:06:00Z"},
            {"identity": "i6", "group": "push-train", "role": "member", "joined_at": "2026-11-19T10:07:00Z"},
            {"identity": "i7", "group": "push-train", "role": "member", "joined_at": "2026-11-19T10:08:00Z"}]}) \
        .on("GET", "/v1/identity/i3", 200, _view("i3", name="halcyon")) \
        .on("GET", "/v1/identity/i4", 200, _view("i4", name="folio", state="unheld")) \
        .on("GET", "/v1/identity/i5", 200, _view("i5", name="marginalia", state="lapsed")) \
        .on("GET", "/v1/identity/i6", 200, _view("i6", name="ghost", state="ended")) \
        .on("GET", "/v1/identity/i7", 200, _view("i7", name="desktop", lease="unleased"))


def test_list_shows_the_leader_and_the_members_by_alias_each_with_its_rdr_253_state() -> None:
    result = _run(_env(_roster_wire()), "list", "push-train")
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line.startswith("  ")}
    assert "leader" in lines["halcyon"] and "live" in lines["halcyon"]
    assert "unheld" in lines["folio"] and "lapsed" in lines["marginalia"] and "ended" in lines["ghost"]
    assert "unleased" in lines["desktop"] and "live" not in lines["desktop"].replace("unleased", "")
    assert "serves nexus-develop" in result.output and "ack deadline 20" in result.output


def test_list_json_is_the_record_and_the_members_with_their_state() -> None:
    result = _run(_env(_roster_wire()), "list", "push-train", "--json")
    data = json.loads(result.stdout)
    assert data["group"]["leader"] == "i3"
    assert [(m["name"], m["role"], m["state"]) for m in data["members"]][:2] == [("halcyon", "leader", "live"), ("folio", "member", "unheld")]


def test_list_of_a_group_with_no_record_and_of_a_disbanded_one() -> None:
    gone = Wire().on("GET", "/v1/group/nobody", 404, {"error": "unknown_group", "message": "no group record nobody"})
    result = _run(_env(gone), "list", "nobody")
    assert result.exit_code == 1 and "unknown_group" in result.output
    ended = Wire().on("GET", "/v1/group/push-train", 200, {**RECORD, "ended_at": "2026-11-19T11:00:00Z", "live": False}) \
                  .on("GET", "/v1/identity/groups/push-train", 200, {"group": "push-train", "members": []})
    out = _run(_env(ended), "list", "push-train")
    assert out.exit_code == 0 and "disbanded" in out.output


# ── a 2xx answer that is not the engine's JSON object (nexus-igea9.7 round 1, S1) ──

_HTML = "<html><body>captive portal</body></html>"


@pytest.mark.parametrize("body", [_HTML, "[]", "null", "42"])
def test_a_2xx_answer_that_is_not_a_json_object_is_an_error_never_an_empty_answer(body: str) -> None:
    wire = Wire().on("GET", "/v1/group/push-train", 200, body).on("GET", "/v1/identity/i3/memberships", 200, body)
    wire.on("GET", "/v1/identity/groups/push-train", 200, body).on("DELETE", "/v1/identity/i3/memberships/push-train", 200, body)
    client = _client(wire)
    for call in (lambda: client.read("push-train"), lambda: client.record("push-train"), lambda: client.memberships("i3"),
                 lambda: client.roster("push-train"), lambda: client.leave("i3", "push-train")):
        with pytest.raises(GroupsError):
            call()


def test_a_membership_answer_without_a_list_is_an_error_not_no_memberships() -> None:
    wire = Wire().on("GET", "/v1/identity/i3/memberships", 200, {"identity": "i3"})
    wire.on("GET", "/v1/identity/groups/push-train", 200, {"members": "nope"})
    client = _client(wire)
    with pytest.raises(GroupsError):
        client.memberships("i3")
    with pytest.raises(GroupsError):
        client.roster("push-train")


def test_leave_and_list_over_an_html_answer_exit_one_instead_of_not_a_member_or_none() -> None:
    wire = Wire().on("DELETE", "/v1/identity/i3/memberships/push-train", 200, _HTML)
    left = _run(_env(wire), "leave", "push-train")
    assert left.exit_code == 1 and "not a member" not in left.output, left.output
    listing = Wire().on("GET", "/v1/group/push-train", 200, RECORD).on("GET", "/v1/identity/groups/push-train", 200, _HTML)
    shown = _run(_env(listing), "list", "push-train")
    assert shown.exit_code == 1 and "(none)" not in shown.output, shown.output


_MALFORMED_ROWS = {
    "a_row_that_is_not_a_map": ["i4"],
    "a_row_with_no_identity": [{"group": "push-train", "role": "member"}],
    "an_identity_that_is_not_text": [{"identity": 4, "group": "push-train", "role": "member"}],
    "a_row_with_no_role": [{"identity": "i4", "group": "push-train"}],    # list formats the role: None would be a TypeError
}


@pytest.mark.parametrize("rows", list(_MALFORMED_ROWS.values()), ids=list(_MALFORMED_ROWS))
def test_a_malformed_roster_is_one_line_and_exit_one_not_a_keyerror(rows: list[Any]) -> None:
    """nexus-igea9.3 L6: ``list`` indexed the roster rows directly, so a row without ``identity`` was a KeyError traceback."""
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD).on("GET", "/v1/identity/groups/push-train", 200, {"members": rows})
    result = _run(_env(wire), "list", "push-train")
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1, (result.exception, result.output)
    assert "bad_response" in result.output and "Traceback" not in result.output, result.output


def test_a_malformed_membership_row_is_one_line_and_exit_one_in_join() -> None:
    """The same for ``join``, which looked each row's ``group`` up by index."""
    wire = Wire().on("GET", "/v1/identity/i3", 200, _view("i3")) \
                 .on("GET", "/v1/identity/i3/memberships", 200, {"identity": "i3", "memberships": [{"role": "member"}]})
    result = _run(_env(wire), "join", "push-train")
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1, (result.exception, result.output)
    assert "bad_response" in result.output and not wire.sent("POST", "/v1/identity/i3/memberships")


def test_a_record_with_no_live_field_is_not_read_as_disbanded() -> None:
    """The engine always sends ``live`` today, but an absent key is no verdict: ``live`` is None (the aura gate then uses its
    local rule) and ``list`` shows the group live while ``ended_at`` is unset, never "disbanded"."""
    bare = {k: v for k, v in RECORD.items() if k != "live"}
    assert GroupRecord.from_wire(bare).live is None
    assert GroupRecord.from_wire({**bare, "live": None}).live is None
    assert GroupRecord.from_wire({**bare, "live": False}).live is False
    wire = Wire().on("GET", "/v1/group/push-train", 200, bare).on("GET", "/v1/identity/groups/push-train", 200, {"members": []})
    result = _run(_env(wire), "list", "push-train")
    assert result.exit_code == 0 and "push-train: live;" in result.output and "disbanded" not in result.output, result.output


_UNREADABLE = {
    "no_leader": {k: v for k, v in RECORD.items() if k != "leader"},              # KeyError
    "deadline_not_a_number": {**RECORD, "ack_deadline": "soon"},                  # ValueError
    "deadline_a_list": {**RECORD, "ack_deadline": [20]},                          # TypeError
    "per_topic_not_a_map": {**RECORD, "ack_deadline_by_topic": ["nexus-develop"]},  # AttributeError
}


def _one_line_exit_one(result: Any) -> None:
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1, (result.exception, result.output)
    assert "unreadable" in result.output and "Traceback" not in result.output, result.output
    assert len([ln for ln in result.output.splitlines() if ln.strip()]) == 1, result.output


@pytest.mark.parametrize("record", list(_UNREADABLE.values()), ids=list(_UNREADABLE))
def test_a_group_record_the_wire_cannot_build_is_one_line_and_exit_one_in_list_and_set(record: dict[str, Any]) -> None:
    """nexus-igea9.7 round 2: GroupRecord.from_wire raised KeyError, ValueError or TypeError out of a verb as a traceback."""
    listing = Wire().on("GET", "/v1/group/push-train", 200, record).on("GET", "/v1/identity/groups/push-train", 200, {"members": []})
    _one_line_exit_one(_run(_env(listing), "list", "push-train"))
    _one_line_exit_one(_run(_env(listing), "list", "push-train", "--json"))
    setting = Wire().on("GET", "/v1/group/push-train", 200, record)
    _one_line_exit_one(_run(_env(setting), "set", "push-train", "--ack-deadline-for", "nexus-develop=40"))
    assert setting.sent("POST", "/v1/group/push-train/set") == [], "nothing was written over a record it could not read"
    answer = Wire().on("POST", "/v1/group/push-train/set", 200, record)
    _one_line_exit_one(_run(_env(answer), "set", "push-train", "--ack-deadline", "25"))


def test_from_wire_names_the_unreadable_record_as_a_groups_error() -> None:
    for record in _UNREADABLE.values():
        with pytest.raises(GroupsError) as seen:
            GroupRecord.from_wire(record)
        assert seen.value.code == "bad_response" and "unreadable" in str(seen.value)


# ── ack-times ───────────────────────────────────────────────────────────────

SHA1, SHA2, SHA3 = "a" * 40, "b" * 40, "c" * 40


def _row(kind: str, at: str, sha: str, who: str = "engine", sender_session: str | None = None, **body: Any) -> Any:
    return SimpleNamespace(id=f"{kind}{at}{who}{sender_session or ''}", created_at=at,
                           dims={"from": who, "kind": kind, "sha": sha}, body=json.dumps(body),
                           sender_session=sender_session)


class _Tuples:
    """A tuple store whose read answers at most *cap* rows a call, as the engine does (it may cap below the ``n`` asked)."""

    def __init__(self, rows: list[Any], cap: int | None = None) -> None:
        self.rows, self.reads, self.cap = rows, [], cap

    def rd(self, subspace: str, keys: Any = None, *, n: int = 1, since: Any = None, **_: Any) -> list[Any]:
        self.reads.append(subspace)
        rows = [r for r in self.rows if since is None or (r.created_at, r.id) > tuple(since)]
        return rows[: min(n, self.cap) if self.cap else n]


def test_ack_times_reports_per_red_the_sha_episode_ack_time_and_minutes() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4"),
        _row("clear", "2026-11-19T10:30:00Z", SHA1),
        # a second red: handed off, and the first owner's late ack does not count
        _row("owner", "2026-11-19T12:00:05Z", SHA2, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T12:00:00Z", refresh=False),
        # the engine's hand-off row carries a NEW episode_at (OwnerDecision.handOn passes in.now()), not the red's
        _row("owner", "2026-11-19T12:20:05Z", SHA2, owner="i5", next=None, reason="handoff", episode_at="2026-11-19T12:20:00Z", refresh=False, handoffs=1),
        _row("ack", "2026-11-19T12:25:00Z", SHA2, who="i4"),
        _row("ack", "2026-11-19T12:41:00Z", SHA2, who="i5"),
        # a third red nobody acknowledged yet
        _row("owner", "2026-11-19T14:00:05Z", SHA3, owner="i4", next="i5", reason="no_record", episode_at="2026-11-19T14:00:00Z", refresh=False),
    ]
    tuples = _Tuples(rows)
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_env(wire, tuples=tuples), "ack-times", "push-train", "--json")
    assert result.exit_code == 0, result.output
    reds = json.loads(result.stdout)["reds"]
    assert [(r["sha"], r["episode_at"], r["acked_at"], r["minutes"], r["acked_by"]) for r in reds] == [
        (SHA1, "2026-11-19T10:00:00Z", "2026-11-19T10:07:00Z", 7.0, "i4"),
        (SHA2, "2026-11-19T12:00:00Z", "2026-11-19T12:41:00Z", 41.0, "i5"),
        (SHA3, "2026-11-19T14:00:00Z", None, None, None)]
    assert [r["handoffs"] for r in reds] == [0, 1, 0]
    assert set(tuples.reads) == {"board/ci/nexus-develop--owner"}, "the topics of the group's serves, on their --owner subspace"


def test_ack_times_names_the_owner_by_alias_and_falls_back_to_the_id() -> None:
    """RDR-252, Sam 2026-11-19: engine 0.1.198 stamps owner_alias beside owner; a row with none (an older engine, or an
    identity with no live alias) is shown by its id, and the JSON carries null for the alias."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, owner="i4", owner_alias="halcyon", next="i5", next_alias="tamsin", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4"),
        _row("owner", "2026-11-19T12:00:05Z", SHA2, owner="i6", next=None, reason="earliest_suspect", episode_at="2026-11-19T12:00:00Z", refresh=False),
        _row("ack", "2026-11-19T12:09:00Z", SHA2, who="i6"),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    reds = json.loads(_run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train", "--json").stdout)["reds"]
    assert [(r["owner"], r["owner_alias"], r["acked_by"], r["acked_by_alias"]) for r in reds] == [
        ("i4", "halcyon", "i4", "halcyon"), ("i6", None, "i6", None)]
    text = _run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train").output
    assert "acked by halcyon at" in text and "acked by i6 at" in text and "acked by i4" not in text


def _with_lookups(env: GroupEnv, alias: Any = None, session: Any = None) -> GroupEnv:
    return dataclasses.replace(env, alias_lookup=alias, session_lookup=session)


def _ack_times(env: GroupEnv) -> list[dict[str, Any]]:
    result = _run(env, "ack-times", "push-train", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["reds"]


_RED = dict(owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False)
SESS_OWNER, SESS_OTHER = "3d4bd7aa-1691-4bc1-b59b-0af9dccfd153", "4e5c8bb0-2792-4cd2-8c6c-1b0aeddfe264"


def _times(rows: list[Any], alias: Any = None, session: Any = None, **kw: Any) -> list[dict[str, Any]]:
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    return _ack_times(_with_lookups(_env(wire, tuples=_Tuples(rows), **kw), alias, session))


class _Lookup:
    """A name or session lookup that records what it was asked and answers from a table (or raises what the table holds)."""

    def __init__(self, table: dict[str, Any]) -> None:
        self.table, self.asked = table, []

    def __call__(self, key: str) -> dict[str, Any]:
        self.asked.append(key)
        answer = self.table[key]
        if isinstance(answer, Exception):
            raise answer
        return {"identity": answer}


def _api_error(status: int) -> Exception:
    from nexus.identity.bind_client import IdentityApiError  # noqa: PLC0415

    return IdentityApiError(status, "x", "no")


def test_the_engines_rule_an_ack_counts_when_its_sender_session_maps_to_the_owner_whatever_from_says() -> None:
    """nexus-igea9, tuple b18af4e1 (2026-11-20): from=marginalia, sender_session the owner's. The engine counts the
    stamp through the session index and never reads `from`, so ack-times does too."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="marginalia", sender_session=SESS_OWNER),
    ]
    sessions = _Lookup({SESS_OWNER: "i4"})
    [red] = _times(rows, session=sessions)
    assert (red["acked_at"], red["minutes"], red["acked_by"]) == ("2026-11-19T10:07:00Z", 7.0, "i4")
    assert sessions.asked == [SESS_OWNER]


def test_a_from_naming_the_owner_does_not_count_when_the_sender_session_is_another_identitys() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session=SESS_OTHER),
    ]
    [red] = _times(rows, session=_Lookup({SESS_OTHER: "i5"}))
    assert red["acked_at"] is None, "the stamp says i5, and the engine would count nobody"


def test_an_ack_whose_session_the_index_does_not_know_counts_for_nobody_as_in_the_engine() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session=SESS_OTHER),
    ]
    [red] = _times(rows, session=_Lookup({SESS_OTHER: _api_error(404)}))
    assert red["acked_at"] is None


def test_a_transient_lookup_error_is_noted_on_stderr_and_never_cached_as_nobody() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:05:00Z", SHA1, who="marginalia", sender_session=SESS_OWNER),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="marginalia", sender_session=SESS_OWNER),
    ]
    answers: list[Any] = [_api_error(503), "i4"]

    def flaky(sid: str) -> dict[str, Any]:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"identity": answer}

    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    env = _with_lookups(_env(wire, tuples=_Tuples(rows)), session=flaky)
    result = _run(env, "ack-times", "push-train", "--json")
    [red] = json.loads(result.stdout)["reds"]
    assert red["acked_at"] == "2026-11-19T10:07:00Z", "the second ack asked again: the failure was not remembered as nobody"
    assert "1 acks could not be resolved; counts may be low" in result.stderr, result.stderr


def test_a_clean_run_prints_no_resolution_note() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4"),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train", "--json")
    assert "could not be resolved" not in result.stderr


def test_a_session_is_asked_once_however_many_acks_it_posted_and_the_cheap_checks_come_first() -> None:
    """Acks outside the red's window, for a sha that is no tip, or before any owner row ask nobody; a session that
    posted three acks is asked once. Falsifiable: drop the cache, or look up before the checks, and `asked` grows."""
    rows = [
        _row("ack", "2026-11-19T09:00:00Z", SHA1, who="x", sender_session="5f6d9cc1-38a3-4de3-9d7d-2c1bfee0f375"),
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:01:00Z", SHA3, who="x", sender_session="6a7e0dd2-49b4-4ef4-8e8e-3d2c0ff1a486"),
        _row("ack", "2026-11-19T10:03:00Z", SHA1, who="x", sender_session=SESS_OTHER),
        _row("ack", "2026-11-19T10:04:00Z", SHA1, who="x", sender_session=SESS_OTHER),
        _row("ack", "2026-11-19T10:05:00Z", SHA1, who="x", sender_session=SESS_OTHER),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="x", sender_session=SESS_OWNER),
    ]
    sessions = _Lookup({SESS_OTHER: "i5", SESS_OWNER: "i4"})
    [red] = _times(rows, session=sessions)
    assert red["acked_at"] == "2026-11-19T10:07:00Z"
    assert sessions.asked == [SESS_OTHER, SESS_OWNER], "the early, no-tip and no-owner acks asked nobody; i5's session once"


def test_an_ack_with_no_sender_session_falls_back_to_the_stamped_pair_of_its_from() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED, owner_alias="marginalia", next_alias="tamsin"),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="marginalia"),
    ]
    [red] = _times(rows)
    assert (red["acked_at"], red["acked_by"]) == ("2026-11-19T10:07:00Z", "i4")


def test_an_ack_with_no_sender_session_and_no_stamped_pair_resolves_through_the_identity_service_once_per_name() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:03:00Z", SHA1, who="tamsin"),
        _row("ack", "2026-11-19T10:04:00Z", SHA1, who="tamsin"),
        _row("ack", "2026-11-19T10:05:00Z", SHA1, who="i5"),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="marginalia"),
    ]
    names = _Lookup({"tamsin": "i5", "marginalia": "i4"})
    [red] = _times(rows, alias=names)
    assert red["acked_at"] == "2026-11-19T10:07:00Z" and red["acked_by"] == "i4"
    assert names.asked == ["tamsin", "marginalia"], "tamsin once; the id i5 is never looked up"


@pytest.mark.parametrize("who", ["3d4bd7aa-1691-4bc1-b59b-0af9dccfd153", "a" + "0" * 16, "../i4", "x/y", "a b", ""])
def test_a_from_that_is_no_name_is_never_put_in_a_url(who: str) -> None:
    """A session id, an agent id or an unsafe string is skipped before any lookup (mailbox_send's own guard)."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who=who),
    ]
    names = _Lookup({})
    [red] = _times(rows, alias=names)
    assert red["acked_at"] is None and names.asked == []


@pytest.mark.parametrize("how", ["lookup-says-another-identity", "stamped-pair-says-another-identity", "unknown-name", "lookup-fails"])
def test_a_name_ack_does_not_count_unless_it_resolves_to_the_owner(how: str) -> None:
    stamped = {"next_alias": "tamsin"} if how == "stamped-pair-says-another-identity" else {}
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED, **stamped),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="tamsin"),
    ]
    # In the stamped case the lookup WOULD name the owner: only the stamped pair (tamsin is next up, i5) keeps it from counting.
    answer = {"lookup-says-another-identity": "i5", "stamped-pair-says-another-identity": "i4",
              "unknown-name": _api_error(404), "lookup-fails": RuntimeError("no identity routes")}[how]
    names = _Lookup({"tamsin": answer})
    [red] = _times(rows, alias=names)
    assert red["acked_at"] is None and red["minutes"] is None
    if how == "stamped-pair-says-another-identity":
        assert names.asked == [], "the red's own stamped pair decided it, before any lookup"


def test_a_name_with_no_lookup_at_all_is_left_uncounted_rather_than_guessed() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="marginalia"),
    ]
    [red] = _times(rows)
    assert red["acked_at"] is None


def test_ack_times_text_names_an_unacknowledged_red_and_a_group_serving_nothing_is_an_error() -> None:
    rows = [_row("owner", "2026-11-19T14:00:05Z", SHA3, owner="i4", next=None, reason="no_record", episode_at="2026-11-19T14:00:00Z", refresh=False)]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train")
    assert result.exit_code == 0 and SHA3[:12] in result.output and "no ack" in result.output
    empty = Wire().on("GET", "/v1/group/idle", 200, {**RECORD, "name": "idle", "serves": []})
    assert _run(_env(empty, tuples=_Tuples([])), "ack-times", "idle").exit_code != 0


def test_ack_times_with_no_reds_says_so_and_skips_a_refresh_row_as_a_new_red() -> None:
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    nothing = _run(_env(wire, tuples=_Tuples([])), "ack-times", "push-train")
    assert nothing.exit_code == 0 and "no reds" in nothing.output
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False),
        _row("owner", "2026-11-19T10:05:05Z", SHA2, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=True),
        _row("ack", "2026-11-19T10:09:00Z", SHA2, who="i4"),
    ]
    reds = json.loads(_run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train", "--json").stdout)["reds"]
    assert [(r["sha"], r["minutes"]) for r in reds] == [(SHA1, 9.0)], "a refresh row keeps its episode, and an ack of any red tip of it counts"


def test_a_hand_off_and_an_escalation_continue_the_open_red_whatever_episode_at_they_carry() -> None:
    """One red, three owner rows: the engine stamps each hand-on with a new episode_at. Minutes run from the FIRST row's."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False, handoffs=0),
        _row("owner", "2026-11-19T10:05:05Z", SHA2, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=True, handoffs=0),
        _row("owner", "2026-11-19T10:25:05Z", SHA2, owner="i5", next=None, reason="handoff", episode_at="2026-11-19T10:25:00Z", refresh=False, handoffs=1),
        _row("owner", "2026-11-19T10:30:05Z", SHA2, owner="i5", next=None, reason="handoff", episode_at="2026-11-19T10:25:00Z", refresh=True, handoffs=1),
        _row("owner", "2026-11-19T10:50:05Z", SHA2, owner=None, next=None, reason="escalated", episode_at="2026-11-19T10:50:00Z", refresh=False, handoffs=2),
        _row("ack", "2026-11-19T10:55:00Z", SHA2, who="i5"),     # i5 is no longer the owner: does not count
        _row("clear", "2026-11-19T11:10:00Z", SHA2),
        _row("owner", "2026-11-19T13:00:05Z", SHA3, owner="i4", next="i5", reason="no_record", episode_at="2026-11-19T13:00:00Z", refresh=False, handoffs=0),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    reds = json.loads(_run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train", "--json").stdout)["reds"]
    assert [(r["sha"], r["episode_at"], r["handoffs"], r["acked_at"], r["cleared_at"]) for r in reds] == [
        (SHA1, "2026-11-19T10:00:00Z", 2, None, "2026-11-19T11:10:00Z"),
        (SHA3, "2026-11-19T13:00:00Z", 0, None, None)], "one red with both hand-ons counted, then the next red"


def test_ack_times_pages_until_an_empty_page_when_the_engine_caps_a_read_below_the_page_size() -> None:
    rows = [_row("owner", f"2026-11-19T{10 + i:02d}:00:05Z", f"{i}" * 40, owner="i4", next=None, reason="no_record",
                 episode_at=f"2026-11-19T{10 + i:02d}:00:00Z", refresh=False) for i in range(5)]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    reds = json.loads(_run(_env(wire, tuples=_Tuples(rows, cap=2)), "ack-times", "push-train", "--json").stdout)["reds"]
    assert len(reds) == 5, "a short page is not the last page"


def test_ack_times_says_so_when_it_stops_at_the_row_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.commands import group_cmd  # noqa: PLC0415

    monkeypatch.setattr(group_cmd, "_PAGE", 2)
    monkeypatch.setattr(group_cmd, "_MAX_ROWS", 4)
    rows = [_row("owner", f"2026-11-19T{10 + i:02d}:00:05Z", f"{i}" * 40, owner="i4", next=None, reason="no_record",
                 episode_at=f"2026-11-19T{10 + i:02d}:00:00Z", refresh=False) for i in range(6)]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train", "--json")
    assert result.exit_code == 0 and len(json.loads(result.stdout)["reds"]) == 4, "stdout stays parseable"
    assert "stopped at 4 rows" in result.stderr and "nexus-develop--owner" in result.stderr
    text = _run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train")
    assert "stopped at 4 rows" in text.stderr
    full = _run(_env(wire, tuples=_Tuples(rows[:3])), "ack-times", "push-train")
    assert "stopped at" not in full.stderr, "no notice when the read reached the end"


def test_importing_the_command_module_does_not_import_the_identity_resolver() -> None:
    """The refusal types are imported when a verb runs (nexus-igea9.7 round 1: the comment said deferred, the import ran at
    decoration time, which is module import)."""
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    code = "import sys, nexus.commands.group_cmd; sys.exit(1 if 'nexus.identity.resolver' in sys.modules else 0)"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr or "nexus.identity.resolver was imported by the module"


def test_a_refusal_the_resolver_names_is_still_one_line_and_exit_one() -> None:
    from nexus.identity.resolver import IdentityConflictError  # noqa: PLC0415

    class Env:
        client = None
        tuples = None

        @staticmethod
        def caller() -> str:
            raise IdentityConflictError([])

    result = CliRunner().invoke(group_group, ["leave", "push-train"], obj={"group_env": Env()})
    assert result.exit_code == 1 and "different identities" in result.output


def test_the_command_is_registered_and_every_verb_has_help() -> None:
    assert "group" in main.commands
    assert sorted(group_group.commands) == sorted(["form", "set", "disband", "override", "join", "leave", "list", "ack-times"])
    for name, command in group_group.commands.items():
        assert command.help, f"nx group {name} has no help text"
    assert isinstance(group_group, click.Group)


# -- a credential that cannot be had (RDR-252 Step 4 round 1, B2) -------------------------------------------------------


def _dead_credential() -> str:
    from nexus.db.data_token import DataTokenMintError  # noqa: PLC0415

    raise DataTokenMintError("mint refused: the credential was revoked", status=401)


def _dead_client(wire: Wire) -> GroupsClient:
    return GroupsClient("http://engine", _dead_credential, http=httpx.Client(transport=httpx.MockTransport(wire)),
                        identity_headers=lambda: dict(TOKEN), retry_headers=lambda sent, path: None)


@pytest.mark.parametrize("call", [
    lambda c: c.read("push-train"),
    lambda c: c.record("push-train"),
    lambda c: c.roster("push-train"),
    lambda c: c.form("push-train", "i3"),
    lambda c: c.memberships("i3"),
    lambda c: c.resolve("tamsin"),
    lambda c: c.identity("i3"),
], ids=["read", "record", "roster", "form", "memberships", "resolve", "identity"])
def test_a_credential_that_cannot_be_minted_is_the_engine_being_unavailable(call: Callable[[GroupsClient], Any]) -> None:
    """The engine could not be asked: ``GroupsUnavailableError``, whichever call it was, and nothing is sent."""
    wire = Wire()
    with pytest.raises(groups.GroupsUnavailableError, match="credential"):
        call(_dead_client(wire))
    assert wire.requests == []


@pytest.mark.parametrize("argv", [
    ("list", "push-train"), ("form", "push-train", "--serves", "nexus-develop"), ("join", "push-train"), ("leave", "push-train"),
    ("disband", "push-train"), ("set", "push-train", "--ack-deadline", "30"),
    ("override", "push-train", "--disband"),
], ids=lambda a: a[0])
def test_a_verb_with_a_dead_credential_prints_one_line_and_exits_one(argv: tuple[str, ...]) -> None:
    wire = Wire()
    res = _run(GroupEnv(client=_dead_client(wire), caller=lambda: "i3", tuples=lambda: None), *argv)
    assert res.exit_code == 1, res.output
    assert res.exception is None or isinstance(res.exception, SystemExit), repr(res.exception)
    assert "Traceback" not in res.output
    assert len(res.output.strip().splitlines()) == 1 and "the engine is not answering" in res.output, res.output
