# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pre-tag cloud leg (tests/e2e/cloud-leg.sh, lib/cloud_leg.py, lib/candidate_sandbox.sh, lib/cloud_leg_tap).

Offline: no cloud, no engine, no wheel. The leg's end-to-end plumbing (a real engine, a real installed wheel, both
slots local) is the documented ``cloud-leg.sh --local-vs-local`` run, which needs the gate jar and a built wheel.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests._module_seam import setattr_in
from tests._tmp_root import child_env_with_tmpdir

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"
LEG_SH = E2E / "cloud-leg.sh"
SANDBOX_SH = E2E / "lib" / "candidate_sandbox.sh"
LEG_PY = E2E / "lib" / "cloud_leg.py"
TAP_DIR = E2E / "lib" / "cloud_leg_tap"
BASH = shutil.which("bash") or "/bin/bash"
SECRET = "tok-SECRET-0123456789abcdef"

_spec = importlib.util.spec_from_file_location("cloud_leg", LEG_PY)
cl = importlib.util.module_from_spec(_spec)
sys.modules["cloud_leg"] = cl
_spec.loader.exec_module(cl)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── refusals: window ────────────────────────────────────────────────────────

def _marker(d: Path, wid: str = "w1", **over: str) -> Path:
    doc = {"window": wid, "scope": "cloud-leg", "tenant": "release-test", "opened_by": "sam",
           "announced": "in the halcyon session", "opened_at": _iso(_now() - timedelta(minutes=5)),
           "expires_at": _iso(_now() + timedelta(hours=2))}
    doc.update(over)
    p = d / f"{wid}.window"
    p.write_text("\n".join(f"{k}: {v}" for k, v in doc.items()) + "\n")
    p.chmod(0o600)
    return p


def test_window_in_effect_is_accepted(tmp_path: Path) -> None:
    _marker(tmp_path)
    assert "in effect" in cl.check_window("w1", tmp_path)


@pytest.mark.parametrize("why, wid, over, fragment", [
    ("no id", "", {}, "no announced cloud window"),
    ("path traversal id", "../x", {}, "no announced cloud window"),
    ("no marker file", "nope", {}, "does not exist"),
    ("marker names another window", "w1", {"window": "w2"}, "names window"),
    ("wrong scope", "w1", {"scope": "other"}, "scope"),
    ("wrong tenant", "w1", {"tenant": "nexus"}, "tenant"),
    ("expired", "w1", {"expires_at": _iso(_now() - timedelta(minutes=1))}, "not in effect"),
    ("not yet open", "w1", {"opened_at": _iso(_now() + timedelta(hours=1)), "expires_at": _iso(_now() + timedelta(hours=2))}, "not in effect"),
    ("unreadable instant", "w1", {"expires_at": "tomorrow"}, "readable"),
    ("longer than 4h", "w1", {"expires_at": _iso(_now() + timedelta(hours=5))}, "more than"),
])
def test_window_refusals(tmp_path: Path, why: str, wid: str, over: dict, fragment: str) -> None:
    _marker(tmp_path, "w1", **over)
    with pytest.raises(cl.Refusal, match=fragment):
        cl.check_window(wid, tmp_path)


def test_window_marker_must_be_0600_and_the_users_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = _marker(tmp_path)
    m.chmod(0o644)
    with pytest.raises(cl.Refusal, match="0600"):
        cl.check_window("w1", tmp_path)
    m.chmod(0o600)
    cl.check_window("w1", tmp_path)
    setattr_in(monkeypatch, cl, "os.getuid", lambda: os.stat(m).st_uid + 1)
    with pytest.raises(cl.Refusal, match="owned by the current user"):
        cl.check_window("w1", tmp_path)


# ── refusals: target ────────────────────────────────────────────────────────

def _target(**over):
    base = dict(service_url="https://api.conexus-nexus.com", token=SECRET, mint_tenant="release-test",
                item_title="nexus cloud test tenant (release-test)", ambient_environ={}, ambient_config=None)
    base.update(over)
    return base


def test_target_ok() -> None:
    assert cl.check_target(**_target()) == "api.conexus-nexus.com"


def test_the_returned_edge_is_the_pinned_entry_not_the_credential_text() -> None:
    edge = cl.check_target(**_target())
    assert edge in cl.ALLOWED_EDGE_HOSTS
    assert any(edge is known for known in cl.ALLOWED_EDGE_HOSTS), "the allowlist's own string, so no credential-derived text reaches the log"


def test_an_edge_host_override_is_explicit_and_exact() -> None:
    env = {cl.EDGE_OVERRIDE_ENV: "edge.staging.example"}
    assert cl.check_target(**_target(service_url="https://edge.staging.example", ambient_environ=env)) == "edge.staging.example"
    with pytest.raises(cl.Refusal, match="not the pinned edge"):
        cl.check_target(**_target(service_url="https://other.example", ambient_environ=env))


def test_the_checked_and_the_sent_secret_are_read_the_same_way(tmp_path: Path) -> None:
    cred = tmp_path / "cred"
    cred.mkdir()
    (cred / "token").write_text("  " + SECRET + "  \n")
    (cred / "service_url").write_text("https://api.conexus-nexus.com\n")
    cl.normalize_cred_dir(cred)
    assert (cred / "token").read_text() == SECRET + "\n" and oct((cred / "token").stat().st_mode & 0o777) == "0o600"
    # an ambient copy with stray whitespace still counts as the operator's own token
    with pytest.raises(cl.Refusal, match="ambient service_token"):
        cl.check_target(**_target(ambient_environ={"NX_SERVICE_TOKEN": f" {SECRET}\n"}))
    (cred / "token").write_text("two\nlines\n")
    with pytest.raises(cl.Refusal, match="more than one line"):
        cl.normalize_cred_dir(cred)


def test_item_title_comes_from_the_items_own_json() -> None:
    assert cl.item_title_from_json('{"title": "nexus cloud test tenant (release-test)", "fields": []}').endswith("(release-test)")
    for bad in ("", "{}", "not json", '{"title": ""}'):
        with pytest.raises(cl.Refusal, match="no title"):
            cl.item_title_from_json(bad)


@pytest.mark.parametrize("over, fragment", [
    ({"service_url": "http://api.conexus-nexus.com"}, "https"),
    ({"service_url": "https://127.0.0.1:8443"}, "loopback or private"),
    ({"service_url": "https://localhost"}, "loopback or private"),
    ({"service_url": "https://u:p@api.conexus-nexus.com"}, "user:password@"),
    ({"mint_tenant": "nexus"}, "not 'release-test'"),
    ({"mint_tenant": ""}, "not 'release-test'"),
    ({"mint_tenant": "release-test-2"}, "not 'release-test'"),
    ({"item_title": "nexus production tenant"}, "word 'test'"),
    ({"item_title": "contest attestation tenant"}, "word 'test'"),
    ({"service_url": "https://evil.example.com"}, "not the pinned edge"),
    ({"service_url": "https://api.conexus-nexus.com.evil.example"}, "not the pinned edge"),
    ({"token": ""}, "empty"),
])
def test_target_refusals(over: dict, fragment: str) -> None:
    with pytest.raises(cl.Refusal, match=fragment) as ei:
        cl.check_target(**_target(**over))
    assert SECRET not in str(ei.value)


def test_target_refuses_the_operators_own_token_from_config_and_env(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(f"credentials:\n  service_url: https://api.conexus-nexus.com\n  mint_token: {SECRET}\n  mint_tenant: nexus\n")
    with pytest.raises(cl.Refusal, match="operator's own ambient mint_token") as ei:
        cl.check_target(**_target(ambient_config=cfg))
    assert SECRET not in str(ei.value)
    with pytest.raises(cl.Refusal, match="ambient service_token"):
        cl.check_target(**_target(ambient_environ={"NX_SERVICE_TOKEN": SECRET}))
    cl.check_target(**_target(ambient_environ={"NX_SERVICE_TOKEN": "another-token-value"}, ambient_config=cfg.with_name("none.yml")))


# ── the wire-ledger parser ──────────────────────────────────────────────────

_FIXTURE_LEDGER = """# ledger

## Unshipped

- `aaaaaaa1111` -- bead nexus-x.1 -- engine tag `engine-service-v9.9.9` -- [additive] New route `GET /v1/catalog/docs/by-scheme?scheme=<s>&limit=`. Direction safety: OLD client + NEW engine is safe.
- `bbbbbbb2222` -- bead nexus-x.2 -- engine tag `engine-service-v9.9.9` -- [additive] New route `POST /v1/widgets/frob` and `GET /v1/catalog/docs/by-author?author=x`. OLD client + NEW engine is safe.
- `ccccccc3333` -- bead nexus-x.3 -- engine tag `engine-service-v9.9.9` -- [additive] text only: a remedy string changed. OLD client + NEW engine is safe.

## Shipped
- `ddddddd4444` -- bead nexus-x.4 -- shipped in `v1.2.3` -- engine half engine-service-v9.9.8 -- [additive] `GET /v1/catalog/sensitive/locators?limit=N` came. OLD client + NEW engine is safe.
"""


def test_ledger_parser_maps_skips_and_flags_unmapped(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    entries = cl.ledger_entries(ledger)
    assert len(entries) == 3
    steps, skipped, unmapped = cl.release_steps(entries)
    assert [s.name for s in steps] == ["route-docs-by-scheme", "route-docs-by-author"]
    assert steps[0].origin.startswith("aaaaaaa nexus-x.1 GET /v1/catalog/docs/by-scheme")
    assert [k for k, _v in unmapped] == ["bbbbbbb nexus-x.2 POST /v1/widgets/frob"]
    assert dict(skipped) == {"ccccccc nexus-x.3": "no wire route named in the entry (text-only or a flag change)"}
    # naming the route accepts it, with the reason on the report
    steps2, skipped2, unmapped2 = cl.release_steps(entries, {"POST /v1/widgets/frob": "engine-internal"})
    assert not unmapped2 and any("accepted: engine-internal" in v for _k, v in skipped2)
    assert len(steps) + len(skipped) + len(unmapped) == 4


def test_ledger_parser_reads_a_named_release(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    steps, skipped, unmapped = cl.release_steps(cl.ledger_entries(ledger, "v1.2.3"))
    assert [s.name for s in steps] == ["route-sensitive-locators"] and not skipped and not unmapped
    assert cl.ledger_entries(ledger, "v0.0.0") == []


def test_a_ledger_that_is_not_a_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(cl.Refusal, match="does not exist"):
        cl.ledger_entries(tmp_path / "nope.md")
    with pytest.raises(cl.Refusal, match="does not exist"):
        cl.build_plan("r0123456abc", tmp_path / "nope.md", None)
    with pytest.raises(cl.Refusal, match="needs the wire ledger"):
        cl.build_plan("r0123456abc", None, None)


# In the lint bucket: an additive wire entry naming a route ROUTE_MAP lacks turned develop red
# twice on 2026-11-20 because this ran only in the default suite, which the pre-push `-m lint` run does not reach.
# So the default `uv run pytest` loop no longer runs it: run it with `-m lint` (or `-m ""`). CI runs it in the lint
# leg on a code push and in the docs-readers lane on a docs-only push (select_docs_reader_tests.py picks this file).
@pytest.mark.lint
def test_every_route_in_the_live_ledger_maps_and_emptying_the_map_turns_it_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live = REPO_ROOT / "docs" / "wire-contract-pending.md"
    entries = cl.ledger_entries(live)
    source = live
    if not cl.entry_routes(entries):  # a ledger with no Unshipped routes proves nothing: use the fixture's
        source = tmp_path / "wire.md"
        source.write_text(_FIXTURE_LEDGER.replace("POST /v1/widgets/frob", "GET /v1/catalog/docs/by-meta"))
        entries = cl.ledger_entries(source)
    assert cl.entry_routes(entries), "no routes to examine: the check would be vacuous"
    steps, _skipped, unmapped = cl.release_steps(entries)
    assert not unmapped, f"map these in ROUTE_MAP (or the release passes --accept-skip): {unmapped}"
    assert steps
    monkeypatch.setattr(cl, "ROUTE_MAP", {})
    _steps, _skipped, unmapped_now = cl.release_steps(entries)
    assert unmapped_now, "emptying ROUTE_MAP must leave every route unmapped"


def test_plan_has_core_floor_and_cleanup_last() -> None:
    plan = cl.build_plan("r0123456abc", None, None, core_only=True)
    names = [s["name"] for s in plan["steps"]]
    assert plan["floor"] == 12
    assert names[-4:] == ["store-delete", "scratch-delete", "memory-delete-threads", "identity-end"]
    assert {"memory-put-threads-identity", "mailbox-send", "mailbox-claim", "scratch-put", "store-put", "store-search",
            "identity-seat-create", "identity-seat-show", "identity-bind"} <= set(names)
    by = {s["name"]: s for s in plan["steps"]}
    assert "optional_verb" not in by["identity-seat-create"] and by["identity-seat-create"]["core"]
    assert by["store-search"]["tries"] == cl.SEARCH_TRIES > 1
    assert not any(s["benign_refusal"] for s in plan["steps"] if s["core"])
    with pytest.raises(ValueError):
        cl.build_plan("BAD ID", None, None, core_only=True)
    assert "@label@" in json.dumps(plan) and SECRET not in json.dumps(plan)


def test_plan_fails_a_full_run_with_zero_ledger_entries_unless_told(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    plan = cl.build_plan("r0123456abc", ledger, "v0.0.0")
    assert plan["ledger_entries"] == 0 and any("zero entries" in f for f in plan["failures"])
    ok = cl.build_plan("r0123456abc", ledger, "v0.0.0", no_ledger_ok="nothing in this release")
    assert not ok["failures"] and ok["no_ledger_ok"]
    assert cl.build_plan("r0123456abc", ledger, "v0.0.0", core_only=True)["failures"] == []


def test_plan_fails_an_unmapped_route_and_rejects_an_accept_skip_for_nothing(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    plan = cl.build_plan("r0123456abc", ledger, None)
    assert any("unmapped ledger route" in f and "/v1/widgets/frob" in f for f in plan["failures"])
    ok = cl.build_plan("r0123456abc", ledger, None, accept_skip={"/v1/widgets/frob": "internal"})
    assert not ok["failures"] and plan["ledger_driven"] == 2
    with pytest.raises(cl.Refusal, match="no ledger entry names"):
        cl.build_plan("r0123456abc", ledger, None, accept_skip={"POST /v1/typo": "x"})
    with pytest.raises(cl.Refusal, match="non-empty reason"):
        cl.parse_pairs(["/v1/widgets/frob:"], "--accept-skip")


def test_core_only_counts_the_routes_it_did_not_drive(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    plan = cl.build_plan("r0123456abc", ledger, None, core_only=True)
    assert plan["core_only"] and plan["not_driven_routes"] == 3 and plan["routes_driven"] == 0
    assert not [s for s in plan["steps"] if s["origin"] != "core"]


# ── the comparator ──────────────────────────────────────────────────────────

def _res(name: str, slot: str, *, exit: int = 0, taps: list | None = None, ready: bool = True, error: str = "",
         expect_ok: bool = True, skipped: str | None = None, blocked: str | None = None, benign: bool = False,
         host: str = "h") -> dict:
    taps = taps if taps is not None else [[200, "GET", "/v1/x"]]
    return {"name": name, "slot": slot, "exit": exit, "taps": taps, "tap_ready": ready, "stdout": "", "error": error,
            "skipped": skipped, "blocked": blocked, "expect_ok": expect_ok, "benign": benign, "attempts": 1,
            "host": host, "hosts": [host] if taps else []}


def test_cloud_only_4xx_fails_and_names_status_and_route() -> None:
    v, why = cl.judge_step(_res("s", "local"), _res("s", "cloud", exit=1, taps=[[403, "POST", "/v1/memory/put"]]))
    assert v == "FAIL" and "cloud-only 403" in why and "/v1/memory/put" in why


def test_a_4xx_local_also_returns_passes_when_the_step_exits_zero() -> None:
    """store-put's benign 404 on the collection get: the step exits 0, the same 404 is in both slots."""
    both = [[404, "GET", "/v1/catalog/collections/get"], [200, "POST", "/v1/catalog/manifest/write_many"]]
    assert cl.judge_step(_res("s", "local", taps=both), _res("s", "cloud", taps=both))[0] == "PASS"


def test_both_slots_refusing_is_a_failure_for_a_core_step_and_a_pass_only_when_flagged_benign() -> None:
    both = [[409, "POST", "/v1/identity"]]
    v, why = cl.judge_step(_res("s", "local", exit=1, taps=both), _res("s", "cloud", exit=1, taps=both))
    assert v == "FAIL" and "failed in both slots" in why
    v, _ = cl.judge_step(_res("s", "local", exit=1, taps=both, benign=True), _res("s", "cloud", exit=1, taps=both, benign=True))
    assert v == "PASS-both-refused"


def test_a_different_status_in_cloud_still_fails() -> None:
    v, _ = cl.judge_step(_res("s", "local", exit=1, taps=[[409, "POST", "/v1/identity"]], benign=True),
                         _res("s", "cloud", exit=1, taps=[[403, "POST", "/v1/identity"]], benign=True))
    assert v == "FAIL"


def test_ids_are_normalised_so_slots_compare() -> None:
    assert cl.norm_route("/v1/identity/i12/bind") == cl.norm_route("/v1/identity/i7/bind") == "/v1/identity/{id}/bind"
    assert cl.norm_route("/v1/memory/put") == "/v1/memory/put"


def test_cloud_exit_failure_without_http_status_fails() -> None:
    v, why = cl.judge_step(_res("s", "local"), _res("s", "cloud", exit=1, error="Error: engine does not advertise the route"))
    assert v == "FAIL" and "cloud exit 1" in why and "advertise" in why


def test_local_only_failure_fails() -> None:
    v, why = cl.judge_step(_res("s", "local", exit=1, taps=[[500, "GET", "/v1/x"]]), _res("s", "cloud"))
    assert v == "FAIL" and "local-only" in why


def test_a_blind_tap_fails_rather_than_passes() -> None:
    assert cl.judge_step(_res("s", "local"), _res("s", "cloud", ready=False))[0] == "FAIL"
    assert cl.judge_step(_res("s", "local"), _res("s", "cloud", taps=[]))[0] == "FAIL"


def test_a_route_the_client_never_called_fails() -> None:
    quiet = _res("s", "cloud") | {"expect_route": "/v1/catalog/docs/by-scheme"}
    loud = _res("s", "local", taps=[[200, "GET", "/v1/catalog/docs/by-scheme"]]) | {"expect_route": "/v1/catalog/docs/by-scheme"}
    v, why = cl.judge_step(loud, quiet)
    assert v == "FAIL" and "never called /v1/catalog/docs/by-scheme" in why and why.startswith("cloud")
    assert cl.judge_step(loud, loud | {"slot": "cloud"})[0] == "PASS"


def test_blocked_and_round_trip_miss_fail() -> None:
    assert cl.judge_step(_res("s", "local"), _res("s", "cloud", blocked="needs identity", exit=-1))[0] == "FAIL"
    assert cl.judge_step(_res("s", "local"), _res("s", "cloud", expect_ok=False))[0] == "FAIL"


def _plan(n: int, floor: int, skipped: list | None = None, **extra) -> dict:
    return {"run": "r0123456", "steps": [{"name": f"s{i}", "core": True, "cleanup": False} for i in range(n)],
            "skipped": skipped or [], "floor": floor, "ledger_entries": 4, "ledger_driven": 4, "routes_driven": 4,
            "failures": [], **extra}


def _slots(n: int, **cloud_over):
    return ([_res(f"s{i}", "local") for i in range(n)],
            [_res(f"s{i}", "cloud", **(cloud_over if i == 0 else {})) for i in range(n)])


def test_compare_counts_and_passes() -> None:
    plan = _plan(3, 3, [["u 1", "accepted: internal"]])
    rep = cl.compare(plan, *_slots(3), window="w1", edge_host="h", environ={})
    assert rep.ok and rep.examined == 3 and rep.failed == 0
    text = rep.render()
    assert "examined 3, skipped 1 (u 1: accepted: internal), failed 0" in text and text.rstrip().endswith("CLOUD LEG PASSED")
    assert "ledger entries driven: 4 of 4 (routes driven: 4; ## Unshipped)" in text
    assert "run r0123456; window w1" in text and "cloud slot tapped hosts (from the request URLs): h (expected h)" in text
    assert text.splitlines()[0].split() == ["step", "local", "cloud", "verdict"]


def test_compare_names_the_release_the_ledger_was_read_for() -> None:
    assert "release v8.5.1" in cl.compare(_plan(1, 1, release="v8.5.1"), *_slots(1), environ={}).render()


def test_compare_fails_on_one_bad_step() -> None:
    cloud = [_res("s0", "cloud"), _res("s1", "cloud", exit=1, taps=[[403, "POST", "/v1/memory/put"]]), _res("s2", "cloud")]
    rep = cl.compare(_plan(3, 3), [_res(f"s{i}", "local") for i in range(3)], cloud, environ={})
    assert not rep.ok and rep.failed == 1 and "failed 1" in rep.render() and rep.render().rstrip().endswith("CLOUD LEG FAILED")


def test_the_floor_counts_passing_core_steps_not_examined_ones() -> None:
    """Three examined, one failed: the old floor (examined >= 3) passed this; the new one counts PASS only."""
    cloud = [_res("s0", "cloud"), _res("s1", "cloud", exit=1, taps=[[403, "POST", "/v1/memory/put"]]), _res("s2", "cloud")]
    rep = cl.compare(_plan(3, 3), [_res(f"s{i}", "local") for i in range(3)], cloud, environ={})
    assert any("only 2 core steps passed" in m for m in rep.nonvacuity)
    assert not cl.compare(_plan(0, 0), [], [], environ={}).ok


def test_zero_core_steps_and_plan_failures_fail() -> None:
    rep = cl.compare(_plan(3, 3, failures=["the ledger selection has zero entries"]), *_slots(3), environ={})
    assert not rep.ok and "PLAN FAILURE: the ledger selection has zero entries" in rep.render() and "failed 1" in rep.render()


def test_core_only_report_line_and_edge_override_line() -> None:
    rep = cl.compare(_plan(1, 1, core_only=True, not_driven_routes=4), *_slots(1), environ={cl.EDGE_OVERRIDE_ENV: "edge.example"})
    text = rep.render()
    assert "CORE-ONLY: 4 ledger routes not driven" in text and f"{cl.EDGE_OVERRIDE_ENV}=edge.example" in text


def test_the_cloud_slot_must_show_the_edge_host() -> None:
    loc = [_res("s0", "local")]
    rep = cl.compare(_plan(1, 1), loc, [_res("s0", "cloud", host="localhost")], edge_host="api.conexus-nexus.com", environ={})
    assert not rep.ok and any("never the edge api.conexus-nexus.com" in m for m in rep.nonvacuity)
    assert cl.compare(_plan(1, 1), loc, [_res("s0", "cloud", host="api.conexus-nexus.com")], edge_host="api.conexus-nexus.com",
                      environ={}).ok


# expected-red ---------------------------------------------------------------

def _red(*statuses: int, why: str = "cloud self-seat ships in 0.1.197") -> "cl.ExpectRed":
    return cl.ExpectRed(frozenset(statuses), why)


def test_expect_red_turns_the_pinned_cloud_only_status_into_expected_red_with_status_and_reason() -> None:
    plan = _plan(3, 3)
    local, cloud = _slots(3, exit=1, taps=[[403, "POST", "/v1/identity"]])
    rep = cl.compare(plan, local, cloud, expect_red={"s0": _red(403)}, environ={})
    assert rep.ok and rep.expected_red == 1 and rep.failed == 0
    text = rep.render()
    assert "EXPECTED-RED" in text and "cloud-only 403 on POST /v1/identity" in text and "cloud self-seat ships in 0.1.197" in text
    assert "pinned @403" in text and "expected-red 1" in text
    assert text.rstrip().endswith("CLOUD LEG PASSED WITH EXPECTED-RED 1: s0")
    assert not cl.compare(plan, local, cloud, environ={}).ok  # without the flag the same run fails
    assert cl.compare(plan, local, cloud, expect_red={"s0": _red(401, 403)}, environ={}).ok  # a comma list pins several


def test_expect_red_does_not_excuse_another_status() -> None:
    """The cut is expected to fix a 403; a 500 is a different fault and must stay red."""
    local, cloud = _slots(2, exit=1, taps=[[500, "POST", "/v1/identity"]])
    rep = cl.compare(_plan(2, 2), local, cloud, expect_red={"s0": _red(403)}, environ={})
    assert not rep.ok and rep.expected_red == 0 and "are not the pinned [403]" in rep.render()
    local, cloud = _slots(2, exit=1, taps=[[403, "POST", "/v1/identity"], [500, "GET", "/v1/y"]])
    assert not cl.compare(_plan(2, 2), local, cloud, expect_red={"s0": _red(403)}, environ={}).ok


@pytest.mark.parametrize("why, cloud_over", [
    ("a blind tap in the cloud", {"ready": False, "exit": 1}),
    ("no HTTP request seen, exit 0", {"taps": []}),
    ("the cloud exits 0 but never called the route", {"expect_route_missing": True}),
    ("a round-trip miss", {"expect_ok": False}),
    ("the cloud exit failure with no HTTP status", {"exit": 1, "taps": [[200, "GET", "/v1/x"]]}),
    ("cloud 403 but exit 0", {"taps": [[403, "POST", "/v1/identity"]]}),
])
def test_expect_red_excuses_nothing_but_the_pinned_cloud_only_status(why: str, cloud_over: dict) -> None:
    over = dict(cloud_over)
    missing = over.pop("expect_route_missing", False)
    local, cloud = _slots(2, **over)
    if missing:
        local[0] = local[0] | {"expect_route": "/v1/identity"} | {"taps": [[200, "POST", "/v1/identity"]]}
        cloud[0] = cloud[0] | {"expect_route": "/v1/identity"}
    rep = cl.compare(_plan(2, 2), local, cloud, expect_red={"s0": _red(403)}, environ={})
    assert not rep.ok and rep.expected_red == 0, (why, rep.render())
    assert "EXPECTED-RED" not in rep.render()


def test_a_pass_on_an_expect_red_step_fails_the_run() -> None:
    rep = cl.compare(_plan(2, 2), *_slots(2), expect_red={"s0": _red(403, why="demo")}, environ={})
    assert not rep.ok and rep.failed == 1 and "expected red but passed: remove --expect-red s0" in rep.render()


def test_a_local_failure_is_not_an_expected_cloud_red() -> None:
    local = [_res("s0", "local", exit=1, taps=[[500, "GET", "/v1/x"]])]
    cloud = [_res("s0", "cloud", exit=1, taps=[[500, "GET", "/v1/x"], [403, "GET", "/v1/y"]])]
    rep = cl.compare(_plan(1, 1), local, cloud, expect_red={"s0": _red(403)}, environ={})
    assert not rep.ok and "not the pinned red" in rep.render()


def test_expect_red_needs_a_known_step_a_status_pin_and_a_reason() -> None:
    with pytest.raises(cl.Refusal, match="not a step of this plan"):
        cl.compare(_plan(1, 1), *_slots(1), expect_red={"nope": _red(403)}, environ={})
    for bad in ("identity-seat-create", "identity-seat-create:why", "identity-seat-create@403", "identity-seat-create@403:  ",
                "identity-seat-create@200:why", "identity-seat-create@abc:why", "identity-seat-create@:why", "@403:why"):
        with pytest.raises(cl.Refusal, match="--expect-red needs"):
            cl.parse_expect_red([bad])
    got = cl.parse_expect_red(["identity-seat-create@403:cloud self-seat: ships in 0.1.197", "identity-seat-show@404,410:no seat"])
    assert got["identity-seat-create"] == cl.ExpectRed(frozenset({403}), "cloud self-seat: ships in 0.1.197")
    assert got["identity-seat-show"].statuses == frozenset({404, 410})


# ── slot runner ─────────────────────────────────────────────────────────────

def _fake_sandbox(tmp_path: Path, nx_body: str | None = None) -> Path:
    sb = tmp_path / "sbx"
    (sb / "secrets").mkdir(parents=True)
    (sb / "secrets" / "NX_MINT_TOKEN").write_text(SECRET + "\n")
    (sb / "home/.local/bin").mkdir(parents=True)
    (sb / "home/.local/share/uv/tools/conexus/bin").mkdir(parents=True)
    # the "runner" execs its arguments, leading NAME=value included; the fake nx leaks the secret on purpose
    run = sb / "run"
    run.write_text('#!/bin/bash\nexec /usr/bin/env "$@"\n')
    nx = sb / "home/.local/bin/nx"
    nx.write_text(nx_body or f'#!/bin/bash\necho "stdout {SECRET}"\necho "Error: refused {SECRET}" >&2\n'
                  'printf "#tap-ready\\n200\\tGET\\t/v1/x\\th\\n" >> "$NX_CLOUDLEG_TAP"\nexit 1\n')
    for p in (run, nx):
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return sb


def _step(name: str = "probe", **over) -> dict:
    base = {"name": name, "kind": "nx", "argv": ["x"], "core": True, "cleanup": False, "identity": False, "needs": [],
            "capture": {}, "stdin": None, "expect_stdout": None, "expect_route": None, "benign_refusal": False, "tries": 1,
            "origin": "core"}
    return base | over


def test_run_slot_scrubs_the_secret_from_results(tmp_path: Path) -> None:
    sb = _fake_sandbox(tmp_path)
    plan = {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step()], "floor": 1}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud")
    blob = json.dumps(res)
    assert SECRET not in blob and "***" in blob
    assert res[0]["exit"] == 1 and res[0]["tap_ready"] and res[0]["taps"] == [[200, "GET", "/v1/x"]] and res[0]["hosts"] == ["h"]
    rep = cl.compare(plan | {"skipped": [], "failures": []}, res, res, environ={})
    assert SECRET not in rep.render()


def test_a_step_whose_needs_are_missing_is_blocked_not_run(tmp_path: Path) -> None:
    sb = _fake_sandbox(tmp_path)
    plan = {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step("dep", needs=["identity"])]}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud")
    assert res[0]["blocked"] and res[0]["exit"] == -1 and not res[0]["taps"]


def test_retries_keep_every_attempts_taps_and_never_retry_a_failure(tmp_path: Path) -> None:
    counter = tmp_path / "count"
    body = ('#!/bin/bash\nn=$(cat "%s" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "%s"\n'
            'printf "#tap-ready\\n" >> "$NX_CLOUDLEG_TAP"\n'
            'if [ "$n" = 1 ]; then printf "403\\tPOST\\t/v1/vectors/search\\th\\n" >> "$NX_CLOUDLEG_TAP"; exit 0; fi\n'
            'printf "200\\tPOST\\t/v1/vectors/search\\th\\n" >> "$NX_CLOUDLEG_TAP"; echo cloudleg-r0123456; exit 0\n') % (counter, counter)
    sb = _fake_sandbox(tmp_path, body)
    plan = {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step("store-search", expect_stdout="@label@", tries=3)]}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud", sleep=lambda s: None)
    assert res[0]["attempts"] == 2 and res[0]["taps"] == [[403, "POST", "/v1/vectors/search"], [200, "POST", "/v1/vectors/search"]]
    local = [_res("store-search", "local")]
    assert "cloud-only 403" in cl.judge_step(local[0], res[0])[1], "a 4xx on an earlier attempt must still fail the step"
    # a non-zero exit is judged at once, not retried
    sb2 = _fake_sandbox(tmp_path / "two")
    res = cl.run_slot(plan, runner=str(sb2 / "run"), sandbox=sb2, host="h", workdir=tmp_path / "w2", slot="cloud", sleep=lambda s: pytest.fail("slept"))
    assert res[0]["attempts"] == 1 and res[0]["exit"] == 1


def test_a_step_that_reads_an_index_retries_then_is_judged_and_the_attempts_are_recorded(tmp_path: Path) -> None:
    counter = tmp_path / "count"
    body = ('#!/bin/bash\nn=$(cat "%s" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "%s"\n'
            'printf "#tap-ready\\n200\\tPOST\\t/v1/vectors/search\\th\\n" >> "$NX_CLOUDLEG_TAP"\n'
            '[ "$n" -ge 3 ] && echo "cloudleg-r0123456 found" ; exit 0\n') % (counter, counter)
    sb = _fake_sandbox(tmp_path, body)
    sleeps: list[float] = []
    plan = {"run": "r0123456", "label": "cloudleg-r0123456",
            "steps": [_step("store-search", expect_stdout="@label@", tries=3, expect_route="/v1/vectors/search")]}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud", sleep=sleeps.append)
    assert res[0]["attempts"] == 3 and res[0]["expect_ok"] and sleeps == [cl.SEARCH_WAIT_S, cl.SEARCH_WAIT_S]
    assert "a=3" in cl.compare({"run": "r0123456", "steps": [plan["steps"][0]], "skipped": [], "failures": [], "floor": 1}, res, res, environ={}).render()
    # never found: tries are bounded and the step is judged on the last attempt
    counter.write_text("-100")
    sleeps.clear()
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w2", slot="cloud", sleep=sleeps.append)
    assert res[0]["attempts"] == 3 and not res[0]["expect_ok"] and len(sleeps) == 2


# ── the HTTP tap ────────────────────────────────────────────────────────────

class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code = 403 if self.path.startswith("/v1/denied") else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_GET  # noqa: N815

    def log_message(self, *a):  # noqa: D401
        pass


def test_tap_records_status_method_route_host_and_nothing_else(tmp_path: Path) -> None:
    pytest.importorskip("httpx")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    tap = tmp_path / "tap.log"
    code = (
        "import httpx, http.client\n"
        f"httpx.Client().get('http://127.0.0.1:{port}/v1/a/b?token={SECRET}', headers={{'Authorization': 'Bearer {SECRET}'}})\n"
        f"c = http.client.HTTPConnection('127.0.0.1', {port}); c.request('POST', '/v1/denied?x={SECRET}', body=b'{SECRET}'); c.getresponse().read()\n"
        f"httpx.Client().get('http://localhost:{port}/v1/not-my-host')\n"
    )
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(TAP_DIR), "NX_CLOUDLEG_TAP": str(tap)}
    try:
        cp = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    assert cp.returncode == 0, cp.stderr
    text = tap.read_text()
    assert text.splitlines() == ["#tap-ready", "200\tGET\t/v1/a/b\t127.0.0.1", "403\tPOST\t/v1/denied\t127.0.0.1",
                                 "200\tGET\t/v1/not-my-host\tlocalhost"]
    assert SECRET not in text
    ready, rows = cl.parse_tap(tap)
    assert ready and rows[:2] == [(200, "GET", "/v1/a/b"), (403, "POST", "/v1/denied")]
    # the host is read from each request's own URL, so it is evidence: the comparator filters on it
    assert cl.parse_tap_hosts(tap)[2] == {"127.0.0.1", "localhost"}


def test_tap_is_inert_without_its_variable(tmp_path: Path) -> None:
    cp = subprocess.run([sys.executable, "-c", "print(1)"], env={"PATH": os.environ["PATH"], "PYTHONPATH": str(TAP_DIR)},
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip() == "1"


# ── cloud-leg.sh: every refusal, and no token ever printed ──────────────────

REAL_TITLE = "nexus cloud test tenant (release-test)"


def _stub_op(bindir: Path, fields: dict[str, str] | None, *, fail: bool = False, title: str = REAL_TITLE) -> None:
    """A stand-in op-auto: `item get <arg> --vault V --fields label=<f> --reveal` prints the field, and
    `item get <arg> --vault V --format json` prints the item's own JSON (its REAL title, whatever the argument said)."""
    bindir.mkdir(parents=True, exist_ok=True)
    cases = "\n".join(f'  {k}) printf "%s\\n" "{v}" ;;' for k, v in (fields or {}).items())
    body = "exit 1" if fail else (
        'for a in "$@"; do [ "$a" = json ] && { echo \'{"title": "' + title + '", "fields": []}\'; exit 0; }; done\n'
        'f=""; for a in "$@"; do case "$a" in label=*) f="${a#label=}" ;; esac; done\n'
        f'case "$f" in\n{cases}\n  *) exit 1 ;;\nesac')
    p = bindir / "op-auto"
    p.write_text(f"#!/bin/bash\n{body}\n")
    p.chmod(0o755)


def _leg(tmp_path: Path, *args: str, op: dict[str, str] | None = None, op_fail: bool = False, window: bool = True,
         ambient: dict[str, str] | None = None, wheel: bool = False, title: str = REAL_TITLE, bash: str = BASH,
         extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    _stub_op(bindir, op, fail=op_fail, title=title)
    wdir = tmp_path / "windows"
    wdir.mkdir(exist_ok=True)
    if window:
        _marker(wdir, "w1")
    home = tmp_path / "home"
    (home / ".config/nexus").mkdir(parents=True, exist_ok=True)
    path = f"{bindir}:{os.environ['PATH']}"
    if extra_path is not None:
        path = f"{extra_path}:{path}"
    env = {"PATH": path, "HOME": str(home), "NX_CLOUD_WINDOW_DIR": str(wdir),
           "NEXUS_CONFIG_DIR": str(home / ".config/nexus"), **(ambient or {})}
    env = child_env_with_tmpdir(env, tmp_path)
    extra = []
    if wheel:
        w = tmp_path / "x.whl"
        w.write_text("not a wheel")
        extra = ["--wheel", str(w)]
    return subprocess.run([bash, str(LEG_SH), *extra, *args], env=env, capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)


GOOD = {"service_url": "https://api.conexus-nexus.com", "token": SECRET, "mint_tenant": "release-test"}


def _refused(cp: subprocess.CompletedProcess[str], fragment: str) -> None:
    assert cp.returncode == 2, (cp.returncode, cp.stdout, cp.stderr)
    assert "REFUSED" in cp.stderr and fragment in cp.stderr, cp.stderr
    assert SECRET not in cp.stdout + cp.stderr


def test_sh_refuses_without_a_window(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, op=GOOD), "no announced cloud window")


def test_sh_refuses_a_window_that_was_never_announced(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w9", op=GOOD, window=False), "does not exist")


def test_sh_refuses_a_missing_op_item(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", op_fail=True), "has no readable field 'service_url'")


def test_sh_refuses_the_operators_tenant(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", op={**GOOD, "mint_tenant": "nexus"}), "not 'release-test'")


def test_sh_refuses_a_missing_mint_tenant_field(tmp_path: Path) -> None:
    only = {k: v for k, v in GOOD.items() if k != "mint_tenant"}
    _refused(_leg(tmp_path, "--window", "w1", op=only), "no readable field 'mint_tenant'")


def test_sh_refuses_a_non_https_edge(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", op={**GOOD, "service_url": "http://api.conexus-nexus.com"}), "https")


def test_sh_refuses_the_operators_ambient_token(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", op=GOOD, ambient={"NX_MINT_TOKEN": SECRET}), "operator's own ambient mint_token")


def test_sh_refuses_a_missing_wheel_after_the_credential_checks(tmp_path: Path) -> None:
    # --core-only: the local plan runs before the wheel check, so a full run would refuse on an empty live ledger instead
    _refused(_leg(tmp_path, "--window", "w1", "--core-only", op=GOOD), "--wheel")


def test_sh_checks_the_items_own_title_not_the_argument(tmp_path: Path) -> None:
    """--op-item still says 'test'; the vault's item answers a title that does not."""
    _refused(_leg(tmp_path, "--window", "w1", op=GOOD, title="nexus production tenant"), "word 'test'")


def test_sh_refuses_an_item_that_answers_no_title(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", op=GOOD, title=""), "answered no title")


def test_sh_refuses_a_host_that_is_not_the_pinned_edge(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", op={**GOOD, "service_url": "https://evil.example.com"}), "not the pinned edge")


def test_sh_refuses_a_window_marker_that_is_not_0600(tmp_path: Path) -> None:
    wdir = tmp_path / "windows"
    wdir.mkdir()
    _marker(wdir, "w1").chmod(0o644)
    _refused(_leg(tmp_path, "--window", "w1", op=GOOD, window=False), "0600")


def test_sh_refuses_an_explicit_ledger_that_does_not_exist(tmp_path: Path) -> None:
    _refused(_leg(tmp_path, "--window", "w1", "--ledger", str(tmp_path / "nope.md"), op=GOOD, wheel=True), "does not exist")


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="no /bin/bash")
def test_sh_plans_under_the_system_bash_with_every_array_empty(tmp_path: Path) -> None:
    """macOS /bin/bash is 3.2: an empty array under `set -u` is an unbound variable there. With no --expect-red,
    --accept-skip or ledger args, the script must still reach and print its plan."""
    cp = _leg(tmp_path, "--local-vs-local", "--plan-only", "--core-only", bash="/bin/bash")
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert "unbound variable" not in cp.stderr and "plan:" in cp.stdout and "CORE-ONLY" in cp.stdout


def test_sh_plan_only_names_unmapped_routes_and_stops_before_any_sandbox(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    cp = _leg(tmp_path, "--local-vs-local", "--plan-only", "--ledger", str(ledger))
    assert cp.returncode == 2 and "unmapped ledger route" in cp.stderr and "/v1/widgets/frob" in cp.stderr
    ok = _leg(tmp_path, "--local-vs-local", "--plan-only", "--ledger", str(ledger), "--accept-skip", "/v1/widgets/frob:internal")
    assert ok.returncode == 0 and "0 unmapped" in ok.stdout and "2 routes driven" in ok.stdout
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("cloudleg-work")]


@pytest.mark.parametrize("flag, fragment", [
    ("identity-seat-create:no pin", "--expect-red needs"),
    ("identity-seat-create@403:", "--expect-red needs"),
    ("no-such-step@403:why", "not steps of this plan"),
])
def test_sh_aborts_on_a_bad_expect_red_before_any_sandbox(tmp_path: Path, flag: str, fragment: str) -> None:
    cp = _leg(tmp_path, "--local-vs-local", "--core-only", "--plan-only", "--expect-red", flag)
    assert cp.returncode == 2 and fragment in cp.stderr, (cp.stdout, cp.stderr)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(("cloudleg-work", "nxcs"))]
    good = _leg(tmp_path, "--local-vs-local", "--core-only", "--plan-only", "--expect-red", "identity-seat-create@403:cut fixes it")
    assert good.returncode == 0, good.stderr


def test_sh_aborts_on_an_unknown_accept_skip(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    cp = _leg(tmp_path, "--local-vs-local", "--plan-only", "--ledger", str(ledger), "--accept-skip", "POST /v1/typo:x")
    assert cp.returncode == 2 and "no ledger entry names" in cp.stderr


def test_sh_refusals_leave_no_sandbox_and_no_credential_files(tmp_path: Path) -> None:
    before = {p.name for p in tmp_path.iterdir()}
    _leg(tmp_path, "--window", "w1", op={**GOOD, "mint_tenant": "nexus"})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(("cloudleg-cred", "cloudleg-work"))]
    assert leftovers == [], (before, leftovers)


# ── candidate_sandbox.sh ────────────────────────────────────────────────────

def _sh(script: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    prelude = f'. "{E2E}/lib/python.sh"; e2e_python_resolve 11 >/dev/null; . "{SANDBOX_SH}"; set -e\n'
    return subprocess.run([BASH, "-c", prelude + script], env=env, capture_output=True, text=True, timeout=120, cwd=cwd)


def test_sandbox_runner_is_a_whitelist_and_cloud_mode_has_no_service_token(tmp_path: Path) -> None:
    root = tmp_path / "sbx"
    cred = tmp_path / "cred"
    cred.mkdir()
    for k, v in GOOD.items():
        (cred / k).write_text(v + "\n")
    leak = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "realhome"), "NX_SERVICE_TOKEN": "AMBIENT-SVC", "NX_MINT_TOKEN": "AMBIENT-MINT",
            "NX_SERVICE_URL": "https://ambient.example", "VOYAGE_API_KEY": "AMBIENT-VOY", "AWS_SECRET_ACCESS_KEY": "AMBIENT-AWS",
            "NEXUS_CONFIG_DIR": str(tmp_path / "realcfg")}
    cp = _sh(f'cand_sandbox_create "{root}" >/dev/null\ncand_sandbox_config cloud "{cred}"\n"$CAND_SBX_RUN" env\n'
             f'echo "HOMEVAR=$CAND_SBX_HOME CFG=$CAND_SBX_CONFIG_DIR BIN=$CAND_SBX_TOOL_BIN"', leak)
    assert cp.returncode == 0, cp.stderr
    out = cp.stdout
    assert "AMBIENT" not in out and "realhome" not in out and "realcfg" not in out
    env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith(("HOMEVAR", " ")))
    assert env["HOME"] == str((root / "home").resolve()) and env["NEXUS_CONFIG_DIR"].endswith("home/.config/nexus")
    assert env["NX_SERVICE_URL"] == GOOD["service_url"] and env["NX_MINT_TOKEN"] == SECRET and env["NX_MINT_TENANT"] == "release-test"
    assert "NX_SERVICE_TOKEN" not in env and env["NX_NO_TELEMETRY"] == "1"
    assert set(env) <= {"HOME", "USER", "LANG", "TERM", "NEXUS_CONFIG_DIR", "CLAUDE_CONFIG_DIR", "NX_NO_TELEMETRY", "TMPDIR",
                        "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "PATH", "NX_SERVICE_URL", "NX_MINT_TOKEN", "NX_MINT_TENANT", "PWD", "SHLVL", "_", "OLDPWD"}
    assert "HOMEVAR=" in out and "/home/.config/nexus" in out and "/home/.local/bin" in out
    # the runner script itself carries no credential
    assert SECRET not in (root / "run").read_text() and SECRET not in (root / "env.sh").read_text()
    assert oct((root / "secrets" / "NX_MINT_TOKEN").stat().st_mode & 0o777) == "0o600"


def test_sandbox_cloud_mode_refuses_a_missing_required_field(tmp_path: Path) -> None:
    cred = tmp_path / "cred"
    cred.mkdir()
    (cred / "service_url").write_text("https://x\n")
    cp = _sh(f'cand_sandbox_create "{tmp_path}/s" >/dev/null\ncand_sandbox_config cloud "{cred}"', {"PATH": os.environ["PATH"]})
    assert cp.returncode != 0 and "field 'token' is missing" in cp.stderr


def test_sandbox_field_map_is_the_single_mapping(tmp_path: Path) -> None:
    cp = _sh("cand_cloud_fields", {"PATH": os.environ["PATH"]})
    assert cp.stdout.split() == ["service_url", "token", "mint_tenant"]
    text = SANDBOX_SH.read_text()
    assert text.count("NX_MINT_TOKEN") >= 1 and text.count("CAND_CLOUD_FIELD_MAP=") == 1


def test_sandbox_local_mode_refuses_a_non_loopback_engine(tmp_path: Path) -> None:
    ej = tmp_path / "engine.json"
    ej.write_text(json.dumps({"base_url": "https://api.conexus-nexus.com", "tenant": "t", "token": "x"}))
    cp = _sh(f'cand_sandbox_create "{tmp_path}/s" >/dev/null\ncand_sandbox_config local "{ej}" 0', {"PATH": os.environ["PATH"]})
    assert cp.returncode != 0 and "non-loopback" in cp.stderr


def test_engine_start_uses_a_fresh_short_pg_root_and_stop_removes_it(tmp_path: Path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "uv").write_text(
        '#!/bin/bash\n# fake `uv run python tests/e2e/lib/engine_holder.py <json> <n>`\n'
        'echo "$NX_TEST_PG_ROOT" > "$FAKE_PGROOT_SEEN"\n'
        'echo \'{"base_url": "http://127.0.0.1:1", "tenants": [{"tenant": "t", "token": "x"}]}\' > "$3"\nsleep 30\n')
    (fakebin / "uv").chmod(0o755)
    seen = tmp_path / "seen"
    env = {"PATH": f"{fakebin}:{os.environ['PATH']}", "FAKE_PGROOT_SEEN": str(seen), "NX_TEST_PG_ROOT": "/tmp/stale-ambient"}
    script = (f'cand_engine_start "{tmp_path}" "{tmp_path}/out" 1\nroot="$CAND_PG_ROOT"\n'
              f'[ -d "$root" ] || exit 7\ncand_engine_stop\n[ ! -e "$root" ] || exit 8\necho "$root"')
    # the fake takes <json> as $3: `uv run python <script> <json>` -> script is $3, json is $4; adapt
    (fakebin / "uv").write_text((fakebin / "uv").read_text().replace('"$3"', '"$4"'))
    cp = _sh(script, env)
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    root = cp.stdout.strip().splitlines()[-1]
    assert root.startswith("/tmp/nxcs-") and len(root) < 40
    assert seen.read_text().strip() == root and root != "/tmp/stale-ambient"


# ── --keep never keeps a credential ─────────────────────────────────────────

_STUB = r"""#!/bin/bash
# Stands in for nx and the candidate python. Taps every route a step might expect, prints what the steps capture,
# and LEAKS the sandbox's tokens into the identity dir (and a log outside the sandbox that proves it did).
H="${NX_SERVICE_URL#*//}"; H="${H%%[:/]*}"
if [ -n "$NX_CLOUDLEG_TAP" ]; then
  printf '#tap-ready\n' >> "$NX_CLOUDLEG_TAP"
  for r in /v1/identity /v1/identity/i1/bind /v1/memory/put /v1/tuples/out /v1/tuples/in /v1/tuples/ack /v1/t1/put \
           /v1/catalog/manifest/write_many /v1/vectors/search; do
    printf '200\tPOST\t%s\t%s\n' "$r" "$H" >> "$NX_CLOUDLEG_TAP"
  done
fi
mkdir -p "$NEXUS_CONFIG_DIR/identity/delegated"
printf '%s %s\n' "$NX_MINT_TOKEN" "$NX_SERVICE_TOKEN" > "$NEXUS_CONFIG_DIR/identity/delegated/i1.token"
printf 'token %s %s\n' "$NX_MINT_TOKEN" "$NX_SERVICE_TOKEN" > "$NEXUS_CONFIG_DIR/identity/last"
# the T1 session lease the real client leaves beside them: a 43-character bearer
printf '{"token": "%s", "expires_at": 1}\n' "$NX_MINT_TOKEN-lease-0123456789abcdefghij" > "$NEXUS_CONFIG_DIR/t1_session_lease.0fb0f3a8-65df-4eca-80db-7f53336bf840"
mkdir -p "$HOME/.cache" "$HOME/.claude"; echo "tok-0123456789abcdefghijklmnopqrstuvwxyzABCDEF" > "$HOME/.cache/x"; echo "$NX_MINT_TOKEN" > "$HOME/.claude/y"
echo leaked >> "@LEAKLOG@"
echo '{"identity": "i1", "token_file": "/nonexistent", "generation": 1, "claim_id": "c1"}'
echo "cloudleg-r0123456abc deadbeef12345678 $*"
"""

_FAKE_UV = r"""#!/bin/bash
case "$1" in
  cache) echo "$TMPDIR"; exit 0 ;;
  tool) mkdir -p "$UV_TOOL_BIN_DIR" "$UV_TOOL_DIR/conexus/bin"
        cp "@STUB@" "$UV_TOOL_BIN_DIR/nx"; cp "@STUB@" "$UV_TOOL_DIR/conexus/bin/python"
        chmod +x "$UV_TOOL_BIN_DIR/nx" "$UV_TOOL_DIR/conexus/bin/python"; exit 0 ;;
  run)  printf '{"base_url": "http://127.0.0.1:1", "tenants": [{"tenant": "t", "token": "localtok-LOCALTOKEN-1"}]}' > "$4"
        exec sleep 600 ;;
esac
exit 1
"""


def _keep_run(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    fake = tmp_path / "fakebin"
    fake.mkdir()
    leaklog = tmp_path / "leaklog"
    stub = tmp_path / "stub.sh"
    stub.write_text(_STUB.replace("@LEAKLOG@", str(leaklog)))
    uv = fake / "uv"
    uv.write_text(_FAKE_UV.replace("@STUB@", str(stub)))
    for p in (stub, uv):
        p.chmod(0o755)
    work = tmp_path / "kept"
    cp = _leg(tmp_path, "--window", "w1", "--keep", "--core-only", "--work-dir", str(work), "--run-id", "r0123456abc",
              op=GOOD, wheel=True, extra_path=fake)
    return cp, work, leaklog


_TOKENISH = re.compile(rb"[A-Za-z0-9_-]{40,}")


def _token_shaped(root: Path) -> list[str]:
    """Files under *root* holding a 40+ character token-shaped string, the uv-installed tools and pythons and their launchers excluded."""
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or "home/.local/share/uv/" in str(p) or "home/.local/bin" in str(p):
            continue  # the installed packages and their launchers are not state
        if _TOKENISH.search(p.read_bytes()):
            hits.append(str(p.relative_to(root)))
    return hits


def test_keep_leaves_no_credential_or_token_in_the_kept_directory(tmp_path: Path) -> None:
    cp, work, leaklog = _keep_run(tmp_path)
    assert cp.returncode == 0, (cp.stdout[-1500:], cp.stderr[-1500:])
    assert "CLOUD LEG PASSED" in cp.stdout and "CORE-ONLY: " in cp.stdout and " ledger routes not driven" in cp.stdout
    assert leaklog.read_text().count("leaked") >= 2, "the stub never leaked, so this test proves nothing"
    assert (work / "plan.json").is_file() and (work / "report.txt").is_file(), "--keep must keep the work dir"
    hits = [str(p) for p in work.rglob("*") if p.is_file() and any(t.encode() in p.read_bytes() for t in (SECRET, "localtok-LOCALTOKEN"))]
    assert hits == [], hits
    assert not list(work.glob("*/secrets")) and not list(work.glob("*/home/.config/nexus/identity")) and not list(work.rglob("*.token"))
    assert SECRET not in cp.stdout + cp.stderr
    assert _token_shaped(work) == [], "a token-shaped string survives in the kept tree"


@pytest.mark.skipif(os.environ.get("NX_CLOUDLEG_REAL_RUN") != "1",
                    reason="set NX_CLOUDLEG_REAL_RUN=1 (needs the gate jar, uv and the network): a real local-vs-local --keep run")
def test_a_real_local_vs_local_keep_run_leaves_no_token_shaped_string(tmp_path: Path) -> None:
    wheel_dir = tmp_path / "wheel"
    subprocess.run(["uv", "build", "--wheel", "-o", str(wheel_dir)], cwd=REPO_ROOT, check=True, capture_output=True, timeout=900)
    wheel = next(wheel_dir.glob("conexus-*.whl"))
    work = tmp_path / "kept"
    cp = subprocess.run([BASH, str(LEG_SH), "--local-vs-local", "--keep", "--wheel", str(wheel), "--work-dir", str(work)],
                        capture_output=True, text=True, timeout=1500, cwd=REPO_ROOT)
    assert cp.returncode == 0 and "CLOUD LEG PASSED" in cp.stdout, (cp.stdout[-2000:], cp.stderr[-1000:])
    assert (work / "local/home/.local/share/uv/tools/conexus").is_dir(), "the installed tools are kept"
    assert _token_shaped(work) == [], "a token-shaped string survives in the kept tree"
    assert not list(work.rglob("t1_session_lease*"))


def test_sandbox_scrub_removes_secrets_identity_and_token_files(tmp_path: Path) -> None:
    root = tmp_path / "sbx"
    home = root / "home"
    stray = home / "stray"  # a directory the client might create under HOME
    cp = _sh(f'cand_sandbox_create "{root}" >/dev/null\nprintf "%s\\n" {SECRET} > "{root}/secrets/NX_MINT_TOKEN"\n'
             f'mkdir -p "{home}/.config/nexus/identity/delegated" "{stray}"\n'
             f'echo {SECRET} > "{home}/.config/nexus/identity/delegated/i1.token"\necho {SECRET} > "{stray}/x.token"\n'
             f'echo keep > "{stray}/other.txt"\necho lease > "{home}/.config/nexus/t1_session_lease.abc"\n'
             f'mkdir -p "{home}/.local/share/uv/tools/conexus/bin" "{home}/.local/state"\n'
             f'echo tool > "{home}/.local/share/uv/tools/conexus/bin/python"\necho x > "{root}/tmp/scratch"\ncand_sandbox_scrub "{root}"',
             {"PATH": os.environ["PATH"]})
    assert cp.returncode == 0, cp.stderr
    assert not (root / "secrets").exists() and not list(root.rglob("*.token"))
    # everything the client wrote under HOME is gone (the T1 lease included); only the installed tools survive
    assert sorted(p.name for p in (home / ".config/nexus").iterdir()) == []
    assert not stray.exists() and not (home / ".local/state").exists() and not list((root / "tmp").iterdir())
    assert (home / ".local/share/uv/tools/conexus/bin/python").is_file()


# ── the gate scripts' own conventions ───────────────────────────────────────

def test_new_scripts_pass_shellcheck() -> None:
    sc = shutil.which("shellcheck")
    if sc is None:
        pytest.skip("shellcheck is not installed")
    cp = subprocess.run([sc, "-x", str(LEG_SH), str(SANDBOX_SH)], capture_output=True, text=True, cwd=REPO_ROOT)
    assert cp.returncode == 0, cp.stdout


def test_cloud_leg_names_no_bare_python3() -> None:
    for p in (LEG_SH, SANDBOX_SH):
        assert "python3 " not in p.read_text().replace("# ", "#"), p


def test_the_left_behind_line_says_why_the_seat_stays_and_why_the_cap_cannot_fill() -> None:
    """RDR-253 retention keeps seats; the 0.1.198 cap counts per creating identity (a fresh one per run)."""
    line = next(ln for ln in LEG_SH.read_text().splitlines() if "left behind" in ln)
    for fact in ("permanent by RDR-253 retention", "per creating identity", "fresh ephemeral identity each run",
                 "tombstones after 30 days", "one collection every run reuses"):
        assert fact in line, (fact, line)
    assert "cannot delete" not in line
