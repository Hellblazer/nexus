# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-nx3l5 shape (c): the ``deployed-engine-version`` tracker is written
from conexus's STEP-6 gate report, never from a typed ``--gate`` value.

Fixture reports mirror the real schema-3 files in the conexus checkout
(``deploy/gate-report-<compactUTC>-v011.json``): the gated engine version is
nested at ``sections.preconditions.version_visibility.observed.release_version``
(read from the live edge during the run), ``identity.jar_version`` is the
control-plane jar and must never be keyed on, ``overall.advisories`` is always a
list, and ``run_timestamp`` is the authoritative tz-aware stamp.

The two real 0.1.88 reports of 2026-08-29 — ``024127Z`` (red) then ``030235Z``
(green) — are the shape every selection rule here is written against.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nexus import deploy_tracker as dt

_T0 = datetime(2026, 8, 29, 2, 44, 44, 84484, tzinfo=UTC)


def _doc(
    *,
    version: str = "0.1.88",
    stamp: datetime | str = _T0,
    passed: bool = True,
    schema: Any = 3,
    advisories: Any = (),
    failures: tuple[str, ...] = (),
    jar_version: str = "1.0-SNAPSHOT",
    with_version: bool = True,
) -> dict[str, Any]:
    observed: dict[str, Any] = {
        "app_version": "1.0-SNAPSHOT",
        "embedding_mode": "voyage",
        "embedding_models": ["voyage-3", "voyage-code-3", "voyage-context-3"],
    }
    if with_version:
        observed["release_version"] = version
    overall: dict[str, Any] = {
        "pass": passed,
        "failures": list(failures),
        "exit_code": 0 if passed else 1,
    }
    if advisories is not None:
        overall["advisories"] = list(advisories)
    return {
        "schema_version": schema,
        "run_timestamp": stamp if isinstance(stamp, str) else stamp.isoformat(),
        "base_url": "https://api.conexus-nexus.com",
        "tenant": "gate-xr789",
        "fixture_sha256": "0" * 64,
        "identity": {
            "jar_version": jar_version,
            "jar_sha256": "f" * 64,
            "jar_build_date": "2026-06-11T12:41:31.768775+00:00",
            "embedding_probe": {},
            "per_collection_embedding_model": {},
        },
        "sections": {
            "preconditions": {
                "version_visibility": {"observed": observed, "pass": True, "violations": []},
            },
            "parity": {},
            "latency": {},
            "recall_ac3": {},
        },
        "overall": overall,
    }


def _name(stamp: datetime) -> str:
    return f"gate-report-{stamp.strftime('%Y%m%dT%H%M%SZ')}-v011.json"


def _write(directory: Path, doc: dict[str, Any], name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(doc["run_timestamp"]) if isinstance(doc["run_timestamp"], str) and doc["run_timestamp"] else _T0
    path = directory / (name or _name(stamp))
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _select(directory: Path, version: str = "0.1.88") -> dt.GateReport:
    return dt.select_authoritative_report(dt.discover_gate_reports(directory), version)


# ── selection rules ──────────────────────────────────────────────────────────


def test_latest_green_wins_over_an_earlier_red_for_the_same_version(tmp_path: Path) -> None:
    """The real 2026-08-29 shape: 024127Z red, 030235Z green, same version."""
    red = _write(tmp_path, _doc(passed=False, failures=("latency /v1/vectors/search: median_p95=2654.1ms > trailing bound 1983.4ms",)))
    green = _write(tmp_path, _doc(stamp=_T0 + timedelta(minutes=20)))

    report = _select(tmp_path)

    assert report.path == green
    assert report.passed is True
    dt.require_green(report)
    assert dt.gate_provenance(report) == f"PASSED {green.name} (advisories: 0)"
    assert red.exists()  # nothing touched


def test_an_earlier_green_never_vouches_over_a_later_red(tmp_path: Path) -> None:
    _write(tmp_path, _doc(passed=True))
    red = _write(tmp_path, _doc(stamp=_T0 + timedelta(minutes=20), passed=False, failures=("regression",)))

    report = _select(tmp_path)

    assert report.path == red
    with pytest.raises(dt.GateReportRed) as excinfo:
        dt.require_green(report)
    assert red.name in str(excinfo.value)
    assert "regression" in str(excinfo.value)


def test_version_filter_keys_on_the_nested_live_read_never_jar_version(tmp_path: Path) -> None:
    # Every report claims jar_version 0.1.99 (the control-plane jar). The
    # LATER report gated 0.1.87; the earlier one gated 0.1.88.
    wanted = _write(tmp_path, _doc(version="0.1.88", jar_version="0.1.99"))
    _write(tmp_path, _doc(version="0.1.87", jar_version="0.1.99", stamp=_T0 + timedelta(hours=1)))

    assert _select(tmp_path, "0.1.88").path == wanted
    with pytest.raises(dt.NoGateReportForVersion):
        _select(tmp_path, "0.1.99")


def test_authority_sorts_on_run_timestamp_not_the_filename(tmp_path: Path) -> None:
    # Filename says "later", field says "earlier" — and vice versa.
    later_field = _write(tmp_path, _doc(stamp=_T0 + timedelta(minutes=30), passed=True), name=_name(_T0))
    _write(tmp_path, _doc(stamp=_T0, passed=False, failures=("x",)), name=_name(_T0 + timedelta(minutes=30)))

    assert _select(tmp_path).path == later_field


def test_schema_drift_on_the_authoritative_report_refuses(tmp_path: Path) -> None:
    """A newer report on an unknown schema must not be skipped in favour of an
    older schema-3 green — that would let the older run vouch for the newer."""
    _write(tmp_path, _doc(passed=True))
    _write(tmp_path, _doc(stamp=_T0 + timedelta(minutes=5), schema=4))

    with pytest.raises(dt.GateReportSchemaError) as excinfo:
        _select(tmp_path)
    assert "schema_version 4" in str(excinfo.value)


def test_missing_directory_is_a_named_refusal(tmp_path: Path) -> None:
    with pytest.raises(dt.GateReportDirectoryError) as excinfo:
        dt.discover_gate_reports(tmp_path / "nope")
    msg = str(excinfo.value)
    assert "gitignored" in msg
    assert dt.GATE_REPORT_DIR_ENV in msg


def test_no_report_for_the_live_version_names_what_was_scanned(tmp_path: Path) -> None:
    # A schema-1 file with no nested version (45 of the 97 real files look like
    # this), plus a report for another version.
    _write(tmp_path, _doc(schema=1, with_version=False), name="gate-report-20260627-v011.json")
    _write(tmp_path, _doc(version="0.1.87"))

    with pytest.raises(dt.NoGateReportForVersion) as excinfo:
        _select(tmp_path, "0.1.88")
    msg = str(excinfo.value)
    assert "2 report(s) scanned" in msg
    assert "1 could not name a version/timestamp" in msg
    assert "0.1.87" in msg


def test_advisories_are_read_on_green_and_carried_into_the_provenance(tmp_path: Path) -> None:
    green = _write(
        tmp_path,
        _doc(advisories=("latency /v1/vectors/search drifting toward bound", {"kind": "bloat", "table": "chunks"})),
    )

    report = _select(tmp_path)

    assert report.path == green
    assert len(report.advisories) == 2
    assert dt.gate_provenance(report) == f"PASSED {green.name} (advisories: 2)"
    assert dt.format_advisory(report.advisories[1]) == '{"kind": "bloat", "table": "chunks"}'


def test_a_green_report_without_an_advisories_list_is_schema_drift(tmp_path: Path) -> None:
    path = _write(tmp_path, _doc(advisories=None))
    with pytest.raises(dt.GateReportSchemaError) as excinfo:
        dt.load_gate_report(path)
    assert "advisories" in str(excinfo.value)


def test_naive_timestamp_cannot_be_a_candidate(tmp_path: Path) -> None:
    _write(tmp_path, _doc(stamp="2026-08-29T03:04:39"), name="gate-report-20260829T030439Z-v011.json")
    with pytest.raises(dt.NoGateReportForVersion) as excinfo:
        _select(tmp_path)
    assert "1 could not name a version/timestamp" in str(excinfo.value)


def test_unparseable_file_is_skipped_not_fatal_when_another_report_serves(tmp_path: Path) -> None:
    (tmp_path / "gate-report-20260101T000000Z-v011.json").write_text("{not json", encoding="utf-8")
    green = _write(tmp_path, _doc())
    assert _select(tmp_path).path == green


# ── the writer, end to end with the live probe patched ───────────────────────


class _FakeMemory:
    puts: list[dict[str, Any]] = []

    def put(self, **kwargs: Any) -> int:
        _FakeMemory.puts.append(kwargs)
        return 1


class _FakeHandle:
    memory = _FakeMemory()

    def __enter__(self) -> _FakeHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def _fake_t2(monkeypatch: pytest.MonkeyPatch) -> type[_FakeMemory]:
    from nexus.commands import _helpers

    _FakeMemory.puts = []
    monkeypatch.setattr(_helpers, "t2_handle", lambda: _FakeHandle())
    return _FakeMemory


def _patch_live(monkeypatch: pytest.MonkeyPatch, release_version: str) -> None:
    from nexus.db import managed_endpoint as me

    caps = me.ManagedCapabilities(
        base_url="https://api.conexus-nexus.com",
        app_version="1.0-SNAPSHOT",
        release_version=release_version,
        embedding_mode="voyage",
        embedding_models=["voyage-context-3"],
        schema_latest_id=None,
        schema_changeset_count=None,
    )
    monkeypatch.setattr(me, "probe_managed_service", lambda **kw: caps)


def test_record_from_dir_writes_the_tracker_through_the_single_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_t2: type[_FakeMemory]
) -> None:
    _patch_live(monkeypatch, "0.1.88")
    _write(tmp_path, _doc(passed=False, failures=("bloat",)))
    green = _write(tmp_path, _doc(stamp=_T0 + timedelta(minutes=20)))

    result = dt.record_deploy_from_gate_report(
        report_dir=tmp_path, url="https://api.conexus-nexus.com", commit="2ca52773f", expected_version="0.1.88",
    )

    assert result.report.path == green
    assert len(_fake_t2.puts) == 1
    put = _fake_t2.puts[0]
    assert put["project"] == "nexus" and put["title"] == "deployed-engine-version"
    assert put["ttl"] is None
    assert put["content"].startswith("engine-service-v0.1.88 @ 2ca52773f; recorded ")
    assert f"gate PASSED {green.name} (advisories: 0)" in put["content"]
    assert put["content"].endswith("verified live at https://api.conexus-nexus.com/version")
    assert result.content == put["content"]


def test_record_refuses_when_the_latest_report_is_red_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_t2: type[_FakeMemory]
) -> None:
    _patch_live(monkeypatch, "0.1.88")
    _write(tmp_path, _doc(passed=True))
    _write(tmp_path, _doc(stamp=_T0 + timedelta(minutes=20), passed=False, failures=("regression",)))

    with pytest.raises(dt.GateReportRed):
        dt.record_deploy_from_gate_report(report_dir=tmp_path, url="https://api.conexus-nexus.com")
    assert _fake_t2.puts == []


def test_record_refuses_when_no_report_gated_the_live_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_t2: type[_FakeMemory]
) -> None:
    _patch_live(monkeypatch, "0.1.89")
    _write(tmp_path, _doc(version="0.1.88"))

    with pytest.raises(dt.NoGateReportForVersion):
        dt.record_deploy_from_gate_report(report_dir=tmp_path, url="https://api.conexus-nexus.com")
    assert _fake_t2.puts == []


def test_explicit_report_must_have_gated_the_live_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_t2: type[_FakeMemory]
) -> None:
    _patch_live(monkeypatch, "0.1.88")
    other = _write(tmp_path, _doc(version="0.1.87"))

    with pytest.raises(dt.GateReportVersionMismatch):
        dt.record_deploy_from_gate_report(report_path=other, url="https://api.conexus-nexus.com")
    assert _fake_t2.puts == []


def test_operator_named_tag_must_match_the_live_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_t2: type[_FakeMemory]
) -> None:
    _patch_live(monkeypatch, "0.1.88")
    _write(tmp_path, _doc())

    with pytest.raises(dt.LiveVersionMismatch):
        dt.record_deploy_from_gate_report(
            report_dir=tmp_path, url="https://api.conexus-nexus.com", expected_version="0.1.89",
        )
    assert _fake_t2.puts == []


def test_commit_resolver_is_called_with_the_live_version_after_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fake_t2: type[_FakeMemory]
) -> None:
    _patch_live(monkeypatch, "0.1.88")
    _write(tmp_path, _doc())
    seen: list[str] = []

    def _resolve(live: str) -> str:
        seen.append(live)
        return "sha-for-" + live

    result = dt.record_deploy_from_gate_report(
        report_dir=tmp_path, url="https://api.conexus-nexus.com", commit="ignored", commit_resolver=_resolve,
    )

    assert seen == ["0.1.88"]
    assert result.content.startswith("engine-service-v0.1.88 @ sha-for-0.1.88; recorded ")


def test_exactly_one_report_source_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        dt.record_deploy_from_gate_report(url="https://api.conexus-nexus.com")
    with pytest.raises(ValueError):
        dt.record_deploy_from_gate_report(report_dir=tmp_path, report_path=tmp_path / "x.json", url="https://x")


def test_env_dir_is_read_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dt.GATE_REPORT_DIR_ENV, raising=False)
    assert dt.gate_report_dir_from_env() is None
    monkeypatch.setenv(dt.GATE_REPORT_DIR_ENV, "  ")
    assert dt.gate_report_dir_from_env() is None
    monkeypatch.setenv(dt.GATE_REPORT_DIR_ENV, "/some/where/deploy")
    assert dt.gate_report_dir_from_env() == Path("/some/where/deploy")
