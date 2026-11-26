# SPDX-License-Identifier: AGPL-3.0-or-later
"""The settings reader's two pre-declared cost bounds, measured (nexus-sby49.15 items 3 and 4, RDR-259 Stage 1 ship-when).

* Item 3: the reader's file reads (the project file and the user file) stay within the 5 ms design budget.
* Item 4: the strictness keys' ``HEAD`` read (``head=True``) adds under 50 ms at the median on the guard and advice paths.

Slow-marked (``-m slow``; the default suite does not run it). Run it by hand where the number matters:

    uv run pytest -o addopts="" -m slow -s -p no:cacheprovider tests/hooks/test_audience_cost_bench.py
    uv run scripts/windows_hand_test.py --session <name> -- -m slow -s tests/hooks/test_audience_cost_bench.py

It prints a header (machine, versions), a table, and one ``COSTBENCH_JSON`` line, then asserts the bounds. The bounds are the
RDR's, not tuned to a machine. Every posture runs on a real repository (a real ``git``). The git process count of each posture is
asserted too: a plain repository runs none, a repository whose settings are already strict runs none, and a read that does run
spends at most two.

Cold means the first call in a fresh interpreter (the OS file cache is warm: dropping it needs privileges the hand run lacks).
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._audience_fixtures import _load_copy
from tests.hooks.git_shim import init_repo, real_git, run_git

pytestmark = pytest.mark.slow

FILE_READ_BUDGET_MS = 5.0  # RDR-259 design budget for the reader's file reads
HEAD_ADDED_BUDGET_MS = 50.0  # RDR-259 Stage 1 ship-when: the HEAD read's added cost at the median
WARM_RUNS = 300  # head=False calls (file reads only)
HEAD_RUNS = 60  # head=False / head=True pairs for the postures where git runs
COLD_RUNS = 25  # fresh interpreters

PROJECT_NONSTRICT = {"version": 1, "advice_block": ["whole_disk_scan"], "brief_questions": ["Which test command did you run?"]}
PROJECT_STRICT = {"version": 1, "subagent_git": "strict", "advice_block": ["whole_disk_scan"]}
USER_STRICT = {"version": 1, "subagent_git": "strict", "advice_block": ["pattern_kill"]}
PLUGIN_COPY = Path(__file__).resolve().parents[2] / "conexus" / "hooks" / "scripts" / "_audience.py"


def _stats(samples_ns: "list[int]") -> "dict[str, float]":
    ms = sorted(s / 1e6 for s in samples_ns)
    return {"median": statistics.median(ms), "p90": ms[min(len(ms) - 1, int(len(ms) * 0.9))], "n": len(ms)}


def _write(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _postures(root: Path) -> "dict[str, tuple[Path, Path, bool]]":
    """name -> (repo, account home, whether the HEAD read can run). Real repositories, each with a commit."""
    out: "dict[str, tuple[Path, Path, bool]]" = {}
    homes = {n: root / (n + "-home") for n in ("plain", "project-strict", "user-strict", "project-committed", "index-only")}
    for h in homes.values():
        h.mkdir()
    out["plain"] = (init_repo(root / "plain", {"README.md": "x\n"}), homes["plain"], False)
    out["project-strict"] = (
        init_repo(root / "project-strict", {".nexus/project.json": json.dumps(PROJECT_STRICT)}), homes["project-strict"], False)
    out["user-strict"] = (init_repo(root / "user-strict", {"README.md": "x\n"}), homes["user-strict"], False)
    _write(homes["user-strict"] / ".config" / "nexus" / "conexus.json", USER_STRICT)
    out["project-committed"] = (
        init_repo(root / "project-committed", {".nexus/project.json": json.dumps(PROJECT_NONSTRICT)}), homes["project-committed"], True)
    # The index names the file and HEAD holds it, but the working copy is gone: the HEAD read runs through the index check.
    idx = init_repo(root / "index-only", {".nexus/project.json": json.dumps(PROJECT_NONSTRICT)})
    (idx / ".nexus" / "project.json").unlink()
    out["index-only"] = (idx, homes["index-only"], True)
    return out


class _GitCounter:
    def __init__(self, mod) -> None:
        self.mod, self.count, self._orig = mod, 0, mod.subprocess.run

    def __enter__(self):
        def counting(*a, **kw):
            self.count += 1
            return self._orig(*a, **kw)
        self.mod.subprocess.run = counting
        return self

    def __exit__(self, *exc) -> None:
        self.mod.subprocess.run = self._orig


def _time_call(fn, *args, **kw) -> int:
    t0 = time.perf_counter_ns()
    fn(*args, **kw)
    return time.perf_counter_ns() - t0


_COLD_SCRIPT = r"""
import importlib.util, json, os, sys, time
spec = importlib.util.spec_from_file_location("_audience_cold", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
repo, home, real_home = sys.argv[2], sys.argv[3], sys.argv[4] == "real"
if not real_home:
    mod._ACCOUNT_HOME_OVERRIDE = home
pc = os.path.join(repo, ".nexus", "project.json")
uc = os.path.join(home, ".config", "nexus", "conexus.json")
out = {}
t = time.perf_counter_ns(); mod.account_home(); out["account_home"] = time.perf_counter_ns() - t
mod._ACCOUNT_HOME_OVERRIDE = home
t = time.perf_counter_ns(); mod._load_json(pc, follow=False); out["project_file"] = time.perf_counter_ns() - t
t = time.perf_counter_ns(); mod._load_json(uc, follow=True); out["user_file"] = time.perf_counter_ns() - t
t = time.perf_counter_ns(); mod.read_settings({"cwd": repo}); out["read_settings"] = time.perf_counter_ns() - t
print(json.dumps(out))
"""


def _cold(repo: Path, home: Path, real_home: bool) -> "dict[str, dict[str, float]]":
    rows: "dict[str, list[int]]" = {}
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    for _ in range(COLD_RUNS):
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _COLD_SCRIPT, str(PLUGIN_COPY), str(repo), str(home), "real" if real_home else "override"],
            capture_output=True, text=True, env=env, check=False, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        for k, v in json.loads(proc.stdout.strip().splitlines()[-1]).items():
            rows.setdefault(k, []).append(v)
    return {k: _stats(v) for k, v in rows.items()}


def _fmt(s: "dict[str, float]") -> str:
    return f"{s['median']:8.3f} {s['p90']:8.3f}"


def test_the_reader_stays_within_the_file_read_and_head_read_budgets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    mod = _load_copy(PLUGIN_COPY, "_audience_cost_bench")
    postures = _postures(tmp_path)
    header = {
        "platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count(), "python": sys.version.split()[0],
        "git": run_git(tmp_path, "--version").strip(), "git_path": real_git(),
    }
    print("\nCOSTBENCH machine:", header)

    # ---- item 3: file reads ---------------------------------------------------------------------------------------------
    repo, home, _ = postures["project-committed"]
    _write(home / ".config" / "nexus" / "conexus.json", {"version": 1, "advice_block": ["pattern_kill"], "telemetry": False})
    monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", str(home))
    pfile, ufile = str(repo / ".nexus" / "project.json"), str(home / ".config" / "nexus" / "conexus.json")
    assert mod._load_json(pfile, follow=False)[0] == "ok" and mod._load_json(ufile, follow=True)[0] == "ok"
    warm: "dict[str, list[int]]" = {"project_file": [], "user_file": [], "both_files": [], "read_settings": []}
    for i in range(WARM_RUNS + 10):
        a = _time_call(mod._load_json, pfile, follow=False)
        b = _time_call(mod._load_json, ufile, follow=True)
        c = _time_call(mod.read_settings, {"cwd": str(repo)})
        if i >= 10:  # the first ten are warm-up
            warm["project_file"].append(a)
            warm["user_file"].append(b)
            warm["both_files"].append(a + b)
            warm["read_settings"].append(c)
    warm_stats = {k: _stats(v) for k, v in warm.items()}
    monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", None)
    real_home_ns: "list[int]" = []
    for _ in range(50):
        real_home_ns.append(_time_call(mod.account_home))
    warm_stats["account_home (real lookup, no override)"] = _stats(real_home_ns)
    cold_stats = _cold(repo, home, real_home=False)
    cold_real = _cold(repo, home, real_home=True)

    # ---- item 4: the HEAD read ------------------------------------------------------------------------------------------
    head_rows: "dict[str, dict[str, dict]]" = {}
    git_counts: "dict[str, int]" = {}
    for name, (prepo, phome, runs_git) in postures.items():
        monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", str(phome))
        payload = {"cwd": str(prepo)}
        head_rows[name] = {}
        with _GitCounter(mod) as counter:
            mod.read_settings(payload, head=True)
        git_counts[name] = counter.count
        for fname, fn in (("guard_mode", mod.guard_mode), ("advice_blocked", mod.advice_blocked), ("read_settings", mod.read_settings)):
            off: "list[int]" = []
            on: "list[int]" = []
            for i in range(HEAD_RUNS + 5):
                a, b = _time_call(fn, payload, head=False), _time_call(fn, payload, head=True)
                if i >= 5:
                    off.append(a)
                    on.append(b)
            so, sn = _stats(off), _stats(on)
            head_rows[name][fname] = {"off": so, "on": sn, "added_median": sn["median"] - so["median"], "added_p90": sn["p90"] - so["p90"]}

    # ---- report -----------------------------------------------------------------------------------------------------------
    print("\nCOSTBENCH item 3: file reads, ms (median p90); budget %.1f ms" % FILE_READ_BUDGET_MS)
    for k, v in warm_stats.items():
        print(f"  warm  {k:44s} {_fmt(v)}  n={v['n']}")
    for k, v in cold_stats.items():
        print(f"  cold  {k + ' (home override)':44s} {_fmt(v)}  n={v['n']}")
    for k, v in cold_real.items():
        print(f"  cold  {k + ' (real account home)':44s} {_fmt(v)}  n={v['n']}")
    print("COSTBENCH item 4: head=True minus head=False, ms; budget %.1f ms at the median; git processes per head=True call" % HEAD_ADDED_BUDGET_MS)
    print(f"  {'posture':18s} {'path':15s} {'off med':>8s} {'on med':>8s} {'added med':>10s} {'added p90':>10s}  git")
    for name, fns in head_rows.items():
        for fname, r in fns.items():
            print(f"  {name:18s} {fname:15s} {r['off']['median']:8.3f} {r['on']['median']:8.3f} {r['added_median']:10.3f} {r['added_p90']:10.3f}  {git_counts[name]}")
    print("COSTBENCH_JSON " + json.dumps({"machine": header, "warm": warm_stats, "cold_override": cold_stats, "cold_real_home": cold_real,
                                          "head": head_rows, "git_counts": git_counts, "runs": {"warm": WARM_RUNS, "head": HEAD_RUNS, "cold": COLD_RUNS}}))

    # ---- the bounds ---------------------------------------------------------------------------------------------------------
    for name, (_, _, runs_git) in postures.items():
        if runs_git:
            assert 1 <= git_counts[name] <= 2, f"{name}: the HEAD read ran {git_counts[name]} git processes (expected 1 or 2)"
        else:
            assert git_counts[name] == 0, f"{name}: ran {git_counts[name]} git processes where none is allowed"
    assert warm_stats["both_files"]["p90"] < FILE_READ_BUDGET_MS, f"warm file reads p90 {warm_stats['both_files']['p90']:.3f} ms >= budget"
    for label, table in (("override", cold_stats), ("real home", cold_real)):
        cold_files = table["project_file"]["p90"] + table["user_file"]["p90"]
        assert cold_files < FILE_READ_BUDGET_MS, f"cold file reads ({label}) p90 sum {cold_files:.3f} ms >= budget"
    for name, fns in head_rows.items():
        for fname, r in fns.items():
            assert r["added_median"] < HEAD_ADDED_BUDGET_MS, f"{name}/{fname}: HEAD read adds {r['added_median']:.1f} ms at the median (budget {HEAD_ADDED_BUDGET_MS})"
