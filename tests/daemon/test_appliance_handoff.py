# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ijue9.29 (RDR-218 P2.1a): the appliance handoff file and its credential.

Record: T2 nexus/rdr-218-appliance-endpoint-handoff-decision section 3,
amended for Sam's O1: the file carries a mint-locked credential, never a
bearer, issued once and persisted beside pg_credentials.

The committed fixture tests/fixtures/appliance/endpoint.schema1.json is the
contract both halves test against: this producer must emit exactly those bytes,
and ijue9.6's reader must accept them, so the two cannot drift.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from nexus._winsec import owner_only_problem
from nexus.daemon.appliance_handoff import (
    ABSENT,
    CHECK_RETRY_S,
    DEAD_MARKER_FILENAME,
    EXPIRED,
    HANDOFF_FILE_ENV,
    HANDOFF_SCHEMA,
    LIVE,
    MINT_CREDENTIAL_FILENAME,
    REVOKED,
    ROOT_TENANT,
    VERIFY_INTERVAL_S,
    ApplianceProjector,
    CredentialDeadError,
    classify_credential,
    ensure_mint_credential,
    handoff_bytes,
    write_handoff_if_changed,
)
from nexus.daemon.storage_service_daemon import APPLIANCE_DEFAULT_PORT
from nexus.db.t2.http_token_store import DEFAULT_TENANT

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "appliance" / "endpoint.schema1.json"
FIXTURE_TOKEN = "nx-fixture-mint-locked-0000"
FIXTURE_TENANT = "default"


def test_the_bytes_are_the_shared_schema_1_fixture() -> None:
    assert handoff_bytes(29517, FIXTURE_TOKEN, FIXTURE_TENANT) == FIXTURE.read_bytes()


def test_the_fixture_is_the_recorded_format() -> None:
    raw = FIXTURE.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 without BOM"
    assert raw.endswith(b"}\n") and not raw.endswith(b"\n\n"), "one trailing newline"
    obj = json.loads(raw.decode("utf-8"))
    assert list(obj) == sorted(obj), "keys sorted"
    assert obj == {
        "host": "127.0.0.1", "mint_tenant": FIXTURE_TENANT, "mint_token": FIXTURE_TOKEN,
        "port": 29517, "schema": HANDOFF_SCHEMA,
    }
    assert "token" not in obj, "the file never carries a bearer (O1)"
    assert HANDOFF_SCHEMA == 1 and HANDOFF_FILE_ENV == "NX_APPLIANCE_HANDOFF_FILE"


@pytest.mark.parametrize("port,token,tenant", [
    (0, "t", "d"), (65536, "t", "d"), (True, "t", "d"),
    (29517, "", "d"), (29517, 5, "d"), (29517, "t", ""), (29517, "t", None),
])
def test_bytes_refuse_values_a_reader_would_refuse(port, token, tenant) -> None:
    with pytest.raises(ValueError):
        handoff_bytes(port, token, tenant)


def _assert_private(path) -> None:
    """Owner-only: mode 0o600 on POSIX; on Windows, where ``st_mode`` reads 0o666 for
    every file, the DACL (``nexus._winsec.owner_only_problem``)."""
    if sys.platform == "win32":
        assert owner_only_problem(path, path.stat().st_mode) is None
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_first_write_is_exact_bytes_mode_0600_and_leaves_no_temp(tmp_path) -> None:
    target = tmp_path / "endpoint.json"
    assert write_handoff_if_changed(target, 29517, FIXTURE_TOKEN, FIXTURE_TENANT) is True
    assert target.read_bytes() == FIXTURE.read_bytes()
    _assert_private(target)
    assert [p.name for p in tmp_path.iterdir()] == ["endpoint.json"]


def test_same_values_leave_bytes_and_mtime_untouched(tmp_path) -> None:
    target = tmp_path / "endpoint.json"
    write_handoff_if_changed(target, 29517, FIXTURE_TOKEN, FIXTURE_TENANT)
    past = 1_600_000_000
    os.utime(target, (past, past))
    ino = target.stat().st_ino
    assert write_handoff_if_changed(target, 29517, FIXTURE_TOKEN, FIXTURE_TENANT) is False
    st = target.stat()
    assert st.st_mtime == past and st.st_ino == ino, "no rewrite when the bytes are equal"


def test_a_changed_port_rewrites_the_file(tmp_path) -> None:
    target = tmp_path / "endpoint.json"
    write_handoff_if_changed(target, 29517, FIXTURE_TOKEN, FIXTURE_TENANT)
    assert write_handoff_if_changed(target, 29518, FIXTURE_TOKEN, FIXTURE_TENANT) is True
    assert json.loads(target.read_text())["port"] == 29518
    _assert_private(target)


def test_a_stale_temp_from_a_crash_is_removed(tmp_path) -> None:
    target = tmp_path / "endpoint.json"
    stale = tmp_path / f".endpoint.json.tmp.{os.getpid()}"
    stale.write_text("torn")
    write_handoff_if_changed(target, 29517, FIXTURE_TOKEN, FIXTURE_TENANT)
    assert not stale.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["endpoint.json"]


def test_a_missing_directory_raises_oserror_for_the_caller_to_log(tmp_path) -> None:
    with pytest.raises(OSError):
        write_handoff_if_changed(tmp_path / "absent" / "endpoint.json", 29517, FIXTURE_TOKEN, FIXTURE_TENANT)


# ── the credential: issued once, persisted, never re-issued ──────────────────


class _Issuer:
    def __init__(self, response: dict | Exception) -> None:
        self.calls = 0
        self._response = response

    def __call__(self) -> dict:
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_absent_credential_is_issued_once_and_persisted_0600(tmp_path) -> None:
    issuer = _Issuer({"token": "mint-abc", "tenant": "default", "token_hash": "h"})
    assert ensure_mint_credential(tmp_path, issuer) == ("mint-abc", "default")
    path = tmp_path / MINT_CREDENTIAL_FILENAME
    _assert_private(path)
    assert ensure_mint_credential(tmp_path, issuer) == ("mint-abc", "default")
    assert issuer.calls == 1, "a persisted credential is never re-issued"


def test_a_persisted_credential_survives_a_new_process(tmp_path) -> None:
    ensure_mint_credential(tmp_path, _Issuer({"token": "mint-abc", "tenant": "default"}))
    never = _Issuer(AssertionError("must not issue"))
    assert ensure_mint_credential(tmp_path, never) == ("mint-abc", "default")
    assert never.calls == 0


def test_an_issue_failure_persists_nothing(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        ensure_mint_credential(tmp_path, _Issuer(RuntimeError("engine said 403")))
    assert list(tmp_path.iterdir()) == []


def test_a_corrupt_credential_file_refuses_rather_than_reissuing(tmp_path) -> None:
    (tmp_path / MINT_CREDENTIAL_FILENAME).write_text("garbage\n")
    issuer = _Issuer({"token": "mint-new", "tenant": "default"})
    with pytest.raises(ValueError, match="refusing to issue a second credential"):
        ensure_mint_credential(tmp_path, issuer)
    assert issuer.calls == 0


# ── review round: the projector's credential policy (coordinator 2026-09-29) ──



class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def _rec(self, level: str, event: str, **kw) -> None:
        self.events.append((level, event, kw))

    def info(self, event: str, **kw) -> None:
        self._rec("info", event, **kw)

    def warning(self, event: str, **kw) -> None:
        self._rec("warning", event, **kw)

    def error(self, event: str, **kw) -> None:
        self._rec("error", event, **kw)

    def names(self) -> list[str]:
        return [e for _l, e, _k in self.events]


class _Engine:
    """In-memory stand-in for the token surface: issue, revoke, list."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.issued = 0
        self.revoked: list[str] = []
        self.list_error: Exception | None = None

    def issue(self) -> dict:
        self.issued += 1
        token = f"mint-{self.issued}"
        h = hashlib.sha256(token.encode()).hexdigest()
        self.rows[h] = {"token_hash": h, "scope": "mint-locked", "revoked_at": None, "expires_at": None}
        return {"token": token, "tenant": "default", "token_hash": h, "scope": "mint-locked"}

    def revoke(self, h: str) -> None:
        self.revoked.append(h)
        if h in self.rows:
            self.rows[h]["revoked_at"] = "now"

    def list_rows(self) -> list[dict]:
        if self.list_error:
            raise self.list_error
        return list(self.rows.values())


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _projector(tmp_path: Path, engine: _Engine, clock: _Clock, log: _Log) -> ApplianceProjector:
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "appliance").mkdir(exist_ok=True)
    return ApplianceProjector(
        config_dir=tmp_path / "cfg", target=tmp_path / "appliance" / "endpoint.json",
        issue=engine.issue, revoke=engine.revoke, list_rows=engine.list_rows,
        log=log, clock=clock,
    )


def _file_token(tmp_path: Path) -> str:
    return json.loads((tmp_path / "appliance" / "endpoint.json").read_text())["mint_token"]


def test_classify_each_state() -> None:
    tok = "mint-x"
    h = hashlib.sha256(tok.encode()).hexdigest()
    row = {"token_hash": h, "scope": "mint-locked", "revoked_at": None, "expires_at": None}
    assert classify_credential([row], tok) == LIVE
    assert classify_credential([], tok) == ABSENT
    assert classify_credential([{**row, "revoked_at": "t"}], tok) == REVOKED
    assert classify_credential([{**row, "scope": "tenant"}], tok) == REVOKED
    assert classify_credential([{**row, "expires_at": "t"}], tok) == EXPIRED


def test_live_credential_is_projected_and_checked_on_a_slow_cadence(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    calls = {"n": 0}
    real = engine.list_rows

    def counting() -> list[dict]:
        calls["n"] += 1
        return real()

    p._list_rows = counting
    assert p.project(29517) is True
    assert _file_token(tmp_path) == "mint-1"
    assert "appliance_mint_credential_issued" in log.names()
    assert not any("mint-1" in str(k) for _l, _e, k in log.events), "the credential never reaches a log"
    for _ in range(5):
        p.project(29517)
    assert calls["n"] == 1, "not a list call per heartbeat"
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    assert calls["n"] == 2 and engine.issued == 1


def test_absent_is_reissued_once_then_treated_as_dead(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    engine.rows.clear()                      # the database was re-provisioned
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    assert engine.issued == 2 and _file_token(tmp_path) == "mint-2"
    assert "appliance_mint_credential_reissued" in log.names()
    engine.rows.clear()                      # and again: something keeps deleting it
    clock.t += VERIFY_INTERVAL_S
    assert p.project(29517) is False
    assert engine.issued == 2, "no loop: at most one re-issue per lifetime"
    assert not (tmp_path / "appliance" / "endpoint.json").exists()
    assert "appliance_mint_credential_keeps_disappearing" in log.names()
    assert p.dead == ABSENT


@pytest.mark.parametrize("field,state", [("revoked_at", REVOKED), ("expires_at", EXPIRED)])
def test_revoked_or_rotated_is_sticky_and_removes_the_file(tmp_path, field, state) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    for row in engine.rows.values():
        row[field] = "2026-09-29T00:00:00Z"
    clock.t += VERIFY_INTERVAL_S
    assert p.project(29517) is False
    assert not (tmp_path / "appliance" / "endpoint.json").exists()
    dead = [k for _l, e, k in log.events if e == "appliance_mint_credential_dead"]
    assert dead and dead[0]["state"] == state and "remove" in dead[0]["remedy"]
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    assert engine.issued == 1, "a revoked credential is never re-issued automatically"
    assert not (tmp_path / "appliance" / "endpoint.json").exists()
    # A new supervisor lifetime sees the same verdict: still no re-issue.
    p2 = _projector(tmp_path, engine, clock, log)
    assert p2.project(29517) is False and engine.issued == 1


def test_a_failed_check_keeps_the_file_and_is_not_absent(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    engine.rows.clear()
    engine.list_error = RuntimeError("engine 503")
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    assert engine.issued == 1, "a transient list failure must not trigger a re-issue"
    assert _file_token(tmp_path) == "mint-1"
    assert "appliance_mint_credential_check_failed" in log.names()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod 0o500 makes a directory read-only only on POSIX; on Windows a directory's mode bits "
    "are ignored and files are still created in it, so the failure this test needs cannot be made that way",
)
def test_an_issued_but_unpersisted_credential_is_revoked(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    (tmp_path / "cfg").chmod(0o500)          # persisting the credential fails
    try:
        with pytest.raises(OSError):
            p.project(29517)
    finally:
        (tmp_path / "cfg").chmod(0o700)
    assert engine.issued == 1 and len(engine.revoked) == 1
    assert "appliance_mint_credential_orphan_revoked" in log.names()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod 0o500 makes a directory read-only only on POSIX; on Windows a directory's mode bits "
    "are ignored and files are still created in it, so the failure this test needs cannot be made that way",
)
def test_an_orphan_that_cannot_be_revoked_is_logged_as_an_error(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)

    def fail(_h: str) -> None:
        raise RuntimeError("engine down")

    p._revoke = fail
    (tmp_path / "cfg").chmod(0o500)
    try:
        with pytest.raises(OSError):
            p.project(29517)
    finally:
        (tmp_path / "cfg").chmod(0o700)
    orphaned = [(lvl, k) for lvl, e, k in log.events if e == "appliance_mint_credential_orphaned"]
    assert orphaned and orphaned[0][0] == "error" and orphaned[0][1]["token_hash"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the loose mode is made with chmod 0o644, which Windows ignores; the Windows tightening is the "
    "ACL (nexus._winsec.ensure_owner_only), covered by tests/test_winsec.py",
)
def test_loose_modes_are_tightened(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    handoff = tmp_path / "appliance" / "endpoint.json"
    cred = tmp_path / "cfg" / MINT_CREDENTIAL_FILENAME
    handoff.chmod(0o644)
    cred.chmod(0o644)
    p.project(29517)
    _assert_private(handoff)
    _assert_private(cred)


def test_the_fixture_port_is_the_appliance_default() -> None:
    assert json.loads(FIXTURE.read_text())["port"] == APPLIANCE_DEFAULT_PORT


def test_root_tenant_is_the_clients_default_tenant() -> None:
    assert ROOT_TENANT == DEFAULT_TENANT


def test_dead_is_durable_across_a_db_restore_and_a_new_lifetime(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    for row in engine.rows.values():
        row["revoked_at"] = "t"
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    assert (tmp_path / "cfg" / DEAD_MARKER_FILENAME).exists()
    assert not (tmp_path / "cfg" / MINT_CREDENTIAL_FILENAME).exists()
    engine.rows.clear()                      # a restore that lost the revoked row
    p2 = _projector(tmp_path, engine, clock, log)
    assert p2.project(29517) is False
    assert engine.issued == 1, "a restore must not resurrect a revoked credential"
    with pytest.raises(CredentialDeadError):
        ensure_mint_credential(tmp_path / "cfg", engine.issue)


def test_removing_the_dead_marker_is_the_remedy(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    for row in engine.rows.values():
        row["revoked_at"] = "t"
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    (tmp_path / "cfg" / DEAD_MARKER_FILENAME).unlink()
    p2 = _projector(tmp_path, engine, clock, log)       # the unit restarts
    assert p2.project(29517) is True
    assert engine.issued == 2 and _file_token(tmp_path) == "mint-2"


def test_other_live_credentials_under_the_label_are_reported(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    engine.rows["stray"] = {"token_hash": "stray", "label": "appliance-windows-client",
                            "scope": "mint-locked", "revoked_at": None, "expires_at": None}
    clock.t += VERIFY_INTERVAL_S
    p.project(29517)
    extra = [k for _l, e, k in log.events if e == "appliance_mint_credential_extra_live"]
    assert extra and extra[0]["token_hashes"] == ["stray"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod 0o500 makes a directory read-only only on POSIX; on Windows a directory's mode bits "
    "are ignored and files are still created in it, so the failure this test needs cannot be made that way",
)
def test_a_marker_that_cannot_be_written_still_takes_the_handoff_down(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    for row in engine.rows.values():
        row["revoked_at"] = "t"
    clock.t += VERIFY_INTERVAL_S
    (tmp_path / "cfg").chmod(0o500)          # the marker cannot be written
    try:
        assert p.project(29517) is False
        assert not (tmp_path / "appliance" / "endpoint.json").exists(), "never advertise a dead credential"
        assert "appliance_mint_credential_dead" in log.names(), "the remedy is logged even so"
        assert "appliance_mint_credential_dead_unrecorded" in log.names()
        assert not (tmp_path / "cfg" / DEAD_MARKER_FILENAME).exists()
    finally:
        (tmp_path / "cfg").chmod(0o700)
    p.project(29517)                         # the marker is retried once writable
    assert (tmp_path / "cfg" / DEAD_MARKER_FILENAME).exists()
    assert engine.issued == 1


def test_a_failed_check_is_retried_after_a_minute_not_every_heartbeat(tmp_path) -> None:
    engine, clock, log = _Engine(), _Clock(), _Log()
    p = _projector(tmp_path, engine, clock, log)
    p.project(29517)
    engine.list_error = RuntimeError("engine 503")
    clock.t += VERIFY_INTERVAL_S
    for _ in range(10):
        p.project(29517)
        clock.t += 1
    failed = [e for e in log.names() if e == "appliance_mint_credential_check_failed"]
    assert len(failed) == 1, "one failed check, not one per heartbeat"
    clock.t += CHECK_RETRY_S
    p.project(29517)
    assert log.names().count("appliance_mint_credential_check_failed") == 2
