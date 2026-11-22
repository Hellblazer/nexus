# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci ack --not-mine <sha>`` (RDR-252 post-close audit S1, bead nexus-igea9): the owner's "this red is not mine".

It posts a row of kind ``disclaim`` on the same ``--owner`` subspace an ack goes to, with the same sha handling and the
same identity stamping. The engine acts on it in a later engine cut (a disclaim from the current owner hands the red on
at once); until then the row is recorded only. All against a fake transport, as tests/test_ci_ack_identity.py is.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands import ci_cmd
from nexus.db.t2.http_tuple_store import HttpTupleStore

OWNER_SUBSPACE = "board/ci/nexus-develop--owner"
TOPIC_KEY = {"topic": "nexus-develop--owner"}
SHA = "ae619ef56" + "0" * 31
ME = "i5"
EP = "2026-11-20T14:00:00Z"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(HttpTupleStore, "_rejected_sessions", {})
    monkeypatch.setenv("NX_SERVICE_TOKEN", "data-token")
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.setattr("nexus.ci_ack.identity_view", lambda who: {"identity": who, "kind": "seat"}, raising=False)


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    held = [ME]
    monkeypatch.setattr("nexus.ci_ack.own_identity", lambda: held[0] if held else "")
    return held


class _Wire:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.rows: list[dict[str, Any]] = []
        #: When set, a read answers only the rows of the subspace it names (a board with none answers nothing).
        self.by_subspace: dict[str, list[dict[str, Any]]] | None = None
        self.reads: list[str] = []
        self.store = HttpTupleStore(base_url="http://mock.test")
        self.store._client = httpx.Client(transport=httpx.MockTransport(self._handle))
        self.store._invalidate_and_reresolve = lambda: None  # type: ignore[method-assign]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/rd"):
            subspace = json.loads(request.content).get("subspace", "")
            self.reads.append(subspace)
            rows = self.rows if self.by_subspace is None else self.by_subspace.get(subspace, [])
            return httpx.Response(200, json={"tuples": rows, "order": "desc", "limit": 300})
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "d" * 64})


@pytest.fixture
def wire() -> _Wire:
    return _Wire()


def _owner_row(sha: str, at: str, owner: str | None = ME, **body: Any) -> dict[str, Any]:
    fields = {"owner": owner, "episode_at": EP, **body}
    return {"id": f"owner-{at}".ljust(64, "0"), "subspace": OWNER_SUBSPACE, "keys": TOPIC_KEY, "created_at": at,
            "dims": {"kind": "owner", "from": "engine", "sha": sha}, "body": json.dumps(fields)}


def _open_red(wire: _Wire, owner: str | None = ME) -> None:
    wire.rows = [_owner_row(SHA, "2026-11-20T14:10:00Z", owner=owner, owner_alias="tamsin")]


def _run(args: list[str], wire: _Wire, monkeypatch: pytest.MonkeyPatch, *, derive: bool = False) -> Any:
    """Run `nx ci ack ARGS`. The board is named with --base nexus-develop unless the args name one or *derive* is set
    (the repo is then derived from the origin remote, as a plain `nx ci ack` does)."""
    monkeypatch.setattr(ci_cmd, "_store", lambda: wire.store)
    named = derive or "--base" in args or "--topic" in args
    return CliRunner().invoke(ci_cmd.ci_group, ["ack", *args] if named else ["ack", *args, "--base", "nexus-develop"])


def test_not_mine_posts_a_disclaim_row_with_the_full_sha_and_the_callers_identity(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_red(wire)
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code == 0, result.output
    [sent] = wire.sent
    assert sent["subspace"] == OWNER_SUBSPACE and sent["keys"] == TOPIC_KEY
    assert sent["dims"] == {"kind": "disclaim", "from": ME, "sha": SHA}, "kind disclaim, the prefix spelled out in full"
    assert sent["nonce"], "a fresh nonce: the template is keys+nonce"
    assert "disclaimed" in result.output and SHA[:7] in result.output and ME in result.output
    assert "recorded" in result.output, "the output says the engine does not act on it yet"


def test_a_plain_ack_is_unchanged_by_the_new_flag(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _run([SHA[:7]], wire, monkeypatch).exit_code == 0
    assert wire.sent[0]["dims"] == {"kind": "ack", "from": ME, "sha": SHA}


def test_the_note_rides_in_the_body_and_is_optional(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    assert _run(["--not-mine", SHA[:7], "--note", "marginalia is fixing it"], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["body"] == "marginalia is fixing it"
    assert _run(["--not-mine", SHA[:7]], wire, monkeypatch).exit_code == 0
    assert not wire.sent[-1].get("body")


@pytest.mark.parametrize("note", ["two\nlines", "x" * 201, "bell\x07"])
def test_a_note_with_a_control_character_or_over_200_characters_is_refused(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, note: str,
) -> None:
    _open_red(wire)
    result = _run(["--not-mine", SHA[:7], "--note", note], wire, monkeypatch)
    assert result.exit_code != 0 and "note" in result.output and wire.sent == []


def test_a_note_without_not_mine_is_refused(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _open_red(wire)
    result = _run([SHA[:7], "--note", "hi"], wire, monkeypatch)
    assert result.exit_code != 0 and "--not-mine" in result.output and wire.sent == []


@pytest.mark.parametrize("arg", ["abc", "not-hex", "d" * 40, "1234567"])
def test_a_sha_that_is_no_tip_of_the_open_red_is_refused_as_an_ack_does(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, arg: str,
) -> None:
    _open_red(wire)
    result = _run(["--not-mine", arg], wire, monkeypatch)
    assert result.exit_code != 0, result.output
    assert wire.sent == []


def test_a_caller_who_is_not_the_owner_is_refused_and_told_the_engine_would_ignore_it(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_red(wire, owner="i9")
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and wire.sent == []
    assert "owned by i9" in result.output and "disclaim" in result.output and "ignore" in result.output


def test_no_open_red_and_no_identity_are_refused(wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    wire.rows = []
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "no open red" in result.output and wire.sent == []
    _open_red(wire)
    identity[:] = []
    result = _run(["--not-mine", SHA[:7]], wire, monkeypatch)
    assert result.exit_code != 0 and "nx identity show" in result.output and wire.sent == []


def test_a_hand_typed_alias_in_a_disclaim_is_restamped_with_the_callers_identity(wire: _Wire, identity: list[str]) -> None:
    """The store stamps a disclaim as it stamps an ack: ``from`` is the poster's identity id, never a name; and a caller
    with no identity is refused rather than written under a name."""
    wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, {"kind": "disclaim", "from": "marginalia", "sha": SHA}, nonce="n1")
    assert wire.sent[0]["dims"]["from"] == ME
    identity[:] = []
    with pytest.raises(ValueError, match="identity"):
        wire.store.out(OWNER_SUBSPACE, TOPIC_KEY, {"kind": "disclaim", "from": "marginalia", "sha": SHA}, nonce="n2")
    assert len(wire.sent) == 1


def test_a_disclaim_on_another_subspace_is_not_restamped(wire: _Wire, identity: list[str]) -> None:
    identity[:] = []
    wire.store.out("board/ci/nexus-develop--done", TOPIC_KEY, {"kind": "disclaim", "from": "marginalia"}, nonce="n1")
    assert wire.sent[0]["dims"]["from"] == "marginalia"


# ── any repo's board: --base names it (Sam, T2 nexus/sam-ruling-generalize-252-2026-11-22) ─────────────────────


@pytest.mark.parametrize("flags", [[], ["--not-mine"]], ids=["ack", "not-mine"])
def test_base_names_any_repos_board(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, flags: list[str],
) -> None:
    _open_red(wire)
    assert _run([*flags, SHA[:7], "--base", "halcyon-main"], wire, monkeypatch).exit_code == 0
    assert wire.sent[-1]["subspace"] == "board/ci/halcyon-main--owner"
    assert wire.sent[-1]["keys"] == {"topic": "halcyon-main--owner"}


# ── no --base: the repo is derived from the origin remote, and nothing falls back to nexus ──────────────────────


@pytest.mark.parametrize(
    ("url", "repo"),
    [
        ("git@github.com:Hellblazer/halcyon.git", "halcyon"),
        ("https://github.com/Hellblazer/halcyon.git", "halcyon"),
        ("https://github.com/Hellblazer/halcyon", "halcyon"),
        ("https://github.com/Hellblazer/halcyon/", "halcyon"),
        ("ssh://git@github.com/Hellblazer/my.repo-x.git", "my.repo-x"),
        ("https://user:tok@github.com/Hellblazer/halcyon.git", "halcyon"),
    ],
)
def test_the_repo_is_the_last_segment_of_the_origin_url(url: str, repo: str) -> None:
    assert ci_cmd._repo_from_origin(url) == repo


@pytest.mark.parametrize("url", ["", "   ", "halcyon", "https://github.com/", "/Hellblazer/.git", "git@github.com:a b/c d.git"])
def test_an_origin_url_with_no_usable_repo_name_gives_none(url: str) -> None:
    assert ci_cmd._repo_from_origin(url) is None


@pytest.mark.parametrize("flags", [[], ["--not-mine"]], ids=["ack", "not-mine"])
def test_with_no_base_the_one_board_of_the_origin_repo_with_an_open_red_is_used(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, flags: list[str],
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "git@github.com:Hellblazer/halcyon.git")
    sub = "board/ci/halcyon-main--owner"
    wire.by_subspace = {sub: [_owner_row(SHA, "2026-11-20T14:10:00Z")]}
    result = _run([*flags, SHA[:7]], wire, monkeypatch, derive=True)
    assert result.exit_code == 0, result.output
    assert wire.sent[-1]["subspace"] == sub and wire.sent[-1]["keys"] == {"topic": "halcyon-main--owner"}
    assert sorted(set(wire.reads)) == ["board/ci/halcyon-develop--owner", sub], "both of the repo's push branches were read"


def test_two_boards_of_the_origin_repo_with_an_open_red_are_ambiguous_and_the_refusal_names_base(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "https://github.com/Hellblazer/halcyon.git")
    row = [_owner_row(SHA, "2026-11-20T14:10:00Z")]
    wire.by_subspace = {"board/ci/halcyon-develop--owner": row, "board/ci/halcyon-main--owner": row}
    result = _run([SHA[:7]], wire, monkeypatch, derive=True)
    assert result.exit_code != 0 and wire.sent == []
    assert "halcyon-develop" in result.output and "halcyon-main" in result.output and "--base" in result.output


def test_no_open_red_on_any_board_of_the_origin_repo_is_refused_naming_base(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: "https://github.com/Hellblazer/halcyon.git")
    wire.by_subspace = {"board/ci/nexus-develop--owner": [_owner_row(SHA, "2026-11-20T14:10:00Z")]}
    result = _run([SHA[:7]], wire, monkeypatch, derive=True)
    assert result.exit_code != 0 and wire.sent == []
    assert "halcyon" in result.output and "--base" in result.output
    assert "board/ci/nexus-develop--owner" not in wire.reads, "an open red on nexus is never a fallback"


@pytest.mark.parametrize("url", ["", "not a url"])
def test_an_origin_that_cannot_be_read_is_refused_naming_base_and_nothing_is_read_or_posted(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch, url: str,
) -> None:
    monkeypatch.setattr(ci_cmd, "_origin_url", lambda: url)
    _open_red(wire)  # a red on every board: a fallback to nexus-develop would find it
    result = _run([SHA[:7]], wire, monkeypatch, derive=True)
    assert result.exit_code != 0 and wire.sent == [] and wire.reads == []
    assert "origin" in result.output and "--base" in result.output


def test_the_real_origin_reader_returns_the_origin_of_the_repository_it_runs_in(tmp_path: Any) -> None:
    import subprocess  # noqa: PLC0415

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert ci_cmd._origin_url(cwd=tmp_path) == "", "a repository with no origin"
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:Hellblazer/halcyon.git"], check=True)
    assert ci_cmd._repo_from_origin(ci_cmd._origin_url(cwd=tmp_path)) == "halcyon"
    assert ci_cmd._origin_url(cwd=tmp_path / "missing") == "", "a directory that is no repository"


def test_base_and_topic_together_are_refused_and_a_base_must_be_a_bare_base(
    wire: _Wire, identity: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_red(wire)
    both = _run([SHA[:7], "--base", "halcyon-main", "--topic", "halcyon-main--owner"], wire, monkeypatch)
    assert both.exit_code != 0 and "--base" in both.output and "--topic" in both.output
    for bad in ("halcyon-main--owner", "a/b", "", "-x"):
        result = _run([SHA[:7], "--base", bad], wire, monkeypatch)
        assert result.exit_code != 0, bad
    assert wire.sent == []


def test_no_repo_is_hardcoded_in_the_ack_the_reds_split_or_the_ack_times_display() -> None:
    """No board is named in the ack verb, ci_ack, ci_reds or the ack-times code: the board is --base, --topic, or the one
    the origin remote names (Sam, T2 nexus/sam-ruling-generalize-252-2026-11-22; never a fallback to nexus)."""
    from pathlib import Path  # noqa: PLC0415

    root = Path(__file__).resolve().parent.parent / "src" / "nexus"
    for name in ("ci_ack.py", "ci_reds.py"):
        assert "nexus-develop" not in (root / name).read_text(encoding="utf-8"), name
    cmd = (root / "commands" / "ci_cmd.py").read_text(encoding="utf-8")
    ack_part = cmd.split("def _current_red")[1]  # _current_red, the origin helpers and the ack verb: the rest of the file
    assert "def _repo_from_origin" in ack_part and "def ci_ack_cmd" in ack_part, "non-vacuity: the slice holds what it audits"
    assert "nexus-develop" not in ack_part and "DEFAULT_BASE" not in cmd


def test_a_disclaim_on_another_boards_red_is_split_and_shown_like_any_other() -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    from nexus import ci_reds  # noqa: PLC0415

    def row(kind: str, at: str, who: str, **body: Any) -> Any:
        return SimpleNamespace(id=f"{kind}{at}", created_at=at, dims={"from": who, "kind": kind, "sha": SHA},
                               body=json.dumps(body) if kind == "owner" else "mine no more", sender_session=None)

    reds, _ = ci_reds.split([row("owner", "2026-11-20T10:00:00Z", "engine", owner="i5", episode_at=EP),
                             row("disclaim", "2026-11-20T10:02:00Z", "i5")])
    assert len(reds) == 1 and [r.body for _, r in reds[0].disclaims] == ["mine no more"]


def test_the_help_says_what_it_does_now_and_after_the_engine_cut() -> None:
    result = CliRunner().invoke(ci_cmd.ci_group, ["ack", "--help"])
    text = " ".join(result.output.split())
    assert "--not-mine" in text and "--note" in text
    assert "recorded only" in text and "hands the red on at once" in text
