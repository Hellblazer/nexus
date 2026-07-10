# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-p9vqa: ``nx migration-audit`` CLI surface (exit codes + rendering)."""
from __future__ import annotations

import dataclasses
import json

import pytest
from click.testing import CliRunner

from nexus.commands.migration_audit_cmd import migration_audit_cmd
from nexus.migration import collision_audit
from nexus.migration.collision_audit import (
    INDETERMINATE,
    MERGED,
    CollisionAuditReport,
    SourceProbe,
    TargetFinding,
)
from nexus.migration.detection import CollectionClassification

_TARGET = "code__1-3__bge-base-en-v15-768__v1"


def _cls(collection: str) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg="local",
        model="bge-base-en-v15-768",
        dim=768,
        support="supported",
        source_count=3,
        has_data=True,
    )


def _finding(verdict: str) -> TargetFinding:
    return TargetFinding(
        target=_TARGET,
        target_exists=True,
        target_count=5,
        union_source_ids=5,
        sources=(
            SourceProbe(_cls("code__1-3__voyage-code-3__v1"), probed_ids=3, present_in_target=3),
            SourceProbe(_cls(_TARGET), probed_ids=3, present_in_target=3),
        ),
        verdict=verdict,
        detail="detail text",
    )


@pytest.fixture
def _wire(monkeypatch):
    """Wire a canned report through the command's seams; returns a setter."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )

    def _set(report: CollisionAuditReport) -> None:
        monkeypatch.setattr(
            collision_audit,
            "audit_target_collisions",
            lambda **kw: report,
        )
        # the command imports the symbol at call time from the module, so
        # patching the module attribute is sufficient — assert that stays true
        # by patching nothing else.

    return _set


def test_clean_store_exits_zero(_wire):
    _wire(CollisionAuditReport(findings=()))
    result = CliRunner().invoke(migration_audit_cmd, [])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output


def test_flagged_store_exits_one_and_names_target(_wire):
    _wire(CollisionAuditReport(findings=(_finding(MERGED),)))
    result = CliRunner().invoke(migration_audit_cmd, [])
    assert result.exit_code == 1
    assert _TARGET in result.output
    assert "merged" in result.output


def test_indeterminate_exits_two(_wire):
    _wire(
        CollisionAuditReport(findings=(_finding(MERGED), _finding(INDETERMINATE)))
    )
    result = CliRunner().invoke(migration_audit_cmd, [])
    assert result.exit_code == 2


def test_json_output_is_machine_readable(_wire):
    _wire(CollisionAuditReport(findings=(_finding(MERGED),)))
    result = CliRunner().invoke(migration_audit_cmd, ["--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["clean"] is False
    assert payload["findings"][0]["target"] == _TARGET
    assert payload["findings"][0]["verdict"] == MERGED
    assert {s["collection"] for s in payload["findings"][0]["sources"]} == {
        "code__1-3__voyage-code-3__v1",
        _TARGET,
    }


def test_json_includes_worlds(monkeypatch):
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )
    finding = dataclasses.replace(_finding(MERGED), worlds=("no-voyage-key",))
    monkeypatch.setattr(
        collision_audit,
        "audit_target_collisions",
        lambda **kw: CollisionAuditReport(findings=(finding,)),
    )
    result = CliRunner().invoke(migration_audit_cmd, ["--json"])
    payload = json.loads(result.output)
    assert payload["findings"][0]["worlds"] == ["no-voyage-key"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], None),
        (["--assume-voyage-key"], True),
        (["--assume-no-voyage-key"], False),
    ],
)
def test_world_assumption_flag_passthrough(monkeypatch, argv, expected):
    """Default = None (both worlds, the drift-proof mode, nexus-772h2);
    --assume-* narrows to one known history."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )
    captured: dict = {}

    def _audit(**kw):
        captured.update(kw)
        return CollisionAuditReport(findings=())

    monkeypatch.setattr(collision_audit, "audit_target_collisions", _audit)
    result = CliRunner().invoke(migration_audit_cmd, argv)
    assert result.exit_code == 0, result.output
    assert captured["voyage_key_present"] is expected


def test_engine_floor_failure_is_clean_cli_error(monkeypatch):
    def _boom():
        raise RuntimeError("engine below required floor")

    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", _boom
    )
    result = CliRunner().invoke(migration_audit_cmd, [])
    assert result.exit_code != 0
    assert "engine below required floor" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("argv", "expected"),
    [([], "both"), (["--legs", "local"], "local"), (["--legs", "cloud"], "cloud")],
)
def test_legs_flag_passthrough(monkeypatch, argv, expected):
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )
    captured: dict = {}

    def _audit(**kw):
        captured.update(kw)
        return CollisionAuditReport(
            findings=(), requested_legs=kw["legs"],
            audited_legs=("local", "cloud") if kw["legs"] == "both" else (kw["legs"],),
        )

    monkeypatch.setattr(collision_audit, "audit_target_collisions", _audit)
    result = CliRunner().invoke(migration_audit_cmd, argv)
    assert result.exit_code == 0, result.output
    assert captured["legs"] == expected


def test_partial_scope_is_rendered_loudly(monkeypatch):
    """A clean verdict under --legs local must carry the PARTIAL SCOPE banner
    — 'clean' may only speak for the audited legs (nexus-ovbmb)."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )
    monkeypatch.setattr(
        collision_audit,
        "audit_target_collisions",
        lambda **kw: CollisionAuditReport(
            findings=(), requested_legs="local", audited_legs=("local",)
        ),
    )
    result = CliRunner().invoke(migration_audit_cmd, ["--legs", "local"])
    assert result.exit_code == 0
    assert "PARTIAL SCOPE" in result.output
    assert "within the audited leg scope" in result.output


def test_partial_scope_in_json(monkeypatch):
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )
    monkeypatch.setattr(
        collision_audit,
        "audit_target_collisions",
        lambda **kw: CollisionAuditReport(
            findings=(), requested_legs="local", audited_legs=("local",)
        ),
    )
    result = CliRunner().invoke(migration_audit_cmd, ["--legs", "local", "--json"])
    payload = json.loads(result.output)
    assert payload["partial_scope"] is True
    assert payload["audited_legs"] == ["local"]


def test_partial_scope_banner_renders_with_findings_too(monkeypatch):
    """A narrowed audit that also FINDS collisions must still disclose the
    partial scope (the banner precedes both render branches)."""
    monkeypatch.setattr(
        "nexus.db.http_vector_client.get_http_vector_client", lambda: object()
    )
    monkeypatch.setattr(
        collision_audit,
        "audit_target_collisions",
        lambda **kw: CollisionAuditReport(
            findings=(_finding(MERGED),),
            requested_legs="local",
            audited_legs=("local",),
        ),
    )
    result = CliRunner().invoke(migration_audit_cmd, ["--legs", "local"])
    assert result.exit_code == 1
    assert "PARTIAL SCOPE" in result.output
    assert "merged" in result.output
