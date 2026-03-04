# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx config — manage credentials and settings."""
import os

import click
import yaml

from nexus.config import (
    CREDENTIALS,
    _global_config_path,
    get_credential,
    load_config,
    set_credential,
)

# ── Signup URLs shown during `nx config init` ─────────────────────────────────

_SIGNUP = {
    "chroma_api_key":    "https://trychroma.com  (Cloud → API Keys)",
    "chroma_database":   "https://trychroma.com  (Cloud → Dashboard — choose a base name, e.g. 'nexus')",
    "voyage_api_key":    "https://voyageai.com   (Dashboard → API Keys)",
}


@click.group("config")
def config_group() -> None:
    """Manage Nexus credentials and settings."""


# ── set ───────────────────────────────────────────────────────────────────────

@config_group.command("set")
@click.argument("key_value")
@click.argument("value", required=False)
def config_set(key_value: str, value: str | None) -> None:
    """Set a credential or config value.

    Accepts KEY=VALUE or KEY VALUE forms:

    \b
      nx config set chroma_api_key=sk-...
      nx config set chroma_api_key sk-...
    """
    if value is None:
        # KEY=VALUE form
        if "=" not in key_value:
            raise click.UsageError("Provide KEY=VALUE or KEY VALUE.")
        key, value = key_value.split("=", 1)
    else:
        key = key_value

    key = key.strip().lower().replace("-", "_")
    set_credential(key, value.strip())
    click.echo(f"Set {key}  →  {_global_config_path()}")


# ── get ───────────────────────────────────────────────────────────────────────

@config_group.command("get")
@click.argument("key")
@click.option("--show", is_flag=True, default=False, help="Reveal the full value instead of masking.")
def config_get(key: str, show: bool) -> None:
    """Print the current value of a credential (env var takes precedence)."""
    key = key.strip().lower().replace("-", "_")
    val = get_credential(key)
    if val:
        click.echo(val if show else _mask(val))
    else:
        click.echo(f"{key}: not set")


# ── list ──────────────────────────────────────────────────────────────────────

def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


@config_group.command("list")
def config_list() -> None:
    """Show all credentials and config settings."""
    click.echo("Credentials  (env var takes precedence over config file)\n")

    path = _global_config_path()
    file_data: dict = {}
    if path.exists():
        file_data = yaml.safe_load(path.read_text()) or {}
    file_creds = file_data.get("credentials", {})

    for cred, env_var in CREDENTIALS.items():
        env_val = os.environ.get(env_var, "")
        file_val = file_creds.get(cred, "")

        if env_val:
            source = f"env:{env_var}"
            display = _mask(env_val)
        elif file_val:
            source = "config.yml"
            display = _mask(file_val)
        else:
            source = ""
            display = "(not set)"

        line = f"  {cred:<22} {display}"
        if source:
            line += f"  [{source}]"
        click.echo(line)

    click.echo("\nSettings\n")
    cfg = load_config()
    for section, values in cfg.items():
        if section == "credentials":
            continue
        if isinstance(values, dict):
            for k, v in values.items():
                click.echo(f"  {section}.{k:<18} {v}")
        else:
            click.echo(f"  {section:<24} {values}")


# ── init ──────────────────────────────────────────────────────────────────────

@config_group.command("init")
def config_init() -> None:
    """Interactive wizard to configure all required credentials.

    Skips any credential already present in the environment.
    Saves to ~/.config/nexus/config.yml.
    """
    click.echo("Nexus credential setup\n")
    click.echo("Keys are stored in ~/.config/nexus/config.yml")
    click.echo("Environment variables always take precedence.\n")

    _required = [
        ("chroma_api_key",  "ChromaDB Cloud API key"),
        ("chroma_database", "ChromaDB database base name (e.g. 'nexus')"),
        ("voyage_api_key",  "Voyage AI API key"),
    ]

    for key, label in _required:
        env_var = CREDENTIALS[key]
        existing_env = os.environ.get(env_var, "")
        existing_file = get_credential(key)

        if existing_env:
            click.echo(f"  {label}: already set via environment ({env_var}={_mask(existing_env)})")
            continue

        url = _SIGNUP.get(key, "")
        if url:
            click.echo(f"  Get yours at: {url}")

        current = _mask(existing_file) if existing_file else None
        prompt_text = f"  {label}"
        val = click.prompt(prompt_text, default=current or "", show_default=bool(current))

        if val and val != current:
            set_credential(key, val)

    click.echo(f"\nSaved to {_global_config_path()}")

    # Auto-provision the four T3 databases if both required credentials are now set.
    api_key = get_credential("chroma_api_key")
    database = get_credential("chroma_database")
    if api_key and database:
        click.echo("\nProvisioning ChromaDB Cloud databases…")
        try:
            from nexus.commands._provision import _cloud_admin_client, ensure_databases
            admin = _cloud_admin_client(api_key)
            created = ensure_databases(admin, base=database)
            for db_name, was_created in created.items():
                status = "created" if was_created else "already exists"
                click.echo(f"  {db_name}: {status}")
        except Exception as exc:
            click.echo(
                f"  Warning: could not auto-create databases ({exc}).\n"
                "  Create these databases in your ChromaDB Cloud dashboard if needed:\n"
                + "\n".join(f"    - {database}_{t}" for t in ("code", "docs", "rdr", "knowledge"))
            )

    click.echo("Run 'nx doctor' to verify all services are reachable.")
