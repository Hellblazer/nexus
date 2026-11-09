# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.7: the scrub gate fails a personal home path, tailnet hostname, chat link or session
trailer in a tracked file unless the allowlist grandfathers it, and its ceilings only fall.

Unit cases run the script on a throwaway git repo; the lint case checks this repo, which is how CI's
lint bucket enforces it. Real-looking strings are built by concatenation so this file stays clean.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "scrub_check", _REPO / "scripts" / "scrub_check.py"
)
assert _spec and _spec.loader
sc = importlib.util.module_from_spec(_spec)
sys.modules["scrub_check"] = sc
_spec.loader.exec_module(sc)

HOME = "/Users/" + "jdoe" + "/git/proj"
LINUX_HOME = "/home/" + "jdoe" + "/src"
TAILNET = "chas." + "tail1234" + ".ts.net"
CHAT = "https://claude.ai/" + "chat/0f1e2d"
TRAILER = "Claude-" + "Session: 0f0f0f0f-0000-4000-8000-000000000000"
PLACEHOLDER = "/Users/" + "you" + "/git/proj"


def _put(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "--", rel], check=True)


def _allow(root: Path, text: str) -> None:
    (root / sc.ALLOWLIST).parent.mkdir(parents=True, exist_ok=True)
    (root / sc.ALLOWLIST).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    _put(tmp_path, "src/clean.py", "x = 1\n")
    _allow(tmp_path, "user you\n")
    return tmp_path


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("home", HOME),
        ("home", LINUX_HOME),
        ("home", "HOME=/Users/" + "jdoe"),  # no slash after the name
        ("home", "cd /home/" + "jdoe"),
        ("home", '"/Users/' + 'jdoe"'),
        ("home", "/mnt/c/Users/" + "jdoe/x"),
        ("home", "C:/Users/" + "jdoe"),
        ("home", "/System/Volumes/Data/Users/" + "jdoe/x"),
        ("home", "/var/home/" + "jdoe"),
        ("home", "gcc -I/home/" + "jdoe/inc"),
        ("home", "rsync://chas/home/" + "jdoe/x"),
        ("winhome", "\\\\wsl$\\Ubuntu\\home\\" + "jdoe\\x"),
        ("winhome", "C:\\Users\\" + "Jdoe\\x"),
        ("winhome", "c:\\\\users\\\\" + "jdoe"),  # JSON-escaped, lower case
        ("tailnet", TAILNET),
        ("tailnet", "tail1234" + ".ts.net"),
        ("chat", CHAT),
        ("chat", "https://claude.ai/" + "share/abc"),
        ("chat", "https://claude.ai/" + "code/session_01AbCdEf"),
        ("session", TRAILER),
        ("session", "claude-" + "session: 0f0f0f0f-0000"),
    ],
)
def test_each_rule_fails_a_new_hit(repo: Path, rule: str, text: str) -> None:
    _put(repo, "docs/note.md", f"see {text}\n")
    errors = sc.check(repo)
    assert len(errors) == 1, errors
    assert "docs/note.md: 1 scrub hits, over no allowance" in errors[0]
    assert f"line 1: {rule}" in errors[0]


def test_placeholders_and_trailer_mentions_pass(repo: Path) -> None:
    _put(
        repo,
        "docs/ok.md",
        f"{PLACEHOLDER}\n~/git/proj\nclaude.ai/install\nstrip the Claude-Session: trailer\n"
        "/Users/ alone\nsee /home/you.\nC:\\Users\\you\\x\nclaude.ai/artifact/abc\n*.ts.net\n",
    )
    assert sc.check(repo) == []


@pytest.mark.parametrize("sep", ["\f", "\r", "\x0b", "\x85", "\u2028"])
def test_a_hit_after_a_control_character_inside_a_line_is_seen(
    repo: Path, sep: str
) -> None:
    _put(repo, "log.txt", f"progress{sep}{HOME}\n")
    assert any("log.txt: 1 scrub hits" in e for e in sc.check(repo))


def test_a_path_with_a_colon_and_a_non_ascii_name_parse(repo: Path) -> None:
    _put(repo, "a:b.md", f"{HOME}\n")
    _put(repo, "\u00fcn.md", f"{HOME}\n")
    errors = "\n".join(sc.check(repo))
    assert "a:b.md: 1 scrub hits" in errors
    assert "\u00fcn.md: 1 scrub hits" in errors


def test_the_durations_file_is_exempt(repo: Path) -> None:
    _put(repo, ".test_durations.json", f'{{"{HOME}": 1}}\n')
    assert sc.check(repo) == []


def test_untracked_file_is_not_scanned(repo: Path) -> None:
    (repo / "draft.md").write_text(HOME + "\n")
    assert sc.check(repo) == []


def test_grandfathered_count_holds_growth_fails_and_shrink_must_lower(
    repo: Path,
) -> None:
    _put(repo, "scripts/spikes/out.jsonl", f"{HOME}\n{HOME}\n")
    _allow(repo, "user you\nscripts/spikes/out.jsonl 2\n")
    assert sc.check(repo) == []
    _put(repo, "scripts/spikes/out.jsonl", f"{HOME}\n{HOME}\n{HOME}\n")
    assert any("3 scrub hits, over its ceiling of 2" in e for e in sc.check(repo))
    _put(repo, "scripts/spikes/out.jsonl", f"{HOME}\n")
    assert any("below its ceiling of 2; lower it" in e for e in sc.check(repo))


def test_two_hits_on_one_line_count_twice(repo: Path) -> None:
    _put(repo, "a.md", f"{HOME} and {TAILNET}\n")
    _allow(repo, "user you\na.md 1\n")
    assert any("2 scrub hits, over its ceiling of 1" in e for e in sc.check(repo))


def test_update_lowers_and_drops_freely_and_refuses_raise_and_new(repo: Path) -> None:
    _put(repo, "a.md", f"{HOME}\n")
    _allow(repo, "user you\na.md 3\ngone.md 1\n")
    assert sc.update_exceptions(repo, allow_raise=False) == []
    assert sc.read_allowlist(repo / sc.ALLOWLIST) == ({"you"}, {"a.md": 1})
    _put(repo, "a.md", f"{HOME}\n{HOME}\n")
    _put(repo, "b.md", f"{CHAT}\n")
    before = (repo / sc.ALLOWLIST).read_text()
    refusals = "\n".join(sc.update_exceptions(repo, allow_raise=False))
    assert "a.md: raise 1 -> 2" in refusals
    assert "b.md: new entry at 1 hits" in refusals
    assert (repo / sc.ALLOWLIST).read_text() == before, (
        "a refused update writes nothing"
    )
    assert sc.update_exceptions(repo, allow_raise=True) == []
    assert sc.read_allowlist(repo / sc.ALLOWLIST)[1] == {"a.md": 2, "b.md": 1}
    assert sc.check(repo) == []
    assert b"\r\n" not in (repo / sc.ALLOWLIST).read_bytes()


def _commit(root: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            "c",
        ],
        check=True,
    )


def test_rev_scans_the_commit_not_the_working_tree(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "--", sc.ALLOWLIST], check=True)
    _commit(repo)
    (repo / "src/clean.py").write_text(
        f"x = '{HOME}'\n"
    )  # uncommitted edit to a tracked file
    assert sc.check(repo, rev="HEAD") == [], "the push gate judges what is pushed"
    assert sc.check(repo) != []
    subprocess.run(["git", "-C", str(repo), "add", "--", "src/clean.py"], check=True)
    _commit(repo)
    (repo / "src/clean.py").write_text("x = 1\n")
    assert any("src/clean.py: 1 scrub hits" in e for e in sc.check(repo, rev="HEAD"))
    assert sc.check(repo) == []


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_base_mode_scans_the_range_messages(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "--", sc.ALLOWLIST], check=True)
    _commit(repo)
    base = _head(repo)
    _put(repo, "src/more.py", "y = 2\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-qm",
            f"work\n\n{TRAILER}",
        ],
        check=True,
    )
    errors = sc.check(repo, rev="HEAD", base=base)
    assert len(errors) == 1 and "message: session" in errors[0], errors
    assert sc.check(repo, rev="HEAD") == [], "without --base only the files are judged"


def test_base_mode_refuses_allowlist_widening(repo: Path) -> None:
    _put(repo, "a.md", f"{HOME}\n")
    _allow(repo, "user you\na.md 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", sc.ALLOWLIST], check=True)
    _commit(repo)
    base = _head(repo)
    _put(repo, "a.md", f"{HOME}\n{HOME}\n")
    _put(repo, "b.md", "/Users/" + "jdoe2/x\n")
    _allow(repo, "user you\nuser jdoe2\na.md 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", sc.ALLOWLIST], check=True)
    _commit(repo)
    assert sc.check(repo, rev="HEAD") == [], "the tip is self-consistent"
    errors = "\n".join(sc.check(repo, rev="HEAD", base=base))
    assert "allowlist adds user jdoe2" in errors
    assert "allowlist raises a.md 1 -> 2" in errors
    assert sc.widening(({"you"}, {}), ({"you"}, {"c.md": 1})) == [
        "allowlist adds c.md at 1"
    ]
    assert sc.widening(({"you"}, {"a.md": 2}), ({"you"}, {"a.md": 1})) == [], (
        "lowering is no widening"
    )


def test_the_first_push_against_a_base_without_an_allowlist_is_not_a_widening(
    repo: Path,
) -> None:
    _commit(repo)  # base: src/clean.py only, no allowlist committed
    base = _head(repo)
    _put(repo, "a.md", f"{HOME}\n")
    _allow(repo, "user you\na.md 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", sc.ALLOWLIST], check=True)
    _commit(repo)
    assert sc.check(repo, rev="HEAD", base=base) == [], (
        "the commit that introduces the gate pushes"
    )


def test_a_bad_rev_is_a_clean_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        sc.main(["--root", str(repo), "check", "--rev", "no-such-ref"]) == sc.EXIT_FOUND
    )
    assert "'no-such-ref' is not a commit" in capsys.readouterr().err


def test_the_allowlist_itself_is_not_scanned(repo: Path) -> None:
    _allow(repo, f"user you\n# example {HOME}\n")
    subprocess.run(["git", "-C", str(repo), "add", "--", sc.ALLOWLIST], check=True)
    assert sc.check(repo) == []


def test_malformed_and_duplicate_lines_are_refused(repo: Path) -> None:
    for text in ("a.md lots\n", "user\n", "a.md 1\na.md 2\n", "user x\nuser x\n"):
        _allow(repo, text)
        with pytest.raises(SystemExit, match="expected one"):
            sc.read_allowlist(repo / sc.ALLOWLIST)


def test_an_empty_repo_is_an_error(tmp_path: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    assert any("no tracked files" in e for e in sc.check(tmp_path))


def test_main_exit_codes(repo: Path) -> None:
    assert sc.main(["--root", str(repo), "check"]) == sc.EXIT_OK
    _put(repo, "a.md", f"{HOME}\n")
    assert sc.main(["--root", str(repo), "check"]) == sc.EXIT_FOUND
    assert sc.main(["--root", str(repo), "update-exceptions"]) == sc.EXIT_FOUND
    assert (
        sc.main(["--root", str(repo), "update-exceptions", "--allow-raise"])
        == sc.EXIT_OK
    )
    assert sc.main(["--root", str(repo), "check"]) == sc.EXIT_OK


def test_the_allowlist_is_fast_lane_eligible() -> None:
    """A plugin-only commit that lowers a count must stay in the plugin-only lane, which refuses
    scripts/; so the allowlist lives under tests/."""
    spec = importlib.util.spec_from_file_location(
        "elig", _REPO / "scripts" / "check_plugin_only_eligibility.py"
    )
    assert spec and spec.loader
    elig = importlib.util.module_from_spec(spec)
    sys.modules["elig"] = elig
    spec.loader.exec_module(elig)
    change = elig.Change("M", sc.ALLOWLIST, "100644", "100644")
    assert elig.classify(change, "base", "head", _REPO)[1] is None


@pytest.mark.lint
def test_repo_is_scrubbed_or_grandfathered() -> None:
    users, _ = sc.read_allowlist(_REPO / sc.ALLOWLIST)
    # Non-vacuity that survives a fully drained allowlist: the loaded rules still see a planted leak,
    # and the scan sees the tree.
    assert "you" in users, "allowlist did not load"
    planted = {r for r, _ in sc.hits_in(f"{HOME} {TAILNET} {CHAT} {TRAILER}", users)}
    assert planted == {"home", "tailnet", "chat", "session"}
    assert sc.tracked_count(_REPO) > 1000
    errors = sc.check(_REPO)
    assert errors == [], "\n".join(errors)
