# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cloud leg's engine detection (nexus-h4hge): a ledger route whose engine tag is later than the engine the cloud
runs is skipped, the report says the run is not a release pass, and --require-deployed fails it. Offline: the only
HTTP is a server on 127.0.0.1."""
from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest
from test_cloud_leg import GOOD, _leg  # the sh harness: a stub op-auto, a window marker, a scratch HOME

REPO_ROOT = Path(__file__).resolve().parents[2]
LEG_PY = REPO_ROOT / "tests" / "e2e" / "lib" / "cloud_leg.py"

_spec = importlib.util.spec_from_file_location("cloud_leg_h4hge", LEG_PY)
cl = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["cloud_leg_h4hge"] = cl
_spec.loader.exec_module(cl)  # type: ignore[union-attr]

_FIXTURE_LEDGER = """# ledger

## Unshipped

- `aaaaaaa1111` -- bead nexus-x.1 -- engine tag `engine-service-v9.9.9` -- [additive] New route `GET /v1/catalog/docs/by-scheme?scheme=<s>&limit=`. Direction safety: OLD client + NEW engine is safe.
- `bbbbbbb2222` -- bead nexus-x.2 -- engine tag `engine-service-v9.9.9` -- [additive] New route `POST /v1/widgets/frob` and `GET /v1/catalog/docs/by-author?author=x`. OLD client + NEW engine is safe.
- `ccccccc3333` -- bead nexus-x.3 -- engine tag `engine-service-v9.9.9` -- [additive] text only: a remedy string changed. OLD client + NEW engine is safe.

## Shipped
- `ddddddd4444` -- bead nexus-x.4 -- shipped in `v1.2.3` -- engine half engine-service-v9.9.8 -- [additive] `GET /v1/catalog/sensitive/locators?limit=N` came. OLD client + NEW engine is safe.
"""


# ── routes whose engine tag the cloud does not run yet (nexus-h4hge) ───────

_NOT_DEPLOYED = "SKIPPED, engine tag engine-service-v9.9.9 not deployed (the cloud runs 9.9.8)"


def test_a_route_whose_engine_tag_is_ahead_of_the_cloud_is_skipped_not_driven(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    entries = cl.ledger_entries(ledger)
    steps, skipped, unmapped = cl.release_steps(entries, cloud_engine=(9, 9, 8))
    assert steps == []
    assert dict(skipped)["aaaaaaa nexus-x.1 GET /v1/catalog/docs/by-scheme"] == _NOT_DEPLOYED
    assert dict(skipped)["bbbbbbb nexus-x.2 GET /v1/catalog/docs/by-author"] == _NOT_DEPLOYED
    # an unmapped route stays a failure whatever the cloud runs: the post-deploy run would hit it anyway
    assert [k for k, _v in unmapped] == ["bbbbbbb nexus-x.2 POST /v1/widgets/frob"]
    assert dict(skipped)["ccccccc nexus-x.3"].startswith("no wire route named")
    # the cloud at or past the tag drives every mapped route, as with no reading at all
    for deployed in ((9, 9, 9), (9, 10, 0)):
        at, _s, _u = cl.release_steps(entries, cloud_engine=deployed)
        assert [s.name for s in at] == ["route-docs-by-scheme", "route-docs-by-author"]


def test_a_route_named_by_a_deployed_entry_too_is_driven(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    text = _FIXTURE_LEDGER.replace("\n## Shipped", "- `eeeeeee5555` -- bead nexus-x.5 -- engine tag `engine-service-v9.9.1` -- "
                                   "[additive] `GET /v1/catalog/docs/by-scheme` again, on an older engine. OLD client + NEW "
                                   "engine is safe.\n\n## Shipped")
    ledger.write_text(text)
    steps, skipped, _u = cl.release_steps(cl.ledger_entries(ledger), cloud_engine=(9, 9, 8))
    assert [(s.name, s.origin.split(" ")[1]) for s in steps] == [("route-docs-by-scheme", "nexus-x.5")]
    assert "aaaaaaa nexus-x.1 GET /v1/catalog/docs/by-scheme" not in dict(skipped)


def test_a_shipped_release_entry_reads_its_engine_half(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER)
    entries = cl.ledger_entries(ledger, "v1.2.3")
    steps, skipped, _u = cl.release_steps(entries, cloud_engine=(9, 9, 7))
    assert not steps and dict(skipped)["ddddddd nexus-x.4 GET /v1/catalog/sensitive/locators"] == (
        "SKIPPED, engine tag engine-service-v9.9.8 not deployed (the cloud runs 9.9.7)")
    assert [s.name for s in cl.release_steps(entries, cloud_engine=(9, 9, 8))[0]] == ["route-sensitive-locators"]


def test_the_plan_records_the_cloud_engine_and_its_skips_and_keeps_the_core_floor(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER.replace("POST /v1/widgets/frob", "GET /v1/catalog/docs/by-meta"))
    plan = cl.build_plan("r0123456abc", ledger, None, cloud_engine="9.9.8")
    assert plan["failures"] == [] and plan["routes_driven"] == 0 and plan["cloud_engine"] == "9.9.8"
    assert plan["not_deployed_routes"] == 3 and plan["floor"] == 12
    full = cl.build_plan("r0123456abc", ledger, None, cloud_engine="v9.9.9")
    assert full["routes_driven"] == 3 and full["not_deployed_routes"] == 0 and full["cloud_engine"] == "9.9.9"
    for bad in ("9.9", "SNAPSHOT", ""):
        with pytest.raises(cl.Refusal, match="cloud engine"):
            cl.build_plan("r0123456abc", ledger, None, cloud_engine=bad)


def test_the_plan_command_prints_the_not_deployed_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER.replace("POST /v1/widgets/frob", "GET /v1/catalog/docs/by-meta"))
    out = tmp_path / "plan.json"
    assert cl.main(["plan", "--run", "r0123456abc", "--ledger", str(ledger), "--cloud-engine", "9.9.8", "--out", str(out)]) == 0
    assert "3 not deployed on the cloud engine 9.9.8" in capsys.readouterr().out
    assert json.loads(out.read_text())["not_deployed_routes"] == 3


def test_the_report_names_the_cloud_engine_and_the_routes_it_skipped(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER.replace("POST /v1/widgets/frob", "GET /v1/catalog/docs/by-meta"))
    plan = cl.build_plan("r0123456abc", ledger, None, cloud_engine="9.9.8")
    text = cl.compare(plan, [], [], None).render()
    assert "cloud engine 9.9.8 (GET /version): 3 ledger routes skipped as not deployed" in text
    assert "SKIPPED, engine tag engine-service-v9.9.9 not deployed" in text


class _VersionH(http.server.BaseHTTPRequestHandler):
    answers: dict[str, tuple[int, bytes]] = {}
    seen: list[dict[str, str]] = []

    def do_GET(self):  # noqa: N802
        self.seen.append({k.lower(): v for k, v in self.headers.items()})
        code, body = self.answers.get(self.path, (404, b"{}"))
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # noqa: D401
        pass


@pytest.fixture
def version_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _VersionH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


@pytest.mark.parametrize("code,body,want", [
    (200, b'{"release_version": "0.1.197", "embedding_mode": "voyage"}', "0.1.197"),
    (200, b'{"release_version": "v0.1.197"}', "0.1.197"),
    (200, b'{"release_version": ""}', None),
    (200, b'{"release_version": "0.1.197-SNAPSHOT"}', None),
    (200, b'{"embedding_mode": "voyage"}', None),
    (200, b"not json", None),
    (200, b'["0.1.197"]', None),
    (503, b'{"release_version": "0.1.197"}', None),
])
def test_the_cloud_engine_is_read_from_version_and_an_unreadable_one_refuses(version_server: str, code: int, body: bytes,
                                                                              want: str | None) -> None:
    _VersionH.answers = {"/version": (code, body)}
    _VersionH.seen = []
    if want is None:
        with pytest.raises(cl.Refusal, match="cloud engine"):
            cl.read_cloud_engine(version_server + "/")
    else:
        assert cl.read_cloud_engine(version_server + "/") == want
    # the read is unauthenticated: no bearer, no tenant, no identity, nothing of the credential
    assert _VersionH.seen and all("authorization" not in h and not any(k.startswith("x-nexus") for k in h)
                                  for h in _VersionH.seen), _VersionH.seen


def test_the_cloud_engine_command_refuses_a_non_https_service_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """check-target ran before it; the command still reads only the https edge, and sends no credential."""
    (tmp_path / "service_url").write_text("http://127.0.0.1:1\n")
    assert cl.main(["cloud-engine", "--cred-dir", str(tmp_path)]) == 2
    assert "https" in capsys.readouterr().err


# ── the verdict, and --require-deployed for the gating rerun ────────────────

def test_a_run_that_skipped_release_routes_is_not_a_release_pass(tmp_path: Path) -> None:
    def report(nd: int, red: int = 0) -> str:
        return cl.Report(rows=[], examined=1, skipped=[], failed=0, expected_red=red, both_refused=0, nonvacuity=[],
                         red_names=["identity-seat-create"] * red, not_deployed=nd).render().splitlines()[-1]
    assert report(0) == "CLOUD LEG PASSED"
    assert report(3).startswith("CLOUD LEG PASSED (NOT A RELEASE PASS: 3 release routes not driven, engine tag not deployed")
    assert "--require-deployed" in report(3)
    assert report(2, red=1).startswith("CLOUD LEG PASSED WITH EXPECTED-RED 1: identity-seat-create (NOT A RELEASE PASS: 2 ")
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER.replace("POST /v1/widgets/frob", "GET /v1/catalog/docs/by-meta"))
    assert cl.compare(cl.build_plan("r0123456abc", ledger, None, cloud_engine="9.9.8"), [], [], None).not_deployed == 3


def test_require_deployed_fails_the_plan_on_any_not_deployed_route(tmp_path: Path) -> None:
    ledger = tmp_path / "wire.md"
    ledger.write_text(_FIXTURE_LEDGER.replace("POST /v1/widgets/frob", "GET /v1/catalog/docs/by-meta"))
    behind = cl.build_plan("r0123456abc", ledger, None, cloud_engine="9.9.8", require_deployed=True)
    assert len(behind["failures"]) == 3 and all(f.startswith("--require-deployed: ") and "not deployed" in f
                                                for f in behind["failures"])
    assert cl.build_plan("r0123456abc", ledger, None, cloud_engine="9.9.9", require_deployed=True)["failures"] == []
    for kw in ({"core_only": True, "cloud_engine": "9.9.9"}, {}):
        with pytest.raises(cl.Refusal, match="--require-deployed needs a cloud run"):
            cl.build_plan("r0123456abc", ledger, None, require_deployed=True, **kw)


# ── cloud-leg.sh: every local refusal runs before the one cloud contact ─────

#: A host the edge override admits and no resolver answers (RFC 6761): the /version read can only fail, offline.
_NOWHERE = "cloudleg-edge.invalid"


def _sh(tmp_path: Path, *args: str, wheel: bool = False):
    return _leg(tmp_path, "--window", "w1", *args, op={**GOOD, "service_url": f"https://{_NOWHERE}"}, wheel=wheel,
                ambient={"NX_CLOUDLEG_EDGE_HOST_OVERRIDE": _NOWHERE})


@pytest.mark.parametrize("args,fragment", [
    (("--core-only",), "--wheel"),                                     # no candidate wheel (core-only: any live ledger)
    (("--ledger", "/nonexistent/wire.md"), "does not exist"),          # a named ledger that is not there
    (("--accept-skip", "POST /v1/typo:x"), "which no ledger entry names"),
    (("--core-only", "--expect-red", "no-such-step@403:x"), "not steps of this plan"),
    (("--core-only", "--require-deployed"), "--require-deployed needs a cloud run"),
])
def test_sh_refuses_locally_before_reading_the_cloud_engine(tmp_path: Path, args: tuple[str, ...], fragment: str) -> None:
    cp = _sh(tmp_path, *args, wheel=fragment != "--wheel")
    assert cp.returncode == 2 and fragment in cp.stderr, (cp.returncode, cp.stderr)
    assert "cloud engine" not in cp.stderr, "the /version read ran before a local refusal"


def test_sh_reads_the_cloud_engine_after_the_local_checks_and_refuses_when_it_cannot(tmp_path: Path) -> None:
    """The control for the test above: with every local check passing, the read is reached, and an engine the leg
    cannot name refuses the run before any sandbox."""
    cp = _sh(tmp_path, "--core-only", "--plan-only")
    assert cp.returncode == 2 and "could not read the cloud engine" in cp.stderr, (cp.returncode, cp.stderr)
    assert "plan:" in cp.stdout, "the local plan runs first"
