# SPDX-License-Identifier: AGPL-3.0-or-later
# ruff: noqa: T201
"""The pre-tag cloud leg's logic: refusals, the step plan, the slot runner and the comparator.

tests/e2e/cloud-leg.sh is the driver; everything testable lives here (tests/scripts/test_cloud_leg.py). Standard
library only, Python 3.11+ (it imports scripts/check_wire_contract_pairing.py, which reads tomllib).

THE LEG: one step list runs through the real ``nx`` client (and a few candidate-python snippets over the client's
own classes) twice, once against a local engine and once against the cloud test tenant, each in its own scratch
HOME. Every HTTP exchange the client makes is recorded at the client boundary by tests/e2e/lib/cloud_leg_tap
(method, route, status; never a header, query or body). A step FAILS when the cloud run returns a 4xx/5xx the local
run does not (the 8.5.0 faults were of this shape: a CDN that stripped X-Nexus-Identity-Token answered 403
identity_token_required, a data credential that could not create seats answered 403), or exits non-zero where the
local run exits zero.

WINDOW. Nothing in the repo announces a cloud window mechanically (AGENTS.md: "a cloud-touching walk runs only
inside an announced window"), so the leg defines one: a marker file ``<window dir>/<id>.window`` of ``key: value``
lines, written by the releaser after the announcement. Keys: ``window`` (the id), ``scope`` (``cloud-leg``),
``tenant`` (``release-test``), ``opened_by``, ``announced`` (where and in whose words), ``opened_at`` and
``expires_at`` (ISO 8601 UTC). The leg refuses unless the file exists, names this id, scope and tenant match,
and now lies between the two instants. The window dir is ``$NX_CLOUD_WINDOW_DIR`` or
``~/.config/nexus-cloud-windows`` of the OPERATOR's real HOME (read before any sandbox exists).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The only tenant the leg may write to. Compared exactly with the credential's mint_tenant.
REQUIRED_TENANT = "release-test"
WINDOW_SCOPE = "cloud-leg"
WINDOW_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RUN_ID_RE = re.compile(r"^[a-z0-9]{6,24}$")

#: The fixed core never shrinks below this many examined (non-cleanup, non-optional) steps; see core_floor().
STEP_TIMEOUT_S = 180

TAP_DIR = Path(__file__).resolve().parent / "cloud_leg_tap"


# ── refusals ────────────────────────────────────────────────────────────────

class Refusal(Exception):
    """The leg refuses to run; the message is safe to print (it never carries a credential)."""


def parse_kv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def window_dir(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("NX_CLOUD_WINDOW_DIR") or (Path(env.get("HOME", "~")).expanduser() / ".config" / "nexus-cloud-windows"))


def check_window(window_id: str, directory: Path, now: datetime | None = None) -> str:
    """Return a one-line description of the open window, or raise Refusal."""
    if not window_id or not WINDOW_ID_RE.match(window_id):
        raise Refusal("no announced cloud window: --window <id> is required and must match [A-Za-z0-9._-]{1,64}")
    marker = Path(directory) / f"{window_id}.window"
    if not marker.is_file():
        raise Refusal(f"no announced cloud window: {marker} does not exist (the releaser writes it after announcing the window)")
    doc = parse_kv(marker.read_text(encoding="utf-8"))
    if doc.get("window") != window_id:
        raise Refusal(f"window marker {marker.name} names window '{doc.get('window')}', not '{window_id}'")
    if doc.get("scope") != WINDOW_SCOPE:
        raise Refusal(f"window {window_id} has scope '{doc.get('scope')}', not '{WINDOW_SCOPE}'")
    if doc.get("tenant") != REQUIRED_TENANT:
        raise Refusal(f"window {window_id} is for tenant '{doc.get('tenant')}', not '{REQUIRED_TENANT}'")
    try:
        opened, expires = _parse_instant(doc["opened_at"]), _parse_instant(doc["expires_at"])
    except (KeyError, ValueError):
        raise Refusal(f"window {window_id} has no readable opened_at/expires_at (ISO 8601)") from None
    moment = now or datetime.now(timezone.utc)
    if not opened <= moment < expires:
        raise Refusal(f"window {window_id} is not in effect now: {doc['opened_at']} to {doc['expires_at']}")
    return f"window {window_id} in effect until {doc['expires_at']} (announced: {doc.get('announced', '(not recorded)')})"


def _ambient_credentials(environ: dict[str, str], config_path: Path | None) -> dict[str, str]:
    """The operator's own service credentials, env first then config.yml's ``credentials:`` block. Values are
    returned for HASH comparison only and are never printed."""
    keys = {"service_url": "NX_SERVICE_URL", "service_token": "NX_SERVICE_TOKEN", "mint_token": "NX_MINT_TOKEN",
            "mint_tenant": "NX_MINT_TENANT"}
    found: dict[str, str] = {}
    if config_path and config_path.is_file():
        in_block = False
        for raw in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            if not raw.startswith((" ", "\t")):
                in_block = raw.strip() == "credentials:"
                continue
            if in_block and ":" in raw:
                k, v = raw.strip().split(":", 1)
                if k in keys:
                    found[k] = v.strip().strip("'\"")
    for k, var in keys.items():
        if environ.get(var):
            found[k] = environ[var]
    return found


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_target(*, service_url: str, token: str, mint_tenant: str, item_title: str,
                 ambient_environ: dict[str, str], ambient_config: Path | None) -> None:
    """Refuse unless the credential is the test tenant's, on the public https edge. Raises Refusal."""
    if not service_url.startswith("https://"):
        raise Refusal("service_url is not the public https edge (https:// required)")
    rest = service_url[len("https://"):].split("/", 1)[0]
    host = rest.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if "@" in rest:
        raise Refusal("service_url carries user:password@, which is refused")
    if host in {"localhost", "127.0.0.1", "::1", ""} or host.endswith(".local") or re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        raise Refusal("service_url is not the public https edge (a loopback or private host)")
    if "test" not in item_title.lower():
        raise Refusal("the 1Password item title does not name a test tenant (it must contain 'test')")
    if mint_tenant != REQUIRED_TENANT:
        raise Refusal(f"the credential's mint_tenant is '{mint_tenant}', not '{REQUIRED_TENANT}': refusing a tenant that is not the test tenant")
    if not token:
        raise Refusal("the credential's token is empty")
    ambient = _ambient_credentials(ambient_environ, ambient_config)
    th = _sha(token)
    for key in ("service_token", "mint_token"):
        if ambient.get(key) and _sha(ambient[key]) == th:
            raise Refusal(f"the test credential is the operator's own ambient {key}: refusing the operator's normal credential")
    if ambient.get("mint_tenant") and ambient["mint_tenant"] != REQUIRED_TENANT and ambient.get("mint_token") \
            and _sha(ambient["mint_token"]) == th:
        raise Refusal("the test credential is bound to the operator's own tenant")


# ── the step plan ───────────────────────────────────────────────────────────

@dataclass
class Step:
    name: str
    kind: str                       # "nx": the installed nx CLI; "py": the candidate venv's python -c <argv[1]>
    argv: list[str]
    core: bool = False
    cleanup: bool = False
    optional_verb: str | None = None   # the nx command group a client may lack (the step is then SKIPPED)
    identity: bool = False          # attach NX_IDENTITY_TOKEN (the bound identity's token file)
    needs: list[str] = field(default_factory=list)      # variables an earlier step must have captured
    capture: dict[str, str] = field(default_factory=dict)  # var -> "json:<key>" | "re:<pattern>"
    stdin: str | None = None
    expect_stdout: str | None = None   # a substring a passing run prints (the round trip)
    expect_route: str | None = None    # a route the client must have called: a client that quietly skips a
                                       # route it believes the engine lacks (a stripped /version flag) would
                                       # otherwise exit 0 having exercised nothing
    origin: str = "core"           # "core", or the ledger entry's "<sha7> <bead> <route>"


_IDENTITY_BIND = r"""
import json, os, socket, time, uuid
from nexus import config
from nexus.identity import token_contract
from nexus.identity.bind_client import IdentityClient, evidence_body
from nexus.identity.delegated import delegated_token_path
from nexus.identity.token_file import TokenFile, identity_dir, write_token_file
c = IdentityClient.from_config()
now = int(time.time() * 1000)
host = socket.gethostname()
view = c.create_ephemeral(display_name="@label@", created_by="session",
                          evidence=evidence_body(session_id=str(uuid.uuid4()), host=host, pid=os.getpid(), started_at=now))
tok, life = token_contract.token_from_response(view)
path = delegated_token_path(identity_dir(config.nexus_config_dir()), view["id"])
write_token_file(path, TokenFile(identity=view["id"], generation=int(view["generation"]), token=tok,
                                 expires_at=token_contract.expires_at(life), session_id=None, pid=os.getpid(),
                                 started_at=now, proc_start=None, host=host, registry_dir=None))
print(json.dumps({"identity": view["id"], "token_file": str(path)}))
"""

_BY_AUTHOR = r"""
import json
from nexus.mcp.catalog import catalog_search
print(catalog_search(author="@label@"))
"""

_LOCATORS = r"""
import json
from nexus.commands.catalog import _get_catalog
print(json.dumps(_get_catalog().sensitive_locators(limit=10), default=str)[:400])
"""


def core_steps() -> list[Step]:
    t = "threads"
    return [
        Step("identity-seat-create", "nx", ["identity", "create", "@label@-seat"], core=True, optional_verb="identity",
             expect_route="/v1/identity"),
        Step("identity-seat-show", "nx", ["identity", "show", "@label@-seat", "--json"], core=True, optional_verb="identity"),
        Step("identity-bind", "py", ["-c", _IDENTITY_BIND], core=True,
             capture={"identity": "json:identity", "token_file": "json:token_file"}, expect_route="/v1/identity"),
        Step("memory-put-threads-identity", "nx",
             ["memory", "put", "-", "-p", t, "-t", "@label@", "--tags", "cloudleg"], core=True, identity=True,
             needs=["identity"], expect_route="/v1/memory/put",
             stdin="session: @label@\nidentity: @identity@\n\n## cloud leg write probe @label@\nstate: written\n"),
        Step("memory-get-threads", "nx", ["memory", "get", "-p", t, "-t", "@label@"], core=True, needs=["identity"],
             expect_stdout="@label@"),
        Step("mailbox-send", "nx", ["tuple", "out", "mailbox/@identity@", "--key", "to=@identity@", "--dim", "from=@identity@",
                                    "--dim", "kind=cloudleg", "--nonce", "@label@-n1", "--body", "@label@"],
             core=True, identity=True, needs=["identity"], expect_route="/v1/tuples/out"),
        Step("mailbox-claim", "nx", ["tuple", "in", "mailbox/@identity@", "--pattern", "to=@identity@", "--claimant",
                                     "@identity@", "--lease-s", "60", "--json"], core=True, identity=True,
             needs=["identity"], capture={"claim_id": "json:claim_id"}, expect_route="/v1/tuples/in"),
        Step("mailbox-ack", "nx", ["tuple", "ack", "@claim_id@", "--claimant", "@identity@"], core=True, identity=True,
             needs=["identity", "claim_id"], expect_route="/v1/tuples/ack"),
        Step("scratch-put", "nx", ["scratch", "put", "@label@ scratch note", "--tags", "cloudleg"], core=True,
             capture={"scratch_id": "re:([0-9a-f]{8,})"}, expect_route="/v1/t1/put"),
        Step("scratch-get", "nx", ["scratch", "get", "@scratch_id@"], core=True, needs=["scratch_id"],
             expect_stdout="@label@"),
        Step("store-put", "nx", ["store", "put", "-", "-c", "cloud-leg-probe", "-t", "@label@", "--tags", "cloudleg"],
             core=True, expect_route="/v1/catalog/manifest/write_many",
             stdin="@label@ is a release-candidate cloud leg round-trip note about pelagic cobalt lanterns.\n"),
        Step("store-search", "nx", ["search", "pelagic cobalt lanterns @label@", "--corpus", "knowledge", "-m", "5"],
             core=True, expect_stdout="@label@", expect_route="/v1/vectors/search"),
        # cleanup, run in this order after the rest
        Step("store-delete", "nx", ["store", "delete", "-c", "cloud-leg-probe", "--title", "@label@", "--yes"],
             core=True, cleanup=True),
        Step("scratch-delete", "nx", ["scratch", "delete", "@scratch_id@"], core=True, cleanup=True, needs=["scratch_id"]),
        Step("memory-delete-threads", "nx", ["memory", "delete", "-p", t, "-t", "@label@", "--yes"], core=True,
             cleanup=True, identity=True, needs=["identity"]),
    ]


#: Release additions: a wire-ledger route -> the client call that exercises it. A route absent here is SKIPPED.
ROUTE_MAP: dict[str, Step] = {
    "GET /v1/catalog/docs/by-scheme": Step("route-docs-by-scheme", "nx", ["doctor", "--check-references"],
                                              expect_route="/v1/catalog/docs/by-scheme"),
    "GET /v1/catalog/docs/by-meta": Step("route-docs-by-meta", "nx", ["catalog", "list", "--meta", "cloudleg=@run@", "--json"],
                                           expect_route="/v1/catalog/docs/by-meta"),
    "GET /v1/catalog/docs/by-author": Step("route-docs-by-author", "py", ["-c", _BY_AUTHOR], expect_route="/v1/catalog/docs/by-author"),
    "GET /v1/catalog/sensitive/locators": Step("route-sensitive-locators", "py", ["-c", _LOCATORS], expect_route="/v1/catalog/sensitive/locators"),
}

_ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[A-Za-z0-9_\-/.{}]+)")


def _load_ledger_module() -> Any:
    path = REPO_ROOT / "scripts" / "check_wire_contract_pairing.py"
    spec = importlib.util.spec_from_file_location("check_wire_contract_pairing", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod  # type: ignore[union-attr]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def ledger_entries(ledger: Path, release: str | None = None) -> list[Any]:
    """The `## Unshipped` entries of the wire ledger (docs/wire-contract-pending.md), or, with *release*, the
    `## Shipped` entries whose `shipped in` is that tag. Parsed by scripts/check_wire_contract_pairing.parse_ledger,
    the one parser of the format."""
    parsed = _load_ledger_module().parse_ledger(Path(ledger))
    if release is None:
        return list(parsed.unshipped.values())
    return [e for e in parsed.shipped.values() if e.shipped_in == release]


def release_steps(entries: list[Any]) -> tuple[list[Step], list[tuple[str, str]]]:
    """(steps, skipped): one step per distinct mapped route; every other entry or route is SKIPPED with a reason."""
    steps: list[Step] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in entries:
        tag = f"{e.sha[:7]} {e.bead}"
        routes = list(dict.fromkeys(f"{m} {p}" for m, p in _ROUTE_RE.findall(e.note)))
        if not routes:
            skipped.append((tag, "no wire route named in the entry (text-only or a flag change)"))
            continue
        for r in routes:
            if r in seen:
                continue
            seen.add(r)
            mapped = ROUTE_MAP.get(r)
            if mapped is None:
                skipped.append((f"{tag} {r}", "no client call is mapped to this route"))
                continue
            steps.append(Step(**{**asdict(mapped), "origin": f"{tag} {r}"}))
    return steps, skipped


def build_plan(run: str, ledger: Path | None, release: str | None) -> dict[str, Any]:
    if not RUN_ID_RE.match(run):
        raise ValueError(f"run id {run!r} must match {RUN_ID_RE.pattern}")
    entries = ledger_entries(ledger, release) if ledger else []
    rsteps, skipped = release_steps(entries)
    main = [s for s in core_steps() if not s.cleanup]
    cleanup = [s for s in core_steps() if s.cleanup]
    steps = main + rsteps + cleanup
    return {"run": run, "label": f"cloudleg-{run}", "steps": [asdict(s) for s in steps],
            "skipped": [list(x) for x in skipped], "ledger_entries": len(entries), "floor": core_floor(steps)}


def core_floor(steps: list[Step] | list[dict[str, Any]]) -> int:
    """The non-vacuity floor: the fixed core's steps that a client must support (not cleanup, not optional-verb)."""
    n = 0
    for s in steps:
        d = s if isinstance(s, dict) else asdict(s)
        if d["core"] and not d["cleanup"] and not d["optional_verb"]:
            n += 1
    return n


# ── running one slot ────────────────────────────────────────────────────────

def _fill(text: str, variables: dict[str, str]) -> str:
    for k, v in variables.items():
        text = text.replace(f"@{k}@", v)
    return text


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if len(s) >= 6:
            text = text.replace(s, "***")
    return text


def read_secrets(sandbox: Path) -> list[str]:
    out = []
    d = Path(sandbox) / "secrets"
    if d.is_dir():
        for f in d.iterdir():
            if f.is_file():
                out.append(f.read_text(encoding="utf-8").strip())
    return [s for s in out if s]


def parse_tap(path: Path) -> tuple[bool, list[tuple[int, str, str]]]:
    """(tap-ready, [(status, METHOD, normalized route)]) from one step's tap file."""
    ready, rows = False, []
    if Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line == "#tap-ready":
                ready = True
                continue
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].lstrip("-").isdigit():
                rows.append((int(parts[0]), parts[1], norm_route(parts[2])))
    return ready, rows


_ID_SEG = re.compile(r"^(i\d+|\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f-]{27}|.*cloudleg-.*)$")


def norm_route(path: str) -> str:
    return "/".join("{id}" if _ID_SEG.match(seg) else seg for seg in path.split("/"))


def _json_tail(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                return doc
    try:
        doc = json.loads(stdout)
        return doc if isinstance(doc, dict) else None
    except ValueError:
        return None


def _capture(spec: str, stdout: str) -> str | None:
    kind, _, arg = spec.partition(":")
    if kind == "json":
        doc = _json_tail(stdout)
        if doc is None:
            return None
        cur: Any = doc
        for part in arg.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            for v in doc.values():  # claim rows may nest the id one level down
                if isinstance(v, dict) and arg in v:
                    return str(v[arg])
                if isinstance(v, list) and v and isinstance(v[0], dict) and arg in v[0]:
                    return str(v[0][arg])
            return None
        return str(cur)
    if kind == "re":
        m = re.search(arg, stdout)
        return m.group(1) if m else None
    return None


def run_slot(plan: dict[str, Any], *, runner: str, sandbox: Path, host: str, workdir: Path, slot: str,
             clients_have: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """Run every step of *plan* in one sandbox. Returns one result dict per step."""
    sandbox = Path(sandbox)
    secrets = read_secrets(sandbox)
    nx = str(sandbox / "home/.local/bin/nx")
    py = str(sandbox / "home/.local/share/uv/tools/conexus/bin/python")
    variables = {"run": plan["run"], "label": plan["label"]}
    Path(workdir).mkdir(parents=True, exist_ok=True)
    verbs: dict[str, bool] = dict(clients_have or {})
    results: list[dict[str, Any]] = []
    for n, step in enumerate(plan["steps"]):
        res: dict[str, Any] = {"name": step["name"], "slot": slot, "exit": None, "taps": [], "tap_ready": False,
                               "stdout": "", "error": "", "skipped": None, "blocked": None}
        verb = step["optional_verb"]
        if verb:
            if verb not in verbs:
                probe = subprocess.run([runner, nx, verb, "--help"], capture_output=True, text=True, timeout=60)
                verbs[verb] = probe.returncode == 0
            if not verbs[verb]:
                res["skipped"] = f"client has no `nx {verb}` command"
                results.append(res)
                continue
        missing = [v for v in step["needs"] if v not in variables]
        if missing:
            res["blocked"] = "needs " + ", ".join(missing) + " from an earlier step"
            res["exit"] = -1
            results.append(res)
            continue
        tapfile = Path(workdir) / f"tap-{slot}-{n:02d}-{step['name']}.log"
        tapfile.unlink(missing_ok=True)
        extras = [f"PYTHONPATH={TAP_DIR}", f"NX_CLOUDLEG_TAP={tapfile}", f"NX_CLOUDLEG_HOST={host}"]
        if step["identity"] and "token_file" in variables:
            extras.append(f"NX_IDENTITY_TOKEN={variables['token_file']}")
        argv = [_fill(a, variables) for a in step["argv"]]
        exe = [nx] if step["kind"] == "nx" else [py]
        cmd = [runner, *extras, *exe, *argv]
        stdin = _fill(step["stdin"], variables) if step["stdin"] is not None else None
        try:
            cp = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=STEP_TIMEOUT_S)
            out, err, code = cp.stdout, cp.stderr, cp.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", "TimeoutExpired", 124
        res["exit"] = code
        res["tap_ready"], taps = parse_tap(tapfile)
        res["taps"] = [list(t) for t in taps]
        res["stdout"] = _scrub(out, secrets)[:600]
        first_err = next((ln for ln in reversed(err.strip().splitlines()) if ln.strip()), "")
        res["error"] = _scrub(first_err, secrets)[:160]
        want = step["expect_stdout"]
        res["expect_ok"] = True if want is None else (_fill(want, variables) in out)
        res["expect_route"] = step.get("expect_route")
        if code == 0:
            for var, spec in step["capture"].items():
                got = _capture(spec, out)
                if got:
                    variables[var] = got
        results.append(res)
    return results


# ── the comparator ──────────────────────────────────────────────────────────

BAD = lambda status: status >= 400 or status == 0  # noqa: E731


def _bad_set(res: dict[str, Any]) -> set[tuple[str, str, int]]:
    return {(m, r, s) for s, m, r in res["taps"] if BAD(s)}


def _cell(res: dict[str, Any]) -> str:
    if res.get("skipped"):
        return "skipped"
    if res.get("blocked"):
        return "blocked"
    bad = sorted({s for s, _m, _r in res["taps"] if BAD(s)})
    head = f"exit {res['exit']}" if res["exit"] else "ok"
    if bad:
        return head + " " + ",".join(str(s) for s in bad)
    return head + (f" ({len(res['taps'])} req)" if res["taps"] else "")


def judge_step(local: dict[str, Any], cloud: dict[str, Any]) -> tuple[str, str]:
    """(verdict, reason). Verdicts: PASS, PASS-both-refused, FAIL, SKIPPED."""
    if local.get("skipped") or cloud.get("skipped"):
        return "SKIPPED", local.get("skipped") or cloud.get("skipped") or ""
    for res in (local, cloud):
        if res.get("blocked"):
            return "FAIL", f"{res['slot']}: blocked, {res['blocked']}"
    for res in (local, cloud):
        if not res["tap_ready"]:
            return "FAIL", f"{res['slot']}: the HTTP tap never installed, so no status was seen"
        if res["exit"] == 0 and not res["taps"]:
            return "FAIL", f"{res['slot']}: the step exited 0 but no HTTP request was seen at the client boundary"
    cloud_only = sorted(_bad_set(cloud) - _bad_set(local))
    if cloud_only:
        m, r, s = cloud_only[0]
        more = f" (+{len(cloud_only) - 1} more)" if len(cloud_only) > 1 else ""
        return "FAIL", f"cloud-only {s} on {m} {r}{more}; local returned none of it"
    if cloud["exit"] != 0 and local["exit"] == 0:
        return "FAIL", f"cloud exit {cloud['exit']} where local exited 0: {cloud['error'] or 'no error text'}"
    if local["exit"] != 0 and cloud["exit"] == 0:
        return "FAIL", f"local-only failure (exit {local['exit']}): {local['error'] or 'no error text'}"
    if local["exit"] != 0 and cloud["exit"] != 0:
        if _bad_set(local) and _bad_set(local) == _bad_set(cloud):
            return "PASS-both-refused", "the same refusal in both slots; the path was not exercised past it"
        return "FAIL", f"failed in both slots without a matching refusal (exit {cloud['exit']}): {cloud['error']}"
    for res in (local, cloud):
        want = res.get("expect_route")
        if want and res["exit"] == 0 and not any(r == want for _s, _m, r in res["taps"]):
            return "FAIL", f"{res['slot']}: the client never called {want} (it skipped the route, e.g. the engine's capability flag is not visible)"
    for res in (local, cloud):
        if not res.get("expect_ok", True):
            return "FAIL", f"{res['slot']}: the round trip did not print its own text back"
    return "PASS", ""


@dataclass
class Report:
    rows: list[dict[str, str]]
    examined: int
    skipped: list[tuple[str, str]]
    failed: int
    both_refused: int
    nonvacuity: list[str]

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.nonvacuity

    def render(self) -> str:
        w = max([len(r["step"]) for r in self.rows] + [4])
        lines = [f"{'step'.ljust(w)}  {'local':<14} {'cloud':<18} verdict"]
        for r in self.rows:
            lines.append(f"{r['step'].ljust(w)}  {r['local']:<14} {r['cloud']:<18} {r['verdict']}" + (f"  - {r['reason']}" if r["reason"] else ""))
        reasons = "; ".join(f"{a}: {b}" for a, b in self.skipped) or "none"
        lines.append("")
        lines.append(f"examined {self.examined}, skipped {len(self.skipped)} ({reasons}), failed {self.failed}"
                     + (f", both-refused {self.both_refused}" if self.both_refused else ""))
        for msg in self.nonvacuity:
            lines.append(f"NON-VACUITY FAILURE: {msg}")
        lines.append("CLOUD LEG PASSED" if self.ok else "CLOUD LEG FAILED")
        return "\n".join(lines)


def compare(plan: dict[str, Any], local: list[dict[str, Any]], cloud: list[dict[str, Any]], floor: int | None = None) -> Report:
    floor = plan.get("floor", 0) if floor is None else floor
    rows: list[dict[str, str]] = []
    examined = failed = refused = 0
    skipped = [(a, b) for a, b in plan.get("skipped", [])]
    by_local = {r["name"]: r for r in local}
    by_cloud = {r["name"]: r for r in cloud}
    for step in plan["steps"]:
        name = step["name"]
        if name not in by_local or name not in by_cloud:
            rows.append({"step": name, "local": "-", "cloud": "-", "verdict": "FAIL", "reason": "no result from a slot"})
            failed += 1
            examined += 1
            continue
        verdict, reason = judge_step(by_local[name], by_cloud[name])
        rows.append({"step": name, "local": _cell(by_local[name]), "cloud": _cell(by_cloud[name]), "verdict": verdict, "reason": reason})
        if verdict == "SKIPPED":
            skipped.append((name, reason))
            continue
        examined += 1
        if verdict == "FAIL":
            failed += 1
        elif verdict == "PASS-both-refused":
            refused += 1
    nonv = []
    if examined == 0:
        nonv.append("every step was skipped or none ran; nothing was examined")
    elif examined < floor:
        nonv.append(f"examined {examined} is below the floor of {floor} (the fixed core)")
    return Report(rows, examined, skipped, failed, refused, nonv)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _read_file(p: Path) -> str:
    return Path(p).read_text(encoding="utf-8").strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cloud_leg.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("check-window"); w.add_argument("--window", required=True); w.add_argument("--dir")
    t = sub.add_parser("check-target"); t.add_argument("--cred-dir", required=True); t.add_argument("--item-title", required=True)
    t.add_argument("--ambient-config")
    p = sub.add_parser("plan"); p.add_argument("--run", required=True); p.add_argument("--ledger"); p.add_argument("--release")
    p.add_argument("--out", required=True)
    r = sub.add_parser("run-slot"); r.add_argument("--plan", required=True); r.add_argument("--slot", required=True)
    r.add_argument("--sandbox", required=True); r.add_argument("--host", required=True); r.add_argument("--workdir", required=True)
    r.add_argument("--out", required=True)
    c = sub.add_parser("compare"); c.add_argument("--plan", required=True); c.add_argument("--local", required=True)
    c.add_argument("--cloud", required=True); c.add_argument("--floor", type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check-window":
            print(check_window(a.window, Path(a.dir) if a.dir else window_dir()))
            return 0
        if a.cmd == "check-target":
            cd = Path(a.cred_dir)
            amb = Path(os.environ.get("NEXUS_CONFIG_DIR") or Path.home() / ".config" / "nexus") / "config.yml"
            check_target(service_url=_read_file(cd / "service_url"), token=_read_file(cd / "token"),
                         mint_tenant=_read_file(cd / "mint_tenant") if (cd / "mint_tenant").is_file() else "",
                         item_title=a.item_title, ambient_environ=dict(os.environ),
                         ambient_config=Path(a.ambient_config) if a.ambient_config else amb)
            print("target ok: https edge, test tenant, not the operator's credential")
            return 0
        if a.cmd == "plan":
            plan = build_plan(a.run, Path(a.ledger) if a.ledger else None, a.release)
            Path(a.out).write_text(json.dumps(plan, indent=1), encoding="utf-8")
            print(f"plan: {len(plan['steps'])} steps ({plan['floor']} floor), {len(plan['skipped'])} skipped, "
                  f"{plan['ledger_entries']} ledger entries")
            return 0
        if a.cmd == "run-slot":
            plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            res = run_slot(plan, runner=str(Path(a.sandbox) / "run"), sandbox=Path(a.sandbox), host=a.host,
                           workdir=Path(a.workdir), slot=a.slot)
            Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
            print(f"slot {a.slot}: {len(res)} steps run")
            return 0
        if a.cmd == "compare":
            plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            rep = compare(plan, json.loads(Path(a.local).read_text()), json.loads(Path(a.cloud).read_text()), a.floor)
            print(rep.render())
            return 0 if rep.ok else 1
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
