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

import json
import os
import stat
from pathlib import Path

import pytest

from nexus.daemon.appliance_handoff import (
    HANDOFF_FILE_ENV,
    HANDOFF_SCHEMA,
    MINT_CREDENTIAL_FILENAME,
    ensure_mint_credential,
    handoff_bytes,
    write_handoff_if_changed,
)

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


def test_first_write_is_exact_bytes_mode_0600_and_leaves_no_temp(tmp_path) -> None:
    target = tmp_path / "endpoint.json"
    assert write_handoff_if_changed(target, 29517, FIXTURE_TOKEN, FIXTURE_TENANT) is True
    assert target.read_bytes() == FIXTURE.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
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
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


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
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
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
