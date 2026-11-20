# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx group ack-times`` against canned tuple rows and canned identity-service answers (RDR-252, nexus-igea9).

Split from tests/test_group_cmd.py (file size cap): the ack-times area lives here, the other verbs there. An ack counts
as the engine counts it, by its sender-session stamp mapped through the session index; ``from`` is never read. The
engine-backed row is tests/identity/test_group_journeys.py.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest

from nexus.commands.group_cmd import GroupEnv
from tests.test_group_cmd import RECORD, Wire, _env, _run

# ── ack-times ───────────────────────────────────────────────────────────────

SHA1, SHA2, SHA3 = "a" * 40, "b" * 40, "c" * 40
#: A session the identity index maps to each identity: an ack counts only through its sender-session stamp (the engine's rule).
SESSIONS = {"i4": "3d4bd7aa-1691-4bc1-b59b-0af9dccfd153", "i5": "4e5c8bb0-2792-4cd2-8c6c-1b0aeddfe264",
            "i6": "5f6d9cc1-38a3-4de3-9d7d-2c1bfee0f375"}


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


def _ack(at: str, sha: str, who: str) -> Any:
    """An ack by identity *who*, stamped with its session as the engine stamps a client's write; `from` is its label."""
    return _row("ack", at, sha, who=who, sender_session=SESSIONS[who])


def _index() -> Any:
    """The identity service's session route, answering for the sessions of :data:`SESSIONS`."""
    return _Lookup({sid: who for who, sid in SESSIONS.items()})


def _stamped(env: GroupEnv) -> GroupEnv:
    return _with_lookups(env, session=_index())


def test_ack_times_reports_per_red_the_sha_episode_ack_time_and_minutes() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False),
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
        _row("clear", "2026-11-19T10:30:00Z", SHA1),
        # a second red: handed off, and the first owner's late ack does not count
        _row("owner", "2026-11-19T12:00:05Z", SHA2, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T12:00:00Z", refresh=False),
        # the engine's hand-off row carries a NEW episode_at (OwnerDecision.handOn passes in.now()), not the red's
        _row("owner", "2026-11-19T12:20:05Z", SHA2, owner="i5", next=None, reason="handoff", episode_at="2026-11-19T12:20:00Z", refresh=False, handoffs=1),
        _ack("2026-11-19T12:25:00Z", SHA2, "i4"),
        _ack("2026-11-19T12:41:00Z", SHA2, "i5"),
        # a third red nobody acknowledged yet
        _row("owner", "2026-11-19T14:00:05Z", SHA3, owner="i4", next="i5", reason="no_record", episode_at="2026-11-19T14:00:00Z", refresh=False),
    ]
    tuples = _Tuples(rows)
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_stamped(_env(wire, tuples=tuples)), "ack-times", "push-train", "--json")
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
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
        _row("owner", "2026-11-19T12:00:05Z", SHA2, owner="i6", next=None, reason="earliest_suspect", episode_at="2026-11-19T12:00:00Z", refresh=False),
        _ack("2026-11-19T12:09:00Z", SHA2, "i6"),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    reds = json.loads(_run(_stamped(_env(wire, tuples=_Tuples(rows))), "ack-times", "push-train", "--json").stdout)["reds"]
    assert [(r["owner"], r["owner_alias"], r["acked_by"], r["acked_by_alias"]) for r in reds] == [
        ("i4", "halcyon", "i4", "halcyon"), ("i6", None, "i6", None)]
    text = _run(_stamped(_env(wire, tuples=_Tuples(rows))), "ack-times", "push-train").output
    assert "acked by halcyon at" in text and "acked by i6 at" in text and "acked by i4" not in text


def _with_lookups(env: GroupEnv, session: Any = None, identity: Any = None) -> GroupEnv:
    return dataclasses.replace(env, session_lookup=session, identity_lookup=identity)


def _ack_times(env: GroupEnv) -> list[dict[str, Any]]:
    result = _run(env, "ack-times", "push-train", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["reds"]


_RED = dict(owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False)
SESS_OWNER, SESS_OTHER = SESSIONS["i4"], SESSIONS["i5"]
SESS_UNKNOWN = "6a7e0dd2-49b4-4ef4-8e8e-3d2c0ff1a486"      # a minted session no identity holds


def _times(rows: list[Any], session: Any = None, identity: Any = None, **kw: Any) -> list[dict[str, Any]]:
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    return _ack_times(_with_lookups(_env(wire, tuples=_Tuples(rows), **kw), session, identity))


class _Lookup:
    """A name or session lookup that records what it was asked and answers from a table (or raises what the table holds)."""

    def __init__(self, table: dict[str, Any]) -> None:
        self.table, self.asked = table, []

    def __call__(self, key: str) -> dict[str, Any]:
        self.asked.append(key)
        answer = self.table[key]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, dict) else {"identity": answer}


def _api_error(status: int, code: str = "x") -> Exception:
    from nexus.identity.bind_client import IdentityApiError  # noqa: PLC0415

    return IdentityApiError(status, code, "no")


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
    [red] = _times(rows, session=_Lookup({SESS_OTHER: _api_error(404, "unknown_session")}))
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
    assert "1 ack could not be resolved; counts may be low" in result.stderr, result.stderr


def test_a_clean_run_prints_no_resolution_note() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_stamped(_env(wire, tuples=_Tuples(rows))), "ack-times", "push-train", "--json")
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


@pytest.mark.parametrize("who", ["i4", "marginalia"])
def test_an_ack_with_no_sender_stamp_counts_for_nobody_whatever_its_from_says(who: str) -> None:
    """nexus-igea9 (the engine's ack half): the engine counts an ack only when its session stamp maps to the
    owner. A null stamp (a tenant credential with no session) counts for nobody, even with `from` naming the owner by id
    or by its stamped alias (the two `from` values the old fallback counted); nothing is asked, nothing is noted as
    unresolved, and the ack is counted as ignored (the fallback's revert fails this test)."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED, owner_alias="marginalia", next_alias="tamsin"),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who=who),
    ]
    sessions = _Lookup({})
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_with_lookups(_env(wire, tuples=_Tuples(rows)), session=sessions), "ack-times", "push-train", "--json")
    [red] = json.loads(result.stdout)["reds"]
    assert red["acked_at"] is None and red["acked_by"] is None and red["minutes"] is None
    assert sessions.asked == [] and "could not be resolved" not in result.stderr, result.stderr
    assert red["ignored_acks"] == 1


def test_an_ack_whose_stamp_maps_to_no_identity_counts_for_nobody_silently() -> None:
    """A minted session no identity holds: 404 unknown_session is the service's own answer, so nobody, with no note."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session=SESS_OTHER),
    ]
    sessions = _Lookup({SESS_OTHER: _api_error(404, "unknown_session")})
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_with_lookups(_env(wire, tuples=_Tuples(rows)), session=sessions), "ack-times", "push-train", "--json")
    [red] = json.loads(result.stdout)["reds"]
    assert red["acked_at"] is None and "could not be resolved" not in result.stderr, result.stderr
    assert red["ignored_acks"] == 1, "an unmapped stamp is an ignored ack, not an unresolved one"


def test_a_stamp_that_maps_to_the_owner_counts_and_an_earlier_null_stamped_ack_does_not() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:05:00Z", SHA1, who="i4"),
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
    ]
    [red] = _times(rows, session=_index())
    assert (red["acked_at"], red["acked_by"], red["minutes"]) == ("2026-11-19T10:07:00Z", "i4", 7.0)


def test_a_session_answer_for_a_superseded_generation_still_counts_for_the_owner_as_in_the_engine() -> None:
    """Identities.identityOf maps a stamp through the session index with no generation check; the route reports
    `current_generation` but the identity is the answer."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session=SESS_OWNER),
    ]
    answer = {"identity": "i4", "generation": 1, "current_generation": 2}
    [red] = _times(rows, session=_Lookup({SESS_OWNER: answer}))
    assert (red["acked_at"], red["acked_by"], red["minutes"], red["ignored_acks"]) == ("2026-11-19T10:07:00Z", "i4", 7.0, 0)


def test_ignored_acks_are_counted_per_red_and_noted_on_stderr_and_never_count_in_minutes() -> None:
    """RDR-252 critique S2: an ack on a red tip, inside the red, with a null or unmapped stamp is the one signal that tells
    "the owner was too slow" from "the owner acked and the engine could not see it". Counted per red (`ignored_acks`),
    noted once on stderr, and never counted in minutes. An ack that fails a cheap check (not a tip, before any owner row),
    one that maps to another identity, and one whose lookup failed are not ignored acks."""
    rows = [
        _ack("2026-11-19T09:00:00Z", SHA1, "i4"),                                        # before the red: no owner row yet
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED, suspects=[SHA2]),
        _row("ack", "2026-11-19T10:02:00Z", SHA1, who="i4"),                             # null stamp, a tip: ignored
        _row("ack", "2026-11-19T10:03:00Z", SHA1, who="i4", sender_session=SESS_UNKNOWN),   # maps to nobody: ignored
        _row("ack", "2026-11-19T10:03:30Z", SHA2, who="i4"),                             # a suspect, no tip: not examined
        _ack("2026-11-19T10:04:00Z", SHA1, "i5"),                                        # another identity's: not ignored
        _row("ack", "2026-11-19T10:04:30Z", SHA1, who="i4", sender_session="not-a-uuid"),   # unresolved, not ignored
        _ack("2026-11-19T10:08:00Z", SHA1, "i4"),                                        # counts
        _row("clear", "2026-11-19T10:30:00Z", SHA1),
        _row("owner", "2026-11-19T12:00:05Z", SHA3, owner="i4", next=None, reason="no_record", episode_at="2026-11-19T12:00:00Z", refresh=False),
        _row("ack", "2026-11-19T12:05:00Z", SHA3, who="i4"),                             # an unacknowledged red with one ignored ack
    ]
    index = _index()

    def lookup(sid: str) -> Any:
        if sid == SESS_UNKNOWN:
            raise _api_error(404, "unknown_session")
        return index(sid)

    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_with_lookups(_env(wire, tuples=_Tuples(rows)), session=lookup), "ack-times", "push-train", "--json")
    reds = json.loads(result.stdout)["reds"]
    assert [(r["sha"], r["minutes"], r["ignored_acks"]) for r in reds] == [(SHA1, 8.0, 2), (SHA3, None, 1)], reds
    assert "3 acks on red tips carried no session stamp the engine maps; they did not count" in result.stderr, result.stderr
    assert "1 ack could not be resolved" in result.stderr, result.stderr


def test_a_run_with_no_ignored_acks_reports_zero_and_prints_no_note() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_stamped(_env(wire, tuples=_Tuples(rows))), "ack-times", "push-train", "--json")
    assert [r["ignored_acks"] for r in json.loads(result.stdout)["reds"]] == [0]
    assert "carried no session stamp" not in result.stderr


def test_a_stamped_ack_with_no_session_route_to_ask_is_noted_as_unresolved_not_counted() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_env(wire, tuples=_Tuples(rows)), "ack-times", "push-train", "--json")
    [red] = json.loads(result.stdout)["reds"]
    assert red["acked_at"] is None
    assert "1 ack could not be resolved; counts may be low" in result.stderr, result.stderr


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
        _ack("2026-11-19T10:09:00Z", SHA2, "i4"),
    ]
    reds = json.loads(_run(_stamped(_env(wire, tuples=_Tuples(rows))), "ack-times", "push-train", "--json").stdout)["reds"]
    assert [(r["sha"], r["minutes"]) for r in reds] == [(SHA1, 9.0)], "a refresh row keeps its episode, and an ack of any red tip of it counts"


def test_a_hand_off_and_an_escalation_continue_the_open_red_whatever_episode_at_they_carry() -> None:
    """One red, three owner rows: the engine stamps each hand-on with a new episode_at. Minutes run from the FIRST row's."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=False, handoffs=0),
        _row("owner", "2026-11-19T10:05:05Z", SHA2, owner="i4", next="i5", reason="earliest_suspect", episode_at="2026-11-19T10:00:00Z", refresh=True, handoffs=0),
        _row("owner", "2026-11-19T10:25:05Z", SHA2, owner="i5", next=None, reason="handoff", episode_at="2026-11-19T10:25:00Z", refresh=False, handoffs=1),
        _row("owner", "2026-11-19T10:30:05Z", SHA2, owner="i5", next=None, reason="handoff", episode_at="2026-11-19T10:25:00Z", refresh=True, handoffs=1),
        _row("owner", "2026-11-19T10:50:05Z", SHA2, owner=None, next=None, reason="escalated", episode_at="2026-11-19T10:50:00Z", refresh=False, handoffs=2),
        _ack("2026-11-19T10:55:00Z", SHA2, "i5"),     # i5 is no longer the owner: does not count
        _row("clear", "2026-11-19T11:10:00Z", SHA2),
        _row("owner", "2026-11-19T13:00:05Z", SHA3, owner="i4", next="i5", reason="no_record", episode_at="2026-11-19T13:00:00Z", refresh=False, handoffs=0),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    reds = json.loads(_run(_stamped(_env(wire, tuples=_Tuples(rows))), "ack-times", "push-train", "--json").stdout)["reds"]
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


# ── round 2: delegated identities, red tips, the engine's state rows, the 404 codes ──────────────────────


def test_a_delegated_childs_ack_counts_for_its_parent_as_in_the_engine() -> None:
    """BoardReads.identities maps a delegated identity to its parent. The session route answers the child's own id with
    kind=delegated and no parent, so the parent is read from the child's identity record."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i8", sender_session=SESS_OTHER),
    ]
    sessions = _Lookup({SESS_OTHER: {"identity": "i8", "kind": "delegated"}})
    records = _Lookup({"i8": {"kind": "delegated", "parent": "i4"}})
    [red] = _times(rows, session=sessions, identity=records)
    assert (red["acked_at"], red["acked_by"]) == ("2026-11-19T10:07:00Z", "i4")
    assert records.asked == ["i8"]


def test_a_delegated_session_answer_that_names_its_parent_needs_no_second_lookup() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i8", sender_session=SESS_OTHER),
    ]
    records = _Lookup({})
    [red] = _times(rows, session=_Lookup({SESS_OTHER: {"identity": "i8", "kind": "delegated", "parent": "i4"}}), identity=records)
    assert red["acked_by"] == "i4" and records.asked == []


def test_a_delegated_child_of_another_identity_does_not_count_for_the_owner() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session=SESS_OTHER),
    ]
    records = _Lookup({"i8": {"kind": "delegated", "parent": "i5"}})
    [red] = _times(rows, session=_Lookup({SESS_OTHER: {"identity": "i8", "kind": "delegated"}}), identity=records)
    assert red["acked_at"] is None


@pytest.mark.parametrize("how", ["record-unreadable", "no-record-lookup", "record-has-no-parent"])
def test_a_delegated_childs_parent_that_cannot_be_read_is_unresolved_and_noted_never_nobody(how: str) -> None:
    """Counted as unresolved and said on stderr, and not remembered: the engine-side fix is `parent` in lookupSession."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:06:00Z", SHA1, who="i8", sender_session=SESS_OTHER),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i8", sender_session=SESS_OTHER),
    ]
    table = {"record-unreadable": _api_error(403, "forbidden"), "no-record-lookup": None,
             "record-has-no-parent": {"kind": "delegated"}}
    records = None if how == "no-record-lookup" else _Lookup({"i8": table[how]})
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    env = _with_lookups(_env(wire, tuples=_Tuples(rows)), _Lookup({SESS_OTHER: {"identity": "i8", "kind": "delegated"}}), records)
    result = _run(env, "ack-times", "push-train", "--json")
    assert json.loads(result.stdout)["reds"][0]["acked_at"] is None
    assert "2 acks could not be resolved; counts may be low" in result.stderr, result.stderr
    if records is not None:
        assert records.asked == ["i8", "i8"], "a failure is not cached as nobody"


def test_an_ack_of_a_suspect_that_is_no_red_tip_does_not_count() -> None:
    """The engine counts a red tip only; a suspect an owner row names may be untested or green."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED, suspects=[SHA2]),
        _ack("2026-11-19T10:07:00Z", SHA2, "i4"),
    ]
    [red] = _times(rows, session=_index())
    assert red["acked_at"] is None
    rows[1] = _ack("2026-11-19T10:07:00Z", SHA1, "i4")
    assert _times(rows, session=_index())[0]["acked_at"] == "2026-11-19T10:07:00Z"


def test_an_owner_or_clear_row_a_session_wrote_is_not_state_as_in_the_engine() -> None:
    """The engine's isState: from=engine, a null sender stamp, a parseable body. A session can post kind=owner or
    kind=clear (BoardCiGuard refuses only from=engine), and the engine ignores both."""
    forged = {**_RED, "owner": "i9", "episode_at": "2026-11-19T10:03:00Z"}
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("clear", "2026-11-19T10:02:00Z", SHA1, who="i9", sender_session=SESS_OTHER),
        _row("owner", "2026-11-19T10:03:00Z", SHA3, who="i9", sender_session=SESS_OTHER, **forged),
        _ack("2026-11-19T10:07:00Z", SHA1, "i4"),
    ]
    reds = _times(rows, session=_index())
    assert [(r["sha"], r["owner"], r["cleared_at"], r["acked_at"]) for r in reds] == [(SHA1, "i4", None, "2026-11-19T10:07:00Z")]


def test_an_owner_row_with_no_readable_body_is_not_state() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        SimpleNamespace(id="bad", created_at="2026-11-19T10:01:00Z", dims={"from": "engine", "kind": "owner", "sha": SHA3},
                        body="{not json", sender_session=None),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4"),
    ]
    assert [r["sha"] for r in _times(rows)] == [SHA1]


def test_a_404_that_is_not_unknown_session_is_unresolved_and_noted_not_nobody() -> None:
    """A route-less engine answers 404 for any session id: that says nothing about the session."""
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session=SESS_OWNER),
    ]
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    env = _with_lookups(_env(wire, tuples=_Tuples(rows)), _Lookup({SESS_OWNER: _api_error(404, None)}))
    result = _run(env, "ack-times", "push-train", "--json")
    assert json.loads(result.stdout)["reds"][0]["acked_at"] is None
    assert "1 ack could not be resolved; counts may be low" in result.stderr


def test_a_sender_stamp_that_is_no_uuid_is_unresolved_and_noted_not_silently_nobody() -> None:
    rows = [
        _row("owner", "2026-11-19T10:00:05Z", SHA1, **_RED),
        _row("ack", "2026-11-19T10:07:00Z", SHA1, who="i4", sender_session="not-a-uuid"),
    ]
    sessions = _Lookup({})
    wire = Wire().on("GET", "/v1/group/push-train", 200, RECORD)
    result = _run(_with_lookups(_env(wire, tuples=_Tuples(rows)), sessions), "ack-times", "push-train", "--json")
    assert json.loads(result.stdout)["reds"][0]["acked_at"] is None and sessions.asked == []
    assert "1 ack could not be resolved" in result.stderr
