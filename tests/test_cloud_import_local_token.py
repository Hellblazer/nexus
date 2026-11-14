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
    """The token route: records every call and hands out ``minted-1``, ``minted-2``, ... (hash ``hash-1``, ...)."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str, int, str]] = []
        self.fail = fail
        self.revoked: list[tuple[str, str, str]] = []
        self.revoke_fails: Exception | None = None

    def __call__(self, base_url: str, root: str, tenant: str, ttl: int, label: str) -> tuple[str, str | None]:
        self.calls.append((base_url, root, tenant, ttl, label))
        if self.fail is not None:
            raise self.fail
        return f"minted-{len(self.calls)}", f"hash-{len(self.calls)}"

    def revoke(self, base_url: str, root: str, token_hash: str) -> None:
        self.revoked.append((base_url, root, token_hash))
        if self.revoke_fails is not None:
            raise self.revoke_fails


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
    return LocalRestoreCredentials(
        Static(held), engine.base_url, root_token=lambda: ROOT, issue=issuer, revoke=issuer.revoke, clock=clock or WallClock(),
    )


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


def test_a_token_the_user_set_with_no_lease_to_compare_against_is_used_as_it_is(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"tok"}) as engine:
        creds = LocalRestoreCredentials(
            Static("tok"), engine.base_url, root_token=lambda: None, issue=issuer, explicit=lambda token: token == "tok",
        )
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
    creds = LocalRestoreCredentials(Static(ROOT), "http://x", root_token=lambda: ROOT, issue=issuer, revoke=issuer.revoke, clock=clock)
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


def test_a_cloud_client_built_by_connect_never_mints_even_when_a_lease_names_its_token(
    backup_file: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through ``connect()`` with a ``service_url`` configured: if connect ever wrapped the cloud credentials, the lease below
    (which equals the held token) would make it mint from the token route, and the engine here has none."""
    with FakeImportEngine(accepted={ROOT}) as engine:
        engine.script.append(lambda req: Reply(403, {"error": "no", "reason": "import_scope_forbidden"}) if req.kind == "open" else None)
        monkeypatch.setenv("NX_SERVICE_URL", engine.base_url)
        monkeypatch.setenv("NX_SERVICE_TOKEN", ROOT)
        monkeypatch.setattr("nexus.db.service_endpoint.discover_lease", lambda: (engine.base_url, ROOT))
        client = ci.connect()
        try:
            assert client._remote is True and type(client._creds) is ManagedCredentials
            with pytest.raises(RestoreRefused, match="nx config set service_token"):
                run_restore(client, backup_file)
        finally:
            client.close_http()
        sent = _bearers(engine)
        paths = [r.path for r in engine.log]
    assert set(sent) == {ROOT}, "the cloud path sends the token it holds, as before"
    assert not any("service-tokens" in p for p in paths), "and never asks the token route for another"


# -- the lease can be unreadable mid-run; the minted token is revoked when the run ends ------------------------------


def test_a_lease_that_stops_being_readable_mid_run_does_not_let_the_root_token_reach_an_import_route(backup_file: Path) -> None:
    """The held token is the root (the user exported it), the first read sees the lease and later reads fail: the root token
    stays known as the supervisor's, so it is still swapped, never sent."""
    reads = iter([ROOT])

    def lease() -> str | None:
        return next(reads, None)   # the first read sees the lease, every later one fails

    issuer = Issuer()
    with FakeImportEngine(accepted={"minted-1"}) as engine:
        creds = LocalRestoreCredentials(
            Static(ROOT), engine.base_url, root_token=lease, issue=issuer, revoke=issuer.revoke, explicit=lambda _t: True,
        )
        result = run_restore(_client(engine, creds), backup_file, poll_interval_s=0.0)
        sent = _bearers(engine)
    assert result.outcome.source == "status"
    assert sent and ROOT not in sent and set(sent) == {"minted-1"}
    assert len(issuer.calls) == 1


def test_a_refresh_after_the_lease_is_lost_still_mints_from_the_remembered_root() -> None:
    clock, issuer = WallClock(), Issuer()
    reads = iter([ROOT])
    creds = LocalRestoreCredentials(
        Static(ROOT), "http://x", root_token=lambda: next(reads, None), issue=issuer, revoke=issuer.revoke, clock=clock,
        explicit=lambda _t: True,
    )
    assert creds.bearer() == "minted-1"
    clock.now += RESTORE_TOKEN_TTL_S - REFRESH_MARGIN_S + 1
    assert creds.bearer() == "minted-2", "the lease cannot be read, and the held token is still known as the supervisor's"
    assert [c[1] for c in issuer.calls] == [ROOT, ROOT]
    assert issuer.revoked == [("http://x", ROOT, "hash-1")], "the replaced token is revoked"


def test_a_token_that_cannot_be_told_from_the_supervisors_is_refused_not_sent(backup_file: Path) -> None:
    """No lease has ever been read and the held token is not one the user set: it may be the root token, so nothing is sent."""
    issuer = Issuer()
    with FakeImportEngine() as engine:
        creds = LocalRestoreCredentials(
            Static(ROOT), engine.base_url, root_token=lambda: None, issue=issuer, revoke=issuer.revoke, explicit=lambda _t: False,
        )
        with pytest.raises(RestoreRefused, match="could not be told from the supervisor's operator token") as err:
            run_restore(_client(engine, creds), backup_file)
        sent = _bearers(engine)
    assert sent == [] and issuer.calls == [] and "nx service token issue --tenant default" in str(err.value)
    assert ROOT not in str(err.value)


def test_a_lease_read_that_raises_is_a_missing_lease_not_a_crash(backup_file: Path) -> None:
    def broken() -> str | None:
        raise OSError("lease file is mid-write")

    with FakeImportEngine(accepted={"tok"}) as engine:
        creds = LocalRestoreCredentials(Static("tok"), engine.base_url, root_token=broken, explicit=lambda t: t == "tok")
        run_restore(_client(engine, creds), backup_file, poll_interval_s=0.0)


def test_the_minted_token_is_revoked_when_the_restore_ends_with_the_root_token_as_the_bearer(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"minted-1"}) as engine:
        run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file, poll_interval_s=0.0)
        base = engine.base_url
    assert issuer.revoked == [(base, ROOT, "hash-1")]


def test_the_minted_token_is_revoked_when_the_restore_is_refused_too(backup_file: Path) -> None:
    issuer = Issuer()
    refuse = Reply(409, {"error": "tenant default holds rows in memory", "reason": "tenant_not_empty", "table": "memory"})
    with FakeImportEngine(accepted={"minted-1"}) as engine:
        engine.script.append(lambda req: refuse if req.kind == "open" else None)
        with pytest.raises(RestoreRefused, match="not empty"):
            run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file)
    assert [r[2] for r in issuer.revoked] == ["hash-1"]


def test_an_explicit_token_is_never_revoked_because_nothing_was_minted(backup_file: Path) -> None:
    issuer = Issuer()
    with FakeImportEngine(accepted={"mine"}) as engine:
        run_restore(_client(engine, _local(engine, "mine", issuer)), backup_file, poll_interval_s=0.0)
    assert issuer.revoked == []


def test_a_revoke_that_fails_changes_neither_the_outcome_nor_a_failure_the_restore_already_had(backup_file: Path) -> None:
    issuer = Issuer()
    issuer.revoke_fails = RuntimeError("the token route is down")
    with FakeImportEngine(accepted={"minted-1"}) as engine:
        result = run_restore(_client(engine, _local(engine, ROOT, issuer)), backup_file, poll_interval_s=0.0)
    assert result.outcome.source == "status" and len(issuer.revoked) == 1, "tried once, and the restore still reports committed"
    refuse = Reply(409, {"error": "tenant default holds rows in memory", "reason": "tenant_not_empty", "table": "memory"})
    issuer2 = Issuer()
    issuer2.revoke_fails = RuntimeError("the token route is down")
    with FakeImportEngine(accepted={"minted-1"}) as engine:
        engine.script.append(lambda req: refuse if req.kind == "open" else None)
        with pytest.raises(RestoreRefused, match="not empty"):
            run_restore(_client(engine, _local(engine, ROOT, issuer2)), backup_file)


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


def test_a_restart_mid_run_revokes_with_the_supervisor_token_the_lease_names_now() -> None:
    """The engine restarted during the restore, so the supervisor's token changed: the revoke uses the new one first."""
    issuer = Issuer()
    lease = {"root": ROOT}
    creds = LocalRestoreCredentials(
        Static(ROOT), "http://x", root_token=lambda: lease["root"], issue=issuer, revoke=issuer.revoke, clock=WallClock(),
    )
    assert creds.bearer() == "minted-1"
    lease["root"] = "supervisor-root-after-restart"
    creds.release()
    assert [r[1] for r in issuer.revoked] == ["supervisor-root-after-restart"]


def test_when_the_new_supervisor_token_cannot_revoke_the_one_it_was_minted_from_is_tried() -> None:
    issuer = Issuer()
    lease = {"root": ROOT}
    tried: list[str] = []

    def revoke(base_url: str, root: str, token_hash: str) -> None:
        tried.append(root)
        if root != ROOT:
            raise RuntimeError("401")

    creds = LocalRestoreCredentials(
        Static(ROOT), "http://x", root_token=lambda: lease["root"], issue=issuer, revoke=revoke, clock=WallClock(),
    )
    creds.bearer()
    lease["root"] = "supervisor-root-after-restart"
    creds.release()
    assert tried == ["supervisor-root-after-restart", ROOT]
