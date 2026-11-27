# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx aura check``'s dictionary checks against a real engine's registry (RDR-249 Phase 1 Step 2,
bead nexus-mdbds.10.2, step 3).

The engine is the suite's self-provisioned session engine (``tests/_engine_substrate.ensure_engine``:
a local PG and the gate jar, booted once per process). Reading the registry needs no tenant: the
template set is loaded at engine boot and is the same for every tenant, and the boot bearer reads
it. So these tests mint no tenant and ask for no ``t2_tenant`` fixture, which keeps the storage-touch
ceiling (``tests/storage_touch_ceiling.toml``) where it is; the module-scoped fixture below carries
the one engine boot. They read the wire with ``httpx`` and the CLI's own seam with the boot bearer.

Non-vacuity: every test asserts that the registry resolved the subspace to a template with
dimensions, so a registry that lists nothing fails them instead of passing them.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.auras import enumerate_auras
from nexus.auras.check import check_auras
from nexus.commands import aura_cmd
from nexus.lua_runtime import library
from nexus.tuple_templates import resolve_tuple_template

from ._support import TD1_EXAMPLE
from .test_check import HEADER


@pytest.fixture(scope="module")
def engine() -> dict[str, Any]:
    from tests._engine_substrate import ensure_engine  # noqa: PLC0415 — the engine boots on first use
    from tests.db._service_fixture import jar_freshness_skip_reason  # noqa: PLC0415

    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and not os.environ.get("NX_T2_SUBSTRATE_EXPECTED")
        and jar_freshness_skip_reason() is not None
    ):
        pytest.skip("engine substrate: service JAR not provisioned on CI (NX_T2_SUBSTRATE_EXPECTED=1 re-arms fail-loud)")
    return ensure_engine()


@pytest.fixture(scope="module")
def templates(engine: dict[str, Any]) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{engine['base_url']}/v1/tuples/registry",
        params={"doc": "1"},
        headers={"Authorization": f"Bearer {engine['bearer']}"},
        timeout=30.0,
    )
    assert resp.status_code == 200, resp.text[:300]
    got = resp.json()["templates"]
    assert got, "the engine's registry lists no templates"
    return got


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    (root / "ci_red_notice.lua").write_text(
        HEADER.format(name="ci_red_notice", args="sha:string, outcome:string"), encoding="ascii"
    )
    yield root


def _findings(shipped: Path, text: str, templates: list[dict[str, Any]], name: str = "ci_develop_red") -> set[tuple[str, str]]:
    (shipped / f"{name}.aura.toml").write_text(text, encoding="utf-8")
    [check] = check_auras(enumerate_auras(cwd=None), cwd=None, templates=templates)
    return {(f.check, f.key) for f in check.findings}


def test_the_td1_aura_passes_the_dictionary_checks_of_the_real_registry(
    shipped: Path, templates: list[dict[str, Any]]
) -> None:
    template = resolve_tuple_template(templates, "board/ci/nexus-develop--done")
    assert template is not None, "the registry has no template for a board/ci subspace"  # non-vacuity
    assert {"kind", "sha", "outcome"} <= set(template["dimensions"])
    assert _findings(shipped, TD1_EXAMPLE, templates) == set()


def test_the_shipped_ci_develop_red_passes_the_dictionary_checks_of_the_real_registry(
    shipped: Path, templates: list[dict[str, Any]]
) -> None:
    """The shipped aura and procedure as they are in the package, against the engine's own board/ci template. Its
    body sources (owner, next, refresh, reason) are known only to an engine that carries RDR-252 Step 1's doc block
    (nexus-igea9.1), so the conexus release that ships this aura pins that engine release (bead note D)."""
    template = resolve_tuple_template(templates, "board/ci/nexus-develop--owner")
    body = ((template or {}).get("doc") or {}).get("body") or {}
    assert {"owner", "next", "refresh", "reason"} <= set(body), (
        "the engine's board/ci template has no owner doc block: this test needs an engine built with nexus-igea9.1"
    )  # non-vacuity
    real = Path(__file__).resolve().parents[2] / "src" / "nexus" / "procedures"
    (shipped / "ci_red_notice.lua").write_bytes((real / "ci_red_notice.lua").read_bytes())
    text = (real / "ci_develop_red.aura.toml").read_text(encoding="utf-8")
    assert _findings(shipped, text, templates) == set()


def test_a_trigger_clear_or_args_field_the_real_template_lacks_is_refused(
    shipped: Path, templates: list[dict[str, Any]]
) -> None:
    template = resolve_tuple_template(templates, "board/ci/nexus-develop--done")
    assert template is not None and "nope" not in template["dimensions"]  # non-vacuity
    bad = (
        TD1_EXAMPLE.replace('match = { kind = "sha", outcome = "failure" }', 'match = { kind = "sha", outcome = "failure", nope = "x" }')
        .replace('match = { kind = "sha", outcome = "success" }', 'match = { kind = "sha", outcome = "success", nada = "y" }')
        .replace('outcome = "dims.outcome"', 'outcome = "dims.nothing"')
    )
    assert _findings(shipped, bad, templates) == {
        ("dictionary", "trigger.match.nope"),
        ("dictionary", "clear.match.nada"),
        ("dictionary", "args.outcome"),
    }


def test_a_body_field_is_checked_against_the_real_templates_doc(
    shipped: Path, templates: list[dict[str, Any]]
) -> None:
    template = resolve_tuple_template(templates, "board/ci/nexus-develop--done")
    assert template is not None and template["doc"]["body"], "the registry serves no doc.body"  # non-vacuity
    good = TD1_EXAMPLE.replace('outcome = "dims.outcome"', 'outcome = "body.state"')
    assert _findings(shipped, good, templates) == set()
    bad = TD1_EXAMPLE.replace('outcome = "dims.outcome"', 'outcome = "body.nothing_here"')
    assert _findings(shipped, bad, templates) == {("dictionary", "args.outcome")}


def test_a_subspace_the_real_registry_has_no_template_for_is_refused(
    shipped: Path, templates: list[dict[str, Any]]
) -> None:
    assert resolve_tuple_template(templates, "board/a/b/c/d") is None  # non-vacuity: really unresolvable
    # one segment deeper than board/ci/<topic> is not a valid trigger subspace for the parser, so
    # use a template-less but valid-looking one by dropping the board/ci template from the list
    without = [t for t in templates if t["name"] != "board/ci/<topic>"]
    assert _findings(shipped, TD1_EXAMPLE, without) == {("dictionary", "trigger.subspace")}


def test_the_cli_seam_reads_the_real_registry_with_the_boot_bearer(
    engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", engine["base_url"])
    monkeypatch.setenv("NX_SERVICE_TOKEN", engine["bearer"])
    got, why = aura_cmd._templates()
    assert why == "", why
    assert got and resolve_tuple_template(got, "board/ci/nexus-develop--done") is not None


def test_the_shipped_ci_develop_red_passes_every_check_against_the_real_registry(
    templates: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file as it ships in the package root (nexus-mdbds.12.1), not a planted copy."""
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    assert resolve_tuple_template(templates, "board/ci/nexus-develop--done") is not None  # non-vacuity
    entries = [e for e in enumerate_auras(cwd=None) if e.name == "ci_develop_red"]
    assert [e.root for e in entries] == ["shipped"], entries
    # This test is about the registry dictionary. The serving-group rule has its own coverage in test_ci_develop_red.py;
    # here a reader that returns the shipped group's record only keeps that check from being skipped.
    from nexus.auras.load_gate import GroupRecord  # noqa: PLC0415

    [check] = check_auras(
        entries, cwd=None, templates=templates,
        read_group=lambda name: GroupRecord(name, serves=("nexus-develop",)), member_groups=lambda: ["push-train"],
    )
    assert not check.findings and not check.skipped and not check.warnings, (check.findings, check.skipped, check.warnings)
