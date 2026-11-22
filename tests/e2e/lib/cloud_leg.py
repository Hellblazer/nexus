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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The only tenant the leg may write to. Compared exactly with the credential's mint_tenant.
REQUIRED_TENANT = "release-test"
WINDOW_SCOPE = "cloud-leg"
WINDOW_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RUN_ID_RE = re.compile(r"^[a-z0-9]{6,24}$")

#: Wall-clock bound for one step's process, in seconds.
STEP_TIMEOUT_S = 180

#: The only edge host the leg will talk to. NX_CLOUDLEG_EDGE_HOST_OVERRIDE names one more, explicitly; the report prints it.
ALLOWED_EDGE_HOSTS = frozenset({"api.conexus-nexus.com"})
EDGE_OVERRIDE_ENV = "NX_CLOUDLEG_EDGE_HOST_OVERRIDE"

#: A window marker may not span more than this (it records an announcement; the leg cannot verify one).
MAX_WINDOW = timedelta(hours=4)

#: store-search reads its own write through an index that may lag: this many tries, this long apart (recorded).
SEARCH_TRIES, SEARCH_WAIT_S = 3, 5.0

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
    st = marker.stat()
    if st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o600:
        raise Refusal(f"window marker {marker.name} must be owned by the current user with mode 0600 (it is {oct(st.st_mode & 0o777)}, uid {st.st_uid})")
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
    if expires - opened > MAX_WINDOW:
        raise Refusal(f"window {window_id} spans {expires - opened}, more than the {MAX_WINDOW} the leg accepts")
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


def normalize_secret(text: str) -> str:
    """The ONE reader of a credential value: surrounding whitespace dropped, one line only. check_target compares this
    and the sandbox sends this (cloud-leg.sh rewrites each credential file through it before either happens)."""
    value = text.strip()
    if not value or "\n" in value or "\r" in value or any(ord(c) < 32 for c in value):
        raise Refusal("a credential value is empty or spans more than one line")
    return value


def normalize_cred_dir(directory: Path) -> None:
    for f in Path(directory).iterdir():
        if f.is_file() and not f.name.startswith("_"):
            value = normalize_secret(f.read_text(encoding="utf-8"))
            os.chmod(f, 0o600)
            fd = os.open(f, os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(value + "\n")


def item_title_from_json(text: str) -> str:
    try:
        title = json.loads(text).get("title")
    except (ValueError, AttributeError):
        title = None
    if not isinstance(title, str) or not title.strip():
        raise Refusal("the 1Password item answered no title (the item was not read)")
    return title


def check_target(*, service_url: str, token: str, mint_tenant: str, item_title: str,
                 ambient_environ: dict[str, str], ambient_config: Path | None) -> str:
    """Refuse unless the credential is the test tenant's, on the pinned public https edge. Raises Refusal.
    *item_title* is the 1Password item's own title as the vault returned it. Returns the edge host."""
    if not service_url.startswith("https://"):
        raise Refusal("service_url is not the public https edge (https:// required)")
    rest = service_url[len("https://"):].split("/", 1)[0]
    host = rest.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if "@" in rest:
        raise Refusal("service_url carries user:password@, which is refused")
    if host in {"localhost", "127.0.0.1", "::1", ""} or host.endswith(".local") or re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        raise Refusal("service_url is not the public https edge (a loopback or private host)")
    override = (ambient_environ.get(EDGE_OVERRIDE_ENV) or "").strip().lower()
    if host not in ALLOWED_EDGE_HOSTS and host != override:
        raise Refusal(f"service_url host '{host}' is not the pinned edge {sorted(ALLOWED_EDGE_HOSTS)} "
                      f"(an explicit {EDGE_OVERRIDE_ENV}=<host> would allow another, and the report prints it)")
    if not re.search(r"\btest\b", item_title, re.IGNORECASE):
        raise Refusal("the 1Password item's own title does not name a test tenant (it must contain the word 'test')")
    if mint_tenant != REQUIRED_TENANT:
        raise Refusal(f"the credential's mint_tenant is '{mint_tenant}', not '{REQUIRED_TENANT}': refusing a tenant that is not the test tenant")
    if not token:
        raise Refusal("the credential's token is empty")
    ambient = _ambient_credentials(ambient_environ, ambient_config)
    th = _sha(token)
    for key in ("service_token", "mint_token"):
        if ambient.get(key) and _sha(normalize_secret(ambient[key])) == th:
            raise Refusal(f"the test credential is the operator's own ambient {key}: refusing the operator's normal credential")
    if ambient.get("mint_tenant") and ambient["mint_tenant"] != REQUIRED_TENANT and ambient.get("mint_token") \
            and _sha(normalize_secret(ambient["mint_token"])) == th:
        raise Refusal("the test credential is bound to the operator's own tenant")
    return host


# ── the step plan ───────────────────────────────────────────────────────────

@dataclass
class Step:
    name: str
    kind: str                       # "nx": the installed nx CLI; "py": the candidate venv's python -c <argv[1]>
    argv: list[str]
    core: bool = False
    cleanup: bool = False
    identity: bool = False          # attach NX_IDENTITY_TOKEN (the bound identity's token file)
    needs: list[str] = field(default_factory=list)      # variables an earlier step must have captured
    capture: dict[str, str] = field(default_factory=dict)  # var -> "json:<key>" | "re:<pattern>"
    stdin: str | None = None
    expect_stdout: str | None = None   # a substring a passing run prints (the round trip)
    expect_route: str | None = None    # a route the client must have called: a client that quietly skips a
                                       # route it believes the engine lacks (a stripped /version flag) would
                                       # otherwise exit 0 having exercised nothing
    benign_refusal: bool = False   # only a step flagged so may pass by being refused identically in both slots
    tries: int = 1                 # attempts before the step is judged (a read of an index that may lag)
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
print(json.dumps({"identity": view["id"], "token_file": str(path), "generation": int(view["generation"])}))
"""

_IDENTITY_END = r"""
import json
from nexus.identity.bind_client import IdentityClient
print(json.dumps(IdentityClient.from_config().bind("@identity@", "end", int("@generation@"))))
"""

def _load_sibling(name: str) -> Any:
    """A module beside this file, loaded by path (this file is run as a script and loaded by path in its tests)."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_routes = _load_sibling("cloud_leg_route_snippets")


def core_steps() -> list[Step]:
    t = "threads"
    return [
        # Bind first: in cloud mode a seat is made only through a live session's own identity token
        # (0.1.197 cloud self-seat), so seat create and show carry the bound identity's token file.
        Step("identity-bind", "py", ["-c", _IDENTITY_BIND], core=True,
             capture={"identity": "json:identity", "token_file": "json:token_file", "generation": "json:generation"},
             expect_route="/v1/identity"),
        Step("identity-seat-create", "nx", ["identity", "create", "@label@-seat"], core=True, identity=True,
             needs=["identity"], expect_route="/v1/identity"),
        Step("identity-seat-show", "nx", ["identity", "show", "@label@-seat", "--json"], core=True, identity=True,
             needs=["identity"]),
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
             core=True, expect_stdout="@label@", expect_route="/v1/vectors/search", tries=SEARCH_TRIES),
        # cleanup, run in this order after the rest
        Step("store-delete", "nx", ["store", "delete", "-c", "cloud-leg-probe", "--title", "@label@", "--yes"],
             core=True, cleanup=True),
        Step("scratch-delete", "nx", ["scratch", "delete", "@scratch_id@"], core=True, cleanup=True, needs=["scratch_id"]),
        Step("memory-delete-threads", "nx", ["memory", "delete", "-p", t, "-t", "@label@", "--yes"], core=True,
             cleanup=True, identity=True, needs=["identity"]),
        Step("identity-end", "py", ["-c", _IDENTITY_END], core=True, cleanup=True, needs=["identity", "generation"],
             expect_route="/v1/identity/{id}/bind"),
    ]


#: Release additions: a wire-ledger route -> the client call that exercises it. A route absent here is UNMAPPED, which
#: fails the run unless --accept-skip names it with a reason.
ROUTE_MAP: dict[str, Step] = {
    "GET /v1/catalog/docs/by-scheme": Step("route-docs-by-scheme", "nx", ["doctor", "--check-references"],
                                           expect_route="/v1/catalog/docs/by-scheme"),
    "GET /v1/catalog/docs/by-meta": Step("route-docs-by-meta", "nx", ["catalog", "list", "--meta", "cloudleg=@run@", "--json"],
                                         expect_route="/v1/catalog/docs/by-meta"),
    "GET /v1/catalog/docs/by-author": Step("route-docs-by-author", "py", ["-c", _routes.BY_AUTHOR],
                                           expect_route="/v1/catalog/docs/by-author"),
    "GET /v1/catalog/sensitive/locators": Step("route-sensitive-locators", "py", ["-c", _routes.LOCATORS],
                                               expect_route="/v1/catalog/sensitive/locators"),
    "GET /v1/catalog/collections/for_tuple": Step("route-collections-for-tuple", "py", ["-c", _routes.FOR_TUPLE],
                                                  expect_route="/v1/catalog/collections/for_tuple"),
    # RDR-252 (nexus-igea9): /v1/tuples/out refuses a client write of from=engine on board/ci (403 reserved_dim);
    # the probe is an ordinary client write, which must still land.
    "POST /v1/tuples/out": Step("route-tuples-out", "nx", ["tuple", "out", "mailbox/@identity@", "--key", "to=@identity@",
                                                           "--dim", "from=@identity@", "--dim", "kind=cloudleg",
                                                           "--nonce", "@label@-out", "--body", "@label@"],
                                identity=True, needs=["identity"], expect_route="/v1/tuples/out"),
    "POST /v1/tuples/ack": Step("route-tuples-ack", "py", ["-c", _routes.TUPLE_ACK], identity=True, needs=["identity"],
                                expect_route="/v1/tuples/ack"),
    # RDR-252 (nexus-igea9.4): the registry listing names a template's retention_by_suffix (board-ci's --owner, 35 days).
    "GET /v1/tuples/registry": Step("route-tuples-registry", "nx", ["tuple", "templates"],
                                    expect_route="/v1/tuples/registry"),
    "GET /v1/catalog/uri_segment_census": Step("route-uri-segment-census", "py", ["-c", _routes.URI_SEGMENTS],
                                               expect_route="/v1/catalog/uri_segment_census"),
    "GET /v1/catalog/rdr229_completion_counts": Step("route-rdr229-completion-counts", "py", ["-c", _routes.COMPLETION_COUNTS],
                                                     expect_route="/v1/catalog/rdr229_completion_counts"),
    "POST /v1/catalog/collections/upsert": Step("route-collections-upsert", "py", ["-c", _routes.COLLECTION_UPSERT],
                                                expect_route="/v1/catalog/collections/upsert"),
    # RDR-229 P3.1 (nexus-p2m1x.17): the completion check's report, and the typed 422 of the dual write.
    # RDR-229 P3.11 (nexus-p2m1x.31): the restore marker's relabel keys.
    "GET /v1/tenant/import-marker": Step("route-tenant-import-marker", "py", ["-c", _routes.IMPORT_MARKER],
                                         expect_route="/v1/tenant/import-marker"),
    "GET /v1/status": Step("route-engine-status", "py", ["-c", _routes.ENGINE_STATUS], expect_route="/v1/status"),
    "POST /v1/catalog/doc/register": Step("route-doc-register-unregistered", "py", ["-c", _routes.DOC_REGISTER_UNREGISTERED],
                                          expect_route="/v1/catalog/doc/register",
                                          expect_stdout="refused the unregistered collection with 422"),
    "GET /v1/catalog/collections/list": Step("route-collections-list", "py", ["-c", _routes.COLLECTIONS_LIST],
                                             expect_route="/v1/catalog/collections/list"),
    "GET /v1/catalog/collections/get": Step("route-collections-get", "py", ["-c", _routes.COLLECTIONS_GET],
                                            expect_route="/v1/catalog/collections/get"),
    "POST /v1/catalog/import/collection": Step("route-import-collection", "py", ["-c", _routes.IMPORT_COLLECTION_REFUSES_AN_ID],
                                               expect_route="/v1/catalog/import/collection",
                                               expect_stdout="collection_id refused 400"),
    "POST /v1/taxonomy/assignments/prune_projection": Step("route-prune-projection", "py", ["-c", _routes.PRUNE_PROJECTION],
                                                           expect_route="/v1/taxonomy/assignments/prune_projection",
                                                           expect_stdout="match=bogus refused 400"),
}

_ROUTE_RE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[A-Za-z0-9_\-/.{}]+)")


def _load_ledger_module() -> Any:
    path = REPO_ROOT / "scripts" / "check_wire_contract_pairing.py"
    spec = importlib.util.spec_from_file_location("check_wire_contract_pairing", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = mod  # type: ignore[union-attr]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_engine = _load_sibling("cloud_leg_engine")
parse_engine_version, entry_engine, _ver = _engine.parse_engine_version, _engine.entry_engine, _engine.version_text


def read_cloud_engine(service_url: str, timeout: float = 15.0) -> str:
    """cloud_leg_engine.read_cloud_engine; an engine the leg cannot name refuses the run."""
    try:
        return _engine.read_cloud_engine(service_url, timeout)
    except _engine.EngineUnreadable as exc:
        raise Refusal(str(exc)) from None


def ledger_entries(ledger: Path, release: str | None = None) -> list[Any]:
    """The `## Unshipped` entries of the wire ledger (docs/wire-contract-pending.md), or, with *release*, the
    `## Shipped` entries whose `shipped in` is that tag (the release skill's Step 4b has moved this release's
    entries there by the time the leg runs). Parsed by scripts/check_wire_contract_pairing.parse_ledger, the one
    parser of the format. A ledger path that is not a file is an error, never an empty ledger."""
    if not Path(ledger).is_file():
        raise Refusal(f"the wire ledger {ledger} does not exist")
    parsed = _load_ledger_module().parse_ledger(Path(ledger))
    if release is None:
        return list(parsed.unshipped.values())
    return [e for e in parsed.shipped.values() if e.shipped_in == release]


def entry_routes(entries: list[Any]) -> list[tuple[str, str]]:
    """[(entry tag, "METHOD /path")] for every route the entries name, in order, de-duplicated by route."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in entries:
        for m, p in _ROUTE_RE.findall(e.note):
            r = f"{m} {p}"
            if r not in seen:
                seen.add(r)
                out.append((f"{e.sha[:7]} {e.bead}", r))
    return out


def release_steps(entries: list[Any], accept_skip: dict[str, str] | None = None,
                  cloud_engine: tuple[int, int, int] | None = None) -> tuple[list[Step], list[tuple[str, str]], list[tuple[str, str]]]:
    """(steps, skipped, unmapped). One step per distinct mapped route. *skipped* are entries that name no route (text-only
    or a flag change), routes named in *accept_skip* (with the reason), and, given the engine release the cloud runs
    (*cloud_engine*), the mapped or accepted routes only entries with a later engine tag name ("SKIPPED, engine tag ... not
    deployed": the cloud would answer 404 for a route it does not have yet). *unmapped* are routes with no mapped client
    call and no accepted skip, deployed or not: each is a failure of the run."""
    accept = dict(accept_skip or {})
    steps: list[Step] = []
    skipped: list[tuple[str, str]] = []
    unmapped: list[tuple[str, str]] = []
    routed_tags = {tag for tag, _r in entry_routes(entries)}
    for e in entries:
        tag = f"{e.sha[:7]} {e.bead}"
        if tag not in routed_tags:
            skipped.append((tag, "no wire route named in the entry (text-only or a flag change)"))

    def ahead(e: Any) -> bool:
        need = entry_engine(e)
        return cloud_engine is not None and need is not None and need > cloud_engine

    live = [e for e in entries if not ahead(e)]
    live_routes = {r for _t, r in entry_routes(live)}
    behind: dict[str, str] = {}   # route -> why, for routes only entries ahead of the cloud name
    for e in entries:
        if ahead(e):
            for m, p in _ROUTE_RE.findall(e.note):
                r = f"{m} {p}"
                if r not in live_routes and r not in behind:
                    behind[r] = (f"SKIPPED, engine tag {e.engine_tag} not deployed "
                                 f"(the cloud runs {_ver(cloud_engine)})")  # type: ignore[arg-type]
    for tag, r in entry_routes(live) + [(t, r) for t, r in entry_routes(entries) if r in behind]:
        mapped = ROUTE_MAP.get(r)
        path = r.split(" ", 1)[1]
        reason = accept.get(r) or accept.get(path)
        if mapped is None and not reason:
            unmapped.append((f"{tag} {r}", "no client call is mapped to this route (map it in ROUTE_MAP or --accept-skip it)"))
        elif r in behind:
            skipped.append((f"{tag} {r}", behind[r]))
        elif mapped is not None:
            steps.append(Step(**{**asdict(mapped), "origin": f"{tag} {r}"}))
        else:
            skipped.append((f"{tag} {r}", f"accepted: {reason}"))
    return steps, skipped, unmapped


def parse_pairs(items: list[str], what: str) -> dict[str, str]:
    """`key:reason` (the reason is mandatory) -> {key: reason}."""
    out: dict[str, str] = {}
    for item in items:
        key, sep, reason = item.partition(":")
        if not sep or not key.strip() or not reason.strip():
            raise Refusal(f"{what} needs <key>:<reason> with a non-empty reason, got '{item}'")
        out[key.strip()] = reason.strip()
    return out


def build_plan(run: str, ledger: Path | None, release: str | None, *, core_only: bool = False,
               accept_skip: dict[str, str] | None = None, no_ledger_ok: str = "",
               cloud_engine: str | None = None, require_deployed: bool = False) -> dict[str, Any]:
    """The step plan. *cloud_engine* is the engine release the cloud runs (``X.Y.Z``, from :func:`read_cloud_engine`);
    given, a ledger route whose engine tag is later is skipped rather than driven (nexus-h4hge), so a run before the
    deploy needs no ``--core-only``. ``None`` (the local-vs-local self-test) drives every mapped route. *require_deployed*
    (the gating rerun after the deploy) makes any such skip a failure of the plan."""
    if require_deployed and (core_only or cloud_engine is None):
        raise Refusal("--require-deployed needs a cloud run that drives the ledger (not --core-only, not --local-vs-local)")
    if not RUN_ID_RE.match(run):
        raise ValueError(f"run id {run!r} must match {RUN_ID_RE.pattern}")
    deployed: tuple[int, int, int] | None = None
    if cloud_engine is not None:
        deployed = parse_engine_version(cloud_engine)
        if deployed is None:
            raise Refusal(f"the cloud engine {cloud_engine!r} is not X.Y.Z")
    entries: list[Any] = []
    if ledger is not None and (not core_only or Path(ledger).is_file()):
        entries = ledger_entries(ledger, release)
    elif ledger is None and not core_only:
        raise Refusal("a full run needs the wire ledger (--ledger), or --core-only")
    failures: list[str] = []
    rsteps: list[Step] = []
    skipped: list[tuple[str, str]] = []
    unmapped: list[tuple[str, str]] = []
    not_driven = 0
    if core_only:
        not_driven = len(entry_routes(entries))
    else:
        rsteps, skipped, unmapped = release_steps(entries, accept_skip, deployed)
        for key in (accept_skip or {}):
            if key not in {r for _t, r in entry_routes(entries)} | {r.split(" ", 1)[1] for _t, r in entry_routes(entries)}:
                raise Refusal(f"--accept-skip names '{key}', which no ledger entry names")
        if not entries and not no_ledger_ok:
            failures.append(f"the ledger selection ({'release ' + release if release else '## Unshipped'}) has zero entries, so no "
                            "release route was driven; pass --core-only, or --no-ledger-entries-ok \"<reason>\" if none is correct")
        failures += [f"unmapped ledger route {t}: {why}" for t, why in unmapped]
        if require_deployed:
            failures += [f"--require-deployed: {t} {why}" for t, why in skipped if why.startswith("SKIPPED, engine tag")]
    main = [s for s in core_steps() if not s.cleanup]
    cleanup = [s for s in core_steps() if s.cleanup]
    steps = main + rsteps + cleanup
    driven = {s.origin.rsplit(" ", 2)[0] for s in rsteps}
    return {"run": run, "label": f"cloudleg-{run}", "steps": [asdict(s) for s in steps],
            "skipped": [list(x) for x in skipped], "unmapped": [list(x) for x in unmapped], "failures": failures,
            "ledger_entries": len(entries), "ledger_driven": len(driven), "routes_driven": len(rsteps),
            "core_only": core_only, "release": release, "no_ledger_ok": no_ledger_ok, "not_driven_routes": not_driven,
            "cloud_engine": _ver(deployed) if deployed else None, "require_deployed": require_deployed,
            "not_deployed_routes": sum(1 for _k, why in skipped if why.startswith("SKIPPED, engine tag")),
            "floor": core_floor(steps)}


def core_floor(steps: list[Step] | list[dict[str, Any]]) -> int:
    """The non-vacuity floor: the fixed core's steps (not cleanup) that must come out PASS (or expected-red)."""
    n = 0
    for s in steps:
        d = s if isinstance(s, dict) else asdict(s)
        if d["core"] and not d["cleanup"]:
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
    ready, rows, _hosts = parse_tap_hosts(path)
    return ready, rows


def parse_tap_hosts(path: Path) -> tuple[bool, list[tuple[int, str, str]], set[str]]:
    """parse_tap plus the set of hosts the tap saw."""
    ready, rows4 = parse_tap_rows(path)
    return ready, [(s, m, r) for s, m, r, _h in rows4], {h for *_x, h in rows4 if h}


def parse_tap_rows(path: Path) -> tuple[bool, list[tuple[int, str, str, str]]]:
    """(tap-ready, [(status, METHOD, normalized route, host)]): every exchange the tap saw, whatever its host."""
    ready, rows = False, []
    if Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line == "#tap-ready":
                ready = True
                continue
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].lstrip("-").isdigit():
                rows.append((int(parts[0]), parts[1], norm_route(parts[2]), parts[3] if len(parts) >= 4 else ""))
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
             sleep: Any = time.sleep) -> list[dict[str, Any]]:
    """Run every step of *plan* in one sandbox. Returns one result dict per step."""
    sandbox = Path(sandbox)
    secrets = read_secrets(sandbox)
    nx = str(sandbox / "home/.local/bin/nx")
    py = str(sandbox / "home/.local/share/uv/tools/conexus/bin/python")
    variables = {"run": plan["run"], "label": plan["label"]}
    Path(workdir).mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for n, step in enumerate(plan["steps"]):
        res: dict[str, Any] = {"name": step["name"], "slot": slot, "exit": None, "taps": [], "tap_ready": False,
                               "stdout_chars": 0, "error": "", "skipped": None, "blocked": None, "attempts": 0,
                               "benign": bool(step.get("benign_refusal")), "host": host}
        missing = [v for v in step["needs"] if v not in variables]
        if missing:
            res["blocked"] = "needs " + ", ".join(missing) + " from an earlier step"
            res["exit"] = -1
            results.append(res)
            continue
        argv = [_fill(a, variables) for a in step["argv"]]
        exe = [nx] if step["kind"] == "nx" else [py]
        stdin = _fill(step["stdin"], variables) if step["stdin"] is not None else None
        want = step["expect_stdout"]
        all_rows: list[tuple[int, str, str, str]] = []
        tap_ready = False
        for attempt in range(1, max(1, int(step.get("tries", 1))) + 1):
            tapfile = Path(workdir) / f"tap-{slot}-{n:02d}-{step['name']}-{attempt}.log"
            tapfile.unlink(missing_ok=True)
            extras = [f"PYTHONPATH={TAP_DIR}", f"NX_CLOUDLEG_TAP={tapfile}"]
            if step["identity"] and "token_file" in variables:
                extras.append(f"NX_IDENTITY_TOKEN={variables['token_file']}")
            try:
                cp = subprocess.run([runner, *extras, *exe, *argv], input=stdin, capture_output=True, text=True,
                                    timeout=STEP_TIMEOUT_S)
                out, err, code = cp.stdout, cp.stderr, cp.returncode
            except subprocess.TimeoutExpired:
                out, err, code = "", "TimeoutExpired", 124
            res["attempts"] = attempt
            res["exit"] = code
            ready, rows = parse_tap_rows(tapfile)
            tap_ready = tap_ready or ready
            all_rows += rows  # every attempt's exchanges count: a 4xx on a retried attempt is still a 4xx
            res["tap_ready"] = tap_ready
            res["taps"] = [[s, m, r] for s, m, r, h in all_rows if h == host]
            res["hosts"] = sorted({h for *_x, h in all_rows if h})
            res["stdout_chars"] = len(out)  # the output itself is not kept: it carries ids and may carry a token the scrub cannot know
            first_err = next((ln for ln in reversed(err.strip().splitlines()) if ln.strip()), "")
            res["error"] = _scrub(first_err, secrets)[:160]
            res["expect_ok"] = True if want is None else (_fill(want, variables) in out)
            if code != 0 or res["expect_ok"]:
                break  # retry only an empty result: a failure or a pass is judged as it stands
            if attempt < int(step.get("tries", 1)):
                sleep(SEARCH_WAIT_S)
        res["expect_route"] = step.get("expect_route")
        if res["exit"] == 0:
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
    tries = f" a={res['attempts']}" if res.get("attempts", 1) > 1 else ""
    if bad:
        return head + " " + ",".join(str(s) for s in bad) + tries
    return head + (f" ({len(res['taps'])} req{tries})" if res["taps"] else tries)


def judge_step(local: dict[str, Any], cloud: dict[str, Any]) -> tuple[str, str]:
    """(verdict, reason). Verdicts: PASS, PASS-both-refused (only a step flagged benign), FAIL."""
    verdict, reason, _cloud_only = _judge(local, cloud)
    return verdict, reason


def _judge(local: dict[str, Any], cloud: dict[str, Any]) -> tuple[str, str, list[tuple[str, str, int]] | None]:
    """(verdict, reason, cloud-only bad exchanges). The third is set ONLY for a FAIL whose cause is a status the cloud
    returned and local did not, on a step where the cloud exited non-zero and local exited zero: the one shape
    --expect-red may excuse. Every other FAIL (a blind tap, no HTTP, a route never called, a round-trip miss, a local
    failure) returns None."""
    for res in (local, cloud):
        if res.get("blocked"):
            return "FAIL", f"{res['slot']}: blocked, {res['blocked']}", None
    for res in (local, cloud):
        if not res["tap_ready"]:
            return "FAIL", f"{res['slot']}: the HTTP tap never installed, so no status was seen", None
        if res["exit"] == 0 and not res["taps"]:
            return "FAIL", f"{res['slot']}: the step exited 0 but no HTTP request was seen at the client boundary", None
    cloud_only = sorted(_bad_set(cloud) - _bad_set(local))
    if cloud_only:
        m, r, s = cloud_only[0]
        more = f" (+{len(cloud_only) - 1} more)" if len(cloud_only) > 1 else ""
        pure = cloud["exit"] != 0 and local["exit"] == 0
        return "FAIL", f"cloud-only {s} on {m} {r}{more}; local returned none of it", (cloud_only if pure else None)
    if cloud["exit"] != 0 and local["exit"] == 0:
        return "FAIL", f"cloud exit {cloud['exit']} where local exited 0: {cloud['error'] or 'no error text'}", None
    if local["exit"] != 0 and cloud["exit"] == 0:
        return "FAIL", f"local-only failure (exit {local['exit']}): {local['error'] or 'no error text'}", None
    if local["exit"] != 0 and cloud["exit"] != 0:
        if local.get("benign") and _bad_set(local) and _bad_set(local) == _bad_set(cloud):
            return "PASS-both-refused", "the same refusal in both slots (a step flagged benign)", None
        return "FAIL", f"failed in both slots (exit {cloud['exit']}): {cloud['error'] or 'no error text'}", None
    for res in (local, cloud):
        want = res.get("expect_route")
        if want and res["exit"] == 0 and not any(r == want for _s, _m, r in res["taps"]):
            return "FAIL", f"{res['slot']}: the client never called {want} (it skipped the route, e.g. the engine's capability flag is not visible)", None
    for res in (local, cloud):
        if not res.get("expect_ok", True):
            return "FAIL", f"{res['slot']}: the round trip did not print its own text back", None
    return "PASS", "", None


@dataclass(frozen=True)
class ExpectRed:
    """One --expect-red: the cloud-only status(es) the step is allowed to show, and why."""
    statuses: frozenset[int]
    reason: str


def parse_expect_red(items: list[str]) -> dict[str, ExpectRed]:
    """`<step>@<status>[,<status>...]:<reason>` -> {step: ExpectRed}. The status pin and the reason are mandatory."""
    out: dict[str, ExpectRed] = {}
    for item in items:
        head, sep, reason = item.partition(":")
        name, at, pin = head.partition("@")
        try:
            statuses = frozenset(int(x) for x in pin.split(",")) if at else frozenset()
        except ValueError:
            statuses = frozenset()
        if not sep or not reason.strip() or not name.strip() or not statuses or any(not 400 <= x <= 599 for x in statuses):
            raise Refusal(f"--expect-red needs <step>@<status>[,<status>]:<reason> with 4xx/5xx statuses and a non-empty reason, got '{item}'")
        out[name.strip()] = ExpectRed(statuses, reason.strip())
    return out


@dataclass
class Report:
    rows: list[dict[str, str]]
    examined: int
    skipped: list[tuple[str, str]]
    failed: int
    expected_red: int
    both_refused: int
    nonvacuity: list[str]
    plan_failures: list[str] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)
    red_names: list[str] = field(default_factory=list)
    not_deployed: int = 0   # release routes the plan skipped: the cloud engine lacks them

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.nonvacuity and not self.plan_failures

    def render(self) -> str:
        w = max([len(r["step"]) for r in self.rows] + [4])
        lines = [f"{'step'.ljust(w)}  {'local':<14} {'cloud':<18} verdict"]
        for r in self.rows:
            lines.append(f"{r['step'].ljust(w)}  {r['local']:<14} {r['cloud']:<18} {r['verdict']}" + (f"  - {r['reason']}" if r["reason"] else ""))
        for msg in self.plan_failures:
            lines.append(f"PLAN FAILURE: {msg}")
        reasons = "; ".join(f"{a}: {b}" for a, b in self.skipped) or "none"
        lines.append("")
        lines.append(f"examined {self.examined}, skipped {len(self.skipped)} ({reasons}), failed {self.failed + len(self.plan_failures)}"
                     + (f", expected-red {self.expected_red}" if self.expected_red else "")
                     + (f", both-refused {self.both_refused}" if self.both_refused else ""))
        lines.extend(self.footer)
        for msg in self.nonvacuity:
            lines.append(f"NON-VACUITY FAILURE: {msg}")
        if not self.ok:
            lines.append("CLOUD LEG FAILED")
        else:
            line = (f"CLOUD LEG PASSED WITH EXPECTED-RED {self.expected_red}: {', '.join(self.red_names)}"
                    if self.expected_red else "CLOUD LEG PASSED")
            if self.not_deployed:   # nexus-h4hge: never read as a pass of the release's routes
                line += (f" (NOT A RELEASE PASS: {self.not_deployed} release routes not driven, engine tag not deployed; "
                         "the gating rerun after the deploy passes --require-deployed)")
            lines.append(line)
        return "\n".join(lines)


def compare(plan: dict[str, Any], local: list[dict[str, Any]], cloud: list[dict[str, Any]], floor: int | None = None, *,
            expect_red: dict[str, ExpectRed] | None = None, window: str = "-", edge_host: str = "",
            environ: dict[str, str] | None = None) -> Report:
    floor = plan.get("floor", 0) if floor is None else floor
    expect_red = dict(expect_red or {})
    names = {s["name"] for s in plan["steps"]}
    for k in expect_red:
        if k not in names:
            raise Refusal(f"--expect-red names '{k}', which is not a step of this plan ({', '.join(sorted(names))})")
    env = os.environ if environ is None else environ
    rows: list[dict[str, str]] = []
    red_names: list[str] = []
    examined = failed = refused = red = core_ok = 0
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
        lres, cres = by_local[name], by_cloud[name]
        verdict, reason, cloud_only = _judge(lres, cres)
        if name in expect_red:
            pin = expect_red[name]
            if verdict != "FAIL":
                verdict, reason = "FAIL", f"expected red but passed: remove --expect-red {name}"
            elif cloud_only is None:
                reason = f"{reason} (not a cloud-only status, so not the pinned red @{','.join(map(str, sorted(pin.statuses)))})"
            else:
                got = sorted({s for _m, _r, s in cloud_only})
                if set(got) <= pin.statuses:
                    verdict, reason = "EXPECTED-RED", f"{reason}; pinned @{','.join(map(str, sorted(pin.statuses)))}: {pin.reason}"
                else:
                    reason = f"{reason}; cloud statuses {got} are not the pinned {sorted(pin.statuses)} ({pin.reason})"
        rows.append({"step": name, "local": _cell(lres), "cloud": _cell(cres), "verdict": verdict, "reason": reason})
        examined += 1
        if verdict == "FAIL":
            failed += 1
        elif verdict == "EXPECTED-RED":
            red += 1
            red_names.append(name)
        elif verdict == "PASS-both-refused":
            refused += 1
        if step["core"] and not step["cleanup"] and verdict in {"PASS", "PASS-both-refused", "EXPECTED-RED"}:
            core_ok += 1
    footer = [f"ledger entries driven: {plan.get('ledger_driven', 0)} of {plan.get('ledger_entries', 0)} "
              f"(routes driven: {plan.get('routes_driven', 0)}; "
              + (f"release {plan['release']}" if plan.get("release") else "## Unshipped") + ")"]
    if plan.get("no_ledger_ok"):
        footer.append(f"no ledger entries accepted: {plan['no_ledger_ok']}")
    if plan.get("core_only"):
        footer.append(f"CORE-ONLY: {plan.get('not_driven_routes', 0)} ledger routes not driven")
    if plan.get("cloud_engine"):
        nd = plan.get("not_deployed_routes", 0)
        footer.append(f"cloud engine {plan['cloud_engine']} (GET /version): " + (f"{nd} ledger routes skipped as not deployed"
                      if nd else "every ledger route's engine tag is deployed") + ("; --require-deployed" if plan.get("require_deployed") else ""))
    if env.get(EDGE_OVERRIDE_ENV):
        footer.append(f"EDGE HOST OVERRIDE in effect: {EDGE_OVERRIDE_ENV}={env[EDGE_OVERRIDE_ENV]}")
    hosts = sorted({h for r in cloud for h in r.get("hosts", [])})
    footer.append(f"cloud slot tapped hosts (from the request URLs): {', '.join(hosts) or 'none'}"
                  + (f" (expected {edge_host})" if edge_host else ""))
    footer.append(f"core steps ok {core_ok} of floor {floor}; run {plan['run']}; window {window}")
    nonv = []
    if edge_host and edge_host not in hosts:
        nonv.append(f"the cloud slot's tap saw host(s) {hosts or 'none'} in the request URLs, never the edge {edge_host}")
    if examined == 0:
        nonv.append("every step was skipped or none ran; nothing was examined")
    elif core_ok < floor:
        nonv.append(f"only {core_ok} core steps passed or were expected-red; the floor is {floor} (the fixed core)")
    return Report(rows, examined, skipped, failed, red, refused, nonv, list(plan.get("failures", [])), footer, red_names,
                  int(plan.get("not_deployed_routes", 0)))


# ── CLI ─────────────────────────────────────────────────────────────────────

def _read_file(p: Path) -> str:
    return normalize_secret(Path(p).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cloud_leg.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("check-window"); w.add_argument("--window", required=True); w.add_argument("--dir")
    nc = sub.add_parser("normalize-cred"); nc.add_argument("--cred-dir", required=True)
    ti = sub.add_parser("item-title")  # stdin: `op item get --format json`; prints the title
    t = sub.add_parser("check-target"); t.add_argument("--cred-dir", required=True); t.add_argument("--item-title-file", required=True)
    t.add_argument("--ambient-config")
    p = sub.add_parser("plan"); p.add_argument("--run", required=True); p.add_argument("--ledger"); p.add_argument("--release")
    p.add_argument("--core-only", action="store_true"); p.add_argument("--accept-skip", action="append", default=[])
    p.add_argument("--no-ledger-entries-ok", default=""); p.add_argument("--out", required=True)
    p.add_argument("--cloud-engine")  # X.Y.Z from `cloud-engine`; unset = drive every mapped route (local-vs-local)
    p.add_argument("--require-deployed", action="store_true")
    ce = sub.add_parser("cloud-engine"); ce.add_argument("--cred-dir", required=True)  # prints the cloud's X.Y.Z
    r = sub.add_parser("run-slot"); r.add_argument("--plan", required=True); r.add_argument("--slot", required=True)
    r.add_argument("--sandbox", required=True); r.add_argument("--host", required=True); r.add_argument("--workdir", required=True)
    r.add_argument("--out", required=True)
    c = sub.add_parser("compare"); c.add_argument("--plan", required=True); c.add_argument("--local", required=True)
    c.add_argument("--cloud", required=True); c.add_argument("--floor", type=int); c.add_argument("--expect-red", action="append", default=[])
    c.add_argument("--window", default="-"); c.add_argument("--edge-host", default="")
    cp_ = sub.add_parser("check-plan"); cp_.add_argument("--plan", required=True); cp_.add_argument("--expect-red", action="append", default=[])
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check-window":
            print(check_window(a.window, Path(a.dir) if a.dir else window_dir()))
            return 0
        if a.cmd == "normalize-cred":
            normalize_cred_dir(Path(a.cred_dir))
            return 0
        if a.cmd == "item-title":
            print(item_title_from_json(sys.stdin.read()))
            return 0
        if a.cmd == "check-target":
            cd = Path(a.cred_dir)
            amb = Path(os.environ.get("NEXUS_CONFIG_DIR") or Path.home() / ".config" / "nexus") / "config.yml"
            edge = check_target(service_url=_read_file(cd / "service_url"), token=_read_file(cd / "token"),
                                mint_tenant=_read_file(cd / "mint_tenant") if (cd / "mint_tenant").is_file() else "",
                                item_title=Path(a.item_title_file).read_text(encoding="utf-8").strip(),
                                ambient_environ=dict(os.environ),
                                ambient_config=Path(a.ambient_config) if a.ambient_config else amb)
            print(f"target ok: pinned https edge {edge}, test tenant, not the operator's credential")
            return 0
        if a.cmd == "plan":
            plan = build_plan(a.run, Path(a.ledger) if a.ledger else None, a.release, core_only=a.core_only,
                              accept_skip=parse_pairs(a.accept_skip, "--accept-skip"), no_ledger_ok=a.no_ledger_entries_ok.strip(),
                              cloud_engine=a.cloud_engine, require_deployed=a.require_deployed)
            Path(a.out).write_text(json.dumps(plan, indent=1), encoding="utf-8")
            nd = (f", {plan['not_deployed_routes']} not deployed on the cloud engine {plan['cloud_engine']}"
                  if plan["cloud_engine"] else "")
            print(f"plan: {len(plan['steps'])} steps ({plan['floor']} floor), {plan['ledger_entries']} ledger entries "
                  f"({plan['routes_driven']} routes driven, {len(plan['unmapped'])} unmapped, {len(plan['skipped'])} skipped{nd})"
                  + ("; CORE-ONLY" if plan["core_only"] else ""))
            return 0
        if a.cmd == "cloud-engine":
            url = _read_file(Path(a.cred_dir) / "service_url")
            if not url.startswith("https://"):
                raise Refusal("service_url is not the public https edge (https:// required)")
            print(read_cloud_engine(url))
            return 0
        if a.cmd == "run-slot":
            plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            res = run_slot(plan, runner=str(Path(a.sandbox) / "run"), sandbox=Path(a.sandbox), host=a.host,
                           workdir=Path(a.workdir), slot=a.slot)
            Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
            print(f"slot {a.slot}: {len(res)} steps run")
            return 0
        if a.cmd == "check-plan":
            plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            if plan.get("failures"):
                raise Refusal("the plan has failures, so nothing runs:\n  " + "\n  ".join(plan["failures"]))
            unknown = sorted(set(parse_expect_red(a.expect_red)) - {s["name"] for s in plan["steps"]})
            if unknown:
                raise Refusal(f"--expect-red names {unknown}, not steps of this plan")
            return 0
        if a.cmd == "compare":
            plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
            rep = compare(plan, json.loads(Path(a.local).read_text()), json.loads(Path(a.cloud).read_text()), a.floor,
                          expect_red=parse_expect_red(a.expect_red), window=a.window, edge_host=a.edge_host)
            print(rep.render())
            return 0 if rep.ok else 1
    except Refusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
