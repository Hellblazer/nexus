# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx identity`` (RDR-253 Step 4, nexus-hczdo.1.5): the take-over guards, release, promote, rename, purge, show,
alive and create. Tenant-free: a fake Claude registry tree under ``tmp_path`` (never the real ``~/.claude``), a
process table that is a dict, and :class:`tests.identity.fake_engine.FakeEngine` standing in for the engine. What
the real engine records for these calls is checked by ``tests/identity/test_identity_step4.py``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from nexus.commands.identity_cmd import identity_group
from nexus.identity.bind_client import IdentityApiError
from nexus.identity.ops import CallerFacts, IdentityEnv
from nexus.identity.token_file import read_token_file, token_path
from tests.identity.fake_engine import FakeEngine
from tests.identity.fakes import HOST, FakeProcesses

T0 = "2026-11-17T08:00:00Z"
T1 = "2026-11-17T09:00:00Z"


class Box:
    """One test's machine: a process table, an engine, and the facts about the caller."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = FakeEngine()
        self.procs = FakeProcesses(tmp_path / "claude-sessions", host=HOST)
        self.identity_dir = tmp_path / "config" / "identity"
        self.facts = CallerFacts(claude_pid=0, ancestors=())
        self.comm: dict[int, str] = {}                 # pid -> command name; every other pid is "claude"

    def env(self) -> IdentityEnv:
        return IdentityEnv(client=self.engine, procs=self.procs, identity_dir=self.identity_dir, environ={},
                           caller=lambda: self.facts, clock=lambda: 1_000.0, client_version="9.9.9-test",
                           command_name=lambda pid: self.comm.get(pid, "claude"))

    def run(self, *args: str) -> Result:
        return CliRunner().invoke(identity_group, list(args), obj={"identity_env": self.env()})

    def holder(self, pid: int, *, session_id: str = "s-0", started_at: int | None = None, host: str = HOST,
               os_start: str = T0) -> dict[str, Any]:
        return {"host": host, "pid": pid, "started_at": started_at if started_at is not None else pid * 1000,
                "proc_start": os_start, "session_id": session_id, "pane": None}

    def running_seat(self, name: str, pid: int, *, entrypoint: str = "cli", generation: int = 1, **kw: Any) -> str:
        """A seat held by a running Claude process *pid*, as the engine and the registry would show it."""
        self.procs.start(pid, T0, session_id=f"sid-{pid}", name=name, entrypoint=entrypoint)
        return self.engine.stage(kind="seat", name=name, state="live", generation=generation,
                                 holder=self.holder(pid, session_id=f"sid-{pid}"), **kw)

    def caller(self, pid: int, ancestors: tuple[int, ...] = (), *, entrypoint: str = "cli", registry: bool = True) -> None:
        """The command runs under Claude process *pid*; *ancestors* are its ancestor pids, nearest first, ending at
        *pid* itself or past it. ``registry=False`` is a plain shell: no registry file for the pid."""
        if registry:
            self.procs.start(pid, T1, session_id=f"sid-{pid}", name="", entrypoint=entrypoint)
        else:
            self.procs.procs[pid] = T1
        self.facts = CallerFacts(claude_pid=pid, ancestors=ancestors or (pid,))


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def binds(box: Box) -> list[tuple[Any, ...]]:
    return box.engine.ids_called("bind")


# ── take-over ───────────────────────────────────────────────────────────────


def test_take_over_from_claude_p_child_is_refused(box: Box) -> None:
    """The first test of the step: a ``claude -p`` child has ``entrypoint: sdk-cli`` though its kind is interactive."""
    box.running_seat("alpha", 100)
    box.caller(200, entrypoint="sdk-cli")
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code != 0
    assert "sdk-cli" in result.output and "interactive" in result.output
    assert binds(box) == [], "nothing is bound"


def test_take_over_from_a_descendant_of_the_holder_is_refused(box: Box) -> None:
    box.running_seat("alpha", 100)
    box.caller(200, ancestors=(200, 150, 100, 1))
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code != 0 and "ancestor" in result.output
    assert binds(box) == []


def test_take_over_from_a_plain_shell_is_refused(box: Box) -> None:
    box.running_seat("alpha", 100)
    box.caller(300, registry=False)          # find_immediate_claude_pid fell back to the shell itself
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code != 0 and "no Claude" in result.output
    assert binds(box) == []


def test_take_over_of_a_seat_the_callers_own_claude_process_holds_is_not_refused_as_an_ancestor(box: Box) -> None:
    """The holder is the caller's own Claude process, which is in its ancestor chain but is not its own ancestor: the
    ancestor guard exempts it, and the take-over of what the process already holds goes through."""
    alpha = box.engine.stage(kind="seat", name="alpha", state="live", generation=2,
                             holder=box.holder(200, session_id="sid-200"))
    box.caller(200, ancestors=(200, 150, 1))
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code == 0 and "ancestor" not in result.output, result.output
    assert [(b[0], b[1]) for b in binds(box)] == [(alpha, "takeover")]
    assert box.engine.bound(alpha)["generation"] == 2, "the same process: no new generation"


@pytest.mark.parametrize("how", ["stale-file-pid-reused", "stale-file-process-gone", "comm-is-not-claude"])
def test_a_registry_file_alone_is_not_a_claude_ancestor(box: Box, how: str) -> None:
    """File presence never decides: a crashed Claude leaves its file behind. The pid must run with the OS start time the
    file records, and be a claude."""
    box.running_seat("alpha", 100)
    box.caller(200)
    if how == "stale-file-pid-reused":
        box.procs.procs[200] = "2026-11-17T12:00:00Z"        # the pid now belongs to a later process
    elif how == "stale-file-process-gone":
        box.procs.stop(200)                                    # exited: its registry file is left behind
    else:
        box.comm[200] = "zsh"
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code != 0 and "no Claude session ancestor" in result.output
    assert binds(box) == []


def test_a_take_over_that_loses_the_compare_and_set_reads_the_seat_again_once(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    box.caller(200)
    real_bind, raced = box.engine.bind, {"n": 0}

    def racing(identity: str, event: str, generation: int, evidence: Any = None) -> Any:
        if event == "takeover" and raced["n"] == 0:
            raced["n"] += 1
            box.engine.records[alpha]["generation"] += 1           # another claimant got in between the read and the bind
        return real_bind(identity, event, generation, evidence)

    box.engine.bind = racing                                        # type: ignore[method-assign]
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code == 0, result.output
    takeovers = [b for b in binds(box) if b[1] == "takeover"]
    assert [b[2] for b in takeovers] == [1, 2], "the second attempt carries the generation read again"
    # and a conflict that persists is an error after the second attempt, not a loop
    box.engine.bind = lambda *a, **k: (_ for _ in ()).throw(IdentityApiError(409, "generation_conflict", "stale", {}))  # type: ignore[method-assign]
    again = box.run("claim", "alpha", "--take-over")
    assert again.exit_code != 0 and "generation_conflict" in again.output


def test_take_over_needs_the_flag(box: Box) -> None:
    box.running_seat("alpha", 100)
    box.caller(200)
    assert box.run("claim", "alpha").exit_code != 0
    assert binds(box) == []


def test_a_take_over_binds_the_callers_process_ends_its_ephemeral_and_writes_its_token_file(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    box.caller(200)
    eph = box.engine.stage(kind="ephemeral", state="live", generation=1, holder=box.holder(200, session_id="sid-200"))
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code == 0, result.output
    assert [(b[0], b[1]) for b in binds(box)] == [(alpha, "takeover"), (eph, "end")], "the claim first, then the close"
    rec = box.engine.bound(alpha)
    assert (rec["generation"], rec["state"], rec["holder"]["pid"]) == (2, "live", 200)
    assert box.engine.bound(eph)["state"] == "ended"
    token = read_token_file(token_path(box.identity_dir, 200, 200_000))
    assert token is not None and (token.identity, token.generation, token.token) == (alpha, 2, f"tok-{alpha}-2")
    assert token.registry_dir == str(box.procs.sessions_dir) and "sid-200" in token.session_ids


def test_a_take_over_releases_the_seat_the_caller_held(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    box.caller(200)
    beta = box.engine.stage(kind="seat", name="beta", state="live", generation=3, holder=box.holder(200, session_id="sid-200"))
    result = box.run("claim", "alpha", "--take-over")
    assert result.exit_code == 0, result.output
    assert [(b[0], b[1]) for b in binds(box)] == [(alpha, "takeover"), (beta, "release")]
    assert box.engine.bound(beta)["state"] == "unheld"


def test_a_take_over_of_an_unknown_seat_binds_nothing(box: Box) -> None:
    box.caller(200)
    result = box.run("claim", "nosuch", "--take-over")
    assert result.exit_code != 0 and "nosuch" in result.output
    assert binds(box) == []


# ── release ─────────────────────────────────────────────────────────────────


def test_release_from_a_plain_shell_frees_a_gone_holders_seat(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    box.procs.stop(100)                        # the holder crashed: its registry file is left behind
    box.facts = CallerFacts(claude_pid=0, ancestors=())      # a plain shell, no Claude ancestor at all
    result = box.run("release", "alpha")
    assert result.exit_code == 0, result.output
    assert box.engine.bound(alpha)["state"] == "unheld"
    (_, event, generation, evidence), = binds(box)
    assert (event, generation) == ("release", 1) and evidence["holder_gone"]["pid"] == 100


def test_release_is_refused_while_the_holder_runs(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    box.facts = CallerFacts(claude_pid=0, ancestors=())
    result = box.run("release", "alpha")
    assert result.exit_code != 0 and "running" in result.output
    assert binds(box) == [] and box.engine.bound(alpha)["state"] == "live"


def test_release_of_a_holder_on_another_host_is_refused_while_its_lease_is_live(box: Box) -> None:
    alpha = box.engine.stage(kind="seat", name="alpha", state="live", generation=2,
                             holder=box.holder(900, host="elsewhere"), reserved_until=2_000.0)
    result = box.run("release", "alpha")
    assert result.exit_code != 0 and "elsewhere" in result.output
    assert binds(box) == [] and box.engine.bound(alpha)["state"] == "live"


def test_release_of_a_holder_on_another_host_after_its_reservation(box: Box) -> None:
    alpha = box.engine.stage(kind="seat", name="alpha", state="lapsed", generation=2,
                             holder=box.holder(900, host="elsewhere"), reserved_until=500.0)
    result = box.run("release", "alpha")
    assert result.exit_code == 0, result.output
    assert box.engine.bound(alpha)["state"] == "unheld"


def test_release_of_a_seat_nobody_holds_is_a_noop(box: Box) -> None:
    box.engine.stage(kind="seat", name="alpha", state="unheld")
    result = box.run("release", "alpha")
    assert result.exit_code == 0 and "not held" in result.output
    assert binds(box) == []


def test_release_when_the_check_cannot_judge_the_holder_is_refused(box: Box) -> None:
    """A holder whose pid runs but whose start time cannot be read is unknown, never gone."""
    alpha = box.running_seat("alpha", 100)
    box.procs.os_start = lambda pid: None      # type: ignore[method-assign]
    result = box.run("release", "alpha")
    assert result.exit_code != 0 and "claim" in result.output
    assert binds(box) == [] and box.engine.bound(alpha)["state"] == "live"


# ── promote, rename, purge ──────────────────────────────────────────────────


def test_promote_turns_an_ephemeral_into_a_seat(box: Box) -> None:
    eph = box.engine.stage(kind="ephemeral", state="live", generation=1, holder=box.holder(200))
    result = box.run("promote", eph, "gamma")
    assert result.exit_code == 0, result.output
    assert box.engine.bound(eph)["kind"] == "seat" and box.engine.aliases["gamma"]["identity"] == eph
    assert "gamma" in result.output


def test_promote_refuses_an_id_shaped_alias_without_asking_the_engine(box: Box) -> None:
    eph = box.engine.stage(kind="ephemeral", state="live", generation=1, holder=box.holder(200))
    result = box.run("promote", eph, "i7")
    assert result.exit_code != 0 and "i<digits>" in result.output
    assert box.engine.ids_called("promote") == []


def test_promote_of_a_seat_is_the_engines_refusal(box: Box) -> None:
    seat = box.engine.stage(kind="seat", name="alpha")
    result = box.run("promote", seat, "beta")
    assert result.exit_code != 0 and "not_ephemeral" in result.output


def test_rename_moves_the_alias_engine_side(box: Box) -> None:
    alpha = box.engine.stage(kind="seat", name="alpha")
    result = box.run("rename", "alpha", "omega")
    assert result.exit_code == 0, result.output
    assert box.engine.bound(alpha)["name"] == "omega"
    assert box.engine.aliases["alpha"]["state"] == "retired" and box.engine.aliases["omega"]["state"] == "live"
    assert "omega" in result.output


def test_rename_refuses_an_id_shaped_new_name(box: Box) -> None:
    box.engine.stage(kind="seat", name="alpha")
    result = box.run("rename", "alpha", "i9")
    assert result.exit_code != 0 and box.engine.ids_called("rename") == []


def test_purge_is_refused_for_a_live_identity(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    result = box.run("purge", alpha, "--operator")
    assert result.exit_code != 0 and "live" in result.output
    assert box.engine.ids_called("purge") == [] and box.engine.purged == []


def test_purge_needs_the_operator_flag(box: Box) -> None:
    ended = box.engine.stage(kind="ephemeral", state="ended", generation=1)
    assert box.run("purge", ended).exit_code != 0
    assert box.engine.ids_called("purge") == []


def test_purge_of_an_ended_identity_and_of_a_lapsed_one_whose_holder_is_gone(box: Box) -> None:
    ended = box.engine.stage(kind="ephemeral", state="ended", generation=1)
    lapsed = box.engine.stage(kind="ephemeral", state="lapsed", generation=1, gone_at="2026-11-18T00:00:00Z",
                              holder=box.holder(300))
    assert box.run("purge", ended, "--operator").exit_code == 0
    assert box.run("purge", lapsed, "--operator").exit_code == 0
    assert box.engine.purged == [ended, lapsed]


def test_purge_of_a_lapsed_identity_with_no_recorded_gone_is_refused(box: Box) -> None:
    lapsed = box.engine.stage(kind="ephemeral", state="lapsed", generation=1, holder=box.holder(300))
    result = box.run("purge", lapsed, "--operator")
    assert result.exit_code != 0 and "gone" in result.output and box.engine.purged == []


# ── show, show --all, alive, create ─────────────────────────────────────────


def test_show_prints_the_identity_its_binding_and_recent_rows(box: Box) -> None:
    alpha = box.running_seat("alpha", 100, client_version="8.4.0")
    box.engine.logs[alpha] = [{"seq": i, "event": "start" if i == 1 else "clear", "generation": 1, "session_id": f"sid-{i}"}
                              for i in range(1, 9)]
    result = box.run("show", "alpha")
    assert result.exit_code == 0, result.output
    for text in (alpha, "alpha", "generation 1", "live", "pid 100", "8.4.0", "start", "clear"):
        assert text in result.output
    assert result.output.count("session sid-") == 5, "the most recent five rows"


def test_show_with_no_argument_names_the_callers_identity(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    box.caller(100, entrypoint="cli")
    box.procs.write_registry(100, 100_000, T0, session_id="sid-100", name="alpha")
    assert alpha in box.run("show").output
    box.facts = CallerFacts(claude_pid=0, ancestors=())
    result = box.run("show")
    assert result.exit_code != 0 and "no identity" in result.output


def test_a_delegated_childs_sentinel_token_file_means_no_identity_not_the_parents(box: Box, tmp_path: Path) -> None:
    """NX_IDENTITY_TOKEN naming a file that holds no identity (the sentinel a dispatch with no delegated identity
    gets) is "no identity"; the resolver would otherwise fall through to the session id and the Claude ancestor and
    answer the parent's."""
    from nexus.identity.ops import own_identity

    box.running_seat("alpha", 100)
    box.caller(100)
    box.procs.write_registry(100, 100_000, T1, session_id="sid-100", name="alpha")
    env = box.env()
    assert own_identity(env) is not None, "without the variable the process is the seat's"
    env.environ = {"NX_IDENTITY_TOKEN": str(tmp_path / "identity" / "delegated" / "none")}
    assert own_identity(env) is None


def test_show_all_lists_a_live_process_with_no_binding(box: Box) -> None:
    box.running_seat("alpha", 100, client_version="8.4.0")
    box.procs.start(101, T0, session_id="sid-101", name="stray")          # live Claude, never bound
    box.procs.start(102, T0, session_id="sid-102", name="dead")
    box.procs.stop(102)                                                    # crashed: a registry file, no process
    result = box.run("show", "--all")
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output and "8.4.0" in result.output
    assert "pid 101" in result.output and "no binding" in result.output
    assert "pid 102" not in result.output
    assert "pid 100" in result.output.split("no binding")[0], "the bound one is listed apart from the unbound"


def test_show_all_leaves_out_claude_p_children_and_a_dead_process_whose_pid_was_reused(box: Box) -> None:
    box.running_seat("alpha", 100)
    box.procs.start(101, T0, session_id="sid-101", name="child", entrypoint="sdk-cli")      # a claude -p child: no binding of its own
    box.procs.start(102, T0, session_id="sid-102", name="reused")
    box.procs.procs[102] = T1                  # the registry file is a dead process's: its pid now belongs to a later one
    box.procs.start(103, T0, session_id="sid-103", name="stray")
    out = box.run("show", "--all").output
    assert "pid 103" in out and "stray" in out
    assert "pid 101" not in out and "pid 102" not in out
    assert "no binding" in out


def test_the_phase2_gate_passes_when_every_live_process_is_bound_at_the_floor(box: Box) -> None:
    from nexus.identity.ops import phase2_gate

    box.running_seat("alpha", 100, client_version="8.5.5")
    box.running_seat("desk", 101, entrypoint="claude-desktop", client_version="8.5.0")
    box.procs.start(102, T0, session_id="sid-102", name="child", entrypoint="sdk-cli")   # a claude -p child: not counted
    ok, lines = phase2_gate(box.env(), floor="8.5.0")
    assert ok, lines
    assert lines[-1].startswith("PHASE2_GATE_OPEN"), lines


def test_the_phase2_gate_refuses_an_unbound_live_process_desktop_included(box: Box) -> None:
    from nexus.identity.ops import phase2_gate

    box.running_seat("alpha", 100, client_version="8.5.5")
    box.procs.start(101, T0, session_id="sid-101", name="kestrel", entrypoint="claude-desktop")   # live, never bound
    ok, lines = phase2_gate(box.env(), floor="8.5.0")
    assert not ok
    text = "\n".join(lines)
    assert "PHASE2_GATE_CLOSED" in text and "pid 101" in text and "kestrel" in text and "no binding" in text


@pytest.mark.parametrize("version", ["8.4.9", None, "unknown", "8.5.0rc1"])
def test_the_phase2_gate_refuses_a_binding_below_the_floor_or_with_no_version(box: Box, version: str | None) -> None:
    from nexus.identity.ops import phase2_gate

    box.running_seat("old", 100, client_version=version)
    ok, lines = phase2_gate(box.env(), floor="8.5.0")
    assert not ok
    assert any("old" in line and "below 8.5.0" in line for line in lines), lines


def test_the_phase2_gate_does_not_count_a_binding_whose_holder_is_gone(box: Box) -> None:
    from nexus.identity.ops import phase2_gate

    box.running_seat("alpha", 100, client_version="8.5.5")
    box.running_seat("sandbox", 101, client_version="8.4.0")
    box.procs.stop(101)                                     # a lapsed sandbox ephemeral: no process, nothing to gate
    ok, lines = phase2_gate(box.env(), floor="8.5.0")
    assert ok, lines
    assert any("sandbox" in line and "not counted" in line for line in lines), lines


def test_the_phase2_gate_script_exits_by_the_verdict(box: Box, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("rdr253_phase2_gate", Path(__file__).parents[1] / "scripts" / "rdr253_phase2_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    box.running_seat("alpha", 100, client_version="8.5.5")
    assert gate.main([], env=box.env()) == 0
    box.procs.start(101, T0, session_id="sid-101", name="stray")
    assert gate.main([], env=box.env()) == 1
    assert "pid 101" in capsys.readouterr().out


def test_show_all_with_a_seat_argument_is_a_usage_error(box: Box) -> None:
    assert box.run("show", "--all", "alpha").exit_code != 0


def test_alive_reports_gone_holders_and_skips_the_callers_own_binding(box: Box) -> None:
    crashed = box.running_seat("crashed", 100)
    running = box.running_seat("running", 101)
    mine = box.running_seat("mine", 102)
    box.caller(102)
    box.procs.stop(100)
    box.procs.stop(102)                                      # "mine" looks gone but is the caller's own process
    result = box.run("alive")
    assert result.exit_code == 0, result.output
    assert [c[0] for c in box.engine.ids_called("gone")] == [crashed]
    assert box.engine.bound(crashed)["state"] == "lapsed"
    assert box.engine.bound(running)["state"] == "live" and box.engine.bound(mine)["state"] == "live"


def test_alive_of_one_seat_runs_the_check_and_records_a_gone_verdict(box: Box) -> None:
    crashed = box.running_seat("crashed", 100)
    running = box.running_seat("running", 101)
    box.procs.stop(100)
    assert "gone" in box.run("alive", "crashed").output
    assert box.engine.bound(crashed)["state"] == "lapsed"
    assert "running" in box.run("alive", "running").output
    assert box.engine.bound(running)["state"] == "live"
    assert [c[0] for c in box.engine.ids_called("gone")] == [crashed]


def test_alive_says_so_when_the_engine_stops_answering_while_it_records_a_gone(box: Box) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    crashed = box.running_seat("crashed", 100)
    box.procs.stop(100)

    def down(*a: Any, **k: Any) -> Any:
        raise IdentityUnavailableError("ConnectError")

    box.engine.gone = down                                  # type: ignore[method-assign]
    result = box.run("alive", "crashed")
    assert result.exit_code == 0 and "gone" in result.output and "not recorded" in result.output
    assert box.engine.bound(crashed)["state"] == "live"


def test_alive_by_pid(box: Box) -> None:
    box.running_seat("alpha", 100)
    assert "running" in box.run("alive", "100").output
    result = box.run("alive", "555")
    assert result.exit_code != 0 and "no binding" in result.output


def test_alive_checks_a_delegated_identity_by_its_dispatchers_pid_and_start_time(box: Box) -> None:
    box.procs.start(400, T0, registry=False)                 # the dispatcher: a process with no registry file
    live = box.engine.stage(kind="delegated", state="live", generation=1, parent="i1", holder=box.holder(400))
    dead = box.engine.stage(kind="delegated", state="live", generation=1, parent="i1", holder=box.holder(401))
    reused = box.engine.stage(kind="delegated", state="live", generation=1, parent="i1", holder=box.holder(402, os_start=T0))
    box.procs.procs[402] = T1                                # pid 402 now belongs to a later process
    result = box.run("alive")
    assert result.exit_code == 0, result.output
    assert sorted(c[0] for c in box.engine.ids_called("gone")) == sorted([dead, reused])
    assert box.engine.bound(live)["state"] == "live"


def test_create_makes_a_seat_created_by_sam(box: Box) -> None:
    result = box.run("create", "alpha")
    assert result.exit_code == 0 and "alpha" in result.output
    assert box.engine.ids_called("create_seat") == [("alpha", "sam", None)]
    assert box.run("create", "alpha").exit_code != 0           # the name is taken
    assert box.run("create", "i4").exit_code != 0


def test_create_for_the_restart_script_records_who_made_it(box: Box) -> None:
    assert box.run("create", "beta", "--created-by", "restart").exit_code == 0
    assert box.engine.ids_called("create_seat") == [("beta", "restart", None)]
    assert box.run("create", "gamma", "--created-by", "nobody").exit_code != 0


def test_show_by_session_id_finds_the_identity_a_session_belongs_to(box: Box) -> None:
    alpha = box.running_seat("alpha", 100)
    result = box.run("show", "--session", "sid-100", "--json")
    assert result.exit_code == 0 and f'"id": "{alpha}"' in result.output
    missing = box.run("show", "--session", "sid-none", "--json")
    assert missing.exit_code != 0


def test_the_wording_the_restart_script_reads(box: Box) -> None:
    """scripts/claude-restart-sessions tells "no such seat" and "no such session" from an engine that is down by these
    two codes in the command's output (tests/scripts/test_claude_restart_sessions_seats.py stands in for the command)."""
    assert "unknown_name" in box.run("show", "nosuch").output
    assert "unknown_session" in box.run("show", "--session", "sid-none", "--json").output
    assert box.run("show", "nosuch").exit_code != 0
