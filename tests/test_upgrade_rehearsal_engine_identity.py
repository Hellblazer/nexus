# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rehearsal proves which engine ran (nexus-jh1f2 cut 7, critique of f4a71518d).

Engine-release Step 5b.1 runs the user's-seat rehearsal on a tree that pins the new engine tag, so the
candidate's ``nx upgrade`` acquires that tag's published binary. Nothing checked that it did: a tree
still pinning the previous tag passed against the previous binary. The driver now reads the pin from
the candidate wheel, reads the serving engine from ``nx doctor``'s convergence line, fails a start
where they differ or the line is missing, and names the engine on each start's line.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parent.parent / "scripts" / "upgrade_rehearsal.py"


def _load():
    spec = importlib.util.spec_from_file_location("_upgrade_rehearsal_engine_identity", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_upgrade_rehearsal_engine_identity"] = module
    spec.loader.exec_module(module)
    return module


ur = _load()

#: The line ``nx doctor`` printed on the 2026-11-10 proof run (from 8.1.1, engine 0.1.187).
CONVERGED = "  \x1b[32m✓\x1b[0m Engine convergence: installed engine v0.1.187 matches the release dependency\n"


def _wheel(tmp_path: Path, pin_line: str | None, version: str = "9.9.9") -> Path:
    path = tmp_path / f"conexus-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("nexus/__init__.py", "")
        if pin_line is not None:
            z.writestr("nexus/engine_version.py", f'"""doc"""\n{pin_line}\nOTHER = (9, 9, 9)\n')
    return path


def test_the_pin_is_read_from_the_wheel(tmp_path: Path) -> None:
    w = _wheel(tmp_path, "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 188)")
    assert ur.engine_pin_of_wheel(w) == "0.1.188"


@pytest.mark.parametrize("pin_line", [None, "SOMETHING_ELSE = (0, 1, 188)"])
def test_a_wheel_without_a_readable_pin_is_refused(tmp_path: Path, pin_line: str | None) -> None:
    with pytest.raises(ur.RehearsalError, match="REQUIRED_ENGINE_VERSION"):
        ur.engine_pin_of_wheel(_wheel(tmp_path, pin_line))


def test_the_serving_engine_is_read_from_doctor() -> None:
    assert ur.serving_engine_from_doctor("Checking x\n" + CONVERGED) == "0.1.187"
    assert ur.serving_engine_from_doctor("  ✓ Process footprint: fine\n") is None


@pytest.mark.parametrize(
    ("doctor", "expected", "doctor_ok", "jar", "words"),
    [
        pytest.param(CONVERGED, "0.1.188", True, None, ["v0.1.187", "v0.1.188"], id="previous-tag-served"),
        pytest.param("  ✓ fine\n", "0.1.188", True, None, ["did not report the install converged", "no engine convergence row"], id="no-line"),
        pytest.param(
            "  ! Engine convergence: engine convergence pending — installed v0.1.186, release dependency v0.1.187\n",
            "0.1.187", True, None, ["did not report the install converged", "installed v0.1.186"], id="pending-row-quoted",
        ),
    ],
)
def test_a_wrong_or_unreported_engine_is_a_finding(
    doctor: str, expected: str, doctor_ok: bool, jar: str | None, words: list[str]
) -> None:
    found = ur.engine_identity_findings(doctor, expected=expected, doctor_ok=doctor_ok, engine_jar=jar)
    assert len(found) == 1 and all(w in found[0] for w in words), found


@pytest.mark.parametrize(
    ("doctor", "expected", "doctor_ok", "jar"),
    [
        pytest.param(CONVERGED, "0.1.187", True, None, id="the-pinned-engine-served"),
        pytest.param("", "0.1.188", False, None, id="doctor-failed-is-reported-elsewhere"),
        pytest.param("  ✓ fine\n", "0.1.188", True, "/x/engine.jar", id="a-jar-override-is-declared"),
        pytest.param("  ✓ fine\n", None, True, None, id="a-published-target-has-no-wheel-pin"),
    ],
)
def test_no_finding_when_the_engine_is_right_or_not_judged(
    doctor: str, expected: str | None, doctor_ok: bool, jar: str | None
) -> None:
    assert ur.engine_identity_findings(doctor, expected=expected, doctor_ok=doctor_ok, engine_jar=jar) == []


def test_the_start_line_names_the_engine() -> None:
    steps = [ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0) for n in ur.JUDGED_STEPS]
    v = ur.build_verdict(
        from_version="8.1.1",
        target="wheel conexus-9.9.9-py3-none-any.whl",
        steps=steps,
        findings=[],
        prepare={"verdict": "prepared", "collections": {"a": 60_000}},
        floor=1,
        engine="0.1.188",
    )
    assert v["engine"] == "0.1.188"
    assert "engine v0.1.188" in ur.verdict_line(v)


def test_main_passes_the_wheels_pin_to_every_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setattr(ur, "install_exit_handlers", lambda: None)
    w = _wheel(tmp_path, "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 188)")
    seen: list[str | None] = []

    def spy(version: str, cfg: object) -> dict:
        seen.append(cfg.expected_engine)  # type: ignore[attr-defined]
        steps = [ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0) for n in ur.JUDGED_STEPS]
        return ur.build_verdict(
            from_version=version, target="t", steps=steps, findings=[],
            prepare={"verdict": "prepared", "collections": {"a": 60_000}}, floor=1,
        )

    monkeypatch.setattr(ur, "rehearse", spy)
    out = tmp_path / "v.json"
    rc = ur.main(["--work", str(tmp_path / "w"), "--target-wheel", str(w),
                  "--published-versions", "7.76.0,7.99.0,8.0.0", "--json-out", str(out)])
    assert seen and set(seen) == {"0.1.188"}, seen
    assert json.loads(out.read_text())["runs"], rc


def _spy_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]) -> tuple[int, list[str | None], str]:
    monkeypatch.delenv("NEXUS_SERVICE_JAR", raising=False)
    monkeypatch.setattr(ur, "install_exit_handlers", lambda: None)
    w = _wheel(tmp_path, "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 187)")
    seen: list[str | None] = []

    def spy(version: str, cfg: object) -> dict:
        seen.append(cfg.expected_engine)  # type: ignore[attr-defined]
        steps = [ur.StepResult(name=n, status="ok", exit_code=0, wall_s=1.0) for n in ur.JUDGED_STEPS]
        return ur.build_verdict(
            from_version=version, target="t", steps=steps, findings=[],
            prepare={"verdict": "prepared", "collections": {"a": 60_000}}, floor=1,
        )

    monkeypatch.setattr(ur, "rehearse", spy)
    monkeypatch.setattr(ur, "fetch_published_versions", lambda: ["7.76.0", "7.99.0", "8.0.0"])
    out = tmp_path / "v.json"
    rc = ur.main(["--work", str(tmp_path / "w"), "--target-wheel", str(w), "--json-out", str(out), *extra])
    return rc, seen, out.read_text() if out.exists() else ""


def test_expect_engine_refuses_a_candidate_pinning_another_tag_before_any_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The unbumped-pin case: the tree still pins 0.1.187, the release is for 0.1.188.
    rc, seen, _ = _spy_main(tmp_path, monkeypatch, ["--expect-engine", "0.1.188"])
    err = capsys.readouterr().err
    assert rc == ur.EXIT_NOT_RUN and seen == [], (rc, seen)
    assert "v0.1.187" in err and "v0.1.188" in err, err


@pytest.mark.parametrize("spelled", ["0.1.187", "v0.1.187", "engine-service-v0.1.187"])
def test_expect_engine_matching_the_pin_runs_and_stays_the_release_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelled: str
) -> None:
    rc, seen, out = _spy_main(tmp_path, monkeypatch, ["--expect-engine", spelled])
    assert seen and set(seen) == {"0.1.187"}, seen
    assert rc == 0 and json.loads(out)["non_default_flags"] == [], out
