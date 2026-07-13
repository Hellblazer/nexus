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


def _get_config_value(dotted_key: str) -> str | None:
    """Look up a dotted key (e.g. ``pdf.mineru_server_url``) in the merged config.

    Returns the string value or ``None`` when the key is absent.
    """
    parts = dotted_key.split(".")
    node: object = load_config()
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return str(node)


# ── Signup hints shown during `nx config init` ────────────────────────────────

_SIGNUP = {
    "service_url":   "Your managed nexus service endpoint, e.g. https://api.conexus-nexus.com (or your provider's URL).",
    "service_token": "Bearer token issued by your service operator.",
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
    if "." in key:
        from nexus.config import set_config_value  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        set_config_value(key, value.strip())
        if key == "claude_assisted_remediation.enabled":
            _record_flag_consent(value.strip())
    else:
        set_credential(key, value.strip())
    click.echo(f"Set {key}  →  {_global_config_path()}")


def _record_flag_consent(raw_value: str) -> None:
    """Audit a grant OR revoke of the RDR-182 durable consent flag.

    The revocation-write half of the consent audit trail (nexus-ykzbj.15):
    the migration docstring promises revoke events are retained as
    first-class rows, so BOTH directions of ``nx config set
    claude_assisted_remediation.enabled <value>`` write a row —
    ``granted`` mirrors the gate's own strict parse (anything that would
    not enable the gate audits as a revoke). BEST-EFFORT by design: this
    command IS the remedy the refusal names, so an audit-store problem must
    never block the flag write itself — it warns loudly instead (unlike the
    release paths, which fail closed; releasing guidance and flipping a
    flag carry different stakes).
    """
    from datetime import datetime, timezone  # noqa: PLC0415 — deliberate function-scoped import

    granted = raw_value.strip().lower() in ("true", "1", "yes")
    try:
        from nexus.commands._helpers import t2_handle  # noqa: PLC0415 — deliberate function-scoped import
        from nexus.remediation import FLAG_CONSENT_SCOPE  # noqa: PLC0415 — deliberate function-scoped import

        with t2_handle() as db:
            if not hasattr(db.telemetry, "record_consent"):
                click.echo(
                    "WARNING: consent audit not recorded (service-mode T2 "
                    "lacks the consent-audit route; upgrade the engine) — the flag "
                    "change itself took effect.",
                    err=True,
                )
                return
            db.telemetry.record_consent(
                scope=FLAG_CONSENT_SCOPE,
                ts=datetime.now(timezone.utc).isoformat(),
                granted=granted,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort audit; the flag write must not be blocked by audit problems
        click.echo(
            f"WARNING: consent audit not recorded ({exc}) — the flag change "
            "itself took effect.",
            err=True,
        )


# ── get ───────────────────────────────────────────────────────────────────────

@config_group.command("get")
@click.argument("key")
@click.option("--show", is_flag=True, default=False, help="Reveal the full value instead of masking.")
def config_get(key: str, show: bool) -> None:
    """Print the current value of a credential or config setting.

    Accepts plain credential names or dotted paths for nested settings:

    \b
      nx config get voyage_api_key
      nx config get pdf.mineru_server_url
    """
    key = key.strip().lower().replace("-", "_")
    if "." in key:
        val = _get_config_value(key)
        if val is not None:
            # Non-credential settings are not secrets; display without masking
            click.echo(val)
        else:
            click.echo(f"{key}: not set")
    else:
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
    """Interactive wizard to configure managed-service (cloud) credentials.

    Collects the managed nexus service endpoint + bearer token (RDR-166).
    Skips any credential already present in the environment. Saves to
    ~/.config/nexus/config.yml.

    Local mode does not use this wizard: run ``nx init`` to choose a local
    embedder, or ``nx init --service`` to provision the local service stack.
    """
    config_path = _global_config_path()
    click.echo("Nexus managed-service setup wizard\n")
    click.echo(f"Credentials are stored in {config_path}")
    click.echo("Environment variables (NX_SERVICE_URL, NX_SERVICE_TOKEN) always take precedence.\n")

    _required = [
        ("service_url",   "Managed service URL"),
        ("service_token", "Managed service token"),
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

    click.echo("\nNext steps:")
    click.echo("  nx doctor          — probe the managed service (reachability + version)")
    click.echo("  nx index repo .    — index your current repository")
