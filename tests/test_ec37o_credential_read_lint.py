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

* ``get_credential("service_token" | "mint_token")``, or the primitive's own
  ``stored_credential`` / ``_stored_credential`` with such a name;
* any reference to ``read_config_yml_credentials`` (the plugin's reader of the credential file);
* any reference to a supervisor-lease reader (``discover_lease``, ``discover_lease_with_wait``,
  ``audited_lease``, ``read_storage_service_lease``), except the URL-only shapes
  ``discover_lease()[0]`` and ``url, _token = discover_lease()`` where the token is dropped on the
  same line.

The allowlist names the enclosing function of each site, with the reason it may read. It only
shrinks: an entry that matches no site fails, so a deleted reader leaves no dead permission
behind. The count of sites examined is asserted, so a walk that found nothing is a failure.
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
LEASE_READERS = frozenset({"discover_lease", "discover_lease_with_wait", "_discover_lease", "audited_lease", "read_storage_service_lease"})
CREDENTIAL_FILE_READERS = frozenset({"read_config_yml_credentials"})


def _ref_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _lease_named(node: ast.AST) -> bool:
    """``lease`` / ``found_lease`` / ``self._lease``: a variable that holds a supervisor lease record."""
    name = _ref_name(node)
    return name is not None and "lease" in name.lower()


class _Walker(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.scope: list[str] = []
        self.parents: list[ast.AST] = []
        self.sites: list[tuple[str, str, str, int]] = []

    def _qual(self) -> str:
        return ".".join(self.scope) or "<module>"

    def visit(self, node: ast.AST) -> None:
        self.parents.append(node)
        super().visit(node)
        self.parents.pop()

    def _enter(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter  # noqa: N815

    def _site(self, kind: str, node: ast.AST) -> None:
        self.sites.append((self.rel, self._qual(), kind, node.lineno))  # type: ignore[attr-defined]

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

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.slice, ast.Constant) and node.slice.value == "token" and _lease_named(node.value):
            self._site("lease[\"token\"]", node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute) and func.attr == "get" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "token" and _lease_named(func.value)
        ):
            self._site("lease.get(\"token\")", node)
        name = _ref_name(node.func)
        if name in NAME_TAKING_READERS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in STORED:
                self._site(f"{name}({first.value!r})", node)
        self.generic_visit(node)

    def _reference(self, node: ast.AST) -> None:
        name = _ref_name(node)
        if name in CREDENTIAL_FILE_READERS:
            self._site(f"reference to {name}", node)
        elif name in LEASE_READERS and not self._url_only(node):
            self._site(f"reference to {name}", node)

    def visit_Name(self, node: ast.Name) -> None:
        self._reference(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._reference(node)
        self.generic_visit(node)


def _walk_source(rel: str, source: str) -> list[tuple[str, str, str, int]]:
    walker = _Walker(rel)
    walker.visit(ast.parse(source))
    return walker.sites


def _sites() -> tuple[list[tuple[str, str, str, int]], int]:
    found: list[tuple[str, str, str, int]] = []
    files = 0
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            files += 1
            rel = path.relative_to(ROOT).as_posix()
            found.extend(_walk_source(rel, path.read_text(encoding="utf-8")))
    return found, files


_PRIMITIVE = "the origin-binding primitive itself (or the resolver it feeds): the read happens next to the origin check"
_PRESENCE = "presence or provenance only: the value is compared or counted, never sent"

#: ``(path, enclosing function)`` -> why it may read a stored credential. Only ever shrinks.
ALLOWED: dict[tuple[str, str], str] = {
    # -- the primitive, CLI side (src/nexus/db/service_endpoint.py)
    ("src/nexus/db/service_endpoint.py", "select_service_token"): _PRIMITIVE,
    ("src/nexus/db/service_endpoint.py", "bound_mint_credential"): _PRIMITIVE,
    ("src/nexus/db/service_endpoint.py", "discover_lease_with_wait"): "the lease reader the primitive's resolvers wait on; returns the lease's own URL with its token",
    ("src/nexus/db/service_endpoint.py", "recover_endpoint_from_lease"): "re-reads the lease after a rotation; URL and token come from the same lease record",
    ("src/nexus/db/service_endpoint.py", "resolve_service_config"): "the host/port leg: the lease is offered to select_service_token, which binds it",
    ("src/nexus/db/service_endpoint.py", "mint_armed"): _PRESENCE,
    ("src/nexus/db/service_endpoint.py", "is_endpoint_env_pinned"): _PRESENCE,
    # -- the primitive, plugin side (conexus/hooks/scripts/_endpoint_resolve.py, a stdlib mirror)
    ("conexus/hooks/scripts/_endpoint_resolve.py", "select_service_token"): _PRIMITIVE,
    ("conexus/hooks/scripts/_endpoint_resolve.py", "audited_lease"): "the owner-only-audited lease read the plugin's select_service_token takes",
    ("conexus/hooks/scripts/_endpoint_resolve.py", "resolve_base_url"): "reads the lease and saved URL to choose the endpoint, then hands the token choice to select_service_token",
    ("conexus/hooks/scripts/_endpoint_resolve.py", "endpoint_usable"): _PRESENCE,
    # -- callers that feed the primitive a lease or a saved credential and take its answer
    ("src/nexus/db/http_vector_client.py", "_discover_lease"): "the vector client's lease source, handed to select_service_token(lease=...)",
    ("src/nexus/db/http_vector_client.py", "_cached_lease"): "caches the pair above; its only consumer is select_service_token(lease=...)",
    ("src/nexus/db/http_vector_client.py", "_wait_for_lease_republication"): "waits for the supervisor to republish; the pair it stores goes back through the same primitive",
    ("src/nexus/db/t2/_refreshable_client.py", "_resolve_token_only"): "hands the lease reader to select_service_token for the explicit-base_url stores",
    ("conexus/hooks/scripts/routing/_lib.py", "_read_service_lease"): "thin wrapper over the shared lease reader; its record feeds the bound resolver",
    ("conexus/hooks/scripts/routing/_lib.py", "_read_config_yml_credentials"): "thin wrapper over the shared credential-file reader; its answer feeds the bound resolver",
    ("conexus/hooks/scripts/routing/_lib.py", "_engine_endpoint"): "the lease leg pairs the lease token with the lease's own host and port, never with an env URL",
    # -- reviewed readers that never send the value to a caller-chosen origin
    ("src/nexus/db/data_token.py", "DataTokenManager._resolve_credential"): "presence only without a base_url (is_configured); with one it returns bound_mint_credential's answer",
    ("src/nexus/db/t2/http_token_store.py", "_static_token_source"): _PRESENCE + " (labels where a token that was already sent came from)",
    ("src/nexus/db/t2/http_token_store.py", "HttpTokenStore._auth_error_lines"): _PRESENCE,
    ("src/nexus/health.py", "_check_mint_token"): _PRESENCE,
    ("src/nexus/health.py", "_check_static_service_token"): "arms-check only; the probe's own token comes from bound_static_service_token",
    ("src/nexus/commands/init.py", "_managed_onboarding"): _PRESENCE + " (is the wizard needed)",
    ("src/nexus/backup/local_token.py", "_set_by_the_user"): _PRESENCE,
    ("src/nexus/backup/local_token.py", "_lease_token"): "compares the bearer already resolved for the base URL with the lease's operator token, and mints/revokes only when they are equal, i.e. at the lease origin",
    ("src/nexus/upgrade_ladder/preconditions.py", "wait_for_engine"): "waits for the lease to name a healthy engine; the token is dropped",
}


@pytest.fixture(scope="module")
def scanned() -> tuple[list[tuple[str, str, str, int]], int]:
    return _sites()


def test_no_stored_credential_is_read_outside_the_primitive(scanned) -> None:
    sites, files = scanned
    stray = [site for site in sites if (site[0], site[1]) not in ALLOWED]
    sys.stderr.write(
        f"ec37o credential-read lint: {len(sites)} read sites examined in {files} files, "
        f"{len(sites) - len(stray)} allowlisted, {len(stray)} stray\n"
    )
    listing = "\n".join(f"  {path}:{line} in {qual}: {kind}" for path, qual, kind, line in stray)
    assert not stray, (
        "a stored credential (service_token, mint_token, the supervisor lease token) is read outside the origin-binding "
        "primitive (nexus-ec37o). Route it through select_service_token / bound_mint_credential, or, for a reader that "
        f"never sends the value to a caller-chosen origin, add it to ALLOWED with the reason:\n{listing}"
    )


def test_every_allowlist_entry_still_matches_a_read(scanned) -> None:
    sites, _files = scanned
    live = {(path, qual) for path, qual, _kind, _line in sites}
    dead = sorted(set(ALLOWED) - live)
    assert not dead, f"allowlist entries that match no read site (delete them, the list only shrinks): {dead}"


def _family(kind: str) -> str:
    """``get_credential('x')`` -> ``get_credential``; ``reference to f`` -> ``reference``; ``lease["token"]`` -> ``lease``."""
    if kind.startswith("lease"):
        return "lease"
    return kind.split("(")[0].split(" ")[0]


def test_the_walk_is_not_vacuous(scanned) -> None:
    sites, files = scanned
    kinds = {_family(kind) for _p, _q, kind, _l in sites}
    assert files >= 500, f"the walk examined {files} files; the scan roots moved"
    assert len(sites) >= 30, f"only {len(sites)} read sites found; the detectors stopped matching"
    # every detector fires at least once on the real tree
    assert {"get_credential", "_stored_credential", "stored_credential", "reference", "lease"} <= kinds, kinds


@pytest.mark.parametrize(
    ("source", "expect"),
    [
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
    ],
)
def test_the_detectors_see_a_read_and_leave_a_url_only_use_alone(source: str, expect: int) -> None:
    """A mutation check on the lint itself: each shape it claims to catch is caught, each it claims to allow is not."""
    assert len(_walk_source("synthetic.py", source)) == expect
