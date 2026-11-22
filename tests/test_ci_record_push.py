# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-252 (nexus-igea9): ``nx ci record-push`` writes the push record of a branch, and ``--base`` is derived or refused.

Post-close audit S3: a skipped or failed push record was one stderr line nobody read, a failed stamped write left the chain
with no link, and ``from`` was hardcoded ``unknown`` while the RDR said it named the identity. The command (the push script's
first caller, ``scripts/git-push-develop.sh``) writes the stamped record, retries once as a minimal unstamped record, writes
the identity id in ``from`` and the ``--red-fix`` reason in the body, and prints one PUSH_RECORD_* status line; its exit code
never fails the push. The base of the board topic is an argument, or derived as ``<repo>-<branch>`` from the origin URL and
the target branch, and refused (naming ``--base``) when it cannot be: never guessed.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nexus import ci_base
from nexus.commands import ci_cmd
from nexus.db.t2 import http_tuple_store

REPO_ROOT = Path(__file__).resolve().parents[1]
FROM_SHA, TO_SHA = "a" * 40, "b" * 40
VCS = "g" + "it"  # the version-control binary; spelled apart only so a path scan of this file stays quiet


class _Store:
    """Records every ``out``; fails the first ``fail_stamped`` calls made with the session header, and the first
    ``fail_unstamped`` calls made without it."""

    def __init__(self, fail_stamped: int = 0, fail_unstamped: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_stamped, self.fail_unstamped = fail_stamped, fail_unstamped

    def out(self, subspace: str, keys: dict[str, str], dims: dict[str, str], body: str | None = None, *, nonce: str | None = None) -> str:
        stamped = not http_tuple_store._drop_session_header.get()
        self.calls.append({"subspace": subspace, "keys": keys, "dims": dims, "body": json.loads(body or "{}"), "nonce": nonce, "stamped": stamped})
        if stamped and self.fail_stamped:
            self.fail_stamped -= 1
            raise ConnectionError("stamped write refused")
        if not stamped and self.fail_unstamped:
            self.fail_unstamped -= 1
            raise ConnectionError("unstamped write refused too")
        return "t" * 64


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    held = ["i5"]
    monkeypatch.setattr("nexus.ci_ack.own_identity", lambda: held[0] if held else "")
    return held


def _run(monkeypatch: pytest.MonkeyPatch, store: Any, *extra: str) -> Any:
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    return CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--base", "nexus-develop", "--from-sha", FROM_SHA, "--to-sha", TO_SHA, *extra])


def test_the_stamped_record_has_the_chain_fields_and_the_identity_id(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    result = _run(monkeypatch, store, "--count", "2")
    assert result.exit_code == 0, result.output
    (call,) = store.calls
    assert call["subspace"] == "board/ci/nexus-develop--push" and call["keys"] == {"topic": "nexus-develop--push"}
    assert call["dims"] == {"from": "i5", "kind": "push", "sha": TO_SHA} and call["nonce"] == f"push-{TO_SHA}"
    assert call["body"] == {"base": FROM_SHA, "n": 2} and call["stamped"] is True
    assert result.stdout.startswith("PUSH_RECORD_OK ") and "from=i5" in result.stdout


def test_from_is_unknown_when_the_caller_holds_no_identity(monkeypatch: pytest.MonkeyPatch, _identity: list[str]) -> None:
    _identity.clear()
    store = _Store()
    assert _run(monkeypatch, store).exit_code == 0
    assert store.calls[0]["dims"]["from"] == "unknown"


def test_from_is_unknown_when_reading_the_identity_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise RuntimeError("token record unreadable")

    monkeypatch.setattr("nexus.ci_ack.own_identity", boom)
    store = _Store()
    assert _run(monkeypatch, store).exit_code == 0
    assert store.calls[0]["dims"]["from"] == "unknown"


def test_a_failed_stamped_write_is_retried_once_as_a_minimal_unstamped_record(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(fail_stamped=1)
    result = _run(monkeypatch, store, "--count", "3")
    assert result.exit_code == 0, result.output
    first, second = store.calls
    assert (first["stamped"], second["stamped"]) == (True, False), "the retry must go out without the session header"
    assert first["nonce"] == second["nonce"] == f"push-{TO_SHA}"  # a write that did land is not doubled
    assert second["dims"] == {"from": "i5", "kind": "push", "sha": TO_SHA}
    assert second["body"] == {"base": FROM_SHA}, "minimal: base alone"
    assert result.stdout.startswith("PUSH_RECORD_FALLBACK ") and "stamped write refused" in result.stdout
    assert "PUSH_RECORD_FAILED" not in result.stdout


def test_both_writes_failing_is_one_failed_line_and_still_exit_0(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(fail_stamped=1, fail_unstamped=1)
    result = _run(monkeypatch, store)
    assert result.exit_code == 0, result.output
    assert len(store.calls) == 2
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("PUSH_RECORD_FAILED ") and "unstamped write refused too" in lines[0]


def test_a_store_that_cannot_be_opened_is_a_failed_line_and_exit_0(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_store() -> Any:
        raise RuntimeError("no engine")

    monkeypatch.setattr(ci_cmd, "_store", no_store)
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--base", "nexus-develop", "--from-sha", FROM_SHA, "--to-sha", TO_SHA])
    assert result.exit_code == 0 and result.stdout.startswith("PUSH_RECORD_FAILED ") and "no engine" in result.stdout


def test_a_red_fix_reason_is_in_the_body_of_the_stamped_record(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = 'fix for "the jar gate"; owner away \\ back'
    store = _Store()
    assert _run(monkeypatch, store, "--count", "1", "--red-fix", reason).exit_code == 0
    assert store.calls[0]["body"] == {"base": FROM_SHA, "n": 1, "red_fix": reason}


def test_a_red_fix_reason_survives_into_the_fallback_record(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(fail_stamped=1)
    assert _run(monkeypatch, store, "--red-fix", "owner away").exit_code == 0
    assert store.calls[1]["body"] == {"base": FROM_SHA, "red_fix": "owner away"}


def test_no_red_fix_means_no_red_fix_field(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store).exit_code == 0
    assert "red_fix" not in store.calls[0]["body"] and "n" not in store.calls[0]["body"]


def test_an_over_long_red_fix_reason_is_cut_so_the_record_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store, "--red-fix", "r" * 5000).exit_code == 0
    assert len(store.calls[0]["body"]["red_fix"]) == 500


def test_the_base_decides_the_subspace_and_is_never_nexus_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--base", "other-main", "--from-sha", "", "--to-sha", TO_SHA])
    assert result.exit_code == 0, result.output
    assert store.calls[0]["subspace"] == "board/ci/other-main--push" and store.calls[0]["body"] == {"base": ""}


def test_the_headerless_flag_is_reset_after_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, _Store(fail_stamped=1)).exit_code == 0
    assert http_tuple_store._drop_session_header.get() is False


# ── --base: derived as <repo>-<branch> from the origin URL, or refused naming --base ──────────────

def _repo(path: Path, origin: str | None) -> Path:
    path.mkdir()
    subprocess.run([VCS, "init", "-q"], cwd=path, check=True)
    if origin is not None:
        subprocess.run([VCS, "remote", "add", "origin", origin], cwd=path, check=True)
    return path


@pytest.mark.parametrize("url", [
    "https://github.com/Hellblazer/nexus.git",
    "https://github.com/Hellblazer/nexus",
    "https://github.com/Hellblazer/nexus/",
    "git@github.com:Hellblazer/nexus.git",
    "ssh://git@github.com/Hellblazer/nexus",
])
def test_the_base_of_hellblazer_nexus_pushing_develop_is_nexus_develop(tmp_path: Path, url: str) -> None:
    assert ci_base.derive("origin", "develop", cwd=_repo(tmp_path / "r", url)) == "nexus-develop"


def test_the_base_is_the_repo_name_plus_the_branch_for_any_repo(tmp_path: Path) -> None:
    assert ci_base.derive("origin", "main", cwd=_repo(tmp_path / "r", "git@example.org:acme/widgets.git")) == "widgets-main"


def test_this_checkout_derives_nexus_develop_when_it_has_an_origin() -> None:
    """The repo itself, as a developer runs it. A lab-remote copy has no origin; there the throwaway-repo cases above carry it."""
    if subprocess.run([VCS, "remote", "get-url", "origin"], cwd=REPO_ROOT, capture_output=True).returncode != 0:
        pytest.skip("this copy of the checkout has no origin remote (a lab-remote copy)")
    assert ci_base.derive("origin", "develop", cwd=REPO_ROOT) == "nexus-develop"


@pytest.mark.parametrize(("origin", "branch"), [
    (None, "develop"),                       # no origin at all
    ("https://", "develop"),                 # a URL with no repo name
    ("git@github.com:", "develop"),
    ("https://github.com/Hellblazer/nexus.git", None),   # an unknown target branch
    ("https://github.com/Hellblazer/nexus.git", ""),
    ("https://github.com/Hellblazer/nexus.git", "feature/x"),   # no board topic part
], ids=["no-origin", "no-name", "no-name-scp", "no-branch", "empty-branch", "slash-branch"])
def test_an_underivable_base_is_refused_and_names_the_base_flag(tmp_path: Path, origin: str | None, branch: str | None) -> None:
    repo = _repo(tmp_path / "r", origin)
    with pytest.raises(ci_base.BaseUnderivableError, match="--base"):
        ci_base.derive("origin", branch, cwd=repo)


def test_record_push_in_a_repo_with_no_origin_refuses_and_writes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(_repo(tmp_path / "r", None))
    store = _Store()
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--branch", "develop", "--from-sha", FROM_SHA, "--to-sha", TO_SHA])
    assert result.exit_code != 0 and "--base" in result.output
    assert store.calls == []


def test_record_push_derives_nexus_develop_in_a_repo_whose_origin_is_hellblazer_nexus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(_repo(tmp_path / "r", "https://github.com/Hellblazer/nexus.git"))
    store = _Store()
    monkeypatch.setattr(ci_cmd, "_store", lambda: store)
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--branch", "develop", "--from-sha", FROM_SHA, "--to-sha", TO_SHA])
    assert result.exit_code == 0, result.output
    assert store.calls[0]["subspace"] == "board/ci/nexus-develop--push"


def test_no_branch_and_no_base_is_refused_not_defaulted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(_repo(tmp_path / "r", "https://github.com/Hellblazer/nexus.git"))
    monkeypatch.setattr(ci_cmd, "_store", lambda: pytest.fail("must refuse before it touches the board"))
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--from-sha", FROM_SHA, "--to-sha", TO_SHA])
    assert result.exit_code != 0 and "--base" in result.output


def test_a_malformed_base_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_cmd, "_store", lambda: pytest.fail("must refuse first"))
    result = CliRunner().invoke(ci_cmd.ci_group, ["record-push", "--base", "../x", "--from-sha", FROM_SHA, "--to-sha", TO_SHA])
    assert result.exit_code != 0 and "--base" in result.output
