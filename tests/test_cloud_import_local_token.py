# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup restore`` against a local engine, and the open's refusal of a model the engine cannot serve (RDR-248,
epic nexus-csayy, asks ``rdr248-local-root`` and ``rdr248-cloud-to-local``, both answered option 1 on 2026-11-14).

* A local install holds the supervisor's root token, which the import routes refuse. The client mints a short-lived
  ``default``-tenant token from it through the token route and sends that; the root token never goes to an import route.
  A token that is not the supervisor's wins untouched. Cloud is unchanged.
* A file that names an embedding model the engine will not serve is refused at the OPEN, before any part is sent, and
  the refusal names the model (the engine's rule is ``TenantImportRoutesTest``; this is the client surfacing it).

The stand-in for the engine's routes is ``tests/_fake_import_engine.py``; the real engine's token route is exercised in
``tests/test_nx_backup_cloud_roundtrip.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.backup import cloud_import as ci
from nexus.backup.cloud_export import ExportClient, ManagedCredentials, run_export
from nexus.backup.cloud_import import ImportClient, RestoreRefused, run_restore
from nexus.backup.local_token import (
    LOCAL_TENANT,
    MANUAL_STEP,
    REFRESH_MARGIN_S,
    RESTORE_TOKEN_TTL_S,
    LocalRestoreCredentials,
)
from tests._fake_export_engine import FakeEngine, Reply, TableSpec
from tests._fake_import_engine import FakeImportEngine

ROOT = "supervisor-root-token"


class Static:
    """The inner credentials: one static token, as ``ManagedCredentials`` resolves it locally."""

    def __init__(self, token: str) -> None:
        self.token = token

    def bearer(self) -> str:
        return self.token

    def rejected(self, sent: str) -> bool:
        return False


class Issuer:
    """The token route: records every call and hands out ``minted-1``, ``minted-2``, ..."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str, int, str]] = []
        self.fail = fail

    def __call__(self, base_url: str, root: str, tenant: str, ttl: int, label: str) -> str:
        self.calls.append((base_url, root, tenant, ttl, label))
        if self.fail is not None:
            raise self.fail
        return f"minted-{len(self.calls)}"


class WallClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def backup_file(tmp_path: Path) -> Path:
    rows = [[b"%d" % i, b"row-%d" % i] for i in range(5)]
    tables = [TableSpec("memory", "data", [("id", "bigint"), ("content", "text")], [rows])]
    dest = tmp_path / "in.nxtenant"
    with FakeEngine(tables) as engine:
        run_export(ExportClient(engine.base_url, _Tok(), sleep=lambda _s: None), dest)
    return dest


class _Tok:
    def bearer(self) -> str:
        return "tok"

    def rejected(self, sent: str) -> bool:
        return False


def _client(engine: FakeImportEngine, creds: LocalRestoreCredentials, *, remote: bool = False) -> ImportClient:
    return ImportClient(engine.base_url, creds, sleep=lambda _s: None, remote=remote)


def _local(engine: FakeImportEngine, held: str, issuer: Issuer, clock: WallClock | None = None) -> LocalRestoreCredentials:
    return LocalRestoreCredentials(Static(held), engine.base_url, root_token=lambda: ROOT, issue=issuer, clock=clock or WallClock())


def _bearers(engine: FakeImportEngine) -> list[str]:
    return [r.bearer for r in engine.log if r.kind != "version"]


# -- rdr248-local-root --------------------------------------------------------------------------------------------


def test_the_supervisors_root_token_is_swapped_for_a_default_tenant_token_and_never_reaches_an_import_route(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"minted-1"}) as engine:
        result = run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file, poll_interval_s=0.0)
        sent = _bearers(engine)
        base = engine.base_url
    assert result.outcome.source == "status"
    assert sent and set(sent) == {"minted-1"}, "every import request carries the minted token"
    assert ROOT not in sent
    assert issuer.calls == [(base, ROOT, LOCAL_TENANT, RESTORE_TOKEN_TTL_S, "nx backup restore")], "one mint, from the root token, for default"


def test_the_ttl_covers_the_longest_session_at_the_defaults() -> None:
    # 60 min upload + NX_IMPORT_LOAD_MAX waiting for the load slot + NX_IMPORT_LOAD_MAX loading, 3,960 s each (RDR-248)
    assert RESTORE_TOKEN_TTL_S >= 3600 + 2 * 3960 + REFRESH_MARGIN_S


def test_an_explicit_token_that_is_not_the_supervisors_wins_and_nothing_is_minted(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"my-tenant-token"}) as engine:
        run_restore(_client(engine, _local(engine, "my-tenant-token", issuer)), backup_file, poll_interval_s=0.0)
        sent = _bearers(engine)
    assert set(sent) == {"my-tenant-token"} and issuer.calls == []


def test_with_no_lease_to_compare_against_the_held_token_is_used_as_it_is(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"tok"}) as engine:
        creds = LocalRestoreCredentials(Static("tok"), engine.base_url, root_token=lambda: None, issue=issuer)
        run_restore(_client(engine, creds), backup_file, poll_interval_s=0.0)
    assert issuer.calls == []


def test_a_mint_that_fails_refuses_before_anything_is_sent_and_names_the_manual_step(backup_file: Path) -> None:
    issuer = Issuer(fail=RuntimeError("the engine answered 500"))
    with FakeImportEngine() as engine:
        with pytest.raises(RestoreRefused) as err:
            run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file)
        sent = _bearers(engine)
    text = str(err.value)
    assert "could not be minted" in text and "the engine answered 500" in text and MANUAL_STEP in text
    assert "nx service token issue --tenant default" in text and "NX_SERVICE_TOKEN" in text
    assert sent == [], "the root token was not sent to the engine as a fallback"
    assert ROOT not in text


def test_the_minted_token_is_replaced_before_it_expires_mid_restore() -> None:
    clock, issuer = WallClock(), Issuer()
    creds = LocalRestoreCredentials(Static(ROOT), "http://x", root_token=lambda: ROOT, issue=issuer, clock=clock)
    assert creds.bearer() == "minted-1" and creds.bearer() == "minted-1" and len(issuer.calls) == 1
    clock.now += RESTORE_TOKEN_TTL_S - REFRESH_MARGIN_S - 1
    assert creds.bearer() == "minted-1", "still inside its life, so not replaced"
    clock.now += 2
    assert creds.bearer() == "minted-2" and len(issuer.calls) == 2, "inside the refresh margin: a fresh one"
    assert all(call[1] == ROOT for call in issuer.calls)


def test_a_minted_token_the_engine_rejects_is_replaced_once_and_the_request_repeated(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"minted-2"}) as engine:
        result = run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file, poll_interval_s=0.0)
        opens = [r.bearer for r in engine.requests("open")]
        sent = _bearers(engine)
    assert result.outcome.source == "status"
    assert opens == ["minted-1", "minted-2"] and ROOT not in sent and len(issuer.calls) == 2


def test_a_token_rejected_again_after_the_replacement_is_a_refusal_with_the_manual_step(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"nobody"}) as engine:
        with pytest.raises(RestoreRefused, match="nx service token issue --tenant"):
            run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file)
        sent = _bearers(engine)
    assert ROOT not in sent and len(issuer.calls) == 3, "the first mint and at most two replacements"


# -- cloud is unchanged; connect() picks by the service_url -----------------------------------------------------------


def test_connect_wraps_the_credentials_for_a_local_engine_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
    monkeypatch.setenv("NX_SERVICE_PORT", "9")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    local = ci.connect()
    assert local._remote is False and isinstance(local._creds, LocalRestoreCredentials)
    monkeypatch.setenv("NX_SERVICE_URL", "https://api.example.invalid")
    cloud = ci.connect()
    assert cloud._remote is True and type(cloud._creds) is ManagedCredentials, "the cloud path keeps its own credentials"


def test_a_cloud_client_never_mints_whatever_token_it_holds(backup_file: Path) -> None:
    with FakeImportEngine(accepted={ROOT}) as engine:
        engine.script.append(lambda req: Reply(403, {"error": "no", "reason": "import_scope_forbidden"}) if req.kind == "open" else None)
        client = ImportClient(engine.base_url, Static(ROOT), sleep=lambda _s: None, remote=True)
        with pytest.raises(RestoreRefused, match="nx config set service_token"):
            run_restore(client, backup_file)


# -- rdr248-cloud-to-local: a model the engine cannot serve is refused at the open, naming it -------------------------


@pytest.mark.parametrize(
    ("reason", "error"),
    [
        ("embedding_model_not_served", "the file names the Voyage embedding model voyage-context-3, which this engine cannot serve: it has no Voyage key"),
        ("missing_embedding_model", "the target has no embedding_models row for voyage-3"),
    ],
)
def test_a_model_the_engine_will_not_serve_is_refused_at_the_open_naming_it_and_nothing_is_uploaded(
    backup_file: Path, reason: str, error: str,
) -> None:
    with FakeImportEngine() as engine:
        engine.script.append(lambda req: Reply(422, {"error": error, "reason": reason, "models": ["m"]}) if req.kind == "open" else None)
        with pytest.raises(RestoreRefused) as err:
            run_restore(ImportClient(engine.base_url, _Tok(), sleep=lambda _s: None), backup_file)
        assert [r.kind for r in engine.log if r.kind != "version"] == ["open"], "the open is the only import request"
    assert error in str(err.value) and reason in str(err.value) and "Nothing was written" in str(err.value)
