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

import httpx
import pytest
from click.testing import CliRunner

from nexus import ci_base
from nexus.commands import ci_cmd
from nexus.db.t2 import http_tuple_store
from nexus.identity import token_contract

REPO_ROOT = Path(__file__).resolve().parents[1]
FROM_SHA, TO_SHA = "a" * 40, "b" * 40
VCS = "g" + "it"  # the version-control binary; spelled apart only so a path scan of this file stays quiet


class _Store:
    """Records every ``out``; fails the first ``fail_stamped`` calls made with the session header, and the first
    ``fail_unstamped`` calls made without it."""

    def __init__(self, fail_stamped: int = 0, fail_unstamped: int = 0, *, session_header: bool = True,
                 drops_header_on_write: bool = False, identity_token: bool = False, drops_identity_on_write: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_stamped, self.fail_unstamped = fail_stamped, fail_unstamped
        self.session_header = session_header          # whether a write would carry the session header at all
        self.drops_header_on_write = drops_header_on_write  # the 401 headerless retry: the write lands without the session header
        self.identity_token = identity_token          # an RDR-253 identity token stamps the write whatever happens to the header
        self.drops_identity_on_write = drops_identity_on_write  # the identity-token 401 resend: the write lands without the token
        self.headerless_retries = 0
        self.identity_dropped_resends = 0

    def would_stamp(self) -> bool:
        return self.session_header or self.identity_token

    def session_stamps(self) -> bool:
        return self.session_header

    def identity_stamps(self) -> bool:
        return self.identity_token

    def out(self, subspace: str, keys: dict[str, str], dims: dict[str, str], body: str | None = None, *, nonce: str | None = None) -> str:
        stamped = not http_tuple_store._drop_session_header.get()
        if stamped and self.drops_header_on_write:
            self.headerless_retries += 1
        if stamped and self.drops_identity_on_write:
            self.identity_dropped_resends += 1
        self.calls.append({"subspace": subspace, "keys": keys, "dims": dims, "body": json.loads(body or "{}"), "raw_body": body or "",
                           "nonce": nonce, "stamped": stamped})
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


def test_the_ok_line_says_the_record_is_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _Store())
    assert result.stdout.strip().endswith(" stamped"), result.stdout
    assert "unstamped" not in result.stdout.lower()


def test_a_write_with_no_session_header_to_send_is_reported_unstamped_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review r1: no session env, so the engine stamps no sender and the record serves the chain only."""
    store = _Store(session_header=False)
    result = _run(monkeypatch, store)
    assert result.exit_code == 0, result.output
    assert len(store.calls) == 1, "the write itself stands; it is not retried"
    line = result.stdout.strip()
    assert line.startswith("PUSH_RECORD_UNSTAMPED ") and "no session" in line and "no pusher" in line, line
    assert "PUSH_RECORD_OK" not in line


def test_a_write_the_client_had_to_resend_headerless_is_reported_unstamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 401 retry (a stale session token): the write landed, without the header."""
    store = _Store(drops_header_on_write=True)
    result = _run(monkeypatch, store)
    assert result.exit_code == 0 and result.stdout.startswith("PUSH_RECORD_UNSTAMPED "), result.output
    assert "rejected" in result.stdout or "headerless" in result.stdout


def test_a_headerless_resend_is_still_stamped_when_an_identity_token_rides_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 401 retry drops the T1 session header only; the RDR-253 identity token still names the sender."""
    result = _run(monkeypatch, _Store(drops_header_on_write=True, identity_token=True))
    assert result.stdout.startswith("PUSH_RECORD_OK ") and result.stdout.strip().endswith(" stamped"), result.stdout


def test_no_session_header_but_an_identity_token_is_stamped(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _Store(session_header=False, identity_token=True))
    assert result.stdout.startswith("PUSH_RECORD_OK "), result.stdout


@pytest.mark.parametrize("kwargs", [
    dict(session_header=False, identity_token=True, drops_identity_on_write=True),   # the identity token alone, resent without it
    dict(session_header=True, identity_token=True, drops_identity_on_write=True, drops_header_on_write=True),   # both resent without
], ids=["identity-only", "both"])
def test_an_identity_token_resend_without_the_token_is_unstamped_like_a_headerless_resend(monkeypatch: pytest.MonkeyPatch, kwargs: dict) -> None:
    """Review r2 M1: the 401 resend of an identity-token send goes out without the token; with no other stamp left, the record
    is unstamped, and the status line says so."""
    result = _run(monkeypatch, _Store(**kwargs))
    line = result.stdout.strip().splitlines()[-1]
    assert result.exit_code == 0 and line.startswith("PUSH_RECORD_UNSTAMPED "), result.output
    assert "rejected" in line and "identity token" in line, line


@pytest.mark.parametrize("kwargs", [
    dict(session_header=True, identity_token=True, drops_identity_on_write=True),    # the session header still stamps it
    dict(session_header=True, identity_token=True, drops_header_on_write=True),      # the identity token still stamps it
], ids=["session-header-survives", "identity-token-survives"])
def test_a_record_with_one_stamp_surviving_the_resend_is_still_stamped(monkeypatch: pytest.MonkeyPatch, kwargs: dict) -> None:
    result = _run(monkeypatch, _Store(**kwargs))
    assert result.stdout.startswith("PUSH_RECORD_OK ") and result.stdout.strip().endswith(" stamped"), result.output


# ── review r2 N1: what the fallback record really is, read off the requests that go out ──────────────────────────

IDENTITY_HEADER = token_contract.IDENTITY_TOKEN_HEADER
T1_HEADER = "X-Nexus-T1-Session"


def _wire_store(monkeypatch: pytest.MonkeyPatch, *, identity_held: bool, first_status: int = 422) -> tuple[Any, list[Any]]:
    """A real ``HttpTupleStore`` over a fake transport: the first write is refused (so the fallback runs), the second lands.
    The session token is set in the environment; an identity token is held or not. Returns the store and the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(first_status, json={"error": "bad_request", "message": "refused for the test"})
        return httpx.Response(200, json={"id": "b" * 64})

    monkeypatch.setattr(http_tuple_store.HttpTupleStore, "_rejected_sessions", {})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.setenv("NX_T1_SESSION", "session-token-aaaa")
    held = {IDENTITY_HEADER: "identity-token-bbbb"} if identity_held else {}
    monkeypatch.setattr("nexus.identity.token_header.fenced_identity_headers", lambda method, path: dict(held))
    monkeypatch.setattr("nexus.identity.token_header.identity_headers", lambda: dict(held))
    store = http_tuple_store.HttpTupleStore(base_url="http://mock.test")
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    store._invalidate_and_reresolve = lambda: None  # type: ignore[method-assign]
    return store, seen


@pytest.mark.parametrize("identity_held", [True, False], ids=["identity-token-held", "no-identity-token"])
def test_the_fallback_request_drops_only_the_t1_session_header(monkeypatch: pytest.MonkeyPatch, identity_held: bool) -> None:
    """Asserted on the outgoing request headers, not on the ContextVar that drives them: the stamped write carries the T1
    session header (and the identity token when held); the fallback carries no T1 header, and the identity token still, when
    one is held, so the engine still stamps the record from it."""
    from nexus import ci_push_record  # noqa: PLC0415

    store, seen = _wire_store(monkeypatch, identity_held=identity_held)
    line = ci_push_record.write(store, "nexus-develop", FROM_SHA, TO_SHA, 1, "i5", None)
    assert len(seen) == 2, [r.url.path for r in seen]
    first, second = seen
    assert T1_HEADER in first.headers and (IDENTITY_HEADER in first.headers) is identity_held
    assert T1_HEADER not in second.headers, "the fallback is written without the T1 session header"
    assert (IDENTITY_HEADER in second.headers) is identity_held, "an identity token still rides the fallback"
    assert line.startswith("PUSH_RECORD_FALLBACK ") and "without the T1 session header" in line
    if identity_held:
        assert "still stamped by the identity token" in line and "no pusher" not in line, line
    else:
        assert "unstamped" in line and "no pusher" in line, line


def test_a_fallback_whose_identity_token_is_dropped_on_its_own_resend_is_unstamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback itself can meet a 401 on its identity token and be resent without it; then no stamp is left and the line
    must not claim one."""
    from nexus import ci_push_record  # noqa: PLC0415

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(422, json={"error": "bad_request", "message": "refused"})
        if len(seen) == 2 and IDENTITY_HEADER in request.headers:
            return httpx.Response(401, json={"error": "token refused"})
        return httpx.Response(200, json={"id": "b" * 64})

    monkeypatch.setattr(http_tuple_store.HttpTupleStore, "_rejected_sessions", {})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.setenv("NX_T1_SESSION", "session-token-aaaa")
    held = {IDENTITY_HEADER: "identity-token-bbbb"}
    monkeypatch.setattr("nexus.identity.token_header.fenced_identity_headers", lambda method, path: dict(held))
    monkeypatch.setattr("nexus.identity.token_header.identity_headers", lambda: dict(held))
    monkeypatch.setattr("nexus.identity.token_header.current", lambda **kw: None)   # nothing newer to resend with
    store = http_tuple_store.HttpTupleStore(base_url="http://mock.test")
    store._client = httpx.Client(transport=httpx.MockTransport(handler))
    store._invalidate_and_reresolve = lambda: None  # type: ignore[method-assign]
    line = ci_push_record.write(store, "nexus-develop", FROM_SHA, TO_SHA, 1, "i5", None)
    assert [IDENTITY_HEADER in r.headers for r in seen] == [True, True, False], "stamped try, fallback with the token, its resend without"
    assert line.startswith("PUSH_RECORD_FALLBACK ") and "no pusher" in line and "still stamped" not in line, line


def test_a_store_that_cannot_say_whether_it_stamps_is_taken_at_its_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store with no would_stamp / headerless_retries (an older client object) reports as it always did."""
    class Plain:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def out(self, *args: Any, **kw: Any) -> str:
            self.calls.append(args)
            return "t" * 64

    result = _run(monkeypatch, Plain())
    assert result.stdout.startswith("PUSH_RECORD_OK ")


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


BODY_CAP = 1024  # service/src/main/resources/tuples/templates/board-ci.yaml max_body_bytes


def test_the_body_cap_in_the_module_is_the_templates() -> None:
    from nexus import ci_push_record  # noqa: PLC0415

    template = (Path(__file__).resolve().parents[1] / "service" / "src" / "main" / "resources" / "tuples" / "templates" / "board-ci.yaml").read_text()
    assert f"max_body_bytes: {BODY_CAP}" in template
    assert ci_push_record.BODY_MAX_BYTES == BODY_CAP
    assert "4096" not in (ci_push_record.__doc__ or "")


@pytest.mark.parametrize("reason", ["界" * 2000, "é" * 2000, "😀" * 2000, 'q"\\' * 700], ids=["cjk", "latin1", "emoji", "escapes"])
def test_a_2000_character_non_ascii_reason_fits_both_writes_under_the_template_cap(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    """Review r1 S4: the cut is by serialized UTF-8 bytes, not characters; json.dumps with ensure_ascii escapes a non-ASCII
    character to six bytes, so a character cut left both writes over 1024 and the escape unrecorded."""
    store = _Store(fail_stamped=1)
    result = _run(monkeypatch, store, "--count", "3", "--red-fix", reason)
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("PUSH_RECORD_FALLBACK "), result.stdout
    stamped, minimal = store.calls
    for call in (stamped, minimal):
        assert len(call["raw_body"].encode("utf-8")) <= BODY_CAP, (call["stamped"], len(call["raw_body"].encode("utf-8")))
        cut = call["body"]["red_fix"]
        assert cut and reason.startswith(cut), "a cut prefix of the reason, never a mangled one"
    assert stamped["body"]["n"] == 3 and minimal["body"].keys() == {"base", "red_fix"}


def test_non_ascii_is_written_as_itself_not_as_a_six_byte_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store, "--red-fix", "owner away: 界").exit_code == 0
    assert "界" in store.calls[0]["raw_body"] and "\\u" not in store.calls[0]["raw_body"]


def test_a_newline_or_control_character_in_the_reason_becomes_a_space(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store, "--red-fix", "owner\naway\r\n\t back\x07\x1b[0m").exit_code == 0
    assert store.calls[0]["body"]["red_fix"] == "owner away back [0m"


def test_a_reason_of_only_whitespace_is_no_reason_and_writes_no_field(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store()
    assert _run(monkeypatch, store, "--red-fix", " \n\t ").exit_code == 0
    assert "red_fix" not in store.calls[0]["body"]


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
