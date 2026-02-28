# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate — migration utilities."""
from __future__ import annotations

import click

from nexus.config import get_credential
from nexus.db.t3 import _STORE_TYPES


def _cloud_admin_client(api_key: str):
    """Return a ChromaDB AdminClient pointed at Chroma Cloud.

    Mirrors the same Settings wiring used internally by ``chromadb.CloudClient``.
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


def ensure_databases(admin, *, tenant: str, base: str) -> dict[str, bool]:
    """Create the four T3 databases under *tenant* if they do not already exist.

    Returns a mapping of ``{db_name: created}`` where ``created=True`` means the
    database was freshly created and ``False`` means it already existed.

    ``UniqueConstraintError`` (HTTP 409) is silently swallowed — it means the
    database already exists, which is the desired end state.
    """
    from chromadb.errors import UniqueConstraintError

    result: dict[str, bool] = {}
    for t in _STORE_TYPES:
        db_name = f"{base}_{t}"
        try:
            admin.create_database(db_name, tenant=tenant)
            result[db_name] = True
        except UniqueConstraintError:
            result[db_name] = False
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
    same number of documents as the source (idempotency).

    The migration is:
    - **Non-destructive**: the source store is never modified.
    - **Idempotent**: if a collection already exists in the destination with the
      same document count as the source, it is silently skipped.
    - **Verbatim**: embeddings from the source are copied directly to the
      destination without re-embedding (no Voyage AI calls needed).
    """
    result: dict[str, int] = {}
    for col_or_name in source.list_collections():
        name = col_or_name if isinstance(col_or_name, str) else col_or_name.name
        src_col = source.get_collection(name)
        src_count = src_col.count()

        # Idempotency check — skip when dest already has the right count.
        if dest.collection_exists(name):
            try:
                info = dest.collection_info(name)
                if info["count"] == src_count:
                    result[name] = 0
                    if verbose:
                        click.echo(f"  skipping {name} ({src_count} docs already in dest)")
                    continue
            except KeyError:
                pass  # collection_info raises KeyError if not found; proceed to copy

        # Fetch all documents with their stored embeddings.
        data = src_col.get(
            include=["documents", "metadatas", "embeddings"],
            limit=src_count,
        )

        dest_col = dest.get_or_create_collection(name)
        dest_col.upsert(
            ids=data["ids"],
            documents=data["documents"],
            embeddings=data["embeddings"],
            metadatas=data["metadatas"],
        )
        copied = len(data["ids"])
        result[name] = copied
        if verbose:
            click.echo(f"  copied {name}: {copied} docs")

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

    \b
    1. Create the four typed databases in your ChromaDB Cloud dashboard:
       nexus_code, nexus_docs, nexus_rdr, nexus_knowledge
    2. Ensure the original single database (nexus) still exists as the source.
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
        raise click.ClickException(
            f"Cannot connect to source database {database!r}: {exc}"
        ) from exc

    # Destination: the new four-store T3Database (creates 4 suffixed CloudClients).
    try:
        from nexus.db import make_t3
        dest = make_t3()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    # Auto-create the four destination databases (idempotent).
    click.echo(f"Ensuring four T3 databases exist for base {database!r}…")
    admin = _cloud_admin_client(api_key)
    created = ensure_databases(admin, tenant=tenant, base=database)
    for db_name, was_created in created.items():
        status = "created" if was_created else "already exists"
        click.echo(f"  {db_name}: {status}")

    click.echo(f"\nMigrating collections from {database!r} to four-store layout…")
    result = migrate_t3_collections(source, dest, verbose=verbose)

    copied_total = sum(v for v in result.values() if v > 0)
    skipped = sum(1 for v in result.values() if v == 0)
    cols = len(result)
    click.echo(
        f"Done: {cols} collection(s) processed, "
        f"{copied_total} doc(s) copied, "
        f"{skipped} collection(s) skipped (already up-to-date)."
    )
