# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 Phase 2 (Gap 3) — unified config-first auth across storage commands.

Today the six T2/catalog ``nx storage migrate <store>`` subcommands gate on
``NX_SERVICE_TOKEN`` env ONLY (``token = os.environ.get("NX_SERVICE_TOKEN")``),
so a user who ran ``nx config set service_token`` but exported nothing is
refused — even though the no-arg ``Http*Store()`` would resolve the token
config-first via ``resolve_service_endpoint``. And ``nx storage migrate all``
has no ``--service-url`` at all. ``nx storage migrate vectors`` is the correct
exemplar (config-first, ``--service-url`` sets ``NX_SERVICE_URL``, no env-only
gate).

This is the Phase-2 auth entry test (bead nexus-t9rmg.7), failing-first:
- the memory subcommand must resolve the token from config.yml with no env;
- every migrate subcommand (incl ``all``) must expose ``--service-url``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import storage_cmd
from nexus.config import set_config_value


def _seed_empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "t2.db"
    sqlite3.connect(str(db)).close()
    return db


def test_migrate_memory_resolves_token_from_config_not_env_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With service_token in config.yml and NO env token, `migrate memory` must
    NOT fail with 'NX_SERVICE_TOKEN is required' — it resolves config-first."""
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    set_config_value("credentials.service_url", "http://127.0.0.1:1")
    set_config_value("credentials.service_token", "config-bearer")

    # Stub the store + ETL so no network happens; the point under test is that
    # the auth gate no longer blocks a config-only user before this point.
    constructed: list[object] = []

    class _FakeStore:
        def __init__(self, *a: object, **k: object) -> None:
            constructed.append(self)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeStore
    )
    monkeypatch.setattr(
        "nexus.db.t2.memory_etl.migrate_memory_rows",
        lambda *a, **k: {"read": 0, "written": 0},
    )

    db = _seed_empty_db(tmp_path)
    result = CliRunner().invoke(storage_cmd.migrate_memory_cmd, ["--db", str(db)])

    assert "NX_SERVICE_TOKEN is required" not in result.output
    assert result.exit_code == 0, result.output
    assert constructed, "store was never constructed — auth gate blocked the run"


def test_migrate_memory_fails_loud_when_no_token_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the env-only gate must NOT make an unconfigured run silent: with
    no token in env, config, or lease, the command fails with a clear,
    actionable error (the new fail-loud path, replacing the old ClickException)."""
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    # No config token; neutralise any live supervisor lease on the dev box so
    # the "nothing configured" case is deterministic.
    monkeypatch.setattr(
        "nexus.db.service_endpoint.discover_lease", lambda: (None, None)
    )

    db = _seed_empty_db(tmp_path)
    result = CliRunner().invoke(
        storage_cmd.migrate_memory_cmd,
        ["--db", str(db), "--service-url", "http://127.0.0.1:1"],
    )

    assert result.exit_code != 0
    assert "service_token" in result.output or "NX_SERVICE_TOKEN" in result.output


def test_service_url_override_restores_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1 fix: the scoped override must NOT leak — it restores the prior
    NX_SERVICE_URL (or unsets it) on exit, so a --service-url on `migrate all`
    cannot bleed into a sibling command sharing the process."""
    # Prior unset → unset after.
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    with storage_cmd._service_url_override("http://override"):
        assert os.environ["NX_SERVICE_URL"] == "http://override"
    assert "NX_SERVICE_URL" not in os.environ

    # Prior set → restored to the prior value after.
    monkeypatch.setenv("NX_SERVICE_URL", "http://prior")
    with storage_cmd._service_url_override("http://override"):
        assert os.environ["NX_SERVICE_URL"] == "http://override"
    assert os.environ["NX_SERVICE_URL"] == "http://prior"

    # No override → no mutation.
    with storage_cmd._service_url_override(None):
        assert os.environ["NX_SERVICE_URL"] == "http://prior"
    assert os.environ["NX_SERVICE_URL"] == "http://prior"


def test_every_migrate_subcommand_exposes_service_url() -> None:
    """`--service-url` must be available on EVERY migrate subcommand, including
    `all` (today it has none). Excludes non-network helper subcommands."""
    migrate_group = storage_cmd.migrate_group
    network_subcommands = {
        "memory", "plans", "telemetry", "taxonomy", "chash", "catalog",
        "vectors", "all",
    }
    missing: list[str] = []
    for name, cmd in migrate_group.commands.items():
        if name not in network_subcommands:
            continue
        opt_names = {opt for p in cmd.params for opt in getattr(p, "opts", [])}
        if "--service-url" not in opt_names:
            missing.append(name)
    assert missing == [], f"missing --service-url: {missing}"
