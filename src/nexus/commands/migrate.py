# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate — migration utilities."""
from __future__ import annotations

from typing import TYPE_CHECKING

import click
import structlog

from nexus.config import get_credential
from nexus.db import make_t3
from nexus.db.t3 import _STORE_TYPES

if TYPE_CHECKING:
    import chromadb

_log = structlog.get_logger(__name__)

# Maximum documents fetched per page during migration.  Keeps memory usage
# bounded even for very large collections.
_PAGE_SIZE = 5_000


def _cloud_admin_client(api_key: str) -> "chromadb.AdminClient":
    """Return a ChromaDB AdminClient pointed at Chroma Cloud.

    Mirrors the same Settings wiring used internally by ``chromadb.CloudClient``
    (verified against chromadb 0.6.x; review if upgrading chromadb major version).
    """
    import chromadb
    from chromadb import Settings
    from chromadb.auth.token_authn import TokenTransportHeader

    settings = Settings()
    settings.chroma_api_impl = "chromadb.api.fastapi.FastAPI"
    settings.chroma_server_host = "api.trychroma.com"
    settings.chroma_server_http_port = 443
    settings.chroma_server_ssl_enabled = True
    settings.chroma_client_auth_provider = (
        "chromadb.auth.token_authn.TokenAuthClientProvider"
    )
    settings.chroma_client_auth_credentials = api_key
    settings.chroma_auth_token_transport_header = TokenTransportHeader.X_CHROMA_TOKEN
    settings.chroma_overwrite_singleton_tenant_database_access_from_auth = True
    return chromadb.AdminClient(settings)


def ensure_databases(
    admin: "chromadb.AdminClient",
    *,
    tenant: str,
    base: str,
) -> dict[str, bool]:
    """Create the four T3 databases under *tenant* if they do not already exist.

    Returns a mapping of ``{db_name: created}`` where ``created=True`` means the
    database was freshly created and ``False`` means it already existed.

    ``UniqueConstraintError`` (HTTP 409) is silently swallowed — it means the
    database already exists, which is the desired end state.
    """
    from chromadb.errors import ChromaError, UniqueConstraintError

    result: dict[str, bool] = {}
    for t in _STORE_TYPES:
        db_name = f"{base}_{t}"
        try:
            admin.create_database(db_name, tenant=tenant)
            result[db_name] = True
        except UniqueConstraintError:
            result[db_name] = False
        except ChromaError as exc:
            # Chroma Cloud may return a plain ChromaError (not UniqueConstraintError)
            # with "already exists" in the message — treat as idempotent.
            if "already exists" in str(exc).lower():
                result[db_name] = False
            else:
                raise
    return result


def migrate_t3_collections(
    source,
    dest,
    *,
    verbose: bool = False,
) -> dict[str, int]:
    """Copy collections from *source* (old single-database CloudClient) to *dest*
    (new four-store T3Database).

    Returns a mapping of ``{collection_name: documents_copied}``.  Entries with
    a value of ``0`` were skipped because the destination already contained the
    same number of documents as the source (idempotency).  Entries with a value
    of ``-1`` indicate a per-collection failure (logged; migration continues).

    The migration is:
    - **Non-destructive**: the source store is never modified.
    - **Idempotent**: if a collection already exists in the destination with the
      same document count as the source, it is silently skipped.
    - **Verbatim**: embeddings from the source are copied directly to the
      destination without re-embedding (no Voyage AI calls needed).
    - **Paginated**: fetches at most ``_PAGE_SIZE`` documents per request to
      keep memory usage bounded on large collections.
    """
    result: dict[str, int] = {}
    for col_or_name in source.list_collections():
        name = col_or_name if isinstance(col_or_name, str) else col_or_name.name
        src_col = source.get_collection(name)
        src_count = src_col.count()

        # Idempotency check — skip when dest already has the right count.
        try:
            info = dest.collection_info(name)
            if info["count"] == src_count:
                result[name] = 0
                if verbose:
                    click.echo(f"  skipping {name} ({src_count} docs already in dest)")
                continue
        except KeyError:
            pass  # collection not found in dest; proceed to copy

        try:
            dest_col = dest.get_or_create_collection(name)
            page_offset = 0
            copied = 0
            while page_offset < src_count:
                page_limit = min(_PAGE_SIZE, src_count - page_offset)
                data = src_col.get(
                    include=["documents", "metadatas", "embeddings"],
                    limit=page_limit,
                    offset=page_offset,
                )
                if not data["ids"]:
                    break
                dest_col.upsert(
                    ids=data["ids"],
                    documents=data["documents"],
                    embeddings=data["embeddings"],
                    metadatas=data["metadatas"],
                )
                copied += len(data["ids"])
                page_offset += len(data["ids"])
                if verbose and src_count > _PAGE_SIZE:
                    click.echo(f"  {name}: {copied}/{src_count} docs…")
            result[name] = copied
            if verbose:
                click.echo(f"  copied {name}: {copied} docs")
        except Exception as exc:
            _log.warning("migrate_collection_failed", collection=name, error=str(exc))
            if verbose:
                click.echo(f"  FAILED {name}: see log for details")
            result[name] = -1

    return result


@click.group("migrate")
def migrate() -> None:
    """Migration utilities for Nexus data stores."""


@migrate.command("t3")
@click.option("--verbose", "-v", is_flag=True, help="Print per-collection progress.")
def migrate_t3_cmd(verbose: bool) -> None:
    """Migrate T3 collections from the old single-database store to the new
    four-store layout.

    Reads all collections from the original ``chroma_database`` (e.g. ``nexus``)
    and copies them to the four typed databases (``nexus_code``, ``nexus_docs``,
    ``nexus_rdr``, ``nexus_knowledge``).  The operation is idempotent — already-
    migrated collections are skipped.

    **Prerequisites:**

    \\b
    1. Ensure the original single database (nexus) still exists as the source.
    2. Run this command — it will auto-create the four typed databases and copy
       your collections.
    """
    import chromadb

    tenant = get_credential("chroma_tenant")
    database = get_credential("chroma_database")
    api_key = get_credential("chroma_api_key")

    if not all([tenant, database, api_key]):
        missing = [k for k, v in [
            ("chroma_tenant", tenant),
            ("chroma_database", database),
            ("chroma_api_key", api_key),
        ] if not v]
        raise click.ClickException(
            f"{', '.join(missing)} not set — run: nx config init"
        )

    # Source: the OLD single ChromaDB Cloud database (unsuffixed name).
    try:
        source = chromadb.CloudClient(
            tenant=tenant, database=database, api_key=api_key
        )
    except Exception as exc:
        _log.debug("source_connect_failed", database=database, error=str(exc))
        raise click.ClickException(
            f"Cannot connect to source database {database!r}: connection failed"
        ) from exc

    # Auto-create the four destination databases (idempotent).
    # Must happen BEFORE make_t3() which connects to those databases.
    click.echo(f"Ensuring four T3 databases exist for base {database!r}…")
    admin = _cloud_admin_client(api_key)
    try:
        created = ensure_databases(admin, tenant=tenant, base=database)
        for db_name, was_created in created.items():
            status = "created" if was_created else "already exists"
            click.echo(f"  {db_name}: {status}")
    except Exception as exc:
        # Chroma Cloud free-tier and some plans reject AdminClient.create_database
        # with Permission denied (HTTP 403).  If the databases already exist in the
        # dashboard this is harmless — make_t3() will succeed.  If they don't exist
        # make_t3() will fail with a clear actionable message.
        _log.debug("ensure_databases_failed", error=str(exc))
        click.echo(
            "  Warning: could not auto-create databases (permission denied or plan restriction).\n"
            "  If you have already created the four databases in your ChromaDB Cloud dashboard,\n"
            "  migration will continue.  Otherwise create these databases first:\n"
            + "\n".join(f"    - {database}_{t}" for t in _STORE_TYPES)
        )

    # Destination: the new four-store T3Database (creates 4 suffixed CloudClients).
    try:
        dest = make_t3()
    except RuntimeError as exc:
        # RuntimeError from T3Database.__init__ is explicitly sanitized (no credential
        # text in the message).  Review this handler if make_t3() internals change.
        raise click.ClickException(str(exc)) from exc

    click.echo(f"\nMigrating collections from {database!r} to four-store layout…")
    result = migrate_t3_collections(source, dest, verbose=verbose)

    copied_total = sum(v for v in result.values() if v > 0)
    skipped = sum(1 for v in result.values() if v == 0)
    failed = sum(1 for v in result.values() if v < 0)
    cols = len(result)
    click.echo(
        f"Done: {cols} collection(s) processed, "
        f"{copied_total} doc(s) copied, "
        f"{skipped} collection(s) skipped (already up-to-date)"
        + (f", {failed} collection(s) failed" if failed else "") + "."
    )
