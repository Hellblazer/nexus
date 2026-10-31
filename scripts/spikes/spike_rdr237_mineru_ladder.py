# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 spikes A1, A2, A3 (MinerU-only PDF extraction with a per-page fallback ladder).

A1: does MinerU honour formula_enable=false per call (subprocess do_parse) and per request (server
    /file_parse), and does the rest of the page still extract?
A2: the MinerU server serves one request at a time in an uncancellable thread. After a client gives
    up on a slow request, is the next request queued behind it, and does restarting the server free
    the slot?
A3: on born-digital, non-math PDFs, is MinerU's text at least as complete as Docling's? Proxy: word
    recall and precision against the PDF's own text layer, which is ground truth for born-digital
    files. A proxy, not a retrieval evaluation.

Run on a lab host: lab-remote -c lab-l -- uv run python scripts/spikes/spike_rdr237_mineru_ladder.py
Writes scripts/spikes/spike_rdr237_results.json. Starts its own mineru-api on a free port with a temp
output root; touches no shared server or pid file.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pymupdf

from nexus._mineru_spawn import _find_free_port, _resolve_mineru_api_bin, _server_env
from nexus.pdf_extractor import PDFExtractor

ROOT = Path(__file__).resolve().parents[2]
MATH_PDF = ROOT / "tests/fixtures/virgo.pdf"
NON_MATH = [ROOT / "tests/fixtures/tc-sql.pdf", ROOT / "papers/2408.04948.pdf"]
OUT = ROOT / "scripts/spikes/spike_rdr237_results.json"
WORD = re.compile(r"[a-z0-9]{3,}")

# Inlined after the Docling removal (nexus-tr1rj.19): this counter was pdf_extractor._count_formula_markers
# (git show 5ed2d9fa1^:src/nexus/pdf_extractor.py), the auto router's formula pre-screen, deleted with it.
_FORMULA_BLOCK_PATTERNS = (
    re.compile(r"\$\$.+?\$\$", re.DOTALL),
    re.compile(r"\\\(.+?\\\)", re.DOTALL),
    re.compile(r"\\\[.+?\\\]", re.DOTALL),
    re.compile(r"\\begin\{equation\*?\}.+?\\end\{equation\*?\}", re.DOTALL),
    re.compile(r"\\begin\{align\*?\}.+?\\end\{align\*?\}", re.DOTALL),
)
_FORMULA_COMMAND_PATTERNS = tuple(
    re.compile(r"\\" + name + r"\b")
    for name in ("frac", "sum", "int", "prod", "partial", "nabla", "mathbb", "mathcal")
)


def _count_formula_markers(text: str) -> int:
    """Block delimiters count once each; command tokens count every occurrence, inside blocks too."""
    return sum(len(pat.findall(text)) for pat in _FORMULA_BLOCK_PATTERNS + _FORMULA_COMMAND_PATTERNS)


WORKER = r'''
import json, sys, time
from pathlib import Path
from mineru.cli.common import do_parse
pdf, out, page, formula = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
t = time.monotonic()
do_parse(out, pdf_file_names=[Path(pdf).stem], pdf_bytes_list=[Path(pdf).read_bytes()], p_lang_list=["en"],
         formula_enable=formula, table_enable=True, start_page_id=page, end_page_id=page)
md = next(Path(out).rglob("*.md")).read_text()
print(json.dumps({"seconds": time.monotonic() - t, "md": md}))
'''


def words(text: str) -> collections.Counter:
    return collections.Counter(WORD.findall(text.lower()))


def overlap(truth: collections.Counter, got: collections.Counter) -> dict:
    hit = sum((truth & got).values())
    return {"recall": round(hit / max(1, sum(truth.values())), 4),
            "precision": round(hit / max(1, sum(got.values())), 4)}


def subprocess_page(pdf: Path, page: int, formula: bool) -> dict:
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run([sys.executable, "-c", WORKER, str(pdf), out, str(page), "1" if formula else "0"],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return {"rc": r.returncode, "stderr": r.stderr[-800:]}
        d = json.loads(r.stdout.strip().splitlines()[-1])
        return {"rc": 0, "seconds": round(d["seconds"], 1), "chars": len(d["md"]),
                "formula_markers": _count_formula_markers(d["md"]), "md_head": d["md"][:300]}


def start_server(tmp: Path) -> tuple[subprocess.Popen, str]:
    port = _find_free_port()
    env = {**os.environ, **_server_env(tmp)}
    exe = _resolve_mineru_api_bin()
    assert exe, "mineru-api not found"
    proc = subprocess.Popen([exe, "--host", "127.0.0.1", "--port", str(port)], env=env,
                            stdout=open(tmp / "server.log", "ab"), stderr=subprocess.STDOUT, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(600):
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError("mineru-api did not come up")


def stop_server(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, 15)
        proc.wait(timeout=60)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass


def server_page(url: str, pdf: Path, start: int, end: int, formula: bool, timeout: float) -> dict:
    t = time.monotonic()
    try:
        with pdf.open("rb") as f:
            r = httpx.post(f"{url}/file_parse", files=[("files", (pdf.name, f, "application/pdf"))], timeout=timeout,
                           data={"backend": "pipeline", "start_page_id": str(start), "end_page_id": str(end),
                                 "formula_enable": str(formula).lower(), "table_enable": "true", "return_md": "true",
                                 "parse_method": "auto", "lang_list": "en"})
        r.raise_for_status()
        md = next(iter(r.json()["results"].values()))["md_content"]
        return {"ok": True, "seconds": round(time.monotonic() - t, 1), "chars": len(md),
                "formula_markers": _count_formula_markers(md)}
    except httpx.TimeoutException:
        return {"ok": False, "timeout": True, "seconds": round(time.monotonic() - t, 1)}


def a2_mid_parse() -> None:
    """A2 follow-up: give up on a six-page request partway through its parse (after the baseline one-page
    time, so parsing has begun), then time the next one-page request on the same server."""
    out = ROOT / "scripts/spikes/spike_rdr237_a2b_results.json"
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        proc, url = start_server(tmp)
        try:
            warm = server_page(url, MATH_PDF, 5, 5, True, 900)
            rows = {"warm_one_page": warm}
            for give_up in (10.0, 30.0):
                rows[f"gave_up_at_{int(give_up)}s"] = server_page(url, MATH_PDF, 6, 11, True, give_up)
                rows[f"next_page_after_{int(give_up)}s_give_up"] = server_page(url, MATH_PDF, 12, 12, True, 900)
            rows["server_log_tail"] = (tmp / "server.log").read_text(errors="replace")[-3000:]
        finally:
            stop_server(proc)
    out.write_text(json.dumps(rows, indent=2))
    print(json.dumps({k: v for k, v in rows.items() if k != "server_log_tail"}, indent=2))  # noqa: T201 — spike output for the operator


def main() -> None:
    if sys.argv[1:] == ["a2b"]:
        a2_mid_parse()
        return
    results: dict = {"host": os.uname().nodename, "cpus": os.cpu_count()}

    # A1, subprocess path: three formula-bearing pages of virgo, formulas on and off.
    a1 = []
    for page in (1, 2, 3):
        a1.append({"page": page, "on": subprocess_page(MATH_PDF, page, True), "off": subprocess_page(MATH_PDF, page, False)})
    results["a1_subprocess"] = a1

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        proc, url = start_server(tmp)
        try:
            # A1, server path.
            results["a1_server"] = [{"page": p, "on": server_page(url, MATH_PDF, p, p, True, 900),
                                     "off": server_page(url, MATH_PDF, p, p, False, 900)} for p in (1, 2, 3)]
            # A2: baseline one page; then give up on a six-page request after 3 s and time the next page.
            base = server_page(url, MATH_PDF, 5, 5, True, 900)
            gave_up = server_page(url, MATH_PDF, 6, 11, True, 3)
            queued = server_page(url, MATH_PDF, 12, 12, True, 900)
            gave_up_again = server_page(url, MATH_PDF, 6, 11, True, 3)
            stop_server(proc)
            proc, url = start_server(tmp)
            after_restart = server_page(url, MATH_PDF, 12, 12, True, 900)
            results["a2"] = {"baseline_one_page": base, "client_gave_up": gave_up,
                             "next_page_without_restart": queued, "client_gave_up_again": gave_up_again,
                             "next_page_after_restart": after_restart}
        finally:
            stop_server(proc)

    # A3: born-digital non-math PDFs, Docling against MinerU, against the text layer.
    a3 = []
    for pdf in NON_MATH:
        truth = words("\n".join(p.get_text(sort=True) for p in pymupdf.open(pdf)))
        row: dict = {"pdf": pdf.name, "pages": len(pymupdf.open(pdf)), "truth_words": sum(truth.values())}
        for extractor in ("docling", "mineru"):
            t = time.monotonic()
            res = PDFExtractor().extract(pdf, extractor=extractor)
            row[extractor] = {"seconds": round(time.monotonic() - t, 1), "chars": len(res.text),
                              "table_rows": res.text.count("\n|"), **overlap(truth, words(res.text))}
        a3.append(row)
    results["a3"] = a3

    OUT.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2)[:4000])  # noqa: T201 — spike output for the operator


if __name__ == "__main__":
    main()
