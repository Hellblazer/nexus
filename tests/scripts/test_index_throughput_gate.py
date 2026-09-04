"""tests/e2e/migration-rehearsal/lib/index_throughput.sh (nexus-98zsp).

The release gates' indexing wall-clock floor. Exercised through bash so the
function the shakeout and the sandbox shakedown source is the one under
test, never a Python look-alike.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2] / "tests/e2e/migration-rehearsal/lib/index_throughput.sh"


def _log(path: Path, chunks: list[int]) -> Path:
    lines = [f"  [{i}/{len(chunks)}] f{i}.py — {c} chunks  (1.0s)" for i, c in enumerate(chunks, 1)]
    lines.append("  [9/9] skipped.py — skipped  (0.0s)")
    lines.append("  [eta] 9/292 files · 1,053 chunks · 20.0s/file avg · ~94 min remaining")
    lines.append("  [post] Staleness caches built — code: 0 docs (0.0s) 999 chunks reported by nothing")
    path.write_text("\n".join(lines) + "\n")
    return path


def _gate(label: str, log: Path, elapsed: int, baseline: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; throughput_gate "$@"', "_", label, str(log), str(elapsed), str(baseline)],
        capture_output=True, text=True, timeout=60, env={"PATH": "/usr/sbin:/usr/bin:/bin", **(env or {})},
    )
    return proc.returncode, proc.stdout


def test_lib_exists_and_parses() -> None:
    assert _LIB.is_file()
    assert (_LIB.parent / "index-throughput-baselines.tsv").is_file()
    subprocess.run(["bash", "-n", str(_LIB)], check=True)


def test_missing_baseline_is_recorded_and_reported_not_passed(tmp_path: Path) -> None:
    log = _log(tmp_path / "idx.log", [30, 20])
    baseline = tmp_path / "baselines.tsv"
    rc, out = _gate("corpus-a", log, 100, baseline)
    assert rc == 2, out
    assert "NO BASELINE" in out
    rows = baseline.read_text().splitlines()
    assert rows[0].startswith("label\t")
    assert rows[1].split("\t")[:3] == ["corpus-a", "2.0000", "50"]
    assert rows[1].split("\t")[5]  # box class recorded


def test_a_baseline_from_another_box_class_is_not_a_ceiling(tmp_path: Path) -> None:
    log = _log(tmp_path / "idx.log", [30, 20])
    baseline = tmp_path / "baselines.tsv"
    baseline.write_text("label\tseconds_per_chunk\tchunks\trecorded_at\tclient\tbox\ncorpus-a\t0.1\t50\tx\ty\tsome-other-box/2c\n")
    rc, out = _gate("corpus-a", log, 100, baseline)  # 2.0 s/chunk, 20x the other box's row
    assert rc == 2, out
    assert "NO BASELINE" in out


def test_within_ceiling_passes_and_above_fails(tmp_path: Path) -> None:
    log = _log(tmp_path / "idx.log", [30, 20])
    baseline = tmp_path / "baselines.tsv"
    box = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; throughput_box_class'],
        capture_output=True, text=True, timeout=60, env={"PATH": "/usr/sbin:/usr/bin:/bin"},
    ).stdout.strip()
    baseline.write_text(
        "label\tseconds_per_chunk\tchunks\trecorded_at\tclient\tbox\n"
        f"corpus-a\t1.0\t50\tx\ty\t{box}\n"
        "corpus-a\t9.0\t50\tx\ty\tsome-other-box/2c\n"
    )
    rc, out = _gate("corpus-a", log, 90, baseline)   # 1.8 s/chunk < 2x
    assert rc == 0, out
    rc, out = _gate("corpus-a", log, 110, baseline)  # 2.2 s/chunk > 2x
    assert rc == 1, out
    assert "FAIL" in out
    # a red never re-records itself: the two seeded rows, nothing appended
    assert baseline.read_text().count("corpus-a") == 2


def test_too_few_chunks_is_not_a_pass(tmp_path: Path) -> None:
    log = _log(tmp_path / "idx.log", [3, 2])
    baseline = tmp_path / "baselines.tsv"
    rc, out = _gate("tiny", log, 10, baseline)
    assert rc == 3, out
    assert "NOT a pass" in out
    assert not baseline.exists()


@pytest.mark.parametrize("script", [
    "tests/e2e/release-sandbox.sh",
    "tests/e2e/migration-rehearsal/rehearse_shakeout.sh",
])
def test_gates_source_the_lib_and_time_their_index_runs(script: str) -> None:
    text = (Path(__file__).resolve().parents[2] / script).read_text()
    assert "index_throughput.sh" in text, f"{script} does not source the throughput lib"
    assert "throughput_gate" in text or "_throughput_step" in text


def test_engine_shape_survives_a_log_without_the_boot_lines(tmp_path: Path) -> None:
    """Called under the gates' set -euo pipefail; a grep miss through tail
    must print the absence, not kill the run (review finding, 2026-09-04)."""
    log = tmp_path / "storage_service.log"
    log.write_text("INFO something else entirely\n")
    proc = subprocess.run(
        ["bash", "-euo", "pipefail", "-c",
         f'source "{_LIB}"; throughput_engine_shape "{log}"; echo STILL_ALIVE'],
        capture_output=True, text=True, timeout=60, env={"PATH": "/usr/sbin:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "STILL_ALIVE" in proc.stdout
    assert "not logged" in proc.stdout


def test_unwritable_baseline_is_reported_not_claimed_recorded(tmp_path: Path) -> None:
    log = _log(tmp_path / "idx.log", [30, 20])
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        rc, out = _gate("corpus-a", log, 100, ro / "baselines.tsv")
    finally:
        ro.chmod(0o700)
    assert rc == 2, out
    assert "not writable" in out
    assert "recorded into" not in out
    assert "corpus-a\t2.0000\t50" in out
