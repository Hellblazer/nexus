# SPDX-License-Identifier: AGPL-3.0-or-later
"""``service_url`` rejects userinfo at set and at read (nexus-ujbf8).

``NX_SERVICE_URL`` / ``service_url`` was used verbatim and unvalidated; the
engine authenticates with a bearer token, so ``user:pass@`` is incoherent and
only leaks (a urllib ``InvalidURL`` echoed it, httpx would send Basic auth).
Every test asserts on the secret NOT appearing in any message or log event.
"""
from __future__ import annotations

from pathlib import Path

import logging
from collections.abc import Iterator

import pytest
import structlog
import yaml
from click.testing import CliRunner

from nexus.cli import main

PW = "SECRETPW-9f3a"
BAD = f"https://user:{PW}@api.example.test"
BAD_TRICKY = f"https://user:{PW}#x@api.example.test"
GOOD = "https://api.example.test"


@pytest.fixture(autouse=True)
def debug_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    for key in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_MINT_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "config.yml"


def _write_bad(cfg: Path, url: str = BAD) -> None:
    cfg.write_text(yaml.dump({"credentials": {"service_url": url, "service_token": "tok"}}))


# ── the predicate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [BAD, BAD_TRICKY, "http://tok@host:80", f"https://:{PW}@h", "https://u@h/x"])
def test_has_userinfo_true(url: str) -> None:
    from nexus._log_safe_url import has_userinfo

    assert has_userinfo(url)


@pytest.mark.parametrize("url", [GOOD, "http://127.0.0.1:9999", "https://h.example/path@x", ""])
def test_has_userinfo_false(url: str) -> None:
    from nexus._log_safe_url import has_userinfo

    assert not has_userinfo(url)


# ── set time ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [BAD, BAD_TRICKY])
def test_set_credential_rejects_and_does_not_persist(cfg: Path, url: str) -> None:
    from nexus.config import set_credential

    with pytest.raises(ValueError) as ei:
        set_credential("service_url", url)
    assert PW not in str(ei.value) and "user:" + PW not in str(ei.value)
    assert not cfg.exists()


def test_set_credential_accepts_clean_url(cfg: Path) -> None:
    from nexus.config import set_credential

    set_credential("service_url", GOOD)
    assert yaml.safe_load(cfg.read_text())["credentials"]["service_url"] == GOOD


@pytest.mark.parametrize("args", [
    ["config", "set", f"service_url={BAD}"],
    ["config", "set", "service_url", BAD],
    ["config", "set", f"credentials.service_url={BAD}"],
])
def test_cli_config_set_rejects(cfg: Path, args: list[str]) -> None:
    result = CliRunner().invoke(main, args)
    assert result.exit_code != 0
    assert PW not in result.output
    assert not cfg.exists()


def test_cli_config_init_wizard_rejects(cfg: Path) -> None:
    result = CliRunner().invoke(main, ["config", "init"], input=f"{BAD}\n\n\n\n\n")
    # click echoes the typed input into the captured output; the refusal is
    # what the program says, so judge only the text after "Error:".
    assert result.exit_code != 0 and "Error:" in result.output
    assert PW not in result.output.split("Error:", 1)[1]
    assert not cfg.exists() or PW not in cfg.read_text()


# ── read time ────────────────────────────────────────────────────────────────


def test_get_service_url_clean(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.config import get_service_url

    monkeypatch.setenv("NX_SERVICE_URL", f" {GOOD}/ ")
    assert get_service_url() == GOOD
    monkeypatch.delenv("NX_SERVICE_URL")
    assert get_service_url() == ""


def test_get_service_url_env_rejects_without_echo(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus._log_safe_url import ServiceUrlUserinfoError
    from nexus.config import get_service_url

    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    with pytest.raises(ServiceUrlUserinfoError) as ei:
        get_service_url()
    assert PW not in str(ei.value) and "NX_SERVICE_URL" in str(ei.value)


def test_get_service_url_config_rejects_without_echo(cfg: Path) -> None:
    from nexus._log_safe_url import ServiceUrlUserinfoError
    from nexus.config import get_service_url

    _write_bad(cfg)
    with pytest.raises(ServiceUrlUserinfoError) as ei:
        get_service_url()
    assert PW not in str(ei.value) and "service_url" in str(ei.value)


def _read_sites() -> list:
    from nexus.db import http_vector_client, managed_endpoint, service_endpoint

    return [
        ("resolve_service_endpoint", service_endpoint.resolve_service_endpoint),
        ("is_endpoint_env_pinned", service_endpoint.is_endpoint_env_pinned),
        ("vector _resolve_endpoint", http_vector_client._resolve_endpoint),
        ("resolve_managed_endpoint", managed_endpoint.resolve_managed_endpoint),
    ]


@pytest.mark.parametrize("source", ["env", "config"])
def test_every_read_site_fails_loud_without_echo(cfg: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    from nexus._log_safe_url import ServiceUrlUserinfoError

    if source == "env":
        monkeypatch.setenv("NX_SERVICE_URL", BAD)
    else:
        _write_bad(cfg)
    sites = [(n, f) for n, f in _read_sites() if f is not None]
    assert len(sites) >= 4, "non-vacuity: the read-site list shrank"
    for name, fn in sites:
        with pytest.raises(ServiceUrlUserinfoError) as ei:
            fn()
        assert PW not in str(ei.value), name


def test_mailbox_drain_skips_with_named_reason_not_the_secret(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook never raises into the session: its loud failure is a _Skip."""
    from nexus.hooks import mailbox_drain

    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    with pytest.raises(mailbox_drain._Skip) as ei:
        mailbox_drain._resolve_base_url()
    assert PW not in str(ei.value) and "NX_SERVICE_URL" in str(ei.value)


def test_probe_managed_service_rejects_explicit_userinfo_url() -> None:
    from nexus._log_safe_url import ServiceUrlUserinfoError
    from nexus.db.managed_endpoint import probe_managed_service

    with pytest.raises(ServiceUrlUserinfoError) as ei:
        probe_managed_service(base_url=BAD, http_get=lambda u, t: pytest.fail("must not call out"))
    assert PW not in str(ei.value)


def test_doctor_rows_report_bad_url_without_echo(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import health

    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    for fn in (health._check_managed_service_probe, health._check_static_service_token):
        rows = fn()
        text = " ".join(f"{r.label} {r.detail} {r.fix_suggestions}" for r in rows)
        assert rows and not rows[0].ok, fn.__name__
        assert PW not in text, fn.__name__


# ── log and message sites ────────────────────────────────────────────────────


def test_doctor_mint_token_host_line_omits_userinfo(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus import health
    from nexus.db import service_endpoint

    monkeypatch.setenv("NX_MINT_TOKEN", "mint-tok")
    monkeypatch.setattr(
        service_endpoint, "resolve_service_endpoint_with_evidence_gate", lambda: (f"{BAD}/x?q={PW}", "tok"),
    )

    class _Mgr:
        def has_live_token(self, *a: object) -> bool:
            return False

        def has_fresh_lease(self, *a: object) -> bool:
            return False

        def bearer_for(self, *a: object) -> str:
            return "t"

        def granted_ttl_seconds(self, *a: object) -> float:
            return 60.0

    from nexus.db import data_token
    monkeypatch.setattr(data_token, "get_data_token_manager", lambda: _Mgr())
    rows = health._check_mint_token()
    text = " ".join(f"{r.label} {r.detail} {r.fix_suggestions}" for r in rows)
    assert PW not in text and "user:" not in text


def test_rebind_warnings_omit_userinfo(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.db import service_endpoint
    from nexus.db.t2.http_token_store import HttpTokenStore

    monkeypatch.setattr(
        service_endpoint, "recover_endpoint_from_lease",
        lambda old, **k: (f"http://user:{PW}@127.0.0.1:1/p", "tok2"),
    )
    store = HttpTokenStore.__new__(HttpTokenStore)
    store._base_url = f"http://other:{PW}@127.0.0.1:2"
    store._auth_token = "t"
    store._using_data_token = False
    store._apply_data_token_preference = lambda: None  # type: ignore[method-assign]

    class _C:
        def close(self) -> None: ...

    store._client = _C()
    store._build_client = lambda: _C()  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as logs:
        assert store._rebind_from_lease() is True
    assert logs and PW not in repr(logs)


def test_token_store_auth_error_omits_userinfo(cfg: Path) -> None:
    from nexus.db.t2.http_token_store import HttpTokenStore

    store = HttpTokenStore.__new__(HttpTokenStore)
    store._base_url = f"http://user:{PW}@127.0.0.1:2"
    store._auth_token = ""
    store._using_data_token = False

    class _Resp:
        status_code = 401
        headers: dict = {}
        text = ""

        def json(self):  # noqa: ANN201
            return {}

    lines = store._auth_error_lines("/v1/x", _Resp())  # type: ignore[arg-type]
    assert PW not in "\n".join(lines)


def test_catalog_and_token_store_init_debug_omit_userinfo(cfg: Path) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    with structlog.testing.capture_logs() as logs:
        c = HttpCatalogClient(base_url=f"http://user:{PW}@127.0.0.1:2", _token="t")
        c.close()
    assert logs and PW not in repr(logs)


# ── the CLI boundary: a refusal is one actionable line, never a traceback ────


def _assert_clean_refusal(result, *, repair: str) -> None:
    from nexus._log_safe_url import ServiceUrlUserinfoError

    assert result.exit_code != 0
    assert not isinstance(result.exception, ServiceUrlUserinfoError), "raw exception reached the process boundary"
    assert "Traceback" not in result.output
    assert PW not in result.output
    assert repair in result.output, result.output


@pytest.mark.parametrize("argv", [
    ["memory", "list", "-p", "x"],
    ["search", "anything"],
    ["store", "list"],
])
def test_data_commands_refuse_a_saved_userinfo_url_cleanly(cfg: Path, argv: list[str]) -> None:
    _write_bad(cfg)
    result = CliRunner().invoke(main, argv)
    _assert_clean_refusal(result, repair="nx config set service_url")


@pytest.mark.parametrize("argv", [["memory", "list", "-p", "x"], ["search", "anything"], ["store", "list"]])
def test_data_commands_refuse_an_env_userinfo_url_cleanly(
    cfg: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str],
) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    result = CliRunner().invoke(main, argv)
    _assert_clean_refusal(result, repair="NX_SERVICE_URL")
    assert "unset NX_SERVICE_URL" in result.output


def test_service_probe_with_a_userinfo_url_refuses_cleanly(cfg: Path) -> None:
    result = CliRunner().invoke(main, ["service", "probe", "--url", BAD])
    _assert_clean_refusal(result, repair="user:password@")


def test_service_probe_with_a_saved_userinfo_url_refuses_cleanly(cfg: Path) -> None:
    _write_bad(cfg)
    result = CliRunner().invoke(main, ["service", "probe"])
    _assert_clean_refusal(result, repair="nx config set service_url")


def test_init_with_a_userinfo_url_refuses_cleanly(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", BAD)
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    monkeypatch.delenv("NX_LOCAL", raising=False)
    result = CliRunner().invoke(main, ["init"])
    _assert_clean_refusal(result, repair="NX_SERVICE_URL")
