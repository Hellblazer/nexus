#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-client-kind smoke for engine chunk-key enforcement (RDR-236 Phase 4 Step 2c, nexus-6pfgb.66).

Run each client kind's chunk write path once against a LOCAL engine that was started with
``NX_CHUNK_KEY_CHECK_MODE=enforce`` and fail loudly if any write is refused with ``chunk_metadata_key_refused``
(the 422 the engine answers on upsert-chunks, update-metadata and the combined manifest writes append,
append_many and write_many). It is the check that every writer in the tree stays inside the CHUNK key set before
the engine's mode is raised anywhere that matters.

Use an engine and tenant nobody else writes to: a kind's chunks are counted as the change in the live chunk
count of the collections that moved while that kind ran, and a second writer would be counted as this one.

What runs, in this order:

  0. Guards. The URL must be loopback with a valid port; ``GET /v1/status`` must report
     ``chunk_key_check_mode: enforce``; a child process resolves its endpoint the way the ``nx`` children will and
     asserts it is the loopback URL given. Any failure is exit 2 and nothing is written.
  1. store-put   ``nx store put`` from stdin: the first write, and it creates the collection the controls use.
  2. Positive control, one request to each of the five routes (upsert-chunks, update-metadata, manifest/append,
     manifest/append_many, manifest/write_many) with a deliberately bad chunk metadata key. Each MUST answer 422
     ``chunk_metadata_key_refused``. A route that answers anything else fails the run: it proves the engine checks
     that route, and that "refused 0" below is a statement about the writers, not a deaf engine. The control
     names a document id that does not exist, so it assumes the engine checks keys before it looks the document
     up; if a route answers something else for that reason the run fails and says so.
  3. mcp-store-put  the MCP ``store_put`` tool function called in a child interpreter (no Claude needed).
  4. index-repo     ``nx index repo`` on a three-file git repo the script creates in a temp dir.
  5. index-pdf      ``nx index pdf --extractor pymupdf`` on a one-page text PDF the script generates.
  6. index-md       ``nx index md`` on a markdown file: ``doc_indexer.index_markdown``, the writer behind
                    ``index md``, ``index rdr`` and the DEVONthink markdown path.
  7. index-repo-oversize  ``nx index repo`` on a repo whose one markdown file has more chunks than one request
                    carries (more than 300), so the file is written as several requests and reaches
                    ``manifest/append``. It fails if it wrote 300 chunks or fewer (the route was not exercised).
                    The per-request cap cannot be lowered from outside, so the fixture is made bigger instead.
  8. dt-index       ``nx dt index --uuid U`` for each ``--dt-uuid``. DEVONthink's markdown and content records
                    take paths ``index-pdf`` does not, so a run without one is NOT PROVEN for it (exit 4) unless
                    ``--accept-unproven dt-index`` says the operator accepts that.
  console           not applicable: the console serves GET routes only (``tests/test_chunk_key_smoke_script.py``
                    fails the day a write route appears, so this row cannot go stale).

Per kind it prints ``ran, wrote N chunks, refused 0``, ``skipped: <reason>``, ``REFUSED ...`` or ``FAILED ...``,
then a summary. Exit codes:

  0  every required kind ran with no refusal, and every kind not run was accepted with ``--accept-unproven``
  1  a write or a control was refused, a kind failed, wrote fewer chunks than its fixture has files, or timed out
  2  the run was not started (non-loopback URL, bad port, no token, wrong engine mode, an endpoint that is not
     the one given, no ``nx`` beside this interpreter)
  4  incomplete: a kind was not run and not accepted (printed as ``NOT PROVEN: <kind>``)

It contacts only the URL given and refuses one that is not loopback. It has no cloud path at all.

How to run (the operator starts the engine; this script does not):

  NX_CHUNK_KEY_CHECK_MODE=enforce nx daemon service start      # a local engine in enforce mode
  export NX_SERVICE_TOKEN=...                                   # or --token-file: the engine's data token
  uv run python scripts/chunk_key_enforcement_smoke.py --url http://127.0.0.1:<port> \\
      [--dt-uuid <DEVONthink record UUID> | --accept-unproven dt-index]
  uv run python scripts/chunk_key_enforcement_smoke.py --url http://127.0.0.1:<port> --plan   # contacts nothing

Run it through ``uv run python`` so the ``nx`` it drives is this checkout's: the script refuses any other.
``--plan`` builds the fixtures and prints the commands. It exercises nothing and proves nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
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

EXIT_OK, EXIT_REFUSED, EXIT_NOT_STARTED, EXIT_INCOMPLETE = 0, 1, 2, 4

#: What the engine sends (``reason``) and what the client's typed error prints; either marks a refusal.
REFUSAL_REASON = "chunk_metadata_key_refused"
REFUSAL_MARKERS: tuple[str, ...] = (REFUSAL_REASON, "the engine refused this write")

#: The status field the engine reports its enforcement mode in, and the value this smoke requires.
MODE_FIELD, MODE_ENFORCE = "chunk_key_check_mode", "enforce"

#: The five routes that answer the refusal, and the control request each is sent.
ROUTES: tuple[str, ...] = (
    "/v1/vectors/upsert-chunks", "/v1/vectors/update-metadata", "/v1/catalog/manifest/append",
    "/v1/catalog/manifest/append_many", "/v1/catalog/manifest/write_many",
)

#: Chunks one request carries at most (``QUOTAS.MAX_RECORDS_PER_WRITE``): a file with more is written as several.
MAX_RECORDS_PER_WRITE = 300

#: The environment a child inherits from the caller, and nothing else: no NX_MINT_*, VOYAGE_*, NX_LOCAL,
#: NX_CONFIG_DIR or any other setting that could move a child off the loopback engine or onto another credential.
ENV_ALLOW_EXACT: frozenset[str] = frozenset({"PATH", "HOME", "LANG", "TMPDIR", "VIRTUAL_ENV"})
ENV_ALLOW_PREFIXES: tuple[str, ...] = ("LC_", "UV_")

#: Why a child carries NX_ALLOW_PROD_WRITE at all: a dev checkout refuses every write without a reason-bearing
#: opt-in. It is safe here SOLELY because the endpoint is pinned to the loopback URL this script checked first
#: (``require_loopback``, then the child-side assertion in ``ENDPOINT_PROBE``); it must never be set from the
#: caller's shell or for any other URL.
PROD_WRITE_REASON = "chunk-key-enforcement-smoke: writes to a loopback engine the operator started (nexus-6pfgb.66)"

RUN_TIMEOUT_S = 900
HTTP_TIMEOUT_S = 30

RAN, SKIPPED, REFUSED, FAILED, NOT_APPLICABLE = "ran", "skipped", "REFUSED", "FAILED", "n/a"

#: Run in a child, with the env the ``nx`` children get: resolve the endpoint the way the client does and print it.
ENDPOINT_PROBE = (
    "import sys, urllib.parse\n"
    "from nexus.db.service_endpoint import is_loopback_host, resolve_service_endpoint\n"
    "base, _token = resolve_service_endpoint()\n"
    "host = urllib.parse.urlsplit(base).hostname or ''\n"
    "print(base)\n"
    "sys.exit(0 if is_loopback_host(host) else 3)\n"
)

MCP_STORE_PUT = (
    "import sys\n"
    "from nexus.mcp.core import store_put\n"
    "out = store_put(content=sys.argv[1], collection='chunk-key-smoke-mcp', title=sys.argv[2])\n"
    "print(out)\n"
    "sys.exit(0 if out.startswith('Stored:') else 1)\n"
)


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
    collections: tuple[str, ...] = ()

    def line(self) -> str:
        if self.status == RAN and self.detail:
            return f"{self.kind:<20} ran, {self.detail}"
        if self.status == RAN:
            return f"{self.kind:<20} ran, wrote {self.chunks} chunks, refused 0" + (
                f" (collections: {', '.join(self.collections)})" if self.collections else "")
        if self.status == SKIPPED:
            return f"{self.kind:<20} skipped: {self.detail}"
        if self.status == NOT_APPLICABLE:
            return f"{self.kind:<20} not applicable: {self.detail}"
        if self.status == REFUSED:
            return f"{self.kind:<20} REFUSED (chunk_metadata_key_refused x{self.refused}): {self.detail}"
        return f"{self.kind:<20} FAILED: {self.detail}"


# ── guards ───────────────────────────────────────────────────────────────────


def require_loopback(url: str) -> str:
    """The URL without a trailing slash, or ``NotStarted`` unless it is an http(s) URL to a loopback host with a
    valid port. An unparseable URL is ``NotStarted`` too, never a traceback."""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise NotStarted(f"--url {url!r} does not parse: {exc}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise NotStarted(f"--url must be an http(s) URL with a host, got {url!r}")
    if parts.username or parts.password:
        raise NotStarted("--url must not carry user:password@")
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise NotStarted(
                f"refusing {host!r}: this smoke writes chunks and only runs against a loopback engine "
                "(localhost or 127.0.0.1 / ::1). It has no cloud path.")
    if port is None or not 1 <= port <= 65535:
        raise NotStarted(f"--url must name the engine's port, 1 to 65535, got {port!r} in {url!r}")
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


def child_env(base: dict[str, str], url: str, token: str, config_dir: Path) -> dict[str, str]:
    """The environment of every child, from an allowlist: the caller's PATH, HOME, LANG, LC_*, TMPDIR, VIRTUAL_ENV and
    UV_* and nothing else, plus the one loopback engine, an empty config dir and the write opt-in
    (see ``PROD_WRITE_REASON`` for why that one is safe). Whatever else the caller's shell exports, a service
    URL, a mint token, a Voyage key, NX_LOCAL, does not reach a child."""
    env = {k: v for k, v in base.items() if k in ENV_ALLOW_EXACT or k.startswith(ENV_ALLOW_PREFIXES)}
    env.update({
        "NX_SERVICE_URL": url, "NX_SERVICE_TOKEN": token, "NEXUS_CONFIG_DIR": str(config_dir),
        # Safe only because NX_SERVICE_URL above is the loopback URL require_loopback checked and the endpoint probe
        # re-checks in a child before anything is written.
        "NX_ALLOW_PROD_WRITE": PROD_WRITE_REASON,
    })
    return env


def nx_command() -> list[str]:
    """This checkout's ``nx``: the one beside this interpreter. Never the ``nx`` on PATH, which may be another
    install talking to another engine; ``NotStarted`` when there is none (run through ``uv run python``)."""
    beside = Path(sys.executable).parent / "nx"
    if not beside.exists():
        raise NotStarted(f"no nx beside {sys.executable}: run this through `uv run python scripts/{Path(__file__).name}` "
                         "so the nx it drives is this checkout's")
    return [str(beside)]


# ── the engine, read-only and the controls ───────────────────────────────────


def _no_redirect() -> urllib.request.HTTPRedirectHandler:
    """A handler that follows no redirect, so the bearer never travels to a Location the engine names."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_a, **_k):  # noqa: ANN002, ANN003
            return None

    return _NoRedirect()


def _opener() -> urllib.request.OpenerDirector:
    """No proxy (the token must not go to one) and no redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _no_redirect())


def _call(url: str, token: str, body: object | None = None) -> tuple[int, object]:
    """One request with the bearer: ``(status, decoded JSON or the raw text)``. An HTTP error is an answer, not an
    exception; a refused connection or timeout is ``URLError``/``OSError`` for the caller."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with _opener().open(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 — url was checked loopback by require_loopback
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", errors="replace")


def _get_json(url: str, token: str) -> object:
    status, body = _call(url, token)
    if status != 200:
        raise OSError(f"GET {url} answered {status}")
    return body


def collection_counts(base_url: str, token: str, get: Callable[[str, str], object] | None = None) -> dict[str, int]:
    """Live chunk count per collection of the token's tenant (``GET /v1/vectors/stats``)."""
    rows = (get or _get_json)(f"{base_url}/v1/vectors/stats", token)
    if not isinstance(rows, list):
        raise RuntimeError(f"/v1/vectors/stats answered {type(rows).__name__}, not a list")
    return {str(r["name"]): int(r.get("count") or 0) for r in rows if isinstance(r, dict) and r.get("name")}


def moved(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    """``{collection: chunks added}`` for the collections whose live count went up between two snapshots."""
    return {name: n - before.get(name, 0) for name, n in after.items() if n > before.get(name, 0)}


BAD_METADATA = {"title": "chunk-key smoke control", "line_start": 1, "chunk_key_smoke_unknown_key": True}


def control_requests(collection: str, run_id: str) -> dict[str, dict]:
    """The one deliberately bad request each route is sent: chunk metadata naming a document key, a position key and
    an unknown key. The document id is made up: see the module docstring for the assumption that follows."""
    chash = hashlib.sha256(f"chunk-key smoke control {run_id}".encode()).hexdigest()
    rows = [{"position": 0, "chash": chash, "chunk_index": 0}]
    chunk = {"chash": chash, "text": f"chunk-key smoke control {run_id}", "metadata": dict(BAD_METADATA)}
    docs = [{"doc_id": "1.0.0", "rows": rows}]
    return {
        "/v1/vectors/upsert-chunks": {"collection": collection, "ids": [chash], "documents": [chunk["text"]], "metadatas": [dict(BAD_METADATA)]},
        "/v1/vectors/update-metadata": {"collection": collection, "ids": [chash], "metadatas": [dict(BAD_METADATA)]},
        "/v1/catalog/manifest/append": {"doc_id": "1.0.0", "collection": collection, "rows": rows, "chunks": [chunk]},
        "/v1/catalog/manifest/append_many": {"docs": docs, "collection": collection, "chunks": [chunk]},
        "/v1/catalog/manifest/write_many": {"docs": docs, "collection": collection, "chunks": [chunk]},
    }


def judge_control(route: str, status: int, body: object) -> Result:
    """The verdict on one control: only a 422 whose ``reason`` is ``chunk_metadata_key_refused`` is a pass."""
    kind = f"control {route.rsplit('/', 1)[-1]}"
    if status == 422 and isinstance(body, dict) and body.get("reason") == REFUSAL_REASON:
        return Result(kind, RAN, detail="refused as it must be")
    shown = body[:200] if isinstance(body, str) else json.dumps(body)[:200]
    return Result(kind, FAILED, detail=(
        f"{route} answered {status} {shown}, not 422 {REFUSAL_REASON}: the engine does not check this route "
        "(or checks it after the document lookup the control's made-up document id cannot pass)"))


def run_controls(base_url: str, token: str, collection: str, run_id: str, call: Callable | None = None) -> list[Result]:
    """Send every route its control. A transport failure is a FAILED row for that route, never a traceback."""
    out: list[Result] = []
    call = call or _call
    for route, body in control_requests(collection, run_id).items():
        try:
            status, answer = call(f"{base_url}{route}", token, body)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            out.append(Result(f"control {route.rsplit('/', 1)[-1]}", FAILED, detail=f"{route}: {exc}"))
            continue
        out.append(judge_control(route, status, answer))
    return out


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


def _git_repo(repo: Path) -> None:
    git = ["git", "-C", str(repo), "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "smoke fixture"]):
        subprocess.run([*git, *args], check=True, capture_output=True, text=True)


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
    _git_repo(repo)
    return repo


def big_markdown(tag: str, sections: int = 420) -> str:
    """Markdown with *sections* sections of about 1,800 characters each, every one different: one chunk each at the
    default 512-token chunk size, so well over the 300 chunks one request carries."""
    out = [f"# Chunk-key smoke oversize document {tag}\n"]
    for i in range(sections):
        body = " ".join(f"s{i:04d}w{j:03d}{hashlib.sha1(f'{tag}{i}{j}'.encode()).hexdigest()[:6]}" for j in range(230))
        out.append(f"\n## Section {i:04d}\n\n{body}\n")
    return "".join(out)


def make_oversize_repo(root: Path, tag: str) -> Path:
    repo = root / "smoke-big-repo"
    repo.mkdir()
    (repo / "big.md").write_text(big_markdown(tag), encoding="utf-8")
    _git_repo(repo)
    return repo


def make_markdown_file(root: Path, tag: str) -> Path:
    path = root / "smoke-doc.md"
    path.write_text(
        f"# Chunk-key smoke markdown {tag}\n\nIntro paragraph for the markdown writer.\n\n## First section\n\n"
        f"Text of the first section {tag}.\n\n## Second section\n\nText of the second section {tag}.\n", encoding="utf-8")
    return path


# ── kinds ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    kind: str
    argv: list[str]
    stdin: str | None = None
    #: the fewest chunks the run must add, over the collections that moved while it ran
    min_chunks: int = 1


def build_steps(nx: list[str], repo: Path, big_repo: Path, pdf: Path, md: Path, run_id: str, dt_uuids: Sequence[str],
                *, oversize: bool = True) -> dict[str, list[Step]]:
    """The steps by phase: ``first`` (store-put, before the controls), ``rest`` (after them)."""
    first = [Step("store-put", [*nx, "store", "put", "-", "--collection", "chunk-key-smoke", "--title", f"smoke-cli-{run_id}"],
                  stdin=f"chunk-key smoke note {run_id}: written by nx store put.\n")]
    rest = [
        Step("mcp-store-put", [sys.executable, "-c", MCP_STORE_PUT, f"chunk-key smoke note {run_id}: written by the MCP tool.",
                               f"smoke-mcp-{run_id}"]),
        Step("index-repo", [*nx, "index", "repo", str(repo)], min_chunks=3),
        Step("index-pdf", [*nx, "index", "pdf", str(pdf), "--extractor", "pymupdf", "--collection", "chunk-key-smoke-pdf"]),
        Step("index-md", [*nx, "index", "md", str(md), "--collection", "chunk-key-smoke-md"]),
    ]
    if oversize:
        rest.append(Step("index-repo-oversize", [*nx, "index", "repo", str(big_repo)], min_chunks=MAX_RECORDS_PER_WRITE + 1))
    rest += [Step("dt-index", [*nx, "dt", "index", "--uuid", u, "--collection", "chunk-key-smoke-dt"]) for u in dt_uuids]
    return {"first": first, "rest": rest}


def run_step(step: Step, env: dict[str, str], *, counts: Callable[[], dict[str, int]] | None = None,
             runner: Callable | None = None) -> Result:
    """Run one kind. ``counts`` reads the live chunk count per collection; the collections whose count rose while the
    kind ran are its own, and their total rise is the chunks written. A timeout or a lost engine is a FAILED row."""
    runner = runner or subprocess.run
    try:
        before = counts() if counts else {}
        proc = runner(step.argv, input=step.stdin, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False)
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        tail = " | ".join(ln.strip() for ln in text.strip().splitlines()[-3:])
        refused = count_refusals(text)
        if refused:
            return Result(step.kind, REFUSED, refused=refused, detail=tail)
        if proc.returncode != 0:
            return Result(step.kind, FAILED, detail=f"exit {proc.returncode}: {tail}")
        after = counts() if counts else {}
    except subprocess.TimeoutExpired:
        return Result(step.kind, FAILED, detail=f"no answer within {RUN_TIMEOUT_S}s; the child was stopped")
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        return Result(step.kind, FAILED, detail=f"could not read the engine's chunk counts: {exc}")
    if not counts:
        return Result(step.kind, RAN)
    gained = moved(before, after)
    total = sum(gained.values())
    if total < step.min_chunks:
        return Result(step.kind, FAILED, collections=tuple(sorted(gained)), detail=(
            f"exit 0 but only {total} chunks were added (at least {step.min_chunks} expected): "
            + ("nothing was exercised" if total == 0 else "the fixture did not reach what this kind is meant to cover")))
    return Result(step.kind, RAN, chunks=total, collections=tuple(sorted(gained)))


def static_rows(dt_uuids: Sequence[str], *, oversize: bool) -> list[Result]:
    rows = [Result("console", NOT_APPLICABLE, detail="the console serves GET routes only (health, threads); it has no chunk write path")]
    if not dt_uuids:
        rows.append(Result("dt-index", SKIPPED, detail=(
            "needs the DEVONthink app and a record: pass --dt-uuid <UUID>. Its markdown and content records take paths "
            "index-pdf does not cover (index-md covers the markdown writer, not DEVONthink's own)")))
    if not oversize:
        rows.append(Result("index-repo-oversize", SKIPPED, detail=(
            "--skip-oversize: manifest/append is then covered by its positive control alone, not by a real multi-request write")))
    return rows


def exit_code(results: Sequence[Result], accepted: Sequence[str] = ()) -> int:
    """Refusal or failure wins (1); else a kind that did not run and was not accepted is incomplete (4); else 0. A run in
    which no kind ran is a failure (1), whatever else it printed."""
    counted = [r for r in results if r.status != NOT_APPLICABLE]
    if any(r.status in (REFUSED, FAILED) for r in counted):
        return EXIT_REFUSED
    if not any(r.status == RAN and not r.kind.startswith("control ") for r in counted):
        return EXIT_REFUSED  # only a control answering is not a writer exercised
    if any(r.status == SKIPPED and r.kind not in accepted for r in counted):
        return EXIT_INCOMPLETE
    return EXIT_OK


def not_proven(results: Sequence[Result], accepted: Sequence[str] = ()) -> list[str]:
    return [r.kind for r in results if r.status == SKIPPED and r.kind not in accepted]


# ── main ─────────────────────────────────────────────────────────────────────


def probe_endpoint(env: dict[str, str], url: str, runner: Callable | None = None) -> None:
    """Resolve the endpoint in a child with the env the ``nx`` children get and require it to be *url* and loopback.
    ``NotStarted`` otherwise, before any write."""
    proc = (runner or subprocess.run)([sys.executable, "-c", ENDPOINT_PROBE], env=env, capture_output=True, text=True, timeout=120, check=False)
    got = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    if proc.returncode != 0 or got[0].rstrip("/") != url.rstrip("/"):
        raise NotStarted(f"a child resolves its endpoint to {got[0]!r} (exit {proc.returncode}), not the loopback {url!r}: "
                         f"{(proc.stderr or '').strip()[-200:]}")


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="loopback engine URL, e.g. http://127.0.0.1:PORT")
    ap.add_argument("--token-file", help="file holding the engine's data token (default: NX_SERVICE_TOKEN)")
    ap.add_argument("--dt-uuid", action="append", default=[], metavar="UUID", help="a DEVONthink record to index (repeatable)")
    ap.add_argument("--accept-unproven", action="append", default=[], metavar="KIND",
                    help="accept that KIND (dt-index, index-repo-oversize) was not run; without it the run exits 4")
    ap.add_argument("--skip-oversize", action="store_true", help="skip the multi-request write (the 420-section fixture)")
    ap.add_argument("--plan", action="store_true", help="build the fixtures and print the commands; contact nothing, prove nothing")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args(argv)
    environ = dict(os.environ if environ is None else environ)
    url = args.url  # bound before the try: the handlers below name it

    run_id = uuid.uuid4().hex[:10]
    work = Path(tempfile.mkdtemp(prefix="chunk-key-smoke-"))
    try:
        try:
            url = require_loopback(args.url)
            nx = nx_command()
            token = "plan-only" if args.plan else resolve_token(args.token_file, environ)
            if not args.plan:
                require_enforce_mode(_get_json(f"{url}/v1/status", token))
            config_dir = work / "config"
            config_dir.mkdir()
            env = child_env(environ, url, token, config_dir)
            if not args.plan:
                probe_endpoint(env, url)
                ver = subprocess.run([*nx, "--version"], env=env, capture_output=True, text=True, timeout=60, check=False)
                _say(f"nx: {nx[0]} ({(ver.stdout or ver.stderr).strip()})")
        except NotStarted as exc:
            _say(f"not started: {exc}", err=True)
            return EXIT_NOT_STARTED
        except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError) as exc:
            _say(f"not started: could not reach or read {url} ({exc})", err=True)
            return EXIT_NOT_STARTED

        repo = make_fixture_repo(work, run_id)
        big_repo = make_oversize_repo(work, run_id) if not args.skip_oversize else work
        pdf = work / "smoke.pdf"
        make_tiny_pdf(pdf, f"Chunk key smoke document {run_id}. It has a few sentences. Each one is plain text. "
                      "The engine must accept every chunk this writes.")
        md = make_markdown_file(work, run_id)
        phases = build_steps(nx, repo, big_repo, pdf, md, run_id, args.dt_uuid, oversize=not args.skip_oversize)
        if args.plan:
            _say("PLAN ONLY: nothing was run or contacted, so this proves nothing about enforcement.")
            _say(f"fixtures in {work}; the engine {url} was not contacted")
            for s in [*phases["first"], *phases["rest"]]:
                shown = s.argv[:2] + ["<inline snippet>"] if s.argv[1:2] == ["-c"] else s.argv[:6]
                _say(f"  {s.kind:<20} {' '.join(shown)}{' ...' if len(s.argv) > 6 else ''}")
            _say(f"  controls: one bad-key request to each of {len(ROUTES)} routes after store-put")
            for r in static_rows(args.dt_uuid, oversize=not args.skip_oversize):
                _say(f"  {r.line()}")
            return EXIT_OK

        def counts() -> dict[str, int]:
            return collection_counts(url, token)

        results: list[Result] = []
        for step in phases["first"]:
            results.append(run_step(step, env, counts=counts))
        control_collection = next((c for r in results if r.status == RAN for c in r.collections), None)
        if control_collection is None:
            results.append(Result("controls", FAILED, detail="store-put wrote no collection to send the controls to"))
        else:
            results.extend(run_controls(url, token, control_collection, run_id))
        if all(r.status == RAN for r in results):
            for step in phases["rest"]:
                results.append(run_step(step, env, counts=counts))
        else:
            _say("stopping after the first write and the controls: the rest would prove nothing", err=True)
        results += static_rows(args.dt_uuid, oversize=not args.skip_oversize)
    finally:
        if args.keep:
            _say(f"kept {work}", err=True)
        else:
            shutil.rmtree(work, ignore_errors=True)

    for r in results:
        _say(r.line())
    accepted = list(args.accept_unproven)
    code = exit_code(results, accepted)
    for kind in not_proven(results, accepted):
        _say(f"NOT PROVEN: {kind}")
    for kind in (r.kind for r in results if r.status == SKIPPED and r.kind in accepted):
        _say(f"accepted unproven: {kind}")
    ran = sum(1 for r in results if r.status == RAN and not r.kind.startswith("control "))
    _say(f"summary: ran {ran}, not run {sum(1 for r in results if r.status == SKIPPED)}, refused {sum(r.refused for r in results)}, "
         f"failed {sum(1 for r in results if r.status == FAILED)}; exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
