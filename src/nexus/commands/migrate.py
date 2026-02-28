# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx migrate — migration utilities."""
from __future__ import annotations

import click

from nexus.config import get_credential


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

    click.echo(f"Migrating from {database!r} to four-store layout…")
    result = migrate_t3_collections(source, dest, verbose=verbose)

    copied_total = sum(v for v in result.values() if v > 0)
    skipped = sum(1 for v in result.values() if v == 0)
    cols = len(result)
    click.echo(
        f"Done: {cols} collection(s) processed, "
        f"{copied_total} doc(s) copied, "
        f"{skipped} collection(s) skipped (already up-to-date)."
    )
