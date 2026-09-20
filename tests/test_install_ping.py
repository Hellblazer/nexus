"""nexus-h5olw — anonymous daily install ping: opt-out, throttle, payload, wire."""

from __future__ import annotations

import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus import install_ping as ip
from nexus.cli import main


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_NO_TELEMETRY", raising=False)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no per-repo .nexus.yml leaks in
    # THE DEV-CHECKOUT GUARD IS ANSWERED "no" FOR EVERY TEST USING THIS
    # FIXTURE, because the suite itself runs from a dev checkout and the guard
    # would otherwise short-circuit every env, config, throttle and wire test
    # below into "off" -- they would all pass while testing nothing they name.
    # The guard's own behaviour is tested in the dev-checkout section, where it
    # is NOT patched, and where the first assertion is that the real function
    # returns True for this very process. Patch the name on the module under
    # test, not is_dev_checkout_process itself: install_ping is what decides.
    monkeypatch.setattr(ip, "running_from_dev_checkout", lambda: False)
    return tmp_path


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[dict] = []
    status = 202

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        _Recorder.bodies.append(json.loads(self.rfile.read(n)))
        self.send_response(_Recorder.status)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch):
    _Recorder.bodies = []
    _Recorder.status = 202
    srv = HTTPServer(("127.0.0.1", 0), _Recorder)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("NX_INSTALL_PING_URL", f"http://127.0.0.1:{srv.server_port}/v1/install-ping")
    yield _Recorder
    srv.shutdown()


# ── opt-out ────────────────────────────────────────────────────────────────

def test_default_is_on(cfg: Path) -> None:
    assert ip.telemetry_enabled() is True
    assert ip.telemetry_status() == {"enabled": True, "source": "default"}


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_env_opt_out(cfg: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("NX_NO_TELEMETRY", value)
    assert ip.telemetry_enabled() is False
    assert ip.telemetry_status()["source"].startswith("env NX_NO_TELEMETRY=")


def test_env_zero_means_unset(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_NO_TELEMETRY", "0")
    assert ip.telemetry_enabled() is True


@pytest.mark.parametrize("raw", ["telemetry:\n  enabled: false\n", "telemetry:\n  enabled: 'off'\n"])
def test_config_opt_out(cfg: Path, raw: str) -> None:
    (cfg / "config.yml").write_text(raw)
    assert ip.telemetry_enabled() is False
    assert ip.telemetry_status() == {"enabled": False, "source": "config telemetry.enabled"}


def test_ping_in_background_returns_none_when_opted_out(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    called: list[bool] = []
    assert ip.ping_in_background(lambda: called.append(True)) is None
    assert called == []


# ── install id and throttle ────────────────────────────────────────────────

def test_install_id_is_uuid_and_stable(cfg: Path) -> None:
    first = ip.install_id()
    uuid.UUID(first)
    assert ip.install_id() == first
    assert (cfg / "install_id").read_text().strip() == first


def test_corrupt_install_id_is_reminted(cfg: Path) -> None:
    (cfg / "install_id").write_text("garbage\n")
    fresh = ip.install_id()
    uuid.UUID(fresh)
    assert (cfg / "install_id").read_text().strip() == fresh


def test_due_never_pinged_then_throttled_24h(cfg: Path) -> None:
    now = 1_800_000_000.0
    assert ip.due(now) is True
    ip._mark_pinged(now, None)
    assert ip.due(now + ip.PING_INTERVAL_S - 1) is False
    assert ip.due(now + ip.PING_INTERVAL_S) is True


# ── payload ────────────────────────────────────────────────────────────────

def test_payload_has_exactly_six_fields(cfg: Path) -> None:
    p = ip.build_payload()
    assert set(p) == {"install_id", "client_version", "mode", "os", "arch", "python"}
    uuid.UUID(p["install_id"])
    assert p["mode"] == "local"
    assert p["python"].count(".") == 1
    assert all(isinstance(v, str) and v for v in p.values())


def test_mode_is_cloud_when_service_url_configured(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", "https://example.invalid")
    assert ip.build_payload()["mode"] == "cloud"


# ── wire ───────────────────────────────────────────────────────────────────

def test_ping_if_due_sends_marks_and_throttles(cfg: Path, receiver: type[_Recorder]) -> None:
    now = 1_800_000_000.0
    assert ip.ping_if_due(now) is True
    assert len(receiver.bodies) == 1
    assert receiver.bodies[0]["install_id"] == ip.install_id()
    assert ip.last_ping_at() == pytest.approx(now)
    assert ip.ping_if_due(now + 60) is False
    assert len(receiver.bodies) == 1


def test_non_2xx_is_false_and_not_marked(cfg: Path, receiver: type[_Recorder]) -> None:
    receiver.status = 429
    assert ip.ping_if_due(1_800_000_000.0) is False
    assert ip.last_ping_at() is None


def test_unreachable_target_is_false_never_raises(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    monkeypatch.setenv("NX_INSTALL_PING_URL", f"http://127.0.0.1:{port}/v1/install-ping")
    assert ip.ping_if_due(1_800_000_000.0) is False
    assert ip.last_ping_at() is None


def test_ping_in_background_runs_target_on_daemon_thread(cfg: Path) -> None:
    done = threading.Event()
    t = ip.ping_in_background(done.set)
    assert t is not None and t.daemon
    assert done.wait(5)


# ── CLI ────────────────────────────────────────────────────────────────────

def test_nx_telemetry_off_status_on(cfg: Path) -> None:
    r = CliRunner()
    out = r.invoke(main, ["telemetry", "status"])
    assert out.exit_code == 0, out.output
    assert "install ping: on (default)" in out.output
    assert "last ping:    never" in out.output

    assert r.invoke(main, ["telemetry", "off"]).exit_code == 0
    assert "enabled: false" in (cfg / "config.yml").read_text()
    out = r.invoke(main, ["telemetry", "status"])
    assert "install ping: off (config telemetry.enabled)" in out.output

    assert r.invoke(main, ["telemetry", "on"]).exit_code == 0
    out = r.invoke(main, ["telemetry", "status"])
    assert "install ping: on (default)" in out.output


# ── the dev-checkout guard (nexus-6doho) ───────────────────────────────────
#
# NOT covered by the `cfg` fixture, which answers the guard "no" so the tests
# above can test what they name. These ask the real question.


def test_this_very_process_is_recognised_as_a_dev_checkout() -> None:
    """THE non-vacuity assert, and the reason the rest of this section means
    something.

    Every test below shows the ping is suppressed when the guard says "dev
    checkout". That is worth nothing unless the guard actually says so
    somewhere real -- a guard wired to a predicate that is never true in
    practice is untested code that reads as protection. The test suite runs
    out of the checkout, so the process making this assertion is itself the
    case the guard exists to catch. Same shape as
    scripts/check_release_workflow_shape.py phase (a), which asserts
    is_dev_checkout_process() is True for the process doing the asserting.

    If this fails, the guard has stopped recognising a checkout and every
    other test in this section has gone vacuous without going red.
    """
    assert ip.running_from_dev_checkout() is True


def test_a_dev_checkout_does_not_ping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured case: this box, install_id 2da3bd8a, pinging production
    daily from a checkout and counted as a user."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_NO_TELEMETRY", raising=False)
    monkeypatch.chdir(tmp_path)

    assert ip.telemetry_enabled() is False
    assert ip.telemetry_status() == {"enabled": False, "source": "dev checkout"}
    assert ip.ping_in_background() is None


def test_a_dev_checkout_sends_nothing_over_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receiver: type[_Recorder]
) -> None:
    """The guard is checked before the request, not after it. A suppression
    that still opened the connection would still be a row somewhere."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_NO_TELEMETRY", raising=False)
    monkeypatch.chdir(tmp_path)

    assert ip.ping_if_due() is False
    assert receiver.bodies == [], f"a dev checkout reached the beacon: {receiver.bodies}"
    assert not (tmp_path / ip.LAST_PING_FILENAME).exists(), (
        "a suppressed ping marked itself as sent, which would also suppress "
        "the NEXT one from a real install sharing this config dir"
    )


def test_the_guard_does_not_suppress_an_installed_copy(
    cfg: Path, receiver: type[_Recorder]
) -> None:
    """The other direction, which is the one that matters commercially: the
    guard must not turn the beacon off for real users. `cfg` answers the
    guard "no", which is what an installed copy reports."""
    assert ip.telemetry_enabled() is True
    assert ip.ping_if_due() is True
    assert len(receiver.bodies) == 1


def test_the_env_var_still_wins_inside_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env opt-out is checked FIRST, so its reported source stays the env
    var rather than becoming "dev checkout". A harness that sets the flag and
    is told something else about why it is off has been given a misleading
    answer about its own configuration."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    monkeypatch.chdir(tmp_path)

    assert ip.telemetry_status() == {
        "enabled": False, "source": f"env {ip.NO_TELEMETRY_ENV}=1",
    }


def test_an_undecidable_check_counts_as_not_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stated as a test because the direction is a judgement call and the
    wrong one is invisible: falling the other way would silently disable the
    beacon for every real user the moment that import broke, and an
    under-count is not visible in the data the way an over-count is."""
    import nexus.db.service_endpoint as se

    def boom() -> bool:
        raise RuntimeError("cannot decide")

    monkeypatch.setattr(se, "is_dev_checkout_process", boom)
    assert ip.running_from_dev_checkout() is False
