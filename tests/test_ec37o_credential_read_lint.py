# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ec37o lint: a stored credential is read only through the origin-binding primitive.

Each stored credential (the saved ``service_token``, the saved ``mint_token``, the supervisor
lease token) goes only to the origin it was issued for, and that holds because every read of
one passes through the primitive (``nexus.db.service_endpoint.select_service_token`` and its
siblings, mirrored in ``conexus/hooks/scripts/_endpoint_resolve.py``). A new read that skips it
is a new place a repository's ``NX_SERVICE_URL`` can steal a token. This lint finds every such
read with an AST walk over ``src/nexus`` and ``conexus/hooks/scripts`` and fails on one outside
the allowlist below.

A *read* here is one of:

* ``get_credential`` / ``stored_credential`` / ``_stored_credential`` with the name ``"service_token"`` or
  ``"mint_token"``, positionally or as ``name=``; with a name that is not a literal (it could be either, so
  it is flagged); or called through an import alias (``from nexus.config import get_credential as gc``);
* ``get_global_config_section("credentials")`` (or with a non-literal name), and any
  ``.get("credentials")`` / ``["credentials"]`` read of the parsed file;
* any reference to a reader of the credential file (``read_config_yml_credentials``,
  ``persisted_credentials``);
* any reference to a lease reader (``discover_lease``, ``discover_lease_with_wait``, ``audited_lease``,
  ``read_storage_service_lease``, ``read_lease_text``, ``read_local_supervisor_token``,
  ``read_data_token_lease``, ``read_lease``, the CLI's ``_read_lease`` family, and the plugin's
  ``_read_service_lease`` / ``_read_data_token_lease``), except the URL-only shapes ``discover_lease()[0]`` and
  ``url, _token = discover_lease()`` where the token is dropped on the same line;
* the token out of a lease record by hand: ``lease["token"]``, ``lease.get("token")``,
  ``endpoint["token"]`` / ``.endpoint.get("token")`` on any lease- or endpoint-named value, and the same on a
  name bound from a ``.discover(...)`` or ``.read_lease(...)`` call or its ``.endpoint``.

The allowlist names the enclosing function and the kind of each site and how many such sites it holds,
with the reason it may read. It is exact in both directions: a new read of the same kind inside an
allowlisted function raises the count and fails, a deleted reader leaves a dead entry and fails, so the
list only shrinks. The count of sites examined is asserted, so a walk that found nothing is a failure.

Not covered (decided, not forgotten): the identity token file and the T1 session token are not stored
credentials bound to an origin (they ride the exported-token residual in ``docs/configuration.md``);
a read of ``config.yml`` by a path the walk cannot see through (``getattr``, ``eval``) is out of reach of
any AST walk.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (ROOT / "src" / "nexus", ROOT / "conexus" / "hooks" / "scripts")

STORED = frozenset({"service_token", "mint_token"})
NAME_TAKING_READERS = frozenset({"get_credential", "stored_credential", "_stored_credential"})
SECTION_READERS = frozenset({"get_global_config_section"})
LEASE_READERS = frozenset({
    "discover_lease", "discover_lease_with_wait", "_discover_lease", "audited_lease", "read_storage_service_lease",
    "discover_storage_service_lease", "_discover_service_lease", "read_lease_text", "read_local_supervisor_token",
    "_read_local_supervisor_token", "read_data_token_lease", "_read_data_token_lease", "_read_service_lease",
    "_read_lease", "_read_lease_newer", "read_lease",
})
CREDENTIAL_FILE_READERS = frozenset({"read_config_yml_credentials", "_read_config_yml_credentials", "persisted_credentials"})
#: calls whose result is a lease record (its ``.endpoint`` mapping carries the token)
RECORD_CALLS = frozenset({"discover", "read_lease"})
TRACKED = NAME_TAKING_READERS | SECTION_READERS | LEASE_READERS | CREDENTIAL_FILE_READERS | RECORD_CALLS

Site = tuple[str, str, str, int]  # (path, enclosing function, kind, line)


def _ref_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _lease_named(node: ast.AST) -> bool:
    """``lease`` / ``found_lease`` / ``self._lease`` / ``endpoint``: a variable that holds a supervisor lease record or its endpoint mapping."""
    name = _ref_name(node)
    return name is not None and ("lease" in name.lower() or name.lower() in ("endpoint", "ep"))


def _const(node: ast.AST | None) -> object:
    return node.value if isinstance(node, ast.Constant) else _NOT_A_LITERAL


_NOT_A_LITERAL = object()


def _first_arg(call: ast.Call, keyword: str = "name") -> ast.AST | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


class _Walker(ast.NodeVisitor):
    def __init__(self, rel: str, aliases: dict[str, str]) -> None:
        self.rel = rel
        self.aliases = aliases
        self.scope: list[str] = []
        self.parents: list[ast.AST] = []
        #: per enclosing function: names bound from a ``.discover(...)`` / ``.read_lease(...)`` call, and names bound from such a record's ``.endpoint``
        self.records: list[set[str]] = [set()]
        self.endpoints: list[set[str]] = [set()]
        self.sites: list[Site] = []

    def _qual(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _canon(self, node: ast.AST) -> str | None:
        name = _ref_name(node)
        if name is not None and isinstance(node, ast.Name):
            return self.aliases.get(name, name)
        return name

    def visit(self, node: ast.AST) -> None:
        self.parents.append(node)
        super().visit(node)
        self.parents.pop()

    def _enter(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.records.append(set())
        self.endpoints.append(set())
        self.generic_visit(node)
        self.records.pop()
        self.endpoints.pop()
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter  # noqa: N815

    def _site(self, kind: str, node: ast.AST) -> None:
        self.sites.append((self.rel, self._qual(), kind, node.lineno))  # type: ignore[attr-defined]

    def _is_record(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            return self._canon(node.func) in RECORD_CALLS
        return isinstance(node, ast.Name) and node.id in self.records[-1]

    def _is_endpoint(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "endpoint":
            return True
        return isinstance(node, ast.Name) and node.id in self.endpoints[-1]

    def visit_Assign(self, node: ast.Assign) -> None:
        value = node.value
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if self._is_record(value):
            self.records[-1].update(names)
        elif isinstance(value, ast.Attribute) and value.attr == "endpoint" and self._is_record(value.value):
            self.endpoints[-1].update(names)
        self.generic_visit(node)

    def _url_only(self, node: ast.AST) -> bool:
        """``discover_lease()[0]``, or ``a, _b = discover_lease()`` (the token dropped)."""
        call = self.parents[-2] if len(self.parents) >= 2 else None
        if isinstance(call, ast.Call) and call.func is node:
            outer = self.parents[-3] if len(self.parents) >= 3 else None
            if isinstance(outer, ast.Subscript) and outer.value is call and isinstance(outer.slice, ast.Constant) and outer.slice.value == 0:
                return True
            if isinstance(outer, ast.Assign) and outer.value is call:
                target = outer.targets[0]
                if isinstance(target, ast.Tuple) and len(target.elts) == 2:
                    second = target.elts[1]
                    return isinstance(second, ast.Name) and second.id.startswith("_")
        return False

    def _token_holder(self, node: ast.AST) -> bool:
        return _lease_named(node) or self._is_endpoint(node) or self._is_record(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = _const(node.slice)
        if key == "token" and self._token_holder(node.value):
            self._site('lease["token"]', node)
        if key == "credentials":
            self._site('["credentials"]', node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
            key = _const(node.args[0])
            if key == "token" and self._token_holder(func.value):
                self._site('lease.get("token")', node)
            if key == "credentials":
                self._site('.get("credentials")', node)
        name = self._canon(func)
        if name in NAME_TAKING_READERS:
            arg = _first_arg(node)
            literal = _const(arg)
            if literal in STORED:
                self._site(f"{name}({literal!r})", node)
            elif arg is not None and literal is _NOT_A_LITERAL:
                self._site(f"{name}(<non-literal>)", node)
        elif name in SECTION_READERS:
            arg = _first_arg(node)
            literal = _const(arg)
            if literal == "credentials":
                self._site(f"{name}('credentials')", node)
            elif arg is not None and literal is _NOT_A_LITERAL:
                self._site(f"{name}(<non-literal>)", node)
        self.generic_visit(node)

    def _is_call_func(self, node: ast.AST) -> bool:
        parent = self.parents[-2] if len(self.parents) >= 2 else None
        return isinstance(parent, ast.Call) and parent.func is node

    def _reference(self, node: ast.AST) -> None:
        name = self._canon(node)
        if name in CREDENTIAL_FILE_READERS:
            self._site(f"reference to {name}", node)
        elif name in LEASE_READERS and not self._url_only(node):
            self._site(f"reference to {name}", node)
        elif name in NAME_TAKING_READERS | SECTION_READERS and not self._is_call_func(node):
            self._site(f"reference to {name}", node)  # the function escaping as a value: a call through it cannot be read

    def visit_Name(self, node: ast.Name) -> None:
        self._reference(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._reference(node)
        self.generic_visit(node)


def _aliases(tree: ast.AST) -> dict[str, str]:
    """``from x import get_credential as gc`` -> ``{"gc": "get_credential"}`` for every tracked name."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname and alias.name in TRACKED:
                    found[alias.asname] = alias.name
    return found


def _walk_source(rel: str, source: str) -> list[Site]:
    tree = ast.parse(source)
    walker = _Walker(rel, _aliases(tree))
    walker.visit(tree)
    return walker.sites


def _sites() -> tuple[list[Site], int]:
    found: list[Site] = []
    files = 0
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            files += 1
            rel = path.relative_to(ROOT).as_posix()
            found.extend(_walk_source(rel, path.read_text(encoding="utf-8")))
    return found, files


def _tally(sites: list[Site]) -> dict[tuple[str, str, str], int]:
    counts: dict[tuple[str, str, str], int] = {}
    for path, qual, kind, _line in sites:
        counts[(path, qual, kind)] = counts.get((path, qual, kind), 0) + 1
    return counts


_PRIMITIVE = "the origin-binding primitive itself (or the resolver it feeds): the read happens next to the origin check"
_PRESENCE = "presence or provenance only: the value is compared or counted, never sent"
_FEEDS = "hands the lease or credential to select_service_token(lease=...), which binds it to its origin"
_DISPLAY = "shows or edits the value on the terminal for the user who ran the command; nothing is sent"
_LIFECYCLE = "supervisor lifecycle: reads the lease to learn whether and where the engine is; the token is not sent"
_DT_LEASE = "reads a data-token lease, which is keyed to the host digest and scheme it was minted for (nexus-ec37o), so it only serves that origin"
_LEASE_OWN = "the token is paired with the host and port of the same lease record, never with an env URL"
_READER = "the lease reader itself: it returns the lease's own endpoint with its token, to a caller that binds it"

Allowed = dict[tuple[str, str, str], tuple[int, str]]


def _allow(table: Allowed, path: str, qual: str, reason: str, *kinds: str) -> None:
    """One entry per (path, function, kind) with the number of such sites; a kind repeated is a count."""
    for kind in dict.fromkeys(kinds):
        table[(path, qual, kind)] = (kinds.count(kind), reason)


#: ``(path, enclosing function, kind)`` -> ``(number of such sites, why they may read)``. Exact in both
#: directions and only ever shrinks: a read added to an allowlisted function changes its count and fails.
ALLOWED: Allowed = {}
_EP = "conexus/hooks/scripts/_endpoint_resolve.py"
_ROUTING = "conexus/hooks/scripts/routing/_lib.py"
_CLI_EP = "src/nexus/db/service_endpoint.py"

# -- the primitive, plugin side (a stdlib mirror)
_allow(ALLOWED, _EP, "select_service_token", _PRIMITIVE, "reference to audited_lease", "reference to read_config_yml_credentials", "stored_credential('mint_token')", "stored_credential('service_token')")
_allow(ALLOWED, _EP, "audited_lease", "the owner-only-audited lease read the plugin's select_service_token takes", "reference to read_local_supervisor_token", "reference to read_storage_service_lease")
_allow(ALLOWED, _EP, "resolve_base_url", "reads the lease and saved URL to choose the endpoint, then hands the token choice to select_service_token", "reference to read_config_yml_credentials", "reference to read_storage_service_lease", "reference to read_storage_service_lease")
_allow(ALLOWED, _EP, "endpoint_usable", _PRESENCE, "reference to read_config_yml_credentials", "reference to read_storage_service_lease", "reference to read_storage_service_lease")
_allow(ALLOWED, _EP, "read_local_supervisor_token", _READER + " (stat-audited first)", "reference to read_lease_text")
_allow(ALLOWED, _EP, "read_storage_service_lease", _READER, "reference to read_lease_text", 'lease.get("token")')
# -- the primitive, CLI side
_allow(ALLOWED, _CLI_EP, "select_service_token", _PRIMITIVE, "_stored_credential('mint_token')", "_stored_credential('service_token')", "reference to discover_lease")
_allow(ALLOWED, _CLI_EP, "bound_mint_credential", _PRIMITIVE, "_stored_credential('mint_token')")
_allow(ALLOWED, _CLI_EP, "_stored_credential", _PRIMITIVE + " (the name-taking read every bound lookup goes through)", "get_credential(<non-literal>)", "reference to persisted_credentials")
_allow(ALLOWED, _CLI_EP, "discover_lease", _READER, 'lease.get("token")')
_allow(ALLOWED, _CLI_EP, "discover_lease_with_wait", "the lease reader the primitive's resolvers wait on; returns the lease's own URL with its token", "reference to discover_lease", "reference to discover_lease")
_allow(ALLOWED, _CLI_EP, "recover_endpoint_from_lease", "re-reads the lease after a rotation; URL and token come from the same lease record", "reference to discover_lease_with_wait")
_allow(ALLOWED, _CLI_EP, "resolve_service_config", "the host/port leg: the lease is offered to select_service_token, which binds it", "reference to discover_lease_with_wait")
_allow(ALLOWED, _CLI_EP, "mint_armed", _PRESENCE, "get_credential('mint_token')")
_allow(ALLOWED, _CLI_EP, "is_endpoint_env_pinned", _PRESENCE, "get_credential('service_token')")
# -- callers that feed the primitive a lease or a saved credential and take its answer
_allow(ALLOWED, "src/nexus/db/http_vector_client.py", "_discover_lease", "the vector client's lease source, handed to select_service_token(lease=...)", "reference to discover_lease")
_allow(ALLOWED, "src/nexus/db/http_vector_client.py", "_cached_lease", "caches the pair above; its only consumer is select_service_token(lease=...)", "reference to _discover_lease")
_allow(ALLOWED, "src/nexus/db/http_vector_client.py", "_wait_for_lease_republication", "waits for the supervisor to republish; the pair it stores goes back through the same primitive", "reference to discover_lease_with_wait")
_allow(ALLOWED, "src/nexus/db/t2/_refreshable_client.py", "_resolve_token_only", "hands the lease reader to select_service_token for the explicit-base_url stores", "reference to discover_lease_with_wait")
_allow(ALLOWED, "src/nexus/hooks/mailbox_drain.py", "_resolve_endpoint._lease", "the drain's lease source, handed to select_service_token(lease=...)", "reference to _read_local_supervisor_token")
_allow(ALLOWED, "src/nexus/hooks/mailbox_drain.py", "_read_local_supervisor_token", "owner-only-audited read of the supervisor token; its one consumer is the select_service_token(lease=...) source above", 'lease.get("token")')
_allow(ALLOWED, _ROUTING, "_read_service_lease", "thin wrapper over the shared lease reader; its record feeds the bound resolver", "reference to read_storage_service_lease", 'lease.get("token")')
_allow(ALLOWED, _ROUTING, "_read_config_yml_credentials", "thin wrapper over the shared credential-file reader; its answer feeds the bound resolver", "reference to read_config_yml_credentials")
_allow(ALLOWED, _ROUTING, "_read_data_token_lease", _DT_LEASE, "reference to read_data_token_lease")
_allow(ALLOWED, _EP, "pinned_endpoint", "the telemetry senders' resolver (RDR-259): the saved service_token goes with the saved URL, the lease token only to the lease's own endpoint (origins_equal), and no bearer goes over cleartext off this machine", "reference to read_storage_service_lease", 'lease.get("token")', "reference to read_config_yml_credentials", "reference to read_data_token_lease", "reference to read_data_token_lease", 'lease["token"]', 'lease["token"]')
_allow(ALLOWED, "src/nexus/commands/settings_cmd.py", "telemetry_endpoint_missing", "answers whether the plugin's pinned_endpoint would name an engine: presence only, nothing is sent", "reference to persisted_credentials", 'lease.get("token")')
_allow(ALLOWED, "src/nexus/commands/settings_cmd.py", "_lease", "the live lease as a dict for telemetry_endpoint_missing's presence check; nothing is sent", 'lease.get("token")')
_allow(ALLOWED, "conexus/hooks/scripts/mailbox_drain.py", "_resolve_endpoint", _DT_LEASE + "; the drain then holds it to the cleartext rule", "reference to read_data_token_lease")
# -- the data-token manager's own lease file
_DT = "src/nexus/db/data_token.py"
_allow(ALLOWED, _DT, "DataTokenManager._resolve_credential", "presence only without a base_url (is_configured); with one it returns bound_mint_credential's answer", "get_credential('mint_token')")
_allow(ALLOWED, _DT, "DataTokenManager._bearer_for_configured", _DT_LEASE, "reference to _read_lease")
_allow(ALLOWED, _DT, "DataTokenManager._mint_guarded", _DT_LEASE, "reference to _read_lease_newer", "reference to _read_lease_newer")
_allow(ALLOWED, _DT, "DataTokenManager._read_lease_newer", _DT_LEASE, "reference to _read_lease")
_allow(ALLOWED, _DT, "DataTokenManager._refresh_locked", _DT_LEASE, "reference to _read_lease_newer")
_allow(ALLOWED, _DT, "DataTokenManager.fresh_lease_token", _DT_LEASE, "reference to _read_lease")
_allow(ALLOWED, _DT, "DataTokenManager.has_fresh_lease", _PRESENCE, "reference to _read_lease")
# -- reviewed readers that never send the value to a caller-chosen origin
_allow(ALLOWED, "src/nexus/db/t2/http_token_store.py", "_static_token_source", _PRESENCE + " (labels where a token that was already sent came from)", "get_credential('service_token')")
_allow(ALLOWED, "src/nexus/db/t2/http_token_store.py", "HttpTokenStore._auth_error_lines", _PRESENCE, "get_credential('mint_token')")
_allow(ALLOWED, "src/nexus/health.py", "_check_mint_token", _PRESENCE, "get_credential('mint_token')")
_allow(ALLOWED, "src/nexus/health.py", "_check_static_service_token", "arms-check only; the probe's own token comes from bound_static_service_token", "get_credential('mint_token')")
_allow(ALLOWED, "src/nexus/health.py", "_check_credential_persistence", _PRESENCE + " (which keys sit in the file versus the shell)", '.get("credentials")')
_allow(ALLOWED, "src/nexus/health.py", "_check_llm_key_persistence", _PRESENCE + " (LLM provider keys)", "get_global_config_section('credentials')")
_allow(ALLOWED, "src/nexus/commands/init.py", "_managed_onboarding", _PRESENCE + " (is the wizard needed)", "get_credential('service_token')", "get_credential(<non-literal>)")
_allow(ALLOWED, "src/nexus/commands/init.py", "_poll_service_lease", _LIFECYCLE, "reference to discover_storage_service_lease")
_allow(ALLOWED, "src/nexus/backup/local_token.py", "_set_by_the_user", _PRESENCE, "get_credential('service_token')")
_allow(ALLOWED, "src/nexus/backup/local_token.py", "_lease_token", "compares the bearer already resolved for the base URL with the lease's operator token, and mints/revokes only when they are equal, i.e. at the lease origin", "reference to discover_lease")
_allow(ALLOWED, "src/nexus/upgrade_ladder/preconditions.py", "wait_for_engine", "waits for the lease to name a healthy engine; the token is dropped", "reference to discover_lease")
_allow(ALLOWED, "src/nexus/upgrade_ladder/preconditions.py", "_default_lease", "the wait's lease source; wait_for_engine drops the token", "reference to discover_storage_service_lease")
_allow(ALLOWED, "src/nexus/commands/daemon.py", "_start_via_autostart_unit", _LIFECYCLE, "reference to discover_storage_service_lease")
_allow(ALLOWED, "src/nexus/commands/daemon.py", "ensure_storage_supervisor", _LIFECYCLE, "reference to discover_storage_service_lease", "reference to discover_storage_service_lease", "reference to discover_storage_service_lease")
_allow(ALLOWED, "src/nexus/commands/daemon.py", "service_status_cmd", "`nx daemon service status` asks the engine the lease names, with that lease's own token on that lease's own host and port (the status route, loopback)", 'lease.get("token")')
_allow(ALLOWED, "src/nexus/daemon/storage_service_daemon.py", "StorageServiceSupervisor._start_locked", "returns the running supervisor's own lease endpoint to `nx daemon service start`'s caller; no request is built here", 'lease.get("token")')
_allow(ALLOWED, "src/nexus/daemon/installer.py", "_probe_survivors", _LIFECYCLE + " (reads the supervisor pid off the lease)", "reference to _discover_service_lease")
_allow(ALLOWED, "src/nexus/commands/config_cmd.py", "config_get", _DISPLAY, "get_credential(<non-literal>)")
_allow(ALLOWED, "src/nexus/commands/config_cmd.py", "config_init", _DISPLAY, "get_credential(<non-literal>)")
_allow(ALLOWED, "src/nexus/commands/config_cmd.py", "config_list", _DISPLAY, '.get("credentials")')
_allow(ALLOWED, "src/nexus/config.py", "get_credential", "the generic reader: every caller that names a stored credential is held by this lint", '.get("credentials")')
_allow(ALLOWED, "src/nexus/config.py", "persisted_credentials", "the file-only reader: every caller that names a stored credential is held by this lint", '.get("credentials")')
_allow(ALLOWED, "src/nexus/config.py", "_persisted_service_url", "reads the saved service_url, which is not a secret", '.get("credentials")')
_allow(ALLOWED, "src/nexus/config.py", "unset_credential", "deletes a saved value", '.get("credentials")')
_allow(ALLOWED, "src/nexus/config.py", "local_embed_profile", "reads the saved voyage_api_key to choose an embedding profile; never sent", "reference to persisted_credentials")
_allow(ALLOWED, "src/nexus/operators/llm_backend.py", "_credential", "LLM provider keys: the name comes from the provider's own key table", "get_credential(<non-literal>)")
_allow(ALLOWED, "src/nexus/operators/llm_backend.py", "real_key_present", _PRESENCE + " (LLM provider keys)", "get_credential(<non-literal>)")
_allow(ALLOWED, "src/nexus/sensitive_output.py", "mcp_output_setting", "reads one named non-credential setting section", "get_global_config_section(<non-literal>)")


@pytest.fixture(scope="module")
def scanned() -> tuple[list[Site], int]:
    return _sites()


def test_no_stored_credential_is_read_outside_the_primitive(scanned) -> None:
    sites, files = scanned
    counts = _tally(sites)
    stray = {key: n for key, n in counts.items() if key not in ALLOWED}
    grown = {key: (n, ALLOWED[key][0]) for key, n in counts.items() if key in ALLOWED and n > ALLOWED[key][0]}
    sys.stderr.write(
        f"ec37o credential-read lint: {len(sites)} read sites in {len(counts)} (function, kind) groups examined in "
        f"{files} files, {len(sites) - sum(n for k, n in counts.items() if k in stray)} allowlisted, "
        f"{sum(stray.values())} stray, {len(grown)} groups grown past their allowance\n"
    )
    lines = {key: sorted(line for p, q, k, line in sites if (p, q, k) == key) for key in [*stray, *grown]}
    listing = "\n".join(f"  {p}:{lines[(p, q, k)]} in {q}: {k}" for p, q, k in lines)
    assert not stray and not grown, (
        "a stored credential (service_token, mint_token, the supervisor lease token) is read outside the origin-binding "
        "primitive, or an allowlisted function gained a read of a kind it already had (nexus-ec37o). Route it through "
        "select_service_token / bound_mint_credential, or, for a reader that never sends the value to a caller-chosen "
        f"origin, add it to ALLOWED with the reason (and the new count):\n{listing}"
    )


def test_every_allowlist_entry_still_matches_its_reads(scanned) -> None:
    sites, _files = scanned
    counts = _tally(sites)
    dead = sorted(key for key in ALLOWED if key not in counts)
    shrunk = sorted((key, counts[key], ALLOWED[key][0]) for key in ALLOWED if key in counts and counts[key] < ALLOWED[key][0])
    assert not dead, f"allowlist entries that match no read site (delete them, the list only shrinks): {dead}"
    assert not shrunk, f"allowlist entries whose function now holds fewer such reads than allowed (lower the count): {shrunk}"


def _family(kind: str) -> str:
    """``get_credential('x')`` -> ``get_credential``; ``reference to f`` -> ``reference``; ``lease["token"]`` -> ``lease``."""
    if kind.startswith("lease"):
        return "lease"
    if kind.startswith(("[", ".get")):
        return "credentials-block"
    return kind.split("(")[0].split(" ")[0]


def test_the_walk_is_not_vacuous(scanned) -> None:
    sites, files = scanned
    kinds = {_family(kind) for _p, _q, kind, _l in sites}
    assert files >= 500, f"the walk examined {files} files; the scan roots moved"
    assert len(sites) >= 60, f"only {len(sites)} read sites found; the detectors stopped matching"
    # every detector fires at least once on the real tree
    assert {"get_credential", "_stored_credential", "stored_credential", "get_global_config_section", "reference", "lease", "credentials-block"} <= kinds, kinds


#: (source, number of sites the walk must find in it): a mutation table for the detectors themselves.
DETECTOR_CASES: list[tuple[str, int]] = [
    ("def f():\n    return get_credential('service_token')\n", 1),
    ("def f():\n    return cfg.get_credential('mint_token')\n", 1),
    ("def f():\n    return get_credential('service_url')\n", 0),
    ("def f(c):\n    return read_config_yml_credentials(c)\n", 1),
    ("def f():\n    return discover_lease()\n", 1),
    ("def f():\n    return discover_lease()[1]\n", 1),
    ("def f():\n    return discover_lease()[0]\n", 0),
    ("def f():\n    url, _token = discover_lease()\n    return url\n", 0),
    ("def f():\n    url, token = discover_lease()\n    return url, token\n", 1),
    ("def f(c):\n    return audited_lease(c)\n", 1),
    ("def f(lease):\n    return lease['token']\n", 1),
    ("def f(lease):\n    return lease.get('token')\n", 1),
    ("def f(other):\n    return other['token']\n", 0),
    # -- the shapes the first version missed
    ("def f(c):\n    return persisted_credentials(c)['service_token']\n", 1),
    ("def f(c):\n    return nexus.config.persisted_credentials(c)\n", 1),
    ("def f():\n    return get_global_config_section('credentials')\n", 1),
    ("def f():\n    return get_global_config_section('llm')\n", 0),
    ("def f(n):\n    return get_global_config_section(n)\n", 1),
    ("def f():\n    return get_credential(name='service_token')\n", 1),
    ("def f():\n    return get_credential(name='service_url')\n", 0),
    ("def f(n):\n    return get_credential(n)\n", 1),
    ("def f(n):\n    return get_credential(name=n)\n", 1),
    ("def f(n):\n    return stored_credential(n)\n", 1),
    ("from nexus.config import get_credential as gc\ndef f():\n    return gc('service_token')\n", 1),
    ("from nexus.config import get_credential as gc\ndef f():\n    return gc('service_url')\n", 0),
    ("from nexus.config import get_credential as gc\ndef f(n):\n    return gc(n)\n", 1),
    ("from nexus.db.service_endpoint import discover_lease as dl\ndef f():\n    return dl()\n", 1),
    ("from x import read_data_token_lease as r\ndef f(c, u):\n    return r(c, u)\n", 1),
    ("from x import persisted_credentials as pc\ndef f(c):\n    return pc(c)\n", 1),
    ("def f():\n    g = get_credential\n    return g('service_token')\n", 1),
    ("def f(reg, scope):\n    return reg.discover(scope).endpoint.get('token')\n", 1),
    ("def f(reg, scope):\n    return reg.discover(scope).endpoint['token']\n", 1),
    ("def f(reg, scope):\n    rec = reg.discover(scope)\n    return rec.endpoint.get('token', '')\n", 1),
    ("def f(reg, scope):\n    ep = reg.discover(scope).endpoint\n    return ep.get('token')\n", 1),
    ("def f(reg, scope):\n    ep = reg.discover(scope).endpoint\n    return ep['token']\n", 1),
    ("def f(reg, scope):\n    ep = reg.discover(scope).endpoint\n    return ep.get('host')\n", 0),
    ("def f(reg, scope):\n    return reg.discover(scope).endpoint.get('port')\n", 0),
    ("def f(c, u):\n    return read_data_token_lease(c, u)\n", 1),
    ("def f(r):\n    return r.read_lease()['token']\n", 2),  # the reader reference and the token read are two sites
    ("def f(r):\n    return r.read_lease()\n", 1),
    ("def f(c):\n    return read_local_supervisor_token(c)\n", 1),
    ("def f(d):\n    return yaml.safe_load(d).get('credentials')\n", 1),
    ("def f(d):\n    return yaml.safe_load(d)['credentials']['service_token']\n", 1),
    ("def f(d):\n    return d.get('llm')\n", 0),
    ("def f(other):\n    return other.get('token')\n", 0),
]


@pytest.mark.parametrize(("source", "expect"), DETECTOR_CASES)
def test_the_detectors_see_a_read_and_leave_a_url_only_use_alone(source: str, expect: int) -> None:
    """A mutation check on the lint itself: each shape it claims to catch is caught, each it claims to allow is not."""
    assert len(_walk_source("synthetic.py", source)) == expect, _walk_source("synthetic.py", source)


def test_a_new_read_inside_an_allowlisted_function_is_seen() -> None:
    """The allowlist is per (function, kind) with a count, so a second read next to a reviewed one changes the tally."""
    one = _tally(_walk_source("a.py", "def f():\n    return get_credential('service_token')\n"))
    two = _tally(_walk_source("a.py", "def f():\n    a = get_credential('service_token')\n    return a, get_credential('service_token')\n"))
    other = _tally(_walk_source("a.py", "def f():\n    return get_credential('service_token'), get_credential('mint_token')\n"))
    assert one == {("a.py", "f", "get_credential('service_token')"): 1}
    assert two == {("a.py", "f", "get_credential('service_token')"): 2}
    assert len(other) == 2
