# SPDX-License-Identifier: AGPL-3.0-or-later
"""Size, rate and time left on the engine and bundle downloads (nexus-mjmjl).
The clock is moved by hand; nothing sleeps."""

from __future__ import annotations

import http.client
import io
from pathlib import Path

import pytest

from nexus.progress import download_meter as dm


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


def test_plain_output_names_the_size_then_reports_rate_and_time_left_then_the_total() -> None:
    clock, out = Clock(), io.StringIO()
    meter = dm.DownloadMeter("nexus-service.txz", 90_000_000, stream=out, tty=False, clock=clock)
    clock.now += 1
    meter.update(5_000_000)  # not due yet
    clock.now += dm.PLAIN_EVERY_S
    meter.update(48_000_000)
    clock.now += 14
    meter.update(90_000_000)
    meter.finish()
    lines = out.getvalue().splitlines()
    assert lines[0] == "downloading nexus-service.txz (90.0 MB)"
    assert lines[1] == (
        "downloading nexus-service.txz: 48.0 MB of 90.0 MB (53%), 3.0 MB/s, about 14 s left"
    )
    assert lines[-1] == "downloaded nexus-service.txz: 90.0 MB in 30 s (3.0 MB/s)"
    assert "\x1b" not in out.getvalue() and "\r" not in out.getvalue()


def test_a_terminal_redraws_one_line_and_clears_it_for_the_final_line() -> None:
    clock, out = Clock(), io.StringIO()
    meter = dm.DownloadMeter("pg.txz", 10_000_000, stream=out, tty=True, clock=clock)
    for i in range(1, 5):
        clock.now += dm.TTY_EVERY_S
        meter.update(i * 2_000_000)
    meter.finish()
    text = out.getvalue()
    assert text.count("\rdownloading pg.txz: ") == 4
    assert "\x1b" not in text  # a legacy Windows console prints escape sequences as text
    last = text.rsplit("\r", 1)[-1]
    assert last == "downloaded pg.txz: 8.0 MB in 2.0 s (4.0 MB/s)\n"


def test_unknown_length_reports_bytes_and_rate_without_a_percentage() -> None:
    clock = Clock()
    meter = dm.DownloadMeter("x.bin", None, stream=io.StringIO(), tty=False, clock=clock)
    clock.now += 4
    meter.update(8_000_000)
    assert meter.status() == "downloading x.bin: 8.0 MB, 2.0 MB/s"


def test_a_small_download_of_unknown_length_says_nothing() -> None:
    clock, out = Clock(), io.StringIO()
    meter = dm.DownloadMeter("x.sigstore.json", None, stream=out, tty=False, clock=clock)
    clock.now += dm.PLAIN_EVERY_S
    meter.update(4000)
    meter.finish()
    assert out.getvalue() == ""


def test_a_broken_stream_never_fails_the_download() -> None:
    class Broken(io.StringIO):
        def write(self, _s):
            raise AttributeError("no console")

    clock = Clock()
    meter = dm.DownloadMeter("x.txz", 9_000_000, stream=Broken(), tty=False, clock=clock)
    clock.now += dm.PLAIN_EVERY_S
    meter.update(5_000_000)
    meter.finish()


def test_small_files_get_no_meter() -> None:
    assert dm.meter_for("x.sha256", 64) is None
    assert dm.meter_for("x.txz", dm.MIN_BYTES) is not None


class _Resp:
    def __init__(self, body: bytes, length: int | None) -> None:
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def read(self, n: int) -> bytes:
        return self._buf.read(n)


def test_the_installer_streams_a_large_asset_through_the_meter(tmp_path: Path, monkeypatch, capsys) -> None:
    from nexus.daemon import binary_install

    monkeypatch.setattr(dm, "MIN_BYTES", 10)
    body = b"x" * 5000
    binary_install._write_body(_Resp(body, len(body)), tmp_path / "engine.txz")
    assert (tmp_path / "engine.txz").read_bytes() == body
    err = capsys.readouterr().err
    assert "downloading engine.txz" in err and "downloaded engine.txz: 5.0 kB in" in err


def test_a_short_body_still_raises_for_a_retry_and_leaves_no_final_line(tmp_path: Path, monkeypatch, capsys) -> None:
    from nexus.daemon import binary_install

    monkeypatch.setattr(dm, "MIN_BYTES", 10)
    with pytest.raises(http.client.IncompleteRead):
        binary_install._write_body(_Resp(b"x" * 100, 5000), tmp_path / "engine.txz")
    assert "downloaded engine.txz" not in capsys.readouterr().err


def test_the_uv_build_tally_adds_totals_rate_and_time_left_to_uvs_own_lines() -> None:
    clock, out = Clock(), io.StringIO()
    tally = dm.UvBuildTally(stream=out, clock=clock, every_s=15)
    clock.now += 15
    tally.tick()  # nothing announced yet: still running
    for line in ("Resolved 120 packages in 2.1s\n", "Downloading torch (64.0MiB)\n", "Downloading scipy (32.0MiB)\n"):
        tally.feed(line)
    clock.now += 15
    tally.feed(" Downloaded scipy\n")
    tally.tick()
    clock.now += 15
    tally.feed(" Downloaded torch\n")
    tally.finish()
    lines = out.getvalue().splitlines()
    assert lines[0] == "build: still running, 15 s so far"
    assert lines[1] == (
        "build: 1 of 2 large package(s) downloaded (33.6 MB of 100.7 MB), 1.1 MB/s, about 1 m 00 s left; 30 s so far"
    )
    assert lines[2] == "build: downloaded 2 large package(s), 100.7 MB, in 45 s"
    assert "\x1b" not in out.getvalue() and "\r" not in out.getvalue()


def test_the_tally_ignores_other_lines_and_a_broken_stream() -> None:
    class Broken(io.StringIO):
        def write(self, _s):
            raise OSError("closed")

    clock = Clock()
    tally = dm.UvBuildTally(stream=Broken(), clock=clock)
    tally.feed("Downloading something odd\n")
    tally.feed(None)  # type: ignore[arg-type]
    clock.now += 60
    tally.tick()
    tally.finish()


def test_a_step_line_clears_a_redrawn_meter_line_first(monkeypatch) -> None:
    """The step heartbeat and the meter share a terminal: the meter's redrawn
    line is blanked before a step line is written, never glued onto it."""
    from nexus.progress.step_report import StepReporter, no_ticker

    clock, err = Clock(), io.StringIO()
    meter = dm.DownloadMeter("engine.txz", 9_000_000, stream=err, tty=True, clock=clock)
    clock.now += dm.TTY_EVERY_S
    meter.update(3_000_000)
    assert dm._ACTIVE is meter
    drawn = len(err.getvalue())
    rep = StepReporter(1, echo=lambda _s: None, stream=io.StringIO(), tty=False, clock=clock, ticker=no_ticker)
    rep.line("  ... 5.0 s elapsed")
    assert err.getvalue()[drawn:].startswith("\r ") and err.getvalue().endswith("\r")
    meter.finish()
    assert dm._ACTIVE is None
