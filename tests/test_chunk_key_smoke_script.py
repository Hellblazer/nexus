# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.66: the logic of ``scripts/chunk_key_enforcement_smoke.py`` that needs no engine.

The live run is by hand against a local engine started with ``NX_CHUNK_KEY_CHECK_MODE=enforce``; what is pinned
here is what must hold before it touches anything and what its verdicts mean: the loopback, port and enforce-mode
guards, the child environment's allowlist, the positive controls on all five routes, the per-kind chunk
accounting and its minimums, the exit-code rule (an unproven kind is exit 4, nothing run is a failure), refusal
detection, the fixtures, and that the console row stays true.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._module_seam import setattr_in

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "chunk_key_enforcement_smoke.py"
_spec = importlib.util.spec_from_file_location("chunk_key_enforcement_smoke", _SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["chunk_key_enforcement_smoke"] = smoke
_spec.loader.exec_module(smoke)


# ── guards ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:1/", "https://[::1]:9", "http://127.9.9.9:65535"])
def test_loopback_urls_pass(url: str) -> None:
    assert smoke.require_loopback(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    ["https://api.conexus-nexus.com", "http://10.0.0.5:8080", "http://192.168.1.2:80", "http://example.com:80", "ftp://127.0.0.1:1",
     "127.0.0.1:8080", "http://user:pw@127.0.0.1:1", "http://localhost.evil.test:1", "", "http://0.0.0.0:80",
     "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:99999", "http://127.0.0.1:abc", "http://[::1", "http://[::1]:x"],
)
def test_anything_else_is_refused_as_not_started_never_a_traceback(url: str) -> None:
    with pytest.raises(smoke.NotStarted):
        smoke.require_loopback(url)


@pytest.mark.parametrize("status", [{}, {"chunk_key_check_mode": "log-only"}, {"chunk_key_check_mode": None}, [], None, "enforce"])
def test_only_enforce_mode_starts_the_run(status: object) -> None:
    with pytest.raises(smoke.NotStarted):
        smoke.require_enforce_mode(status)


def test_enforce_mode_starts_the_run() -> None:
    smoke.require_enforce_mode({"chunk_key_check_mode": "enforce", "other": 1})


def test_a_token_is_required(tmp_path: Path) -> None:
    with pytest.raises(smoke.NotStarted):
        smoke.resolve_token(None, {})
    f = tmp_path / "t"
    f.write_text("abc\n", encoding="utf-8")
    assert smoke.resolve_token(str(f), {}) == "abc"
    assert smoke.resolve_token(None, {"NX_SERVICE_TOKEN": " xyz "}) == "xyz"


def test_the_child_env_is_an_allowlist(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin", "HOME": "/h", "LANG": "en_US", "LC_ALL": "C", "LC_CTYPE": "x", "TMPDIR": "/t", "VIRTUAL_ENV": "/v",
        "UV_CACHE_DIR": "/u", "UV_PROJECT_ENVIRONMENT": "/p",
        # none of these may pass
        "NX_SERVICE_URL": "https://api.conexus-nexus.com", "NX_SERVICE_HOST": "h", "NX_SERVICE_PORT": "1",
        "NX_SERVICE_TOKEN": "old", "NEXUS_CONFIG_DIR": "/real", "NX_CONFIG_DIR": "/real2", "NX_ALLOW_PROD_WRITE": "x",
        "NX_MINT_TOKEN": "m", "NX_MINT_URL": "u", "VOYAGE_API_KEY": "v", "NX_VOYAGE_API_KEY": "v2", "NX_LOCAL": "1",
        "ANTHROPIC_API_KEY": "a", "AWS_SECRET_ACCESS_KEY": "s", "KEEP_ME_NOT": "1",
    }
    env = smoke.child_env(base, "http://127.0.0.1:5", "tok", tmp_path)
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "VIRTUAL_ENV", "UV_CACHE_DIR", "UV_PROJECT_ENVIRONMENT"}
    pinned = {"NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NEXUS_CONFIG_DIR", "NX_ALLOW_PROD_WRITE"}
    assert set(env) == allowed | pinned
    assert env["NX_SERVICE_URL"] == "http://127.0.0.1:5" and env["NX_SERVICE_TOKEN"] == "tok"
    assert env["NEXUS_CONFIG_DIR"] == str(tmp_path)
    assert env["PATH"] == "/bin" and env["UV_CACHE_DIR"] == "/u"
    assert "chunk-key-enforcement-smoke" in env["NX_ALLOW_PROD_WRITE"]


def test_the_endpoint_probe_must_resolve_to_the_given_loopback_url() -> None:
    def runner(out: str, rc: int = 0):
        return lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr="boom")

    smoke.probe_endpoint({}, "http://127.0.0.1:5", runner=runner("http://127.0.0.1:5\n"))
    for out, rc in (("https://api.conexus-nexus.com\n", 3), ("http://127.0.0.1:6\n", 0), ("", 1)):
        with pytest.raises(smoke.NotStarted):
            smoke.probe_endpoint({}, "http://127.0.0.1:5", runner=runner(out, rc))


def test_nx_is_the_one_beside_this_interpreter_never_the_one_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    setattr_in(monkeypatch, smoke, "sys.executable", str(tmp_path / "bin" / "python"))
    with pytest.raises(smoke.NotStarted):
        smoke.nx_command()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "nx").write_text("#!/bin/sh\n", encoding="utf-8")
    assert smoke.nx_command() == [str(tmp_path / "bin" / "nx")]


def test_the_status_and_control_requests_use_no_proxy_and_follow_no_redirect() -> None:
    import urllib.request  # noqa: PLC0415 — test-local import

    opener = smoke._opener()
    # An empty ProxyHandler has no protocol methods, so the opener does not install it, and passing it stops
    # build_opener adding the default one that reads HTTP_PROXY: no ProxyHandler of any kind is active.
    assert not [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    redirect = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, 302, "Found", {}, "http://elsewhere/") is None


def test_the_opener_ignores_the_callers_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request  # noqa: PLC0415 — test-local import

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    assert [h for h in urllib.request.build_opener().handlers if isinstance(h, urllib.request.ProxyHandler)], \
        "control: a default opener does pick the proxy up"
    assert not [h for h in smoke._opener().handlers if isinstance(h, urllib.request.ProxyHandler)]


# ── the positive controls ────────────────────────────────────────────────────


def test_a_control_exists_for_each_of_the_five_routes_and_each_names_bad_keys() -> None:
    reqs = smoke.control_requests("c1", "rid")
    assert tuple(reqs) == smoke.ROUTES and len(reqs) == 5
    for route, body in reqs.items():
        text = json.dumps(body)
        assert "line_start" in text and "title" in text and "chunk_key_smoke_unknown_key" in text, route
        assert body["collection"] == "c1"
    assert {"docs", "chunks"} <= set(reqs["/v1/catalog/manifest/write_many"]) and {"doc_id", "rows", "chunks"} <= set(reqs["/v1/catalog/manifest/append"])


def test_only_a_422_with_the_reason_passes_a_control() -> None:
    ok = {"reason": "chunk_metadata_key_refused", "refused": []}
    assert smoke.judge_control("/v1/vectors/upsert-chunks", 422, ok).status == smoke.RAN
    for status, body in ((200, {"ok": 1}), (422, {"reason": "unregistered_collection"}), (404, "nope"), (422, "chunk_metadata_key_refused"),
                         (500, ok), (422, None)):
        r = smoke.judge_control("/v1/catalog/manifest/append", status, body)
        assert r.status == smoke.FAILED, (status, body)
        assert "does not check this route" in r.detail


def test_every_route_is_sent_its_control_and_a_wrong_answer_fails_that_route_only() -> None:
    seen: list[str] = []

    def call(url: str, token: str, body: object):
        route = url.split("127.0.0.1:1", 1)[1]
        seen.append(route)
        if route.endswith("update-metadata"):
            return 200, {"updated": 1}
        if route.endswith("append"):
            raise urllib.error.URLError("refused")
        return 422, {"reason": "chunk_metadata_key_refused"}

    rows = smoke.run_controls("http://127.0.0.1:1", "t", "c1", "rid", call=call)
    assert seen == list(smoke.ROUTES), "all five are sent, none skipped after a failure"
    by = {r.kind: r.status for r in rows}
    assert by == {"control upsert-chunks": smoke.RAN, "control update-metadata": smoke.FAILED, "control append": smoke.FAILED,
                  "control append_many": smoke.RAN, "control write_many": smoke.RAN}


# ── per-kind accounting ──────────────────────────────────────────────────────


def _runner(rc: int, out: str = "", err: str = ""):
    return lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _step(min_chunks: int = 1) -> object:
    return smoke.Step("index-repo", ["nx", "index", "repo", "x"], min_chunks=min_chunks)


def _snaps(*snapshots: dict):
    it = iter(snapshots)
    return lambda: next(it)


def test_a_kind_counts_only_the_collections_that_moved_while_it_ran() -> None:
    before = {"a": 10, "b": 5, "untouched": 99}
    after = {"a": 14, "b": 5, "new": 3, "untouched": 99}
    assert smoke.moved(before, after) == {"a": 4, "new": 3}
    r = smoke.run_step(_step(3), {}, counts=_snaps(before, after), runner=_runner(0, "done"))
    assert (r.status, r.chunks, r.collections) == (smoke.RAN, 7, ("a", "new"))
    assert "wrote 7 chunks, refused 0" in r.line() and "a, new" in r.line()


def test_a_kind_that_wrote_fewer_than_its_minimum_fails() -> None:
    r = smoke.run_step(_step(3), {}, counts=_snaps({"a": 1}, {"a": 3}), runner=_runner(0))
    assert r.status == smoke.FAILED and "only 2 chunks" in r.detail and "at least 3" in r.detail


def test_a_kind_that_moved_nothing_is_a_failure_not_a_pass() -> None:
    r = smoke.run_step(_step(1), {}, counts=_snaps({"a": 1}, {"a": 1}), runner=_runner(0))
    assert r.status == smoke.FAILED and "nothing was exercised" in r.detail


def test_a_refusal_wins_over_a_nonzero_exit() -> None:
    r = smoke.run_step(_step(), {}, counts=_snaps({}, {}), runner=_runner(1, "", "Error: the engine refused this write: x"))
    assert (r.status, r.refused) == (smoke.REFUSED, 1)


def test_a_nonzero_exit_without_a_refusal_is_a_failure() -> None:
    r = smoke.run_step(_step(), {}, counts=_snaps({}, {}), runner=_runner(2, "", "boom"))
    assert r.status == smoke.FAILED and "exit 2" in r.detail


def test_a_timeout_is_a_failed_row_not_a_traceback() -> None:
    def runner(*a, **k):
        raise subprocess.TimeoutExpired("nx", 1)

    r = smoke.run_step(_step(), {}, counts=_snaps({}, {}), runner=runner)
    assert r.status == smoke.FAILED and "no answer within" in r.detail


@pytest.mark.parametrize("exc", [urllib.error.URLError("down"), ConnectionRefusedError("no"), RuntimeError("bad stats")])
def test_a_lost_engine_is_a_failed_row_not_a_traceback(exc: Exception) -> None:
    def counts():
        raise exc

    r = smoke.run_step(_step(), {}, counts=counts, runner=_runner(0))
    assert r.status == smoke.FAILED and "could not read the engine's chunk counts" in r.detail


def test_collection_counts_reads_the_stats_rows() -> None:
    rows = [{"name": "a", "count": 3}, {"name": "b", "count": None}, {"name": "c", "count": 4}, "junk", {"count": 2}]
    assert smoke.collection_counts("http://127.0.0.1:1", "t", get=lambda u, t: rows) == {"a": 3, "b": 0, "c": 4}
    with pytest.raises(RuntimeError):
        smoke.collection_counts("http://127.0.0.1:1", "t", get=lambda u, t: {"x": 1})


def test_refusals_are_counted_from_either_marker() -> None:
    text = "ok\nError: the engine refused this write: its chunk metadata names keys\nHTTP 422 chunk_metadata_key_refused\nfine"
    assert smoke.count_refusals(text) == 2
    assert smoke.count_refusals("all good\nnothing here") == 0


# ── the exit code ────────────────────────────────────────────────────────────


def _r(kind: str, status: str, **kw: object) -> object:
    return smoke.Result(kind, status, **kw)


@pytest.mark.parametrize(
    "results, accepted, code",
    [
        ([_r("a", smoke.RAN, chunks=3)], (), 0),
        ([_r("a", smoke.RAN, chunks=3), _r("console", smoke.NOT_APPLICABLE)], (), 0),
        ([_r("a", smoke.RAN, chunks=3), _r("dt-index", smoke.SKIPPED, detail="x")], (), 4),
        ([_r("a", smoke.RAN, chunks=3), _r("dt-index", smoke.SKIPPED, detail="x")], ("dt-index",), 0),
        ([_r("a", smoke.RAN, chunks=3), _r("dt-index", smoke.SKIPPED), _r("index-repo-oversize", smoke.SKIPPED)], ("dt-index",), 4),
        ([_r("a", smoke.RAN, chunks=3), _r("a", smoke.REFUSED, refused=1), _r("dt-index", smoke.SKIPPED)], (), 1),
        ([_r("a", smoke.RAN, chunks=3), _r("b", smoke.FAILED, detail="boom")], ("dt-index",), 1),
        ([_r("dt-index", smoke.SKIPPED)], ("dt-index",), 1),
        ([_r("control upsert-chunks", smoke.RAN, detail="refused as it must be")], (), 1),
        ([], (), 1),
    ],
)
def test_the_exit_code_rule(results: list, accepted: tuple, code: int) -> None:
    assert smoke.exit_code(results, accepted) == code


def test_the_unproven_kinds_are_named() -> None:
    results = [_r("a", smoke.RAN, chunks=1), _r("dt-index", smoke.SKIPPED), _r("index-repo-oversize", smoke.SKIPPED)]
    assert smoke.not_proven(results) == ["dt-index", "index-repo-oversize"]
    assert smoke.not_proven(results, ["dt-index"]) == ["index-repo-oversize"]


def test_dt_is_an_explicit_skip_without_a_uuid_with_a_true_reason_and_the_console_is_not_applicable() -> None:
    rows = {r.kind: r for r in smoke.static_rows([], oversize=True)}
    assert rows["dt-index"].status == smoke.SKIPPED and "--dt-uuid" in rows["dt-index"].detail
    assert "index-pdf does not cover" in rows["dt-index"].detail
    assert "covered by index-pdf" not in rows["dt-index"].detail
    assert rows["console"].status == smoke.NOT_APPLICABLE
    assert "dt-index" not in {r.kind for r in smoke.static_rows(["U"], oversize=True)}
    assert "index-repo-oversize" in {r.kind for r in smoke.static_rows(["U"], oversize=False)}


# ── fixtures and steps ───────────────────────────────────────────────────────


def test_the_fixture_repos_markdown_and_pdf_are_real(tmp_path: Path) -> None:
    repo = smoke.make_fixture_repo(tmp_path, "tag1")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    assert sorted(tracked) == ["README.md", "main.py", "notes.txt"]
    big = smoke.make_oversize_repo(tmp_path, "tag1")
    assert subprocess.run(["git", "-C", str(big), "ls-files"], capture_output=True, text=True, check=True).stdout.split() == ["big.md"]
    text = (big / "big.md").read_text(encoding="utf-8")
    sections = re.findall(r"^## Section \d{4}$", text, flags=re.M)
    assert len(sections) == 420 > smoke.MAX_RECORDS_PER_WRITE, "more sections than one request carries chunks"
    assert len({m for m in re.findall(r"s0007w\d{3}\w{6}", text)}) == 230, "sections differ from each other"
    assert all(len(s) > 1500 for s in text.split("\n## ")[1:]), "each section is about a chunk"
    assert smoke.make_markdown_file(tmp_path, "t").read_text(encoding="utf-8").count("\n## ") == 2
    pdf = tmp_path / "t.pdf"
    smoke.make_tiny_pdf(pdf, "Alpha beta. Gamma (delta). Epsilon.")
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref_at = int(re.search(rb"startxref\n(\d+)\n", data).group(1))
    assert data[xref_at:xref_at + 4] == b"xref"
    entries = list(re.finditer(rb"(\d{10}) 00000 n ", data))
    assert len(entries) == 5
    for m in entries:
        assert re.match(rb"\d+ 0 obj", data[int(m.group(1)):])
    fitz = pytest.importorskip("fitz", reason="pymupdf not installed here; the xref walk above is the structural check")
    with fitz.open(pdf) as doc:
        assert "Gamma (delta)" in doc[0].get_text()


def test_every_kind_the_header_names_has_a_step_in_order(tmp_path: Path) -> None:
    nx = ["/x/nx"]
    steps = smoke.build_steps(nx, tmp_path / "r", tmp_path / "b", tmp_path / "p.pdf", tmp_path / "m.md", "rid", [])
    assert [s.kind for s in steps["first"]] == ["store-put"]
    assert [s.kind for s in steps["rest"]] == ["mcp-store-put", "index-repo", "index-pdf", "index-md", "index-repo-oversize"]
    by = {s.kind: s for s in [*steps["first"], *steps["rest"]]}
    assert by["index-md"].argv[:4] == ["/x/nx", "index", "md", str(tmp_path / "m.md")]
    assert by["index-repo-oversize"].min_chunks == smoke.MAX_RECORDS_PER_WRITE + 1, "fewer chunks would not reach manifest/append"
    assert by["index-repo"].min_chunks == 3, "one chunk per fixture file"
    no_big = smoke.build_steps(nx, tmp_path / "r", tmp_path / "b", tmp_path / "p.pdf", tmp_path / "m.md", "rid", [], oversize=False)
    assert "index-repo-oversize" not in [s.kind for s in no_big["rest"]]
    with_dt = smoke.build_steps(nx, tmp_path / "r", tmp_path / "b", tmp_path / "p.pdf", tmp_path / "m.md", "rid", ["U1", "U2"])
    assert [s.kind for s in with_dt["rest"]].count("dt-index") == 2


def test_the_mcp_snippet_calls_store_put_and_maps_its_reply_to_the_exit_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import types  # noqa: PLC0415 — test-local import

    seen: dict = {}
    for reply, code in (("Stored: abc -> knowledge__x", 0), ("Error: the engine refused this write", 1)):
        fake = types.ModuleType("nexus.mcp.core")
        fake.store_put = lambda **kw: (seen.update(kw), reply)[1]
        monkeypatch.setitem(sys.modules, "nexus.mcp.core", fake)
        monkeypatch.setattr(sys, "argv", ["-c", "body text", "the-title"])
        with pytest.raises(SystemExit) as caught:
            exec(compile(smoke.MCP_STORE_PUT, "<mcp-snippet>", "exec"), {})  # noqa: S102 — the script's own child snippet
        assert caught.value.code == code
        assert reply in capsys.readouterr().out
    assert seen == {"content": "body text", "collection": "chunk-key-smoke-mcp", "title": "the-title"}


def test_the_console_still_has_no_write_route() -> None:
    """The smoke reports the console as not applicable because it serves reads only. A write route there
    (a POST/PUT/PATCH/DELETE handler) means the smoke must cover it: this fails so the row cannot go stale."""
    root = Path(__file__).resolve().parents[1] / "src" / "nexus" / "console"
    sources = list(root.rglob("*.py"))
    assert sources, "the console package moved: update this census (a gate that examined nothing passed)"
    pattern = re.compile(r"\.(post|put|patch|delete)\(|methods\s*=\s*\[[^\]]*['\"](POST|PUT|PATCH|DELETE)", re.I)
    hits = [f"{p.name}: {m.group(0)}" for p in sources for m in pattern.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []


# ── main ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def _nx_beside_python(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bindir = tmp_path / "venv-bin"
    bindir.mkdir()
    (bindir / "nx").write_text("#!/bin/sh\n", encoding="utf-8")
    setattr_in(monkeypatch, smoke, "sys.executable", str(bindir / "python"))


@pytest.mark.parametrize("url", ["https://api.conexus-nexus.com", "http://127.0.0.1", "http://[::1", "http://127.0.0.1:99999", ""])
def test_main_refuses_a_bad_url_before_any_network_and_names_it(monkeypatch: pytest.MonkeyPatch, capsys, url: str) -> None:
    def no_network(*a, **k):
        raise AssertionError("the smoke touched the network before the loopback check")

    monkeypatch.setattr(smoke, "_get_json", no_network)
    monkeypatch.setattr(smoke, "_call", no_network)
    assert smoke.main(["--url", url], environ={"NX_SERVICE_TOKEN": "t"}) == smoke.EXIT_NOT_STARTED
    assert "not started" in capsys.readouterr().err


def test_main_refuses_a_log_only_engine(monkeypatch: pytest.MonkeyPatch, capsys, _nx_beside_python) -> None:
    monkeypatch.setattr(smoke, "_get_json", lambda url, token: {"chunk_key_check_mode": "log-only"})
    assert smoke.main(["--url", "http://127.0.0.1:1"], environ={"NX_SERVICE_TOKEN": "t"}) == smoke.EXIT_NOT_STARTED
    assert "enforce" in capsys.readouterr().err


def test_main_refuses_when_there_is_no_nx_beside_the_interpreter(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path) -> None:
    setattr_in(monkeypatch, smoke, "sys.executable", str(tmp_path / "nowhere" / "python"))
    assert smoke.main(["--url", "http://127.0.0.1:1", "--plan"], environ={}) == smoke.EXIT_NOT_STARTED
    assert "no nx beside" in capsys.readouterr().err


def test_plan_mode_says_it_proved_nothing_builds_fixtures_and_contacts_no_engine(monkeypatch: pytest.MonkeyPatch, capsys, _nx_beside_python) -> None:
    def no_network(*a, **k):
        raise AssertionError("--plan contacted an engine")

    monkeypatch.setattr(smoke, "_get_json", no_network)
    monkeypatch.setattr(smoke, "_call", no_network)
    assert smoke.main(["--url", "http://127.0.0.1:1", "--plan"], environ={}) == smoke.EXIT_OK
    out = capsys.readouterr().out
    assert "PLAN ONLY: nothing was run or contacted, so this proves nothing about enforcement." in out
    for kind in ("store-put", "mcp-store-put", "index-repo", "index-pdf", "index-md", "index-repo-oversize", "console", "dt-index", "controls"):
        assert kind in out


def test_a_whole_run_against_a_fake_engine_exits_4_without_dt_and_0_with_it_accepted(monkeypatch: pytest.MonkeyPatch, capsys, _nx_beside_python) -> None:
    """The orchestration end to end with the children and the engine faked: the guards pass, store-put runs first, the
    five controls follow, the other kinds run, and a missing DEVONthink record is NOT PROVEN (exit 4) until accepted."""
    state = {"count": 0}
    order: list[str] = []

    monkeypatch.setattr(smoke, "_get_json", lambda url, token: {"chunk_key_check_mode": "enforce"})
    monkeypatch.setattr(smoke, "probe_endpoint", lambda env, url, runner=None: None)
    monkeypatch.setattr(smoke, "collection_counts", lambda url, token, get=None: {"c1": state["count"]})
    monkeypatch.setattr(smoke, "_call", lambda url, token, body=None: (order.append("control:" + url.rsplit("/", 1)[-1]) or
                                                                       (422, {"reason": "chunk_metadata_key_refused"})))

    def fake_run(argv, **kw):
        if argv[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--version" in argv:
            return SimpleNamespace(returncode=0, stdout="nx 1.2.3", stderr="")
        if argv[1:3] == ["index", "repo"] and "big" in argv[3]:
            state["count"] += 400
        else:
            state["count"] += 5
        order.append(next((a for a in argv[1:3] if a in ("store", "index")), "mcp") + ":" + (argv[2] if len(argv) > 2 else ""))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    setattr_in(monkeypatch, smoke, "subprocess.run", fake_run)
    args = ["--url", "http://127.0.0.1:1"]
    env = {"NX_SERVICE_TOKEN": "t"}
    assert smoke.main(args, environ=env) == smoke.EXIT_INCOMPLETE
    out = capsys.readouterr().out
    assert "NOT PROVEN: dt-index" in out
    first_control = next(i for i, o in enumerate(order) if o.startswith("control:"))
    assert order[0].startswith("store:"), "store-put is the first write"
    assert sum(o.startswith("control:") for o in order) == 5 and order[first_control + 4].startswith("control:")
    assert not any(o.startswith(("index:", "mcp:")) for o in order[:first_control]), "no other writer before the controls"
    assert "wrote 400 chunks" in out and "nx: " in out and "nx 1.2.3" in out

    state["count"] = 0
    order.clear()
    assert smoke.main([*args, "--accept-unproven", "dt-index"], environ=env) == smoke.EXIT_OK
    assert "accepted unproven: dt-index" in capsys.readouterr().out


def test_a_control_that_is_not_refused_stops_the_run_before_the_other_writers(monkeypatch: pytest.MonkeyPatch, capsys, _nx_beside_python) -> None:
    state = {"count": 0}
    ran: list[str] = []
    monkeypatch.setattr(smoke, "_get_json", lambda url, token: {"chunk_key_check_mode": "enforce"})
    monkeypatch.setattr(smoke, "probe_endpoint", lambda env, url, runner=None: None)
    monkeypatch.setattr(smoke, "collection_counts", lambda url, token, get=None: {"c1": state["count"]})
    monkeypatch.setattr(smoke, "_call", lambda url, token, body=None: (200, {"ok": True}))

    def fake_run(argv, **kw):
        if "--version" not in argv and argv[0] != "git":
            ran.append(" ".join(argv[1:3]))
            state["count"] += 5
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    setattr_in(monkeypatch, smoke, "subprocess.run", fake_run)
    assert smoke.main(["--url", "http://127.0.0.1:1"], environ={"NX_SERVICE_TOKEN": "t"}) == smoke.EXIT_REFUSED
    assert ran == ["store put"], "only the first write ran"
    assert "does not check this route" in capsys.readouterr().out
