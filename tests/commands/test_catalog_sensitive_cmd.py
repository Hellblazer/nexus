# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 3d (nexus-rfvyy.6.11): ``nx catalog sensitive`` and ``nx catalog list --sensitive``.

Driven through ``CliRunner`` against the engine substrate (the gate-jar engine carries the catalog
sensitivity changeset and route). Each gate is pinned by a test that goes red when the gate is deleted:
the usage check, the capability probe, the reason requirement, the zero-match refusal and the list filter.

Decisions recorded in the module under test: ``--path`` may be narrowed by ``--collection`` and ``--owner``;
a tumbler is a dotted tumbler only (never a title); the probe runs before any target is resolved.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main
from tests._chunk_seed import _psql_superuser, _pg_state, ambient_tenant

pytestmark = pytest.mark.t2_tenant

_COLL_A = "knowledge__sensA__bge-base-en-v15-768__v1"
_COLL_B = "knowledge__sensB__bge-base-en-v15-768__v1"


@pytest.fixture(autouse=True)
def _env(monkeypatch, t2_service_env):
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")


@pytest.fixture
def docs(t2_service_env):
    """Four documents: a1 and a2 in collection A (paths under docs/ and notes/), b1 and b2 in B."""
    with HttpCatalogClient() as cat:
        owner = cat.register_owner("sens-owner", owner_type="curator", tumbler_prefix="71.1")

        def reg(title: str, path: str, coll: str) -> str:
            return str(cat.register(
                owner, title, content_type="knowledge", file_path=path, physical_collection=coll,
                source_uri=f"file:///sens/{path}",
            ))

        out = {
            "a1": reg("a1", "docs/a1.md", _COLL_A),
            "a2": reg("a2", "notes/a2.md", _COLL_A),
            "b1": reg("b1", "docs/b1.md", _COLL_B),
            "b2": reg("b2", "notes/b2.md", _COLL_B),
        }
    return out


def _flags(*tumblers: str) -> dict[str, bool]:
    with HttpCatalogClient() as cat:
        return {t: cat.resolve(t, follow_alias=False).sensitive for t in tumblers}


def _sensitive_set(docs: dict[str, str]) -> set[str]:
    flags = _flags(*docs.values())
    by_tumbler = {v: k for k, v in docs.items()}
    return {by_tumbler[t] for t, on in flags.items() if on}


def _json(result):
    """The JSON document in the output; the suite's production-write guard may log a warning line before it."""
    lines = result.output.splitlines()
    start = next(i for i, line in enumerate(lines) if line[:1] in ("{", "["))
    return json.loads("\n".join(lines[start:]))


def _run(*args: str):
    return CliRunner().invoke(main, ["catalog", *args])


def _audit(tumbler: str) -> list[tuple[str, str, str]]:
    rows = _psql_superuser(
        _pg_state(),
        "SELECT old, new, reason FROM nexus.catalog_sensitivity_changes "
        f"WHERE tenant_id = '{ambient_tenant()}' AND tumbler = '{tumbler}' ORDER BY id",
    )
    return [tuple(line.split("|", 2)) for line in rows.splitlines() if line.strip()]


@pytest.fixture
def no_route_call(monkeypatch):
    """Fail the test if the set_sensitivity route is reached at all."""
    calls: list = []

    def boom(self, *a, **kw):
        calls.append((a, kw))
        raise AssertionError("set_sensitivity must not be reached")

    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", boom)
    return calls


# ── set ─────────────────────────────────────────────────────────────────────


def test_a_tumbler_is_flagged_and_nothing_else(docs) -> None:
    result = _run("sensitive", docs["a1"])
    assert result.exit_code == 0, result.output
    assert _sensitive_set(docs) == {"a1"}
    assert docs["a1"] in result.output


def test_setting_an_already_flagged_document_reports_zero_changed(docs) -> None:
    assert _run("sensitive", docs["a1"]).exit_code == 0
    result = _run("sensitive", docs["a1"], "--json")
    assert result.exit_code == 0, result.output
    payload = _json(result)
    assert payload["matched"] == 1 and payload["changed"] == 0


def test_collection_flags_every_document_in_it_and_none_outside(docs) -> None:
    result = _run("sensitive", "--collection", _COLL_A)
    assert result.exit_code == 0, result.output
    assert _sensitive_set(docs) == {"a1", "a2"}


def test_path_flags_only_the_matching_documents(docs) -> None:
    result = _run("sensitive", "--path", "docs/*")
    assert result.exit_code == 0, result.output
    assert _sensitive_set(docs) == {"a1", "b1"}


def test_path_narrowed_by_collection(docs) -> None:
    result = _run("sensitive", "--path", "docs/*", "--collection", _COLL_B)
    assert result.exit_code == 0, result.output
    assert _sensitive_set(docs) == {"b1"}


def test_a_target_that_matches_nothing_is_an_error_not_a_silent_success(docs, no_route_call) -> None:
    for args in (["--path", "nothing/*"], ["--collection", "knowledge__nope__bge-base-en-v15-768__v1"], ["9.9.9"]):
        result = _run("sensitive", *args)
        assert result.exit_code != 0, args
    assert no_route_call == []
    assert _sensitive_set(docs) == set()


def test_a_collection_beyond_one_request_is_sent_in_batches(docs, monkeypatch) -> None:
    """The route caps a request at 1000 tumblers; the verb batches, and the counts add up."""
    from nexus.commands.catalog_cmds import sensitivity

    monkeypatch.setattr(sensitivity, "_BATCH", 1)
    sent: list[int] = []
    real = HttpCatalogClient.set_sensitivity

    def spy(self, tumblers, sensitive, **kw):
        sent.append(len(tumblers))
        return real(self, tumblers, sensitive, **kw)

    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", spy)
    result = _run("sensitive", "--collection", _COLL_A, "--json")
    assert result.exit_code == 0, result.output
    assert sent == [1, 1]
    payload = _json(result)
    assert payload["matched"] == 2 and payload["changed"] == 2
    assert _sensitive_set(docs) == {"a1", "a2"}


# ── usage ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["1.1.1", "--collection", "c"],
        ["1.1.1", "--path", "*"],
        ["1.1.1", "--clear"],
        ["--owner", "1.1"],
        ["1.1.1", "--owner", "1.1"],
        ["--collection", "c", "--owner", "1.1"],
    ],
)
def test_a_malformed_invocation_is_a_usage_error_before_any_network_call(args, no_route_call, monkeypatch) -> None:
    def no_probe(self):
        raise AssertionError("no network call before the usage check")

    monkeypatch.setattr(HttpCatalogClient, "_version_flags", no_probe)
    result = _run("sensitive", *args)
    assert result.exit_code != 0
    assert result.exit_code == 2, result.output
    assert no_route_call == []


# ── clear ───────────────────────────────────────────────────────────────────


def test_clear_without_a_reason_refuses_and_leaves_the_flag(docs, monkeypatch) -> None:
    assert _run("sensitive", docs["a1"]).exit_code == 0
    calls: list = []
    monkeypatch.setattr(HttpCatalogClient, "set_sensitivity", lambda self, *a, **kw: calls.append(a))
    result = _run("sensitive", docs["a1"], "--clear")
    assert result.exit_code == 2, result.output
    assert "--reason" in result.output
    assert calls == [], "the usage check comes before the route call"
    assert _sensitive_set(docs) == {"a1"}
    assert len(_audit(docs["a1"])) == 1, "no audit row was written by the refused clear"


def test_clear_with_a_blank_reason_refuses(docs) -> None:
    assert _run("sensitive", docs["a1"]).exit_code == 0
    result = _run("sensitive", docs["a1"], "--clear", "--reason", "   ")
    assert result.exit_code == 2, result.output
    assert _sensitive_set(docs) == {"a1"}


def test_clear_with_a_reason_clears_and_the_audit_row_records_it(docs) -> None:
    assert _run("sensitive", docs["a1"]).exit_code == 0
    result = _run("sensitive", docs["a1"], "--clear", "--reason", "published, reviewed 2026-11")
    assert result.exit_code == 0, result.output
    assert _sensitive_set(docs) == set()
    assert _audit(docs["a1"]) == [("f", "t", "set"), ("t", "f", "published, reviewed 2026-11")]


# ── the capability probe ────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["absent", "probe_raises"])
def test_an_engine_without_the_capability_is_refused_before_the_route_call(docs, no_route_call, monkeypatch, mode) -> None:
    if mode == "absent":
        monkeypatch.setattr(HttpCatalogClient, "_version_flags", lambda self: {"release_version": "0.0.1"})
    else:
        def raising(self):
            raise OSError("engine unreachable")

        monkeypatch.setattr(HttpCatalogClient, "_version_flags", raising)
    result = _run("sensitive", docs["a1"])
    assert result.exit_code != 0
    assert "sensitiv" in result.output.lower()
    assert no_route_call == []
    assert _sensitive_set(docs) == set()


def test_the_probe_runs_before_a_target_is_resolved(docs, monkeypatch) -> None:
    """An unsupported engine is told so even for a target that matches nothing, so the refusal is the capability's."""
    monkeypatch.setattr(HttpCatalogClient, "_version_flags", lambda self: {"release_version": "0.0.1"})
    result = _run("sensitive", "--path", "nothing/*")
    assert result.exit_code != 0
    assert "does not support document sensitivity" in result.output


# ── nx catalog list --sensitive ─────────────────────────────────────────────


def test_list_sensitive_lists_exactly_the_flagged_documents(docs) -> None:
    assert _run("sensitive", docs["a2"]).exit_code == 0
    assert _run("sensitive", docs["b1"]).exit_code == 0

    text = _run("list", "--sensitive")
    assert text.exit_code == 0, text.output
    listed = {line.split()[0] for line in text.output.splitlines() if line.strip() and line[0].isdigit()}
    assert listed == {docs["a2"], docs["b1"]}

    as_json = _run("list", "--sensitive", "--json")
    assert as_json.exit_code == 0, as_json.output
    rows = _json(as_json)
    assert {r["tumbler"] for r in rows} == {docs["a2"], docs["b1"]}
    assert all(r["sensitive"] is True for r in rows)


def test_list_sensitive_pages_after_filtering(docs) -> None:
    for key in ("a1", "a2", "b1"):
        assert _run("sensitive", docs[key]).exit_code == 0
    first = _json(_run("list", "--sensitive", "--json", "-n", "2"))
    second = _json(_run("list", "--sensitive", "--json", "-n", "2", "--offset", "2"))
    assert len(first) == 2 and len(second) == 1, "paging applies to the sensitive subset, not the whole catalog"
    assert {r["tumbler"] for r in first + second} == {docs["a1"], docs["a2"], docs["b1"]}


def test_list_without_the_flag_still_lists_everything(docs) -> None:
    assert _run("sensitive", docs["a1"]).exit_code == 0
    rows = _json(_run("list", "--json"))
    assert {docs[k] for k in docs} <= {r["tumbler"] for r in rows}
