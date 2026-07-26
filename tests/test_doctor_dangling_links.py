# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor --check-dangling-links (nexus-ysrwi, GH #1419 issue 7).

``catalog_links`` has no foreign key to ``catalog_documents``
(catalog-001-baseline.xml carries only a PK + a UNIQUE constraint), so a
link whose ``from_tumbler`` or ``to_tumbler`` resolves to no document
accumulates silently. Steve Harris's backup held 5 of 52 links pointing at
tumblers with no document anywhere in the same ``pg_dump``.

Two branches, matching the nexus-ingey / nexus-k0luu false-clean-diagnostics
pattern already established for the aspect-queue, trim-telemetry, and T3
legacy-metadata checks (see test_false_clean_diagnostics_service_mode.py):

  sqlite branch    a REAL ``Catalog`` + real SQLite ``link_audit()`` query
                   against a genuinely dangling row (``link()`` then
                   ``delete_document()`` — mirrors
                   tests/test_catalog_links.py::TestDangling.test_audit_orphaned).
                   Not mocked: the query result is the query's own answer.
  service branch   ``HttpCatalogClient.orphaned_links()`` over HTTP; an
                   unreachable service must report UNKNOWN and exit 2, never
                   a false-clean zero.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog


# ── sqlite branch: a genuinely dangling row, not a mocked answer ────────────


def _seed_dangling_catalog(tmp_path: Path) -> Catalog:
    """Real Catalog + real SQLite: link a -> b, then hard-delete b.

    The resulting row is picked up by ``Catalog.link_audit()``'s own
    ``NOT EXISTS`` SQL (catalog_links.py), not by a stubbed return value.
    """
    d = tmp_path / "catalog"
    d.mkdir()
    cat = Catalog(d, d / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="deadbeef")
    a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
    b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
    cat.link(a, b, "cites", created_by="user")
    cat.delete_document(b)
    return cat


@pytest.fixture()
def dangling_catalog(tmp_path: Path) -> Catalog:
    return _seed_dangling_catalog(tmp_path)


class TestSqliteBranchDetectsARealDanglingRow:
    def test_check_reports_the_dangling_link(
        self, dangling_catalog: Catalog, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda **kw: dangling_catalog,
        )
        runner = CliRunner()
        with runner.isolation() as (out, _err, _):
            doctor_mod._run_check_dangling_links()
            printed = out.getvalue().decode()

        assert "1 link(s)" in printed, printed
        assert "sqlite backend" in printed

    def test_clean_catalog_reports_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        d = tmp_path / "catalog"
        d.mkdir()
        cat = Catalog(d, d / ".catalog.db")
        owner = cat.register_owner("nexus", "repo", repo_hash="deadbeef")
        a = cat.register(owner, "a.py", content_type="code", file_path="a.py")
        b = cat.register(owner, "b.py", content_type="code", file_path="b.py")
        cat.link(a, b, "cites", created_by="user")

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda **kw: cat,
        )
        runner = CliRunner()
        with runner.isolation() as (out, _err, _):
            doctor_mod._run_check_dangling_links()
            printed = out.getvalue().decode()

        assert "0 found" in printed, printed
        assert "clean" in printed

    def test_strict_exits_nonzero_on_a_real_dangling_row(
        self, dangling_catalog: Catalog, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda **kw: dangling_catalog,
        )
        with pytest.raises(SystemExit) as exc:
            doctor_mod._run_check_dangling_links(strict=True)
        assert exc.value.code == 1

    def test_default_is_warn_only_not_strict(
        self, dangling_catalog: Catalog, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without --strict-dangling-links a dangling row must be reported,
        not turned into a process exit — matches --check-t3-legacy-metadata's
        default warn-only behavior."""
        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader",
            lambda **kw: dangling_catalog,
        )
        # Must not raise.
        doctor_mod._run_check_dangling_links(strict=False)

    def test_catalog_not_initialized_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda **kw: None,
        )
        runner = CliRunner()
        with runner.isolation() as (out, _err, _):
            doctor_mod._run_check_dangling_links()
            printed = out.getvalue().decode()
        assert "not initialized" in printed


# ── service branch ───────────────────────────────────────────────────────────


@pytest.fixture()
def service_mode(monkeypatch: pytest.MonkeyPatch):
    """Point catalog reads at the service backend for this test, and reset
    the module-global shared HttpCatalogClient (nexus-5en9j) before AND
    after so this test's mocked client never leaks into a sibling test —
    mirrors TestFactorySeam._reset_shared_service_catalog_client in
    tests/catalog/test_http_catalog_client.py."""
    from nexus.catalog.factory import reset_shared_service_catalog_client_for_tests

    monkeypatch.setenv("NX_STORAGE_BACKEND_CATALOG", "service")
    reset_shared_service_catalog_client_for_tests()
    yield
    reset_shared_service_catalog_client_for_tests()


class TestServiceBranchRoutesToTheEngine:
    def test_reports_dangling_links_from_the_service(
        self, service_mode: None,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        sample = [
            {
                "id": 1, "from_tumbler": "1.1.5", "to_tumbler": "1.1.999",
                "link_type": "implements", "created_by": "user", "side": "to",
            },
        ]
        with patch(
            "nexus.catalog.http_catalog_client.HttpCatalogClient"
        ) as client:
            client.return_value.orphaned_links.return_value = sample
            runner = CliRunner()
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_check_dangling_links()
                printed = out.getvalue().decode()

        assert "1 link(s)" in printed, printed
        assert "service backend" in printed
        assert "1.1.999" in printed

    def test_zero_dangling_links_reports_clean(
        self, service_mode: None,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        with patch(
            "nexus.catalog.http_catalog_client.HttpCatalogClient"
        ) as client:
            client.return_value.orphaned_links.return_value = []
            runner = CliRunner()
            with runner.isolation() as (out, _err, _):
                doctor_mod._run_check_dangling_links()
                printed = out.getvalue().decode()

        assert "0 found" in printed, printed
        assert "clean" in printed

    def test_unreachable_service_reports_UNKNOWN_and_exits_2(
        self, service_mode: None,
    ) -> None:
        """The trap when adding a service branch: falling back to a clean
        zero on a transport error would reproduce the exact false-clean
        defect nexus-ingey / nexus-k0luu removed elsewhere."""
        from nexus.commands import doctor as doctor_mod

        with patch(
            "nexus.catalog.http_catalog_client.HttpCatalogClient"
        ) as client:
            client.side_effect = httpx.ConnectError("refused")
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                with pytest.raises(click.exceptions.Exit) as exc:
                    doctor_mod._run_check_dangling_links()
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exc.value.exit_code == 2
        assert "UNKNOWN" in printed, printed
        assert "0 found" not in printed
        assert "link(s)" not in printed

    def test_unresolvable_endpoint_also_reports_UNKNOWN(
        self, service_mode: None,
    ) -> None:
        """Endpoint-resolution failure raises ServiceEndpointUnresolvableError
        (a RuntimeError, NOT an httpx.HTTPError) — catching only httpx
        errors let this escape as a traceback at the sibling checks before
        the review-2026-07-25 fix; this check must not repeat that gap."""
        from nexus.commands import doctor as doctor_mod
        from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

        with patch(
            "nexus.catalog.http_catalog_client.HttpCatalogClient"
        ) as client:
            client.side_effect = ServiceEndpointUnresolvableError(
                "no lease, no token"
            )
            runner = CliRunner()
            with runner.isolation() as (out, err, _):
                with pytest.raises(click.exceptions.Exit) as exc:
                    doctor_mod._run_check_dangling_links()
                printed = out.getvalue().decode() + err.getvalue().decode()

        assert exc.value.exit_code == 2
        assert "UNKNOWN" in printed, printed

    def test_strict_exits_nonzero_when_links_found(
        self, service_mode: None,
    ) -> None:
        from nexus.commands import doctor as doctor_mod

        sample = [
            {
                "id": 1, "from_tumbler": "1.1.5", "to_tumbler": "1.1.999",
                "link_type": "implements", "created_by": "user", "side": "to",
            },
        ]
        with patch(
            "nexus.catalog.http_catalog_client.HttpCatalogClient"
        ) as client:
            client.return_value.orphaned_links.return_value = sample
            with pytest.raises(SystemExit) as exc:
                doctor_mod._run_check_dangling_links(strict=True)
        assert exc.value.code == 1
