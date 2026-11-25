# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-igea9 review round 1: ``nx ci record-push --proof-info LEG=TREE12,STARTED,ELAPSED,HOST`` puts the particulars of each
heavy-leg proof the push relied on in the record body, as ``proofs``, beside ``skips`` and ``red_fix``. The board then shows
which run (tree, start, duration, host) stood behind a push. The whole body stays under the template's 1024 bytes.
"""
from __future__ import annotations

import pytest

from tests.test_ci_record_push import _identity, _run, _Store  # noqa: F401 -- _identity is an autouse fixture

BODY_CAP = 1024  # service/src/main/resources/tuples/templates/board-ci.yaml max_body_bytes
CHECKS = ("file-size", "changelog", "context-budget", "wire-ledger", "cloud-leg", "plugin-ledger", "docs-readers", "write-seam", "check-gate")
PROOFS = ("docs-readers=aaaaaaaaaaaa,2026-11-22T14:30:00Z,160,chas", "write-seam=aaaaaaaaaaaa,2026-11-22T14:40:00Z,95,qwent")


def test_a_proof_info_is_in_the_body_under_proofs(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    result = _run(monkeypatch, store, "--proof-info", PROOFS[0], "--proof-info", PROOFS[1])
    assert result.exit_code == 0, result.output
    assert store.calls[0]["body"]["proofs"] == {
        "docs-readers": {"tree": "aaaaaaaaaaaa", "at": "2026-11-22T14:30:00Z", "s": 160, "host": "chas"},
        "write-seam": {"tree": "aaaaaaaaaaaa", "at": "2026-11-22T14:40:00Z", "s": 95, "host": "qwent"},
    }


def test_no_proof_info_means_no_proofs_field(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store).exit_code == 0
    assert "proofs" not in store.calls[0]["body"]


@pytest.mark.parametrize("bad", ["docs-readers", "docs-readers=aaaa", "docs-readers=aaaa,2026,x,chas", "=a,b,1,c", "Docs=a,b,1,c",
                                 "docs-readers=a,b,-1,c"])
def test_a_malformed_proof_info_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    store = _Store()
    result = _run(monkeypatch, store, "--proof-info", bad)
    assert result.exit_code == 2 and store.calls == [], result.output


def test_proofs_skips_and_a_red_fix_all_fit_the_template_cap_and_the_fallback_keeps_them(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(fail_stamped=1)
    args = [a for name in CHECKS for a in ("--skip", f"{name}=" + "界" * 3000)]
    result = _run(monkeypatch, store, "--count", "4", "--red-fix", "r" * 900, "--proof-info", PROOFS[0],
                  "--proof-info", "write-seam=aaaaaaaaaaaa,2026-11-22T14:40:00Z,95," + "h" * 300, *args)
    assert result.exit_code == 0, result.output
    stamped, minimal = store.calls
    for call in (stamped, minimal):
        assert len(call["raw_body"].encode("utf-8")) <= BODY_CAP, len(call["raw_body"].encode("utf-8"))
        assert set(call["body"]["proofs"]) == {"docs-readers", "write-seam"} and set(call["body"]["skips"]) == set(CHECKS)
        assert call["body"]["red_fix"], "the proofs and skips leave the red-fix reason some room"
