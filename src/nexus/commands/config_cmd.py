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

# ── Signup hints shown during `nx config init` ────────────────────────────────

_SIGNUP = {
    "chroma_api_key":  "https://trychroma.com  →  Cloud  →  API Keys",
    "chroma_database": (
        "Choose a database name (e.g. 'nexus'). Nexus will provision this database in ChromaDB Cloud."
    ),
    "voyage_api_key":  "https://voyageai.com   →  Dashboard  →  API Keys",
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

_SEP = "─" * 60


@config_group.command("init")
def config_init() -> None:
    """Interactive wizard to configure all required credentials.

    Skips any credential already present in the environment.
    Saves to ~/.config/nexus/config.yml.
    """
    config_path = _global_config_path()
    click.echo("Nexus setup wizard\n")
    click.echo(f"Credentials are stored in {config_path}")
    click.echo("Environment variables (CHROMA_API_KEY, etc.) always take precedence.\n")

    _required = [
        ("chroma_api_key",  "ChromaDB Cloud API key"),
        ("chroma_database", "ChromaDB database base name"),
        ("voyage_api_key",  "Voyage AI API key"),
    ]

    for key, label in _required:
        env_var = CREDENTIALS[key]
        existing_env = os.environ.get(env_var, "")
        existing_file = get_credential(key)

        click.echo(_SEP)
        if existing_env:
            click.echo(f"{label}")
            click.echo(f"  Already set via environment: {env_var}={_mask(existing_env)}")
            click.echo("  (skipping — unset the environment variable to override here)")
            continue

        hint = _SIGNUP.get(key, "")
        if hint:
            click.echo(f"{label}")
            click.echo(f"  {hint}")

        current = _mask(existing_file) if existing_file else None
        val = click.prompt(
            f"\n  Enter value",
            default=current or "",
            show_default=bool(current),
            prompt_suffix=" > ",
        )

        if val and val != current:
            set_credential(key, val)

    click.echo(_SEP)
    click.echo(f"\nCredentials saved to {config_path}")

    # Auto-provision the T3 database if both required credentials are now set.
    api_key = get_credential("chroma_api_key")
    database = get_credential("chroma_database")
    if api_key and database:
        click.echo(f"\nProvisioning ChromaDB Cloud database '{database}'…")
        try:
            from nexus.commands._provision import _cloud_admin_client, ensure_databases
            admin = _cloud_admin_client(api_key)
            created = ensure_databases(admin, base=database)
            for db_name, was_created in sorted(created.items()):
                icon = "+" if was_created else "·"
                status = "created" if was_created else "already exists"
                click.echo(f"  {icon} {db_name}: {status}")
        except Exception as exc:
            click.echo(f"\n  Warning: could not auto-provision database ({exc}).")
            click.echo(f"  Create '{database}' manually in the ChromaDB Cloud dashboard.")

    click.echo("\nNext steps:")
    click.echo("  nx doctor          — verify all services are reachable")
    click.echo("  nx index repo .    — index your current repository")
