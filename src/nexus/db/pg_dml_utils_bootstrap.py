# SPDX-License-Identifier: AGPL-3.0-or-later
"""Superuser bootstrap for the dml_utils / ddl_utils libraries (RDR-246 Phase 1 Step 4).

RDR-246 runs data moves as background jobs through ``dml_utils``, which needs the
``pg_background`` extension and three NOLOGIN roles the libraries name. Only the
cluster superuser can create them: ``nexus_admin`` is NOSUPERUSER, and the
libraries' changelogs run before nexus's own. So the step lives here, beside the
pgvector pre-install, in :func:`bootstrap_dml_utils`.

It creates, in the nexus database:

* the ``pg_background`` extension (which brings ``pgbackground_role``);
* ``dml_utils_owner``, ``dml_utils_caller`` and ``ddl_utils_caller``, all
  NOLOGIN NOBYPASSRLS (an existing role that drifted is converged);
* CREATE on the database and on schema ``public`` for ``dml_utils_owner``;
  ``ddl_utils_caller`` and ``pgbackground_role`` to ``dml_utils_owner``;
  ``dml_utils_owner``, ``dml_utils_caller`` and ``pgbackground_role`` to
  ``nexus_admin`` (so the engine can ``SET ROLE dml_utils_owner``);
* the ``liquibase`` schema owned by ``dml_utils_owner``, where both libraries
  keep their tracking tables.

Idempotent, and best-effort by contract: a bundle with no pg_background library
returns a result carrying the reason and changes nothing, so the engine finds
the extension missing, serves, and reports registered moves ``blocked:``. The
callers in :mod:`nexus.db.pg_provision` also catch anything this raises, so no
failure here ever fails a service start. The verified recipe is T2
``nexus_rdr/246-research-5``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import structlog

from nexus.db.pg_provision import (
    NEXUS_DB_NAME,
    PgBinaries,
    _psql,
    _psql_tuples,
    _sql_ident,
)

_log = structlog.get_logger(__name__)

EXTENSION = "pg_background"
#: Created by ``CREATE EXTENSION pg_background``; grantable once the extension exists.
BACKGROUND_ROLE = "pgbackground_role"
LIBRARY_SCHEMA = "liquibase"
#: The owner role's name is fixed by dml_utils (its default-privilege changeset names it).
OWNER_ROLE = "dml_utils_owner"
DML_CALLER_ROLE = "dml_utils_caller"
DDL_CALLER_ROLE = "ddl_utils_caller"
LIBRARY_ROLES: tuple[str, ...] = (OWNER_ROLE, DML_CALLER_ROLE, DDL_CALLER_ROLE)


@dataclass
class DmlUtilsBootstrap:
    """What one :func:`bootstrap_dml_utils` call did."""

    #: Why nothing was done (library absent, CREATE EXTENSION refused); None when it ran.
    skipped_reason: str | None = None
    extension_created: bool = False
    roles_created: list[str] = field(default_factory=list)
    schema_created: bool = False

    @property
    def applied(self) -> bool:
        return self.skipped_reason is None


def _state(bins: PgBinaries, port: int, os_user: str) -> dict[str, str]:
    """One round trip: what already exists. Role flags read ``name:login:bypassrls`` with 1/0 flags (a boolean concatenated to text is ``true``, not ``t``)."""
    names = ", ".join(f"'{r}'" for r in LIBRARY_ROLES)
    row = _psql_tuples(
        bins, port, NEXUS_DB_NAME, os_user,
        "SELECT "
        f"(SELECT count(*) FROM pg_extension WHERE extname = '{EXTENSION}'), "
        f"(SELECT count(*) FROM pg_available_extensions WHERE name = '{EXTENSION}'), "
        "(SELECT count(*) FROM pg_roles WHERE rolname = 'nexus_admin'), "
        f"(SELECT coalesce(pg_get_userbyid(nspowner)::text, '') FROM pg_namespace "
        f"WHERE nspname = '{LIBRARY_SCHEMA}'), "
        "(SELECT coalesce(string_agg(rolname || ':' || rolcanlogin::int || ':' || rolbypassrls::int, ','), '') "
        f"FROM pg_roles WHERE rolname IN ({names}))",
    )
    installed, available, admin, schema_owner, roles = (row.split("|") + [""] * 5)[:5]
    return {
        "installed": installed, "available": available, "admin": admin,
        "schema_owner": schema_owner, "roles": roles,
    }


def bootstrap_dml_utils(bins: PgBinaries, port: int, os_user: str) -> DmlUtilsBootstrap:
    """Create or converge the dml_utils prerequisites; see the module docstring.

    Raises on a psql failure past the extension step (the callers log and carry
    on). A missing pg_background library is NOT an exception: it returns a result
    with ``skipped_reason`` and creates nothing, roles included, since the grants
    of ``pgbackground_role`` need the extension.
    """
    out = DmlUtilsBootstrap()
    state = _state(bins, port, os_user)

    if state["installed"] == "0":
        if state["available"] == "0":
            out.skipped_reason = "pg_background is not in this PostgreSQL bundle (no control file)"
            _log.warning("pg_dml_utils_bootstrap_skipped", reason=out.skipped_reason)
            return out
        try:
            _psql(bins, port, NEXUS_DB_NAME, os_user, f"CREATE EXTENSION IF NOT EXISTS {EXTENSION}")
        except subprocess.CalledProcessError as exc:
            # A control file with no loadable library, the shape of a bundle built without it.
            out.skipped_reason = f"CREATE EXTENSION {EXTENSION} failed: {(exc.stderr or b'').strip()!r}"
            _log.warning("pg_dml_utils_bootstrap_skipped", reason=out.skipped_reason)
            return out
        out.extension_created = True
        _log.info("pg_extension_created", extension=EXTENSION)

    have = {
        name: (login == "1", bypass == "1")
        for name, login, bypass in (item.split(":") for item in state["roles"].split(",") if item)
    }
    owner = _sql_ident(OWNER_ROLE)
    statements: list[str] = []
    for role in LIBRARY_ROLES:
        ident = _sql_ident(role)
        if role not in have:
            statements.append(
                f"CREATE ROLE {ident} NOLOGIN NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
            out.roles_created.append(role)
        elif have[role] != (False, False):
            statements.append(f"ALTER ROLE {ident} NOLOGIN NOBYPASSRLS")
            _log.warning("pg_dml_utils_role_converged", role=role, was_login_bypassrls=have[role])
    statements += [
        f"GRANT CREATE ON DATABASE {_sql_ident(NEXUS_DB_NAME)} TO {owner}",
        f"GRANT CREATE ON SCHEMA public TO {owner}",
        f"GRANT {_sql_ident(DDL_CALLER_ROLE)}, {_sql_ident(BACKGROUND_ROLE)} TO {owner}",
    ]
    if state["admin"] != "0":
        statements.append(
            f"GRANT {owner}, {_sql_ident(DML_CALLER_ROLE)}, {_sql_ident(BACKGROUND_ROLE)} TO nexus_admin"
        )
    else:
        _log.warning("pg_dml_utils_no_nexus_admin", note="grants to nexus_admin skipped")
    if not state["schema_owner"]:
        statements.append(f"CREATE SCHEMA {_sql_ident(LIBRARY_SCHEMA)} AUTHORIZATION {owner}")
        out.schema_created = True
    elif state["schema_owner"] != OWNER_ROLE:
        # Not ours to take: moves report blocked rather than re-owning a schema we did not make.
        _log.warning(
            "pg_dml_utils_schema_owner_mismatch",
            schema=LIBRARY_SCHEMA, owner=state["schema_owner"], expected=OWNER_ROLE,
        )

    # One -c string is one implicit transaction: all of it or none of it.
    _psql(bins, port, NEXUS_DB_NAME, os_user, "; ".join(statements))
    _log.info(
        "pg_dml_utils_bootstrap_applied",
        extension_created=out.extension_created,
        roles_created=out.roles_created,
        schema_created=out.schema_created,
    )
    return out


def bootstrap_dml_utils_best_effort(
    bins: PgBinaries, port: int, os_user: str, *, via: str,
) -> DmlUtilsBootstrap | None:
    """:func:`bootstrap_dml_utils` for the provisioning call sites: a failure is logged,
    never raised, so a service start never fails on it and moves report ``blocked:``."""
    try:
        return bootstrap_dml_utils(bins, port, os_user)
    except Exception as exc:  # noqa: BLE001 — repair path must never break the no-op re-run or a start
        _log.warning("pg_dml_utils_bootstrap_failed", via=via, error=str(exc))
        return None
