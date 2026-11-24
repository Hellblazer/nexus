# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-igea9 (Sam, 2026-11-22): ``nx ci record-push --skip CHECK=REASON`` puts each push-time check override in the push
record body, as ``skips``, beside ``red_fix``. scripts/git-push-develop.sh passes one ``--skip`` per check it let through;
the board then shows every escape. The body stays under the template's 1024 bytes, so the reasons are cut, never the names.
"""
from __future__ import annotations

import pytest

from tests.test_ci_record_push import FROM_SHA, TO_SHA, _identity, _run, _Store  # noqa: F401 -- _identity is an autouse fixture

BODY_CAP = 1024  # service/src/main/resources/tuples/templates/board-ci.yaml max_body_bytes
CHECKS = ("file-size", "changelog", "wire-ledger", "cloud-leg", "plugin-ledger", "docs-readers", "write-seam", "check-gate")


def test_a_skip_is_in_the_body_under_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    result = _run(monkeypatch, store, "--count", "1", "--skip", "file-size=ceiling raise approved, bead 7")
    assert result.exit_code == 0, result.output
    assert store.calls[0]["body"] == {"base": FROM_SHA, "n": 1, "skips": {"file-size": "ceiling raise approved, bead 7"}}


def test_skips_repeat_and_ride_the_fallback_record_too(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(fail_stamped=1)
    result = _run(monkeypatch, store, "--skip", "file-size=a", "--skip", "wire-ledger=b", "--red-fix", "owner away")
    assert result.exit_code == 0, result.output
    for call in store.calls:
        assert call["body"]["skips"] == {"file-size": "a", "wire-ledger": "b"} and call["body"]["red_fix"] == "owner away"


def test_no_skip_means_no_skips_field(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store).exit_code == 0
    assert "skips" not in store.calls[0]["body"]


@pytest.mark.parametrize("bad", ["file-size", "=reason", "file-size=", "file-size= \n", "File Size=reason"])
def test_a_malformed_skip_is_a_usage_error_and_writes_nothing(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    store = _Store()
    result = _run(monkeypatch, store, "--skip", bad)
    assert result.exit_code == 2 and store.calls == [], result.output


def test_a_reason_is_one_line_and_cut_while_every_check_name_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(fail_stamped=1)
    args = [a for name in CHECKS for a in ("--skip", f"{name}=" + "界\n" * 3000)]
    result = _run(monkeypatch, store, "--count", "4", "--red-fix", "r" * 900, *args)
    assert result.exit_code == 0, result.output
    stamped, minimal = store.calls
    for call in (stamped, minimal):
        assert len(call["raw_body"].encode("utf-8")) <= BODY_CAP, len(call["raw_body"].encode("utf-8"))
        assert set(call["body"]["skips"]) == set(CHECKS), "a name is never cut, only its reason"
        assert all("\n" not in text for text in call["body"]["skips"].values())
        assert call["body"]["red_fix"], "the skips leave the red-fix reason some room"
    assert stamped["body"]["n"] == 4
