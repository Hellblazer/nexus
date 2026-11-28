#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-client-kind smoke for engine chunk-key enforcement (RDR-236 Phase 4 Step 2c, nexus-6pfgb.66).

Run each client kind's chunk write path once against a LOCAL engine that was started with
``NX_CHUNK_KEY_CHECK_MODE=enforce`` and fail loudly if any write is refused with
``chunk_metadata_key_refused`` (the 422 the engine answers on upsert-chunks, update-metadata and the combined
manifest writes). It is the check that every writer in the tree stays inside the CHUNK key set before the
engine's mode is raised anywhere that matters.

Kinds and how each is driven (one subprocess each, an isolated NEXUS_CONFIG_DIR, no shared state):

  index-repo   ``nx index repo`` on a three-file git repo the script creates in a temp dir.
  index-pdf    ``nx index pdf --extractor pymupdf`` on a one-page text PDF the script generates.
  store-put    ``nx store put`` from stdin.
  mcp-store-put the MCP ``store_put`` tool function called in-process in a child interpreter (no Claude).
  dt-index     ``nx dt index --uuid U`` for each ``--dt-uuid``; skipped without one (it needs the DEVONthink
               app and a record in it; the indexer's chunk writer is the one ``index-pdf`` already drives).
  console      not applicable: the console serves GET routes only (``tests/test_chunk_key_smoke_script.py``
               fails the day a write route appears, so this row cannot go stale unnoticed).

Output, one line per kind: ``ran, wrote N chunks, refused 0``, ``skipped: <reason>``, ``REFUSED ...`` or
``FAILED ...``. Exit codes:

  0  at least one kind ran and none was refused or failed
  1  a write was refused (chunk_metadata_key_refused) or a kind failed or wrote nothing
  2  the run was not started: non-loopback URL, no token, or the engine is not in enforce mode
  3  every kind skipped (a smoke that exercised nothing is a failure, not a pass)

It never contacts anything but the URL given, and refuses a URL that is not loopback (``localhost`` or a
loopback IP literal). It has no cloud path at all: do not point it at one.

How to run (the operator starts the engine; this script does not):

  NX_CHUNK_KEY_CHECK_MODE=enforce nx daemon service start      # a local engine, enforce mode
  export NX_SERVICE_TOKEN=...                                   # or --token-file; the local engine's data token
  uv run python scripts/chunk_key_enforcement_smoke.py --url http://127.0.0.1:<port>
  uv run python scripts/chunk_key_enforcement_smoke.py --url http://127.0.0.1:<port> --plan   # no engine contact

``--plan`` builds the fixtures and prints the commands without contacting any engine, the safe dry path.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

EXIT_OK, EXIT_REFUSED, EXIT_NOT_STARTED, EXIT_ALL_SKIPPED = 0, 1, 2, 3

#: What the engine sends (``reason``) and what the client's typed error prints; either marks a refusal.
REFUSAL_MARKERS: tuple[str, ...] = ("chunk_metadata_key_refused", "the engine refused a chunk write")

#: The status field the engine reports its enforcement mode in, and the value this smoke requires.
MODE_FIELD, MODE_ENFORCE = "chunk_key_check_mode", "enforce"

PROD_WRITE_REASON = "chunk-key-enforcement-smoke: writes to a loopback engine the operator started (nexus-6pfgb.66)"
RUN_TIMEOUT_S = 900

RAN, SKIPPED, REFUSED, FAILED, NOT_APPLICABLE = "ran", "skipped", "REFUSED", "FAILED", "n/a"


def _say(msg: str = "", *, err: bool = False) -> None:
    """One line to stdout (or stderr). The script's output is its result; ruff bans print."""
    (sys.stderr if err else sys.stdout).write(msg + "\n")


class NotStarted(Exception):
    """The run was refused before any write (exit 2)."""


@dataclass(frozen=True)
class Result:
    kind: str
    status: str
    chunks: int = 0
    refused: int = 0
    detail: str = ""

    def line(self) -> str:
        if self.status == RAN:
            return f"{self.kind:<14} ran, wrote {self.chunks} chunks, refused {self.refused}"
        if self.status == SKIPPED:
            return f"{self.kind:<14} skipped: {self.detail}"
        if self.status == NOT_APPLICABLE:
            return f"{self.kind:<14} not applicable: {self.detail}"
        if self.status == REFUSED:
            return f"{self.kind:<14} REFUSED (chunk_metadata_key_refused x{self.refused}): {self.detail}"
        return f"{self.kind:<14} FAILED: {self.detail}"


# ── guards ───────────────────────────────────────────────────────────────────


def require_loopback(url: str) -> str:
    """The URL without a trailing slash, or ``NotStarted`` unless it is an http(s) URL to a loopback host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotStarted(f"--url must be an http(s) URL with a host, got {url!r}")
    if parts.username or parts.password:
        raise NotStarted("--url must not carry user:password@")
    host = parts.hostname
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise NotStarted(
                f"refusing {host!r}: this smoke writes chunks and only runs against a loopback engine "
                "(localhost or 127.0.0.1 / ::1). It has no cloud path.")
    return url.rstrip("/")


def require_enforce_mode(status: object) -> None:
    """``NotStarted`` unless the engine's ``/v1/status`` body reports ``chunk_key_check_mode == enforce``."""
    mode = status.get(MODE_FIELD) if isinstance(status, dict) else None
    if mode != MODE_ENFORCE:
        raise NotStarted(
            f"the engine reports {MODE_FIELD}={mode!r}, not {MODE_ENFORCE!r}: start it with "
            "NX_CHUNK_KEY_CHECK_MODE=enforce (a smoke against a log-only engine proves nothing).")


def resolve_token(token_file: str | None, environ: dict[str, str]) -> str:
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else environ.get("NX_SERVICE_TOKEN", "").strip()
    if not token:
        raise NotStarted("no token: export NX_SERVICE_TOKEN or pass --token-file (the local engine's data token)")
    return token


def count_refusals(text: str) -> int:
    """Lines of *text* that carry a refusal marker (the engine's reason or the client's typed message)."""
    return sum(1 for ln in text.splitlines() if any(m in ln for m in REFUSAL_MARKERS))


def exit_code(results: Sequence[Result]) -> int:
    """Refusal or failure wins; else all-skipped is its own failure; else 0. ``n/a`` rows count for nothing."""
    counted = [r for r in results if r.status != NOT_APPLICABLE]
    if any(r.status in (REFUSED, FAILED) for r in counted):
        return EXIT_REFUSED
    if not any(r.status == RAN for r in counted):
        return EXIT_ALL_SKIPPED
    return EXIT_OK


# ── the engine, read-only ────────────────────────────────────────────────────


def _get_json(url: str, token: str) -> object:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — url was checked loopback by require_loopback
        return json.loads(resp.read())


def live_chunk_total(base_url: str, token: str, get: Callable[[str, str], object] = _get_json) -> int:
    """Sum of live chunk counts over every collection of the token's tenant (``GET /v1/vectors/stats``)."""
    rows = get(f"{base_url}/v1/vectors/stats", token)
    if not isinstance(rows, list):
        raise RuntimeError(f"/v1/vectors/stats answered {type(rows).__name__}, not a list")
    return sum(int(r.get("count") or 0) for r in rows if isinstance(r, dict))


# ── fixtures ─────────────────────────────────────────────────────────────────


def make_tiny_pdf(path: Path, text: str) -> None:
    """A one-page PDF whose text layer is *text* (Helvetica, one line per sentence), valid xref included."""
    lines = [s.strip() for s in text.split(".") if s.strip()]
    ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
    for s in lines:
        esc = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({esc}.) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


def make_fixture_repo(root: Path, tag: str) -> Path:
    """A three-file git repo (code, prose, text) with one commit. *tag* makes every run's content new."""
    repo = root / "smoke-repo"
    repo.mkdir()
    (repo / "main.py").write_text(
        f'"""Chunk-key smoke {tag}."""\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n\n\n'
        f'def greet(name: str) -> str:\n    return f"hello {{name}} {tag}"\n', encoding="utf-8")
    (repo / "README.md").write_text(
        f"# Smoke repo {tag}\n\nA tiny repository the chunk-key enforcement smoke indexes once.\n\n"
        "## Usage\n\nRun main.py. Nothing else lives here.\n", encoding="utf-8")
    (repo / "notes.txt").write_text(f"plain notes for the smoke run {tag}\nsecond line of notes\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid"]
    for args in (["init", "-q"], ["add", "main.py", "README.md", "notes.txt"], ["commit", "-q", "-m", "smoke fixture"]):
        subprocess.run([*git, *args], check=True, capture_output=True, text=True)
    return repo


# ── kinds ────────────────────────────────────────────────────────────────────

MCP_STORE_PUT = (
    "import sys\n"
    "from nexus.mcp.core import store_put\n"
    "out = store_put(content=sys.argv[1], collection='chunk-key-smoke', title=sys.argv[2])\n"
    "print(out)\n"
    "sys.exit(0 if out.startswith('Stored:') else 1)\n"
)


def nx_command() -> list[str]:
    """The ``nx`` that sits beside this interpreter (the venv the script runs in), else the one on PATH."""
    beside = Path(sys.executable).parent / "nx"
    if beside.exists():
        return [str(beside)]
    found = shutil.which("nx")
    if found:
        return [found]
    return [sys.executable, "-c", "from nexus.cli import main; main()"]


@dataclass(frozen=True)
class Step:
    kind: str
    argv: list[str]
    stdin: str | None = None


def build_steps(repo: Path, pdf: Path, run_id: str, dt_uuids: Sequence[str]) -> list[Step]:
    nx = nx_command()
    steps = [
        Step("index-repo", [*nx, "index", "repo", str(repo)]),
        Step("index-pdf", [*nx, "index", "pdf", str(pdf), "--extractor", "pymupdf", "--collection", "chunk-key-smoke-pdf"]),
        Step("store-put", [*nx, "store", "put", "-", "--collection", "chunk-key-smoke", "--title", f"smoke-cli-{run_id}"],
             stdin=f"chunk-key smoke note {run_id}: written by nx store put.\n"),
        Step("mcp-store-put", [sys.executable, "-c", MCP_STORE_PUT, f"chunk-key smoke note {run_id}: written by the MCP tool.", f"smoke-mcp-{run_id}"]),
    ]
    for u in dt_uuids:
        steps.append(Step("dt-index", [*nx, "dt", "index", "--uuid", u, "--collection", "chunk-key-smoke-dt"]))
    return steps


def child_env(base: dict[str, str], url: str, token: str, config_dir: Path) -> dict[str, str]:
    """The environment of every child: the one loopback engine, an empty config dir, nothing inherited that
    could point a child elsewhere (a service URL, host, port, token or cloud opt-in in the caller's shell)."""
    env = {k: v for k, v in base.items() if not k.startswith(("NX_SERVICE_", "NX_ALLOW_PROD_WRITE", "NEXUS_CONFIG_DIR"))}
    env.update({
        "NX_SERVICE_URL": url, "NX_SERVICE_TOKEN": token, "NEXUS_CONFIG_DIR": str(config_dir),
        "NX_ALLOW_PROD_WRITE": PROD_WRITE_REASON,
    })
    return env


def run_step(step: Step, env: dict[str, str], url: str, token: str, *,
             total: Callable[[], int] | None = None, runner=subprocess.run) -> Result:
    """Run one kind. ``total`` reads the engine's live chunk total; the delta around the run is the chunks written."""
    before = total() if total else 0
    proc = runner(step.argv, input=step.stdin, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    refused = count_refusals(text)
    tail = " | ".join(ln.strip() for ln in text.strip().splitlines()[-3:])
    if refused:
        return Result(step.kind, REFUSED, refused=refused, detail=tail)
    if proc.returncode != 0:
        return Result(step.kind, FAILED, detail=f"exit {proc.returncode}: {tail}")
    wrote = (total() - before) if total else 0
    if total and wrote <= 0:
        return Result(step.kind, FAILED, detail=f"exit 0 but the live chunk total did not move (before {before}): nothing was exercised")
    return Result(step.kind, RAN, chunks=wrote)


def static_rows(dt_uuids: Sequence[str]) -> list[Result]:
    rows = [Result("console", NOT_APPLICABLE, detail="the console serves GET routes only (health, threads); it has no chunk write path")]
    if not dt_uuids:
        rows.append(Result("dt-index", SKIPPED, detail="needs the DEVONthink app and a record: pass --dt-uuid <UUID>; "
                           "the chunk writer it ends in is the one index-pdf drives"))
    return rows


# ── main ─────────────────────────────────────────────────────────────────────


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="loopback engine URL, e.g. http://127.0.0.1:PORT")
    ap.add_argument("--token-file", help="file holding the engine's data token (default: NX_SERVICE_TOKEN)")
    ap.add_argument("--dt-uuid", action="append", default=[], metavar="UUID", help="a DEVONthink record to index (repeatable)")
    ap.add_argument("--plan", action="store_true", help="build the fixtures and print the commands; contact no engine")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args(argv)
    environ = dict(os.environ if environ is None else environ)

    try:
        url = require_loopback(args.url)
        token = "plan-only" if args.plan else resolve_token(args.token_file, environ)
        if not args.plan:
            require_enforce_mode(_get_json(f"{url}/v1/status", token))
    except NotStarted as exc:
        _say(f"not started: {exc}", err=True)
        return EXIT_NOT_STARTED
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _say(f"not started: could not read {url}/v1/status ({exc})", err=True)
        return EXIT_NOT_STARTED

    run_id = uuid.uuid4().hex[:10]
    work = Path(tempfile.mkdtemp(prefix="chunk-key-smoke-"))
    try:
        repo = make_fixture_repo(work, run_id)
        pdf = work / "smoke.pdf"
        make_tiny_pdf(pdf, f"Chunk key smoke document {run_id}. It has a few sentences. Each one is plain text. "
                      "The engine must accept every chunk this writes.")
        config_dir = work / "config"
        config_dir.mkdir()
        steps = build_steps(repo, pdf, run_id, args.dt_uuid)
        if args.plan:
            _say(f"plan only; fixtures in {work} (engine {url} not contacted)")
            for s in steps:
                shown = s.argv[:2] + ["<inline store_put snippet>"] if s.argv[1:2] == ["-c"] else s.argv[:6]
                _say(f"  {s.kind:<14} {' '.join(shown)}{' ...' if len(s.argv) > 6 else ''}")
            for r in static_rows(args.dt_uuid):
                _say(f"  {r.line()}")
            return EXIT_OK
        env = child_env(environ, url, token, config_dir)
        results = [run_step(s, env, url, token, total=lambda: live_chunk_total(url, token)) for s in steps]
        results += static_rows(args.dt_uuid)
    finally:
        if args.keep:
            _say(f"kept {work}", err=True)
        else:
            shutil.rmtree(work, ignore_errors=True)

    for r in results:
        _say(r.line())
    code = exit_code(results)
    ran = sum(1 for r in results if r.status == RAN)
    skipped = sum(1 for r in results if r.status == SKIPPED)
    _say(f"summary: ran {ran}, skipped {skipped}, refused {sum(r.refused for r in results)}, "
          f"failed {sum(1 for r in results if r.status == FAILED)}; exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
