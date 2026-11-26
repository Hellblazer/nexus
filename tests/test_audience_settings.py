# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings reader (nexus-sby49.8, RDR-259 Decisions 1 to 4): ``SETTINGS``, the file checks, the problem list, cleaning.

The two byte copies of ``_audience.py`` are driven by every test here (``audience`` is parametrised over them). The
table-driven cases (every failure row, the precedence matrix, the ``HEAD`` union) live in ``tests/fixtures/audience_corpus.json``
and ``tests/hooks/test_audience_parity.py``; this file holds what a corpus row cannot say: the module's shape, the hang-free
reads, the cleaning bounds, the notice text, the once-per-session log, the labels and the did-you-mean.
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import threading

import pytest

from tests._module_seam import setattr_in

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_COPY = ROOT / "conexus" / "hooks" / "scripts" / "_audience.py"
SRC_COPY = ROOT / "src" / "nexus" / "hooks" / "_audience.py"
ADVICE_ROWS_FILE = ROOT / "conexus" / "hooks" / "scripts" / "routing" / "destructive_action_advice.py"


@pytest.fixture
def audience(audience_copy):
    """One byte copy of the reader, pinned to an empty account home (``audience.HOME``); no ``CLAUDE_PROJECT_DIR``."""
    return audience_copy


def _repo(path: pathlib.Path, **project) -> pathlib.Path:
    (path / ".git").mkdir(parents=True)
    if project:
        (path / ".nexus").mkdir()
        (path / ".nexus" / "project.json").write_text(json.dumps({"version": 1, **project}), encoding="utf-8")
    return path


def _user(home: pathlib.Path, body: object) -> pathlib.Path:
    f = home / ".config" / "nexus" / "conexus.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return f


def _probs(s) -> list[tuple[str, str]]:
    return sorted((p.file, p.key) for p in s.problems)


# ---------------------------------------------------------------------------
# The module's shape
# ---------------------------------------------------------------------------


def test_settings_is_the_one_table_of_ten_keys(audience):
    assert list(audience.SETTINGS) == [
        "version", "subagent_git", "advice_block", "brief_questions", "preflight", "ci_wait", "credential_helper",
        "integration_branch", "telemetry", "t1_nudge",
    ]
    s = audience.SETTINGS
    assert [k for k, v in s.items() if v.project and not v.user] == ["preflight", "ci_wait", "credential_helper", "integration_branch"]
    assert [k for k, v in s.items() if v.user and not v.project] == ["telemetry", "t1_nudge"]
    assert all(s[k].project and s[k].user for k in ("version", "subagent_git", "advice_block", "brief_questions"))
    assert {k: s[k].limit for k in ("preflight", "ci_wait", "credential_helper", "integration_branch")} == {
        "preflight": 160, "ci_wait": 200, "credential_helper": 600, "integration_branch": 100,
    }
    assert s["brief_questions"].limit == 160 and audience.MAX_BRIEF_QUESTIONS == 3
    assert s["subagent_git"].failure == "strict" and s["telemetry"].failure is False and s["t1_nudge"].failure is True
    assert s["subagent_git"].default == "seatbelt" and s["telemetry"].default is False and s["t1_nudge"].default is True


def test_the_advice_row_ids_are_the_advice_tables(audience):
    """A row id is a public contract once a user writes it; the reader's list is the advice table's, row for row."""
    tree = ast.parse(ADVICE_ROWS_FILE.read_text(encoding="utf-8"))
    ids: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Row" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                ids.append(first.value)
    assert len(ids) >= 8, f"found {len(ids)} Row(...) ids in the advice table; the probe went blind"
    assert sorted(audience.ADVICE_ROW_IDS) == sorted(ids)


def test_the_module_is_stdlib_only_and_reads_one_environment_variable():
    for path in (PLUGIN_COPY, SRC_COPY):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stdlib = set(__import__("sys").stdlib_module_names)
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.level == 0:
                imported.add((n.module or "").split(".")[0])
        assert imported <= stdlib | {"__future__"}, imported - stdlib
        reads = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == "environ" and getattr(n.value, "id", "") == "os":
                reads.append(n)
        src = path.read_text(encoding="utf-8")
        assert reads, "the probe found no os.environ use at all (CLAUDE_PROJECT_DIR is read)"
        # The names may appear where telemetry_child_env strips or sets them in a copy of the environment it is GIVEN
        # (RDR-259 Decision 9); what is forbidden is reading them from this process: no ``os.environ[...]``, no getenv.
        for n in ast.walk(tree):
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "environ":
                raise AssertionError("the reader may only .get the CLAUDE_PROJECT_DIR variable")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("getenv", "putenv"):
                raise AssertionError("the reader reads no environment variable but CLAUDE_PROJECT_DIR")
        assert src.count("CLAUDE_PROJECT_DIR") >= 1
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("get", "pop", "setdefault", "copy"):
                if isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "environ":
                    if n.func.attr == "get":
                        assert isinstance(n.args[0], ast.Constant) and n.args[0].value == "CLAUDE_PROJECT_DIR"
                    else:
                        raise AssertionError("the reader may only .get the CLAUDE_PROJECT_DIR variable")


# ---------------------------------------------------------------------------
# Defaults and the shape of a read
# ---------------------------------------------------------------------------


def test_a_plain_repo_reads_every_default_with_no_problem(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values == {
        "subagent_git": "seatbelt", "advice_block": [], "brief_questions": [], "preflight": None, "ci_wait": None,
        "credential_helper": None, "integration_branch": None, "telemetry": False, "t1_nudge": True,
    }
    assert s.problems == [] and s.sources["subagent_git"] == "default"
    assert audience.project_settings({"cwd": str(repo)}) == {}
    assert audience.user_settings() == {
        "subagent_git": "seatbelt", "advice_block": [], "brief_questions": [], "telemetry": False, "t1_nudge": True,
    }


def test_a_valid_project_file_gives_every_key_and_its_source(audience, tmp_path):
    repo = _repo(tmp_path / "r", subagent_git="strict", advice_block=["pattern_kill"], brief_questions=["One?"], preflight="tools/pre.sh",
                 ci_wait="ci-wait <sha>", credential_helper="Use tools/cred.sh.", integration_branch="develop")
    p = {"cwd": str(repo)}
    s = audience.read_settings(p)
    assert s.problems == []
    assert s.values["preflight"] == "tools/pre.sh" and s.values["ci_wait"] == "ci-wait <sha>"
    assert s.values["credential_helper"] == "Use tools/cred.sh." and s.values["integration_branch"] == "develop"
    assert s.sources["subagent_git"] == "project" and s.sources["preflight"] == "project"
    assert audience.project_text(p, "preflight") == "tools/pre.sh" and audience.integration_branch(p) == "develop"
    assert audience.project_brief_questions(p) == ["One?"] and audience.advice_blocked(p) == frozenset({"pattern_kill"})


def test_a_user_file_gives_the_user_only_keys(audience, tmp_path):
    _user(audience.HOME, {"version": 1, "subagent_git": "strict", "telemetry": True, "t1_nudge": False, "advice_block": ["whole_disk_scan"],
                          "brief_questions": ["Q?"]})
    repo = _repo(tmp_path / "r")
    assert audience.telemetry_enabled() is True and audience.t1_nudge_enabled() is False
    assert audience.guard_mode({"cwd": str(repo)}) == "strict"
    assert audience.advice_blocked({"cwd": str(repo)}) == frozenset({"whole_disk_scan"})
    assert audience.user_brief_questions() == ["Q?"]


def test_a_project_file_cannot_set_a_user_only_key_and_a_user_file_cannot_set_a_project_only_key(audience, tmp_path):
    repo = _repo(tmp_path / "r", telemetry=True, t1_nudge=False, subagent_git="seatbelt")
    _user(audience.HOME, {"preflight": "x", "ci_wait": "y", "credential_helper": "z", "integration_branch": "main"})
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["telemetry"] is False and s.values["t1_nudge"] is True
    assert s.values["preflight"] is None and s.values["ci_wait"] is None and s.values["integration_branch"] is None
    assert _probs(s) == sorted([
        (".nexus/project.json", "telemetry"), (".nexus/project.json", "t1_nudge"),
        ("conexus.json", "preflight"), ("conexus.json", "ci_wait"), ("conexus.json", "credential_helper"),
        ("conexus.json", "integration_branch"),
    ])


# ---------------------------------------------------------------------------
# File checks: they never hang, never follow a project link, follow a user link
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="FIFOs are POSIX")
def test_a_fifo_where_the_project_file_belongs_returns_promptly_and_reads_strict(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    os.mkfifo(repo / ".nexus" / "project.json")
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(audience.guard_mode({"cwd": str(repo)})), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the reader blocked opening a FIFO"
    assert out == ["strict"]
    assert _probs(audience.read_settings({"cwd": str(repo)})) == [(".nexus/project.json", "")]


@pytest.mark.skipif(os.name == "nt", reason="FIFOs are POSIX")
def test_a_fifo_behind_a_user_link_returns_promptly_and_reads_strict(audience, tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    f = audience.HOME / ".config" / "nexus" / "conexus.json"
    f.parent.mkdir(parents=True)
    f.symlink_to(fifo)
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(audience.guard_mode({"cwd": str(_repo(tmp_path / "r"))})), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive() and out == ["strict"]


def test_a_megabyte_project_file_is_not_read(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text('{"version": 1}' + " " * (1 << 20))
    assert audience.guard_mode({"cwd": str(repo)}) == "strict"
    assert _probs(audience.read_settings({"cwd": str(repo)})) == [(".nexus/project.json", "")]


def test_the_size_cap_is_64_kib_exactly(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    body = '{"version": 1}'
    f = repo / ".nexus" / "project.json"
    f.write_text(body + " " * (65536 - len(body)))
    assert audience.read_settings({"cwd": str(repo)}).problems == []
    f.write_text(body + " " * (65537 - len(body)))
    assert audience.guard_mode({"cwd": str(repo)}) == "strict"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_project_symlink_is_not_followed_but_a_user_symlink_is(audience, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"version": 1, "advice_block": ["pattern_kill"]}))
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").symlink_to(target)
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["subagent_git"] == "strict" and s.values["advice_block"] == []
    assert _probs(s) == [(".nexus/project.json", "")]
    f = audience.HOME / ".config" / "nexus" / "conexus.json"
    f.parent.mkdir(parents=True)
    f.symlink_to(target)
    plain = _repo(tmp_path / "plain")
    u = audience.read_settings({"cwd": str(plain)})
    assert u.values["advice_block"] == ["pattern_kill"] and u.problems == []


def test_a_byte_order_mark_is_read_normally_with_no_problem(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"version": 1, "subagent_git": "strict"}).encode())
    _user(audience.HOME, "")
    f = audience.HOME / ".config" / "nexus" / "conexus.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"telemetry": True}).encode())
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["subagent_git"] == "strict" and s.values["telemetry"] is True and s.problems == []


def test_an_absent_file_is_no_problem_and_no_account_home_is_one_environment_problem(audience, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    assert audience.read_settings({"cwd": str(repo)}).problems == []
    monkeypatch.setattr(audience, "account_home", lambda: None)
    s = audience.read_settings({"cwd": str(repo)})
    assert _probs(s) == [("environment", "")] and s.problems[0].category == "environment"
    assert audience.guard_mode({"cwd": str(repo)}) == "seatbelt" and audience.telemetry_enabled() is False


# ---------------------------------------------------------------------------
# Validation rejects; cleaning is for the notice text only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,value", [
    ("preflight", "run\nthis"), ("preflight", "run\x07this"), ("preflight", "x" * 161), ("ci_wait", "y" * 201),
    ("credential_helper", "z" * 601), ("integration_branch", "a b"), ("integration_branch", "a..b"), ("integration_branch", "-x"),
    ("integration_branch", "b" * 101), ("preflight", 5), ("ci_wait", ["a"]),
])
def test_a_bad_text_is_rejected_whole_never_cleaned_into_a_value(audience, tmp_path, key, value):
    repo = _repo(tmp_path / "r", **{key: value})
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values[key] is None, "a rejected text must be absent, not a cleaned copy of itself"
    assert _probs(s) == [(".nexus/project.json", key)]


def test_the_text_limits_count_utf8_bytes(audience, tmp_path):
    ok = "é" * 80  # 160 bytes
    repo = _repo(tmp_path / "ok", preflight=ok)
    assert audience.read_settings({"cwd": str(repo)}).values["preflight"] == ok
    bad = _repo(tmp_path / "bad", preflight="é" * 81)  # 81 characters, 162 bytes
    assert audience.read_settings({"cwd": str(bad)}).values["preflight"] is None


def test_an_empty_text_is_unset_and_surrounding_space_is_dropped(audience, tmp_path):
    repo = _repo(tmp_path / "r", preflight="   ", ci_wait="  wait <sha>  \n")
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["preflight"] is None and s.values["ci_wait"] == "wait <sha>" and s.problems == []


def test_unknown_keys_get_a_did_you_mean_and_dollar_keys_are_silent(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, "subagent-git": "strict", "$comment": "hi", "$schema": "x", "zzz": 1}))
    s = audience.read_settings({"cwd": str(repo)})
    assert _probs(s) == [(".nexus/project.json", "subagent-git"), (".nexus/project.json", "zzz")]
    by_key = {p.key: p for p in s.problems}
    assert "subagent_git" in by_key["subagent-git"].what and "did you mean" in by_key["subagent-git"].what
    assert "did you mean" not in by_key["zzz"].what
    assert s.values["subagent_git"] == "seatbelt", "a misspelt key is ignored, not honoured"


def test_a_problem_names_the_file_the_key_what_was_wrong_and_what_is_used_instead(audience, tmp_path):
    repo = _repo(tmp_path / "r", subagent_git="loose")
    (p,) = audience.read_settings({"cwd": str(repo)}).problems
    assert (p.file, p.key) == (".nexus/project.json", "subagent_git")
    assert "loose" in p.what and p.used == "strict" and p.forced_strict is True and p.category == "settings"


def test_the_cut_of_file_controlled_text_in_a_problem_is_60_bytes_and_clean(audience, tmp_path):
    nasty = "k\x1b[31m" + "é" * 80 + "‮\n"
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, nasty: 1}))
    (p,) = audience.read_settings({"cwd": str(repo)}).problems
    text = audience.format_problem(p)
    assert "\x1b" not in text and "‮" not in text and "\n" not in text
    quoted = text.split('"')[1] if '"' in text else ""
    assert 0 < len(quoted.encode("utf-8")) <= 60, text


def test_clean_text_removes_controls_bidi_and_cuts_on_a_character_boundary(audience):
    assert audience.clean_text("a\x00b\x1bc‮d e") == "abcde"
    cut = audience.clean_text("é" * 100, 60)
    assert len(cut.encode("utf-8")) <= 60 and cut.endswith("...")
    cut.encode("utf-8").decode("utf-8")  # a clean boundary
    assert audience.clean_text("short") == "short"
    assert audience.quoted('a"b') .startswith('"') and audience.quoted('a"b').endswith('"')


def test_brief_questions_merge_project_then_user_drop_duplicates_and_cap_at_three_with_a_problem_per_extra(audience, tmp_path):
    repo = _repo(tmp_path / "r", brief_questions=["P1?", "P2?"])
    _user(audience.HOME, {"brief_questions": ["P2?", "U1?", "U2?"]})
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["brief_questions"] == ["P1?", "P2?", "U1?"]
    assert audience.project_brief_questions({"cwd": str(repo)}) == ["P1?", "P2?"]
    assert audience.user_brief_questions({"cwd": str(repo)}) == ["U1?"]
    assert audience.user_brief_questions() == ["P2?", "U1?", "U2?"][:3], "without a payload the user's own list is returned, capped"
    assert _probs(s) == [("conexus.json", "brief_questions")]


def test_a_list_item_of_the_wrong_type_or_an_unknown_advice_id_is_dropped_with_a_problem_each(audience, tmp_path):
    repo = _repo(tmp_path / "r", advice_block=["pattern_kill", 3, "no_such_row", None, "pattern_kill"], brief_questions="one")
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["advice_block"] == ["pattern_kill"] and s.values["brief_questions"] == []
    assert _probs(s) == sorted([(".nexus/project.json", "advice_block")] * 3 + [(".nexus/project.json", "brief_questions")])


def test_version_rules(audience, tmp_path):
    bad = [(".nexus/project.json", "version")]
    for version, probs in ((1, []), (2, bad), (0, bad), ("1", bad), (True, bad)):
        repo = _repo(tmp_path / ("v" + str(version) + type(version).__name__))
        (repo / ".nexus").mkdir()
        (repo / ".nexus" / "project.json").write_text(json.dumps({"version": version, "subagent_git": "strict"}))
        s = audience.read_settings({"cwd": str(repo)})
        assert _probs(s) == probs, version
        assert s.values["subagent_git"] == "strict", "known keys are read whatever the version"


def test_a_missing_version_is_fine(audience, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps({"subagent_git": "strict"}))
    s = audience.read_settings({"cwd": str(repo)})
    assert s.problems == [] and s.values["subagent_git"] == "strict"


# ---------------------------------------------------------------------------
# Failure values answer in the safe direction; readers never raise
# ---------------------------------------------------------------------------


def test_every_reader_answers_its_failure_value_when_the_read_raises(audience, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(audience, "read_settings", boom)
    p = {"cwd": str(tmp_path)}
    assert audience.guard_mode(p) == "strict" and audience.guard_mode(p, head=True) == "strict"
    assert audience.telemetry_enabled() is False and audience.t1_nudge_enabled() is True
    assert audience.advice_blocked(p) == frozenset() and audience.project_text(p, "ci_wait") is None
    assert audience.project_brief_questions(p) == [] and audience.user_brief_questions() == []
    assert audience.integration_branch(p) is None
    assert audience.forced_strict_clause(p) is None


def test_read_settings_itself_never_raises_and_reads_strict_when_a_stage_blows_up(audience, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r", advice_block=["pattern_kill"])

    def boom(*a, **k):
        raise RuntimeError("broken")

    setattr_in(monkeypatch, audience, "os.path.realpath", boom)
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["subagent_git"] == "strict"
    assert any(p.forced_strict for p in s.problems)


def test_the_home_lookup_raising_leaves_the_user_defaults_and_one_problem(audience, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(audience, "account_home", boom)
    repo = _repo(tmp_path / "r")
    s = audience.read_settings({"cwd": str(repo)})
    assert s.values["telemetry"] is False and s.values["subagent_git"] == "seatbelt"
    assert [p.category for p in s.problems] == ["environment"]


# ---------------------------------------------------------------------------
# The strict clause, the log, the labels
# ---------------------------------------------------------------------------


def test_forced_strict_clause_names_the_file_and_the_reason_and_is_silent_otherwise(audience, tmp_path):
    good = _repo(tmp_path / "good", subagent_git="strict")
    assert audience.forced_strict_clause({"cwd": str(good)}) is None
    bad = _repo(tmp_path / "bad")
    (bad / ".nexus").mkdir()
    (bad / ".nexus" / "project.json").write_text("{not json")
    clause = audience.forced_strict_clause({"cwd": str(bad)})
    assert clause is not None and clause.startswith("the full guard applies because ")
    assert ".nexus/project.json" in clause and "could not be read" in clause and "\n" not in clause


def test_note_problems_logs_each_problem_once_per_session(audience, tmp_path):
    logs = audience.HOME / ".config" / "nexus" / "logs"
    logs.mkdir(parents=True)
    repo = _repo(tmp_path / "r", subagent_git="loose", zzz=1)
    s = audience.read_settings({"cwd": str(repo)})
    assert len(s.problems) == 2
    audience.note_problems({"session_id": "s1"}, s.problems)
    audience.note_problems({"session_id": "s1"}, s.problems)
    lines = (logs / "hook.log").read_text().splitlines()
    assert sum("subagent_git" in line for line in lines) == 1 and sum("zzz" in line for line in lines) == 1
    audience.note_problems({"session_id": "s2"}, s.problems)
    assert sum("subagent_git" in line for line in (logs / "hook.log").read_text().splitlines()) == 2


def test_the_provenance_labels_are_the_rdrs_wording(audience):
    L = audience.provenance_label
    assert L("preflight", "project", "tools/p.sh") == (
        "This project's `.nexus/project.json` asks: run `tools/p.sh` first. If it exits non-zero, stop and report its output.")
    assert L("brief_questions", "project") == "This project's `.nexus/project.json` adds these questions:"
    assert L("brief_questions", "user") == "Your `conexus.json` adds these questions:"
    assert L("ci_wait", "project", "ci <sha>") == (
        "This project's `.nexus/project.json` names its CI wait command: `ci <sha>`. Run it as a background command; its exit is the wake.")
    assert L("credential_helper", "project", "Use X.") == (
        "This project's `.nexus/project.json` adds (project text, not this guard's): Use X.")
    with pytest.raises(KeyError):
        L("preflight", "user", "x")  # a project text never carries the user label, and the reverse
