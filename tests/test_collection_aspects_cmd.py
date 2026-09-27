# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR bead nexus-l46pu (follow-up to nexus-kk4ut): ``nx collection aspects``.

Round-trips against the REAL engine substrate (``_pin_t2_substrate``'s
autouse per-test tenant — no catalog mocking here, unlike
``tests/test_collection_cmd.py``'s T3-mocked suite): the CLI verb reads
and writes the engine's ``catalog_collections.aspects_enabled`` column via
``make_catalog_reader``/``make_catalog_writer``, and this exercises the
full path — CLI -> HttpCatalogClient -> the real Java service -> Postgres
-> back.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _register(name: str, *, content_type: str = "docs") -> None:
    from nexus.catalog.factory import make_catalog_writer

    # embedding_model deliberately omitted: register_collection derives it
    # (collection_registration_kwargs) from the tenant's own embedding
    # profile, which this test's substrate seeds for local-mode content
    # types -- an explicit mismatched value 422s (RDR-204 profile conflict).
    make_catalog_writer().register_collection(
        name, content_type=content_type, owner_id="l46pu-cli",
    )


def test_bare_shows_the_column_default(runner: CliRunner) -> None:
    _register("docs__l46pu-cli-a")
    result = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-a"])
    assert result.exit_code == 0, result.output
    assert "aspects_enabled=False" in result.output


def test_enable_then_bare_shows_true(runner: CliRunner) -> None:
    _register("docs__l46pu-cli-b")

    enabled = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-b", "--enable"])
    assert enabled.exit_code == 0, enabled.output
    assert "aspects_enabled=True" in enabled.output

    shown = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-b"])
    assert shown.exit_code == 0, shown.output
    assert "aspects_enabled=True" in shown.output


def test_disable_flips_it_back(runner: CliRunner) -> None:
    _register("docs__l46pu-cli-c")
    assert runner.invoke(
        main, ["collection", "aspects", "docs__l46pu-cli-c", "--enable"]
    ).exit_code == 0

    disabled = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-c", "--disable"])
    assert disabled.exit_code == 0, disabled.output
    assert "aspects_enabled=False" in disabled.output

    shown = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-c"])
    assert "aspects_enabled=False" in shown.output


def test_the_engine_row_is_the_one_actually_changed(runner: CliRunner) -> None:
    """Non-vacuity: read the row directly through the client, not just the
    CLI's own echo, so a CLI that printed the right thing without writing
    anything cannot pass this."""
    from nexus.catalog.factory import make_catalog_reader

    _register("docs__l46pu-cli-d")
    assert runner.invoke(
        main, ["collection", "aspects", "docs__l46pu-cli-d", "--enable"]
    ).exit_code == 0

    row = make_catalog_reader().get_collection("docs__l46pu-cli-d")
    assert row is not None
    assert row["aspects_enabled"] is True


def test_unregistered_collection_fails_loud(runner: CliRunner) -> None:
    result = runner.invoke(main, ["collection", "aspects", "docs__l46pu-never-registered"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_set_on_unregistered_collection_fails_loud(runner: CliRunner) -> None:
    result = runner.invoke(
        main, ["collection", "aspects", "docs__l46pu-never-registered-2", "--enable"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output
