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
import shutil
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
])
def test_window_refusals(tmp_path: Path, why: str, wid: str, over: dict, fragment: str) -> None:
    _marker(tmp_path, "w1", **over)
    with pytest.raises(cl.Refusal, match=fragment):
        cl.check_window(wid, tmp_path)


# ── refusals: target ────────────────────────────────────────────────────────

def _target(**over):
    base = dict(service_url="https://api.conexus-nexus.com", token=SECRET, mint_tenant="release-test",
                item_title="nexus cloud test tenant (release-test)", ambient_environ={}, ambient_config=None)
    base.update(over)
    return base


def test_target_ok() -> None:
    cl.check_target(**_target())


@pytest.mark.parametrize("over, fragment", [
    ({"service_url": "http://api.conexus-nexus.com"}, "https"),
    ({"service_url": "https://127.0.0.1:8443"}, "loopback or private"),
    ({"service_url": "https://localhost"}, "loopback or private"),
    ({"service_url": "https://u:p@api.conexus-nexus.com"}, "user:password@"),
    ({"mint_tenant": "nexus"}, "not 'release-test'"),
    ({"mint_tenant": ""}, "not 'release-test'"),
    ({"mint_tenant": "release-test-2"}, "not 'release-test'"),
    ({"item_title": "nexus production tenant"}, "test"),
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


def test_ledger_parser_maps_and_skips(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    entries = cl.ledger_entries(ledger)
    assert len(entries) == 3
    steps, skipped = cl.release_steps(entries)
    assert [s.name for s in steps] == ["route-docs-by-scheme", "route-docs-by-author"]
    assert steps[0].origin.startswith("aaaaaaa nexus-x.1 GET /v1/catalog/docs/by-scheme")
    reasons = dict(skipped)
    assert any("POST /v1/widgets/frob" in k and "no client call is mapped" in v for k, v in reasons.items())
    assert any(k == "ccccccc nexus-x.3" and "no wire route" in v for k, v in reasons.items())
    assert len(steps) + len(skipped) == 4  # every route of every entry is accounted for


def test_ledger_parser_reads_a_named_release(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    steps, skipped = cl.release_steps(cl.ledger_entries(ledger, "v1.2.3"))
    assert [s.name for s in steps] == ["route-sensitive-locators"] and not skipped
    assert cl.ledger_entries(ledger, "v0.0.0") == []


def test_live_ledger_parses_and_every_entry_is_accounted_for() -> None:
    ledger = REPO_ROOT / "docs" / "wire-contract-pending.md"
    entries = cl.ledger_entries(ledger)
    steps, skipped = cl.release_steps(entries)
    assert len(steps) + len(skipped) >= len(entries)


def test_plan_has_core_floor_and_cleanup_last(tmp_path: Path) -> None:
    plan = cl.build_plan("r0123456abc", None, None)
    names = [s["name"] for s in plan["steps"]]
    assert plan["floor"] == 10
    assert names[-3:] == ["store-delete", "scratch-delete", "memory-delete-threads"]
    assert {"memory-put-threads-identity", "mailbox-send", "mailbox-claim", "scratch-put", "store-put", "store-search",
            "identity-seat-create", "identity-seat-show", "identity-bind"} <= set(names)
    with pytest.raises(ValueError):
        cl.build_plan("BAD ID", None, None)
    assert "@label@" in json.dumps(plan) and SECRET not in json.dumps(plan)


# ── the comparator ──────────────────────────────────────────────────────────

def _res(name: str, slot: str, *, exit: int = 0, taps: list | None = None, ready: bool = True, error: str = "",
         expect_ok: bool = True, skipped: str | None = None, blocked: str | None = None) -> dict:
    return {"name": name, "slot": slot, "exit": exit, "taps": taps if taps is not None else [[200, "GET", "/v1/x"]],
            "tap_ready": ready, "stdout": "", "error": error, "skipped": skipped, "blocked": blocked, "expect_ok": expect_ok}


def test_cloud_only_4xx_fails_and_names_status_and_route() -> None:
    v, why = cl.judge_step(_res("s", "local"), _res("s", "cloud", exit=1, taps=[[403, "POST", "/v1/memory/put"]]))
    assert v == "FAIL" and "cloud-only 403" in why and "/v1/memory/put" in why


def test_a_4xx_local_also_returns_passes() -> None:
    both = [[409, "POST", "/v1/identity"]]
    v, why = cl.judge_step(_res("s", "local", exit=1, taps=both), _res("s", "cloud", exit=1, taps=both))
    assert v == "PASS-both-refused"


def test_a_different_status_in_cloud_still_fails() -> None:
    v, _ = cl.judge_step(_res("s", "local", exit=1, taps=[[409, "POST", "/v1/identity"]]),
                         _res("s", "cloud", exit=1, taps=[[403, "POST", "/v1/identity"]]))
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


def _plan(n: int, floor: int, skipped: list | None = None) -> dict:
    return {"steps": [{"name": f"s{i}"} for i in range(n)], "skipped": skipped or [], "floor": floor}


def test_compare_counts_and_passes() -> None:
    plan = _plan(3, 3, [["u 1", "no client call is mapped"]])
    rep = cl.compare(plan, [_res(f"s{i}", "local") for i in range(3)], [_res(f"s{i}", "cloud") for i in range(3)])
    assert rep.ok and rep.examined == 3 and rep.failed == 0
    text = rep.render()
    assert "examined 3, skipped 1 (u 1: no client call is mapped), failed 0" in text and "CLOUD LEG PASSED" in text
    assert text.splitlines()[0].split() == ["step", "local", "cloud", "verdict"]


def test_compare_fails_on_one_bad_step() -> None:
    plan = _plan(3, 3)
    cloud = [_res("s0", "cloud"), _res("s1", "cloud", exit=1, taps=[[403, "POST", "/v1/memory/put"]]), _res("s2", "cloud")]
    rep = cl.compare(plan, [_res(f"s{i}", "local") for i in range(3)], cloud)
    assert not rep.ok and rep.failed == 1 and "failed 1" in rep.render() and "CLOUD LEG FAILED" in rep.render()


def test_non_vacuity_below_floor_and_all_skipped() -> None:
    plan = _plan(3, 3)
    mixed_local = [_res("s0", "local"), _res("s1", "local", skipped="no verb"), _res("s2", "local")]
    mixed_cloud = [_res("s0", "cloud"), _res("s1", "cloud", skipped="no verb"), _res("s2", "cloud")]
    rep = cl.compare(plan, mixed_local, mixed_cloud)
    assert rep.failed == 0 and not rep.ok and "below the floor of 3" in rep.render()
    none_local = [_res(f"s{i}", "local", skipped="x") for i in range(3)]
    none_cloud = [_res(f"s{i}", "cloud", skipped="x") for i in range(3)]
    rep = cl.compare(plan, none_local, none_cloud)
    assert not rep.ok and "nothing was examined" in rep.render()
    rep = cl.compare(_plan(0, 0), [], [])
    assert not rep.ok


# ── slot runner: no secret in any output ────────────────────────────────────

def _fake_sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sbx"
    (sb / "secrets").mkdir(parents=True)
    (sb / "secrets" / "NX_MINT_TOKEN").write_text(SECRET + "\n")
    (sb / "home/.local/bin").mkdir(parents=True)
    (sb / "home/.local/share/uv/tools/conexus/bin").mkdir(parents=True)
    # the "runner" execs its arguments, leading NAME=value included; the fake nx leaks the secret on purpose
    run = sb / "run"
    run.write_text('#!/bin/bash\nexec /usr/bin/env "$@"\n')
    nx = sb / "home/.local/bin/nx"
    nx.write_text(f'#!/bin/bash\necho "stdout {SECRET}"\necho "Error: refused {SECRET}" >&2\nprintf "#tap-ready\\n200\\tGET\\t/v1/x\\n" >> "$NX_CLOUDLEG_TAP"\nexit 1\n')
    for p in (run, nx):
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return sb


def test_run_slot_scrubs_the_secret_from_results(tmp_path: Path) -> None:
    sb = _fake_sandbox(tmp_path)
    plan = {"run": "r0123456", "label": "cloudleg-r0123456",
            "steps": [{"name": "probe", "kind": "nx", "argv": ["x"], "core": True, "cleanup": False, "optional_verb": None,
                       "identity": False, "needs": [], "capture": {}, "stdin": None, "expect_stdout": None, "origin": "core"}]}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud")
    blob = json.dumps(res)
    assert SECRET not in blob and "***" in blob
    assert res[0]["exit"] == 1 and res[0]["tap_ready"] and res[0]["taps"] == [[200, "GET", "/v1/x"]]
    plan["floor"] = 1
    rep = cl.compare(plan, res, res)
    assert SECRET not in rep.render()


def test_a_step_whose_needs_are_missing_is_blocked_not_run(tmp_path: Path) -> None:
    sb = _fake_sandbox(tmp_path)
    plan = {"run": "r0123456", "label": "cloudleg-r0123456",
            "steps": [{"name": "dep", "kind": "nx", "argv": ["x"], "core": True, "cleanup": False, "optional_verb": None,
                       "identity": False, "needs": ["identity"], "capture": {}, "stdin": None, "expect_stdout": None, "origin": "core"}]}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud")
    assert res[0]["blocked"] and res[0]["exit"] == -1 and not res[0]["taps"]


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


def test_tap_records_status_method_route_and_nothing_else(tmp_path: Path) -> None:
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
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(TAP_DIR), "NX_CLOUDLEG_TAP": str(tap), "NX_CLOUDLEG_HOST": "127.0.0.1"}
    try:
        cp = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    assert cp.returncode == 0, cp.stderr
    text = tap.read_text()
    assert text.splitlines() == ["#tap-ready", "200\tGET\t/v1/a/b", "403\tPOST\t/v1/denied"]
    assert SECRET not in text
    ready, rows = cl.parse_tap(tap)
    assert ready and rows == [(200, "GET", "/v1/a/b"), (403, "POST", "/v1/denied")]


def test_tap_is_inert_without_its_variable(tmp_path: Path) -> None:
    cp = subprocess.run([sys.executable, "-c", "print(1)"], env={"PATH": os.environ["PATH"], "PYTHONPATH": str(TAP_DIR)},
                        capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip() == "1"


# ── cloud-leg.sh: every refusal, and no token ever printed ──────────────────

def _stub_op(bindir: Path, fields: dict[str, str] | None, *, fail: bool = False) -> None:
    """A stand-in op-auto: `item get <title> --vault V --fields label=<f> --reveal` prints the field."""
    bindir.mkdir(parents=True, exist_ok=True)
    cases = "\n".join(f'  {k}) printf "%s\\n" "{v}" ;;' for k, v in (fields or {}).items())
    body = "exit 1" if fail else (
        'f=""; for a in "$@"; do case "$a" in label=*) f="${a#label=}" ;; esac; done\n'
        f'case "$f" in\n{cases}\n  *) exit 1 ;;\nesac')
    p = bindir / "op-auto"
    p.write_text(f"#!/bin/bash\n{body}\n")
    p.chmod(0o755)


def _leg(tmp_path: Path, *args: str, op: dict[str, str] | None = None, op_fail: bool = False, window: bool = True,
         ambient: dict[str, str] | None = None, wheel: bool = False) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    _stub_op(bindir, op, fail=op_fail)
    wdir = tmp_path / "windows"
    wdir.mkdir(exist_ok=True)
    if window:
        _marker(wdir, "w1")
    home = tmp_path / "home"
    (home / ".config/nexus").mkdir(parents=True, exist_ok=True)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home), "NX_CLOUD_WINDOW_DIR": str(wdir),
           "NEXUS_CONFIG_DIR": str(home / ".config/nexus"), "TMPDIR": str(tmp_path), **(ambient or {})}
    extra = []
    if wheel:
        w = tmp_path / "x.whl"
        w.write_text("not a wheel")
        extra = ["--wheel", str(w)]
    return subprocess.run([BASH, str(LEG_SH), *extra, *args], env=env, capture_output=True, text=True, timeout=120, cwd=REPO_ROOT)


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
    _refused(_leg(tmp_path, "--window", "w1", op=GOOD), "--wheel")


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
