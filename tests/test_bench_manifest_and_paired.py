# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bench manifest, the refusal to compare across manifests, and paired alternating
A/B timing compared by median (nexus-3ceda.11; ``scripts/bench/manifest.py``,
``scripts/bench/paired.py``, ``scripts/bench/runner.py``)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bench.manifest import ManifestMismatch, build_manifest, comparable, engine_build, host_class
from bench.paired import run_paired, summarize
from bench.runner import compare_reports, main, run_paired_bench
from bench.schema import Query


class _Clock:
    """Advances only when an arm runs, by the arm's scripted duration."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _arm(clock: _Clock, log: list[str], name: str, durations: list[float]):
    def run() -> None:
        log.append(name)
        clock.now += durations.pop(0)
    return run


class TestHostAndEngine:
    def test_a_lab_run_grant_is_named_by_its_size(self) -> None:
        cls = host_class({"LAB_CPUS": "16", "LAB_MEM_MB": "49152"})
        assert cls.startswith("lab-run 16c/49152M ")

    def test_without_a_grant_the_host_is_unmanaged(self) -> None:
        assert host_class({}).startswith("unmanaged ")

    def test_a_garbled_grant_is_not_trusted(self) -> None:
        assert host_class({"LAB_CPUS": "8", "LAB_MEM_MB": "lots"}).startswith("unmanaged ")

    def test_no_host_name_is_recorded(self) -> None:
        """The class is built only from the grant, the core count and the platform."""
        import os
        import platform

        system = f"{platform.system().lower()}-{platform.machine().lower()}"
        assert host_class({}) == f"unmanaged {os.cpu_count() or 0}c {system}"
        assert host_class({"LAB_CPUS": "8", "LAB_MEM_MB": "16384"}) == f"lab-run 8c/16384M {system}"

    def test_the_engine_is_its_release_or_unstamped(self) -> None:
        assert engine_build(lambda: (0, 1, 183)) == "release 0.1.183"
        assert engine_build(lambda: None) == "unstamped"


def _repo(tmp_path: Path) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        *args], check=True, capture_output=True)
    git("init", "-q")
    (tmp_path / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "init")
    return tmp_path


class TestBuildManifest:
    def test_it_records_the_sha_and_whether_the_tree_was_clean(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        m = build_manifest(root, env={}, probe=lambda: (0, 1, 1))
        assert m["git_sha"] == sha and m["git_dirty"] is False
        assert m["engine_build"] == "release 0.1.1" and m["host_class"].startswith("unmanaged ")
        (root / "f.txt").write_text("two\n")
        assert build_manifest(root, env={}, probe=lambda: None)["git_dirty"] is True

    def test_no_value_in_the_manifest_names_the_host(self, tmp_path: Path) -> None:
        import platform
        import socket

        names = {n.split(".")[0].lower() for n in (socket.gethostname(), platform.node()) if n}
        m = build_manifest(_repo(tmp_path), env={}, probe=lambda: None)
        words = {w.lower() for v in m.values() for w in str(v).replace("/", " ").split()}
        assert not names & words, f"the manifest names the host: {names & words}"

    def test_outside_a_checkout_the_sha_is_unknown_and_nothing_raises(self, tmp_path: Path) -> None:
        """A ``lab-remote --no-git`` copy has no .git; neither key is compared, so the result
        still compares with one from the same host and engine."""
        m = build_manifest(tmp_path, env={}, probe=lambda: None)
        assert m["git_sha"] == "unknown" and m["git_dirty"] is None
        assert m["cpu_model"]


_M = {"git_sha": "a" * 40, "host_class": "lab-run 8c/16384M linux-x86_64", "cpu_model": "AMD EPYC 7763",
      "engine_build": "release 0.1.183"}


class TestComparable:
    def test_two_shas_on_one_host_and_engine_compare(self) -> None:
        comparable({"manifest": _M}, {"manifest": {**_M, "git_sha": "b" * 40}})

    @pytest.mark.parametrize("key,value", [
        ("host_class", "unmanaged 10c darwin-arm64"), ("engine_build", "unstamped"),
        # One grant size on two different hosts: the CPU tells them apart.
        ("cpu_model", "Intel Core i7-12700"),
    ])
    def test_a_different_host_or_engine_is_refused_naming_it(self, key: str, value: str) -> None:
        with pytest.raises(ManifestMismatch, match=key):
            comparable({"manifest": _M}, {"manifest": {**_M, key: value}})

    def test_a_report_without_a_manifest_is_refused(self) -> None:
        with pytest.raises(ManifestMismatch, match="no manifest"):
            comparable({"manifest": _M}, {"by_path": {}})
        with pytest.raises(ManifestMismatch, match="no manifest"):
            comparable({"manifest": _M}, {"manifest": None})


class TestRunPaired:
    def test_the_arms_alternate_abba_and_every_run_is_timed(self) -> None:
        clock, log = _Clock(), []
        samples = run_paired(_arm(clock, log, "a", [1.0, 2.0, 3.0]), _arm(clock, log, "b", [4.0, 5.0, 6.0]),
                             3, clock=clock)
        assert log == ["a", "b", "b", "a", "a", "b"]
        assert samples == {"a": [1.0, 2.0, 3.0], "b": [4.0, 5.0, 6.0]}

    def test_fewer_than_one_pair_is_refused(self) -> None:
        with pytest.raises(ValueError):
            run_paired(lambda: None, lambda: None, 0)


class TestSummarize:
    def test_medians_paired_difference_and_every_sample_kept(self) -> None:
        a = [1.0, 1.1, 0.9, 1.0, 9.0]          # one slow outlier in A
        b = [0.8, 0.9, 0.7, 0.85, 0.75]
        s = summarize({"a": a, "b": b})
        assert s["median_a_s"] == 1.0 and s["median_b_s"] == 0.8
        assert s["median_paired_diff_s"] == pytest.approx(-0.2)
        assert s["b_faster_pairs"] == 5
        assert s["outliers"] == {"a": 1, "b": 0}
        assert s["samples_s"]["a"] == a, "the outlier is kept, not trimmed"

    def test_below_four_samples_outliers_are_not_computed(self) -> None:
        assert summarize({"a": [1.0, 2.0], "b": [1.0, 9.0]})["outliers"] == {"a": None, "b": None}

    def test_unequal_counts_are_refused(self) -> None:
        with pytest.raises(ValueError):
            summarize({"a": [1.0], "b": []})


def _q(qid: str) -> Query:
    return Query(qid=qid, category="factual", text=qid)


class TestRunPairedBench:
    def test_two_paths_per_query_with_a_median_of_paired_differences(self) -> None:
        clock = _Clock()

        def path(cost: float):
            def handler(q: Query) -> dict:
                clock.now += cost
                return {}
            return handler

        out = run_paired_bench([_q("q1"), _q("q2")], handlers={"A": path(2.0), "C": path(1.5)}, pairs=3, clock=clock)
        assert (out["a"], out["b"], out["pairs"]) == ("A", "C", 3)
        assert out["median_of_query_paired_diffs_s"] == pytest.approx(-0.5)
        assert out["b_faster_queries"] == 2
        assert out["per_query"]["q1"]["samples_s"]["a"] == [2.0, 2.0, 2.0]

    def test_it_takes_exactly_two_paths(self) -> None:
        with pytest.raises(ValueError, match="exactly two"):
            run_paired_bench([_q("q")], handlers={"A": lambda q: {}}, pairs=1)

    def test_a_path_that_returns_an_error_row_fails_the_ab(self) -> None:
        """The live handlers (bench.paths) report a failed retrieval as a row whose error is
        set, never by raising; timed, it would read as a fast path."""
        with pytest.raises(RuntimeError, match="path B failed on query q: engine unreachable"):
            run_paired_bench([_q("q")], handlers={"A": lambda q: {}, "B": lambda q: {"error": "engine unreachable"}},
                             pairs=2)

    def test_an_empty_error_still_fails_the_ab(self) -> None:
        """Path A's error is nx's stderr, which can be empty on a non-zero exit."""
        with pytest.raises(RuntimeError, match="path A failed on query q"):
            run_paired_bench([_q("q")], handlers={"A": lambda q: {"error": ""}, "B": lambda q: {}}, pairs=2)

    def test_a_path_that_raises_fails_the_ab(self) -> None:
        def broken(q: Query) -> dict:
            raise RuntimeError("path down")

        with pytest.raises(RuntimeError, match="path down"):
            run_paired_bench([_q("q")], handlers={"A": lambda q: {}, "B": broken}, pairs=2)


def _report(manifest: dict, ndcg: float, elapsed: float) -> dict:
    return {"manifest": manifest, "by_path": {"A": {"mean_ndcg_at_3": ndcg, "mean_ndcg_at_3_scored": None,
                                                     "mean_elapsed_s": elapsed, "errors": 0}}}


_GOLDEN = Path(__file__).resolve().parents[1] / "bench" / "queries" / "spike_5q.yaml"


def _cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() with stand-in paths and a manifest that probes no engine."""
    import bench.runner as runner

    monkeypatch.setattr(runner, "_live_handlers", lambda corpus, scope_b: {
        "A": lambda q: {"chunks": []}, "B": lambda q: {"chunks": []}, "C": lambda q: {"chunks": []}})
    monkeypatch.setattr(runner, "build_manifest", lambda root: dict(_M))


class TestMainWritesTheManifest:
    def test_a_sweep_report_carries_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _cli(monkeypatch)
        out = tmp_path / "r.json"
        assert main([str(_GOLDEN), "--out", str(out), "--paths", "A"]) == 0
        assert json.loads(out.read_text())["manifest"] == _M

    def test_a_paired_report_carries_it_with_the_arms_in_the_order_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _cli(monkeypatch)
        out = tmp_path / "r.json"
        assert main([str(_GOLDEN), "--out", str(out), "--paths", "C,A", "--pairs", "2"]) == 0
        report = json.loads(out.read_text())
        assert report["manifest"] == _M
        assert (report["a"], report["b"], report["pairs"]) == ("C", "A", 2)

    @pytest.mark.parametrize("argv", [
        ["--paths", "A,B,C", "--pairs", "2"], ["--paths", "A", "--pairs", "2"],
        ["--paths", "A,B", "--pairs", "3"], ["--paths", "A,B", "--pairs", "0"],
    ], ids=["three-paths", "one-path", "odd-pairs", "zero-pairs"])
    def test_a_bad_pairs_request_is_a_usage_error_before_any_work(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str],
    ) -> None:
        import bench.runner as runner

        monkeypatch.setattr(runner, "_live_handlers", lambda *a: pytest.fail("built handlers for a bad request"))
        with pytest.raises(SystemExit) as info:
            main([str(_GOLDEN), *argv])
        assert info.value.code == 2


class TestCompare:
    def test_it_reports_each_path_old_and_new(self) -> None:
        out = compare_reports(_report(_M, 0.5, 2.0), _report({**_M, "git_sha": "b" * 40}, 0.6, 1.5))
        assert out["paths"]["A"]["mean_ndcg_at_3"] == {"old": 0.5, "new": 0.6}
        assert out["paths"]["A"]["mean_elapsed_s"] == {"old": 2.0, "new": 1.5}

    def test_the_cli_refuses_reports_from_different_hosts(self, tmp_path: Path, capsys) -> None:
        old, new = tmp_path / "old.json", tmp_path / "new.json"
        old.write_text(json.dumps(_report(_M, 0.5, 2.0)))
        new.write_text(json.dumps(_report({**_M, "host_class": "unmanaged 10c darwin-arm64"}, 0.6, 1.5)))
        assert main(["--compare", str(old), str(new)]) == 2
        assert "refused" in capsys.readouterr().err

    def test_a_paired_report_is_refused_not_compared_as_empty(self) -> None:
        with pytest.raises(ManifestMismatch, match="not a sweep report"):
            compare_reports({"manifest": _M, "per_query": {}}, _report(_M, 0.6, 1.5))

    def test_the_cli_compares_reports_from_one_host(self, tmp_path: Path, capsys) -> None:
        old, new = tmp_path / "old.json", tmp_path / "new.json"
        old.write_text(json.dumps(_report(_M, 0.5, 2.0)))
        new.write_text(json.dumps(_report(_M, 0.6, 1.5)))
        assert main(["--compare", str(old), str(new)]) == 0
        assert json.loads(capsys.readouterr().out)["paths"]["A"]["mean_ndcg_at_3"]["new"] == 0.6
