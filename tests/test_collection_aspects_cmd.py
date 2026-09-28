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


def test_bare_shows_no_opinion_for_an_untouched_row(runner: CliRunner) -> None:
    """Round-2 fix, Finding A: an untouched row is null ("no opinion"),
    never a coerced ``aspects_enabled=False`` -- the bare show falls back
    to (and names) the local config answer instead."""
    _register("docs__l46pu-cli-a")
    result = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-a"])
    assert result.exit_code == 0, result.output
    assert "no opinion" in result.output
    assert "aspects_enabled=False" not in result.output
    assert "aspects_enabled=True" not in result.output


def test_enable_then_bare_shows_true(runner: CliRunner) -> None:
    _register("docs__l46pu-cli-b")

    enabled = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-b", "--enable", "--yes"])
    assert enabled.exit_code == 0, enabled.output
    assert "aspects_enabled=True" in enabled.output

    shown = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-b"])
    assert shown.exit_code == 0, shown.output
    assert "aspects_enabled=True" in shown.output


def test_disable_flips_it_back(runner: CliRunner) -> None:
    _register("docs__l46pu-cli-c")
    assert runner.invoke(
        main, ["collection", "aspects", "docs__l46pu-cli-c", "--enable", "--yes"]
    ).exit_code == 0

    disabled = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-c", "--disable", "--yes"])
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
        main, ["collection", "aspects", "docs__l46pu-cli-d", "--enable", "--yes"]
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


def test_non_docs_collection_refuses_with_a_clear_error(runner: CliRunner) -> None:
    """Round-2 critic item 4: aspects_enabled only applies to docs__."""
    _register("code__l46pu-cli-guard", content_type="code")
    result = runner.invoke(main, ["collection", "aspects", "code__l46pu-cli-guard"])
    assert result.exit_code != 0
    assert "not a docs__ collection" in result.output

    result2 = runner.invoke(main, ["collection", "aspects", "code__l46pu-cli-guard", "--enable"])
    assert result2.exit_code != 0
    assert "not a docs__ collection" in result2.output


def test_enable_prints_the_tenant_wide_cost_notice_at_execution_time(runner: CliRunner) -> None:
    """Round-2 critic item 3: the notice fires on the actual invocation,
    not only inside --help text."""
    _register("docs__l46pu-cli-notice")
    result = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-notice", "--enable", "--yes"])
    assert result.exit_code == 0, result.output
    assert "tenant-wide" in result.output.lower()
    assert "llm call" in result.output.lower()


def test_enable_emits_a_structured_log_event(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.commands.collection as collection_mod

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        collection_mod._log, "info",
        lambda event, **kw: calls.append((event, kw)),
    )

    _register("docs__l46pu-cli-logged")
    result = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-logged", "--enable", "--yes"])
    assert result.exit_code == 0, result.output
    matches = [kw for event, kw in calls if event == "collection_aspects_enabled_set"]
    assert len(matches) == 1
    assert matches[0]["collection"] == "docs__l46pu-cli-logged"
    assert matches[0]["aspects_enabled"] is True
    assert matches[0]["tenant_wide"] is True


def test_enable_without_yes_prompts_and_declining_writes_nothing(runner: CliRunner) -> None:
    """nexus-l46pu round-2 fix round item 4 (blast-radius confirmation):
    without --yes, the write prompts; declining must leave the row
    untouched, exactly like nx collection delete's confirm gate."""
    from nexus.catalog.factory import make_catalog_reader

    _register("docs__l46pu-cli-decline")
    result = runner.invoke(
        main, ["collection", "aspects", "docs__l46pu-cli-decline", "--enable"], input="n\n",
    )
    assert result.exit_code != 0

    row = make_catalog_reader().get_collection("docs__l46pu-cli-decline")
    assert row is not None
    assert row["aspects_enabled"] is None


def test_enable_non_interactive_without_yes_refuses(runner: CliRunner) -> None:
    """A non-interactive invocation (no answer available at all) must
    refuse rather than hang or silently proceed."""
    _register("docs__l46pu-cli-noninteractive")
    result = runner.invoke(main, ["collection", "aspects", "docs__l46pu-cli-noninteractive", "--enable"])
    assert result.exit_code != 0


def test_from_config_dry_run_reports_without_writing(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.catalog.factory import make_catalog_reader

    _register("docs__l46pu-cli-fcdry")
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda *a, **k: {"aspects": {"docs_collections": ["docs__l46pu-cli-fcdry*"]}},
    )
    result = runner.invoke(main, ["collection", "aspects", "--from-config", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "docs__l46pu-cli-fcdry" in result.output
    assert "would" in result.output.lower()

    row = make_catalog_reader().get_collection("docs__l46pu-cli-fcdry")
    assert row["aspects_enabled"] is None, "dry-run must not write"


def test_from_config_syncs_every_locally_matched_collection(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.catalog.factory import make_catalog_reader

    _register("docs__l46pu-cli-fc1")
    _register("docs__l46pu-cli-fc2")
    _register("docs__l46pu-cli-other")  # deliberately not matched by the pattern below
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda *a, **k: {"aspects": {"docs_collections": ["docs__l46pu-cli-fc1*", "docs__l46pu-cli-fc2*"]}},
    )
    result = runner.invoke(main, ["collection", "aspects", "--from-config", "--yes"])
    assert result.exit_code == 0, result.output

    reader = make_catalog_reader()
    assert reader.get_collection("docs__l46pu-cli-fc1")["aspects_enabled"] is True
    assert reader.get_collection("docs__l46pu-cli-fc2")["aspects_enabled"] is True
    assert reader.get_collection("docs__l46pu-cli-other")["aspects_enabled"] is None


def test_from_config_without_yes_prompts_and_declining_writes_nothing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.catalog.factory import make_catalog_reader

    _register("docs__l46pu-cli-fc-decline")
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda *a, **k: {"aspects": {"docs_collections": ["docs__l46pu-cli-fc-decline*"]}},
    )
    result = runner.invoke(main, ["collection", "aspects", "--from-config"], input="n\n")
    assert result.exit_code != 0
    assert "will enable: docs__l46pu-cli-fc-decline" in result.output

    row = make_catalog_reader().get_collection("docs__l46pu-cli-fc-decline")
    assert row is not None
    assert row["aspects_enabled"] is None


def test_from_config_dry_run_never_prompts(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run never writes, so it must never prompt either -- a
    non-interactive dry-run (no input at all) must still succeed."""
    _register("docs__l46pu-cli-fc-dryprompt")
    monkeypatch.setattr(
        "nexus.config.load_config",
        lambda *a, **k: {"aspects": {"docs_collections": ["docs__l46pu-cli-fc-dryprompt*"]}},
    )
    result = runner.invoke(main, ["collection", "aspects", "--from-config", "--dry-run"])
    assert result.exit_code == 0, result.output


def test_from_config_combined_with_name_is_refused(runner: CliRunner) -> None:
    result = runner.invoke(main, ["collection", "aspects", "docs__x", "--from-config"])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output
