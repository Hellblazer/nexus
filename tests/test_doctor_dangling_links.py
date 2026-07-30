# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor --check-dangling-links (nexus-ysrwi, GH #1419 issue 7).

``catalog_links`` has no foreign key to ``catalog_documents``
(catalog-001-baseline.xml carries only a PK + a UNIQUE constraint), so a
link whose ``from_tumbler`` or ``to_tumbler`` resolves to no document
accumulates silently. Steve Harris's backup held 5 of 52 links pointing at
tumblers with no document anywhere in the same ``pg_dump``.

Service branch only, matching the nexus-ingey / nexus-k0luu
false-clean-diagnostics pattern already established for the aspect-queue,
trim-telemetry, and T3 legacy-metadata checks (see
test_false_clean_diagnostics_service_mode.py):

  service branch   ``HttpCatalogClient.orphaned_links()`` over HTTP; an
                   unreachable service must report UNKNOWN and exit 2, never
                   a false-clean zero.

nexus-i711w terminal deletion: TestSqliteBranchDetectsARealDanglingRow (the
sqlite ``link_audit()`` branch, 5 tests) retired with the local catalog.
"""
from __future__ import annotations

from unittest.mock import patch

import click
import httpx
import pytest
from click.testing import CliRunner

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


# --- review findings, 2026-07-25 (CRE-DAY2) ---------------------------------
#
# Both of these are the SAME defect class the four doctor checks were rewritten
# for earlier the same day: a check that appears to have run and found nothing,
# when in fact it could not run or could not read what came back.


class TestWireShapeIsNotTrusted:
    def test_a_non_dict_response_raises_the_type_doctor_handles(self) -> None:
        """A bare JSON array would make `.get()` raise AttributeError, which is
        NOT in doctor's `except (httpx.HTTPError, RuntimeError)` -- so the
        caller would crash with a raw traceback instead of the UNKNOWN/exit(2)
        contract this check exists to guarantee."""
        from unittest.mock import patch

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        client = HttpCatalogClient.__new__(HttpCatalogClient)
        with patch.object(HttpCatalogClient, "_get", return_value=[1, 2, 3]):
            with pytest.raises(RuntimeError) as exc:
                client.orphaned_links()
        assert "wire shape" in str(exc.value).lower()
        assert "list" in str(exc.value), "the error must name what actually arrived"

    def test_an_empty_response_is_still_an_empty_list_not_an_error(self) -> None:
        """Non-regression: no orphans is a legitimate, common answer."""
        from unittest.mock import patch

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        client = HttpCatalogClient.__new__(HttpCatalogClient)
        for empty in ({}, {"links": []}, None):
            with patch.object(HttpCatalogClient, "_get", return_value=empty):
                assert client.orphaned_links() == []

    @pytest.mark.parametrize("missing", ["from_tumbler", "to_tumbler", "link_type"])
    def test_a_row_missing_an_endpoint_is_named_loudly(self, missing: str) -> None:
        """Silent `.get()` defaults degraded a drifted shape to
        `[?] None --None--> None`, which reads as a clean report."""
        from nexus.commands.doctor import _require

        row = {"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}
        del row[missing]
        assert _require(row, missing) == f"<MISSING:{missing}>"

    def test_a_complete_row_renders_its_real_values(self) -> None:
        from nexus.commands.doctor import _require

        row = {"from_tumbler": "1.1.1", "to_tumbler": "1.1.2", "link_type": "cites"}
        assert _require(row, "from_tumbler") == "1.1.1"
        assert _require(row, "link_type") == "cites"
