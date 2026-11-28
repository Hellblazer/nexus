# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.66: the logic of ``scripts/chunk_key_enforcement_smoke.py`` that needs no engine.

The live run is by hand against a local engine started with ``NX_CHUNK_KEY_CHECK_MODE=enforce``; what is pinned
here is what must hold before it touches anything: the loopback and enforce-mode guards, the exit-code rule
(an all-skipped run is a failure), refusal detection, the child environment's scrub, the fixtures, and that the
console row stays true.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "chunk_key_enforcement_smoke.py"
_spec = importlib.util.spec_from_file_location("chunk_key_enforcement_smoke", _SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["chunk_key_enforcement_smoke"] = smoke
_spec.loader.exec_module(smoke)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:1/", "https://[::1]:9", "http://127.9.9.9"])
def test_loopback_urls_pass(url: str) -> None:
    assert smoke.require_loopback(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    ["https://api.conexus-nexus.com", "http://10.0.0.5:8080", "http://192.168.1.2", "http://example.com", "ftp://127.0.0.1",
     "127.0.0.1:8080", "http://user:pw@127.0.0.1", "http://localhost.evil.test", "", "http://0.0.0.0:80"],
)
def test_anything_else_is_refused(url: str) -> None:
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


def _r(status: str, **kw: object) -> object:
    return smoke.Result("k", status, **kw)


@pytest.mark.parametrize(
    "results, code",
    [
        ([_r(smoke.RAN, chunks=3)], 0),
        ([_r(smoke.RAN, chunks=3), _r(smoke.SKIPPED, detail="x"), _r(smoke.NOT_APPLICABLE)], 0),
        ([_r(smoke.SKIPPED, detail="x"), _r(smoke.SKIPPED, detail="y")], 3),
        ([_r(smoke.NOT_APPLICABLE), _r(smoke.SKIPPED, detail="y")], 3),
        ([], 3),
        ([_r(smoke.RAN, chunks=3), _r(smoke.REFUSED, refused=1)], 1),
        ([_r(smoke.RAN, chunks=3), _r(smoke.FAILED, detail="boom")], 1),
        ([_r(smoke.SKIPPED), _r(smoke.REFUSED, refused=2)], 1),
    ],
)
def test_the_exit_code_rule(results: list, code: int) -> None:
    assert smoke.exit_code(results) == code


def test_refusals_are_counted_from_either_marker() -> None:
    text = "ok\nError: the engine refused a chunk write: its metadata names keys\nHTTP 422 chunk_metadata_key_refused\nfine"
    assert smoke.count_refusals(text) == 2
    assert smoke.count_refusals("all good\nnothing here") == 0


def _runner(rc: int, out: str = "", err: str = ""):
    return lambda *a, **k: SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _step() -> object:
    return smoke.Step("store-put", ["nx", "store", "put", "-"], stdin="x")


def test_a_clean_write_reports_the_chunk_delta() -> None:
    totals = iter([10, 14])
    r = smoke.run_step(_step(), {}, "u", "t", total=lambda: next(totals), runner=_runner(0, "Stored"))
    assert (r.status, r.chunks, r.refused) == (smoke.RAN, 4, 0)
    assert "ran, wrote 4 chunks, refused 0" in r.line()


def test_a_refusal_wins_over_a_nonzero_exit() -> None:
    r = smoke.run_step(_step(), {}, "u", "t", total=lambda: 0, runner=_runner(1, "", "Error: the engine refused a chunk write: x"))
    assert (r.status, r.refused) == (smoke.REFUSED, 1)


def test_a_nonzero_exit_without_a_refusal_is_a_failure() -> None:
    r = smoke.run_step(_step(), {}, "u", "t", total=lambda: 0, runner=_runner(2, "", "boom"))
    assert r.status == smoke.FAILED and "exit 2" in r.detail


def test_a_zero_exit_that_wrote_nothing_is_a_failure_not_a_pass() -> None:
    r = smoke.run_step(_step(), {}, "u", "t", total=lambda: 7, runner=_runner(0, "done"))
    assert r.status == smoke.FAILED and "did not move" in r.detail


def test_the_child_env_drops_inherited_endpoints_and_pins_the_loopback_one(tmp_path: Path) -> None:
    base = {"PATH": "/bin", "NX_SERVICE_URL": "https://api.conexus-nexus.com", "NX_SERVICE_HOST": "h", "NX_SERVICE_PORT": "1",
            "NX_SERVICE_TOKEN": "old", "NEXUS_CONFIG_DIR": "/real", "NX_ALLOW_PROD_WRITE": "x", "KEEP": "1"}
    env = smoke.child_env(base, "http://127.0.0.1:5", "tok", tmp_path)
    assert env["NX_SERVICE_URL"] == "http://127.0.0.1:5" and env["NX_SERVICE_TOKEN"] == "tok"
    assert env["NEXUS_CONFIG_DIR"] == str(tmp_path)
    assert "NX_SERVICE_HOST" not in env and "NX_SERVICE_PORT" not in env
    assert env["PATH"] == "/bin" and env["KEEP"] == "1"
    assert "chunk-key-enforcement-smoke" in env["NX_ALLOW_PROD_WRITE"]


def test_live_total_sums_the_stats_rows() -> None:
    rows = [{"name": "a", "count": 3}, {"name": "b", "count": None}, {"name": "c", "count": 4}, "junk"]
    assert smoke.live_chunk_total("http://127.0.0.1:1", "t", get=lambda u, t: rows) == 7
    with pytest.raises(RuntimeError):
        smoke.live_chunk_total("http://127.0.0.1:1", "t", get=lambda u, t: {"x": 1})


def test_the_fixture_repo_and_pdf_are_real(tmp_path: Path) -> None:
    repo = smoke.make_fixture_repo(tmp_path, "tag1")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    assert sorted(tracked) == ["README.md", "main.py", "notes.txt"]
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


def test_every_kind_the_header_names_has_a_step(tmp_path: Path) -> None:
    steps = smoke.build_steps(tmp_path / "r", tmp_path / "p.pdf", "rid", [])
    assert [s.kind for s in steps] == ["index-repo", "index-pdf", "store-put", "mcp-store-put"]
    with_dt = smoke.build_steps(tmp_path / "r", tmp_path / "p.pdf", "rid", ["U1", "U2"])
    assert [s.kind for s in with_dt].count("dt-index") == 2


def test_dt_is_an_explicit_skip_without_a_uuid_and_the_console_is_not_applicable() -> None:
    rows = {r.kind: r for r in smoke.static_rows([])}
    assert rows["dt-index"].status == smoke.SKIPPED and "--dt-uuid" in rows["dt-index"].detail
    assert rows["console"].status == smoke.NOT_APPLICABLE
    assert "dt-index" not in {r.kind for r in smoke.static_rows(["U"])}


def test_the_console_still_has_no_write_route() -> None:
    """The smoke reports the console as not applicable because it serves reads only. A write route there
    (a POST/PUT/PATCH/DELETE handler) means the smoke must cover it: this fails so the row cannot go stale."""
    root = Path(__file__).resolve().parents[1] / "src" / "nexus" / "console"
    sources = list(root.rglob("*.py"))
    assert sources, "the console package moved: update this census (a gate that examined nothing passed)"
    pattern = re.compile(r"\.(post|put|patch|delete)\(|methods\s*=\s*\[[^\]]*['\"](POST|PUT|PATCH|DELETE)", re.I)
    hits = [f"{p.name}: {m.group(0)}" for p in sources for m in pattern.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []


def test_main_refuses_a_non_loopback_url_before_any_network(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def no_network(*a, **k):
        raise AssertionError("the smoke touched the network before the loopback check")

    monkeypatch.setattr(smoke, "_get_json", no_network)
    assert smoke.main(["--url", "https://api.conexus-nexus.com"], environ={"NX_SERVICE_TOKEN": "t"}) == smoke.EXIT_NOT_STARTED
    assert "loopback" in capsys.readouterr().err


def test_main_refuses_a_log_only_engine(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(smoke, "_get_json", lambda url, token: {"chunk_key_check_mode": "log-only"})
    assert smoke.main(["--url", "http://127.0.0.1:1"], environ={"NX_SERVICE_TOKEN": "t"}) == smoke.EXIT_NOT_STARTED
    assert "enforce" in capsys.readouterr().err


def test_plan_mode_builds_fixtures_and_contacts_no_engine(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def no_network(*a, **k):
        raise AssertionError("--plan contacted an engine")

    monkeypatch.setattr(smoke, "_get_json", no_network)
    assert smoke.main(["--url", "http://127.0.0.1:1", "--plan"], environ={}) == smoke.EXIT_OK
    out = capsys.readouterr().out
    for kind in ("index-repo", "index-pdf", "store-put", "mcp-store-put", "console", "dt-index"):
        assert kind in out


def test_the_mcp_snippet_calls_store_put_and_maps_its_reply_to_the_exit_code(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import types  # noqa: PLC0415 — test-local import

    seen: dict = {}
    for reply, code in (("Stored: abc -> knowledge__x", 0), ("Error: the engine refused a chunk write", 1)):
        fake = types.ModuleType("nexus.mcp.core")
        fake.store_put = lambda **kw: (seen.update(kw), reply)[1]
        monkeypatch.setitem(sys.modules, "nexus.mcp.core", fake)
        monkeypatch.setattr(sys, "argv", ["-c", "body text", "the-title"])
        with pytest.raises(SystemExit) as caught:
            exec(compile(smoke.MCP_STORE_PUT, "<mcp-snippet>", "exec"), {})  # noqa: S102 — the script's own child snippet
        assert caught.value.code == code
        assert reply in capsys.readouterr().out
    assert seen == {"content": "body text", "collection": "chunk-key-smoke", "title": "the-title"}
