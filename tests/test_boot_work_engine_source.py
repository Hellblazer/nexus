# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's ``/v1/status`` ``boot_work`` reaches nx upgrade, nx doctor and the service status
(nexus-mjmjl, nexus-f3yqd.3).

Before the engine binds HTTP, the supervisor's migration record says what it is doing; once it serves,
the engine's ``boot_work`` list does (engine-service 0.1.187). An engine that predates the field (no key)
or cannot be reached shows nothing, and an empty list is idle.
"""

from __future__ import annotations

from pathlib import Path

from nexus.progress import boot_work

NOW = 1_800_000_000.0
PCI = {
    "id": "pci-build", "what": "building per-collection vector indexes", "started_at": "2027-01-15T08:00:00Z",
    "done": 3, "total": 7, "unit": "indexes", "safe_to_stop": True, "on_stop": "the build resumes on the next start",
    "step": None,
}


def _fetch(body):
    return lambda **_kw: body


def test_the_engines_boot_work_is_rendered_when_no_migration_record_exists(tmp_path: Path) -> None:
    lines = boot_work.notice(tmp_path, clock=lambda: NOW, fetch=_fetch({"boot_work": [PCI]}))
    assert lines and lines[0].startswith("The engine is building per-collection vector indexes"), lines
    assert "3 of 7 indexes" in lines[0] or "3/7" in lines[0] or "3 of 7" in lines[0], lines[0]
    assert "the build resumes on the next start" in " ".join(lines)


def test_idle_an_older_engine_and_an_unreachable_one_show_nothing(tmp_path: Path) -> None:
    for body in ({"boot_work": []}, {"status": "ok"}, None):
        assert boot_work.notice(tmp_path, clock=lambda: NOW, fetch=_fetch(body)) == []


def test_the_supervisors_migration_record_comes_first(tmp_path: Path, monkeypatch) -> None:
    record = {**PCI, "id": "vectors-030-1", "what": "migrating your data (one-time)"}
    monkeypatch.setattr(boot_work, "local_entries", lambda config_dir, clock=None: [record])
    asked: list[bool] = []

    def fetch(**_kw):
        asked.append(True)
        return {"boot_work": [PCI]}

    lines = boot_work.notice(tmp_path, clock=lambda: NOW, fetch=fetch)
    assert lines[0].startswith("The engine is migrating your data") and asked == []


def test_malformed_entries_are_skipped(tmp_path: Path) -> None:
    body = {"boot_work": ["not an entry", 3, PCI]}
    lines = boot_work.notice(tmp_path, clock=lambda: NOW, fetch=_fetch(body))
    assert lines and lines[0].startswith("The engine is building")


def test_a_local_install_with_no_lease_is_not_asked(tmp_path: Path, monkeypatch) -> None:
    """No supervisor lease: the endpoint resolver (whose not-running error is a storage touch) is never called."""
    import nexus.db.http_engine_status as status  # noqa: PLC0415 — the module patched

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.db.service_endpoint.discover_lease", lambda: (None, None))
    asked: list[bool] = []
    monkeypatch.setattr(status, "fetch_engine_status", lambda **_kw: asked.append(True) or {"boot_work": [PCI]})
    assert boot_work.notice(tmp_path, clock=lambda: NOW) == [] and asked == []


def test_a_local_install_with_a_lease_is_asked(tmp_path: Path, monkeypatch) -> None:
    import nexus.db.http_engine_status as status  # noqa: PLC0415 — the module patched

    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.db.service_endpoint.discover_lease", lambda: ("http://127.0.0.1:1", "t"))
    monkeypatch.setattr(status, "fetch_engine_status", lambda **_kw: {"boot_work": [PCI]})
    assert boot_work.notice(tmp_path, clock=lambda: NOW)[0].startswith("The engine is building")


def test_a_long_step_repeats_the_engines_work_at_its_heartbeats() -> None:
    """The critic's gap: one notice before a 20-minute wait froze at its first numbers. A step reporter with a
    work source prints the engine's lines at a heartbeat, at most every work_every_s."""
    from nexus.progress.step_report import StepReporter, no_ticker  # noqa: PLC0415 — the reporter under test

    now = [0.0]
    out: list[str] = []
    calls = [0]

    def work() -> list[str]:
        calls[0] += 1
        return [f"The engine is building per-collection vector indexes: {calls[0]} of 7 indexes."]

    reporter = StepReporter(
        1, echo=out.append, tty=False, clock=lambda: now[0], ticker=no_ticker, heartbeat_s=5.0,
        work=work, work_every_s=30.0,
    )
    with reporter.step("Run the upgrade ladder's data steps") as step:
        for _ in range(13):  # 65 s of heartbeats every 5 s
            now[0] += 5.0
            step.tick()
    engine = [line for line in out if line.startswith("The engine is building")]
    assert engine == [
        "The engine is building per-collection vector indexes: 1 of 7 indexes.",
        "The engine is building per-collection vector indexes: 2 of 7 indexes.",
        "The engine is building per-collection vector indexes: 3 of 7 indexes.",
    ], out
