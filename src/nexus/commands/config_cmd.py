# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx config — manage credentials and settings."""
import os
import stat
import sys
from pathlib import Path

import click
import yaml

from nexus.config import (
    CREDENTIALS,
    NON_SECRET_CREDENTIALS,
    _global_config_path,
    get_credential,
    load_config,
    set_credential,
)


#: RDR-204 P3.4 (nexus-ft04v.25): the two ``nx config set`` keys whose value
#: the engine adopts only at process spawn. The engine is the only writer of
#: ``nexus.embedding_profile`` and writes it at boot from these, so until a
#: restart the engine's profile and this client's intent differ invisibly.
#: The command itself writes NOTHING to the profile.
RESTART_HINT_KEYS: frozenset[str] = frozenset({"local.embed_model", "voyage_api_key"})
#: The restart recipe as docs/cli-reference.md "Local mode with Voyage" states
#: it; tests/test_config_cmd.py pins the two to the same string.
SERVICE_RESTART_COMMAND: str = "nx daemon service stop && nx daemon service start"
SERVICE_RESTART_HINT: str = (
    "A restart is required for the engine to adopt this (it reads the model "
    f"and key only at spawn): {SERVICE_RESTART_COMMAND}"
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

def _is_secret(key: str) -> bool:
    """A credential that is not a plain setting (nexus-ssqk9's exemptions)."""
    return "." not in key and key not in NON_SECRET_CREDENTIALS


#: Whether file permissions are POSIX mode bits, which ``--from-file`` checks.
_POSIX_MODES: bool = os.name == "posix"


def _read_private_file(path: Path) -> str:
    """Read a value file, refusing one other users can read (nexus-6fvwo).

    The point of ``--from-file`` is a credential that never becomes visible
    to another process; a file group or others can reach has already failed
    that, so it is refused with the remedy rather than read.

    The mode is checked on the open handle and the value read from that same
    handle, so the file checked is the file read. POSIX only: Windows has no
    mode bits to check, and its ACLs are not inspected.
    """
    with path.open(encoding="utf-8") as handle:
        mode = os.fstat(handle.fileno()).st_mode
        if not _POSIX_MODES:
            click.echo(
                f"Note: {path}'s permissions were not checked (no POSIX mode "
                "bits on this platform); make sure only you can read it.",
                err=True,
            )
        elif mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise click.UsageError(
                f"{path} is accessible to group or others (mode {stat.S_IMODE(mode):04o}); "
                f"a credential file must be 0600. Run: chmod 600 {path}"
            )
        return handle.read()


@config_group.command("set")
@click.argument("key_value")
@click.argument("value", required=False)
@click.option(
    "--stdin", "from_stdin", is_flag=True, default=False,
    help="Read the value from stdin, so it never appears in the process list.",
)
@click.option(
    "--from-file", "from_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None,
    help="Read the value from a file that only you can read (mode 0600).",
)
def config_set(key_value: str, value: str | None, from_stdin: bool, from_file: Path | None) -> None:
    """Set a credential or config value.

    Accepts KEY=VALUE or KEY VALUE forms, or KEY with --stdin / --from-file.
    For a secret, prefer --stdin or --from-file: a value on the command line
    is visible to every process you run (ps).

    \b
      printf %s "$VOYAGE_KEY" | nx config set voyage_api_key --stdin
      nx config set voyage_api_key --from-file ~/.secrets/voyage.key
      nx config set pdf.extractor=mineru
    """
    inline = value is not None or "=" in key_value
    sources = [inline, from_stdin, from_file is not None]
    if sum(sources) > 1:
        raise click.UsageError("Give the value one way: inline, --stdin or --from-file.")
    if from_stdin or from_file is not None:
        key = key_value
        raw = sys.stdin.read() if from_stdin else _read_private_file(from_file)
        value = raw.rstrip("\r\n")
        if not value.strip():
            raise click.UsageError("The value read was empty; nothing was set.")
    elif value is None:
        # KEY=VALUE form
        if "=" not in key_value:
            raise click.UsageError("Provide KEY=VALUE, KEY VALUE, or KEY --stdin.")
        key, value = key_value.split("=", 1)
    else:
        key = key_value

    key = key.strip().lower().replace("-", "_")
    if "." in key:
        from nexus.config import set_config_value  # noqa: PLC0415 — deliberate function-scoped import (defer heavy/optional dep, avoid circular import)
        set_config_value(key, value.strip())
    else:
        set_credential(key, value.strip())
    click.echo(f"Set {key}  →  {_global_config_path()}")
    if inline and value.strip() and _is_secret(key):
        click.echo(
            f"Note: {key} was on the command line, where other processes can read "
            f"it. Next time: nx config set {key} --stdin (or --from-file PATH).",
            err=True,
        )
    if key in RESTART_HINT_KEYS:
        click.echo(SERVICE_RESTART_HINT)


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
            # nexus-ssqk9: a NON_SECRET_CREDENTIALS entry (e.g. mint_tenant)
            # is a plain setting, not a secret -- display it unmasked
            # regardless of --show, same as a dotted-key setting above.
            click.echo(val if (show or key in NON_SECRET_CREDENTIALS) else _mask(val))
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
@click.option(
    "--keys-only", is_flag=True, default=False,
    help="Show which keys are set and where, with no value characters at all.",
)
def config_list(keys_only: bool) -> None:
    """Show all credentials and config settings.

    Secrets are masked but keep their first and last four characters. When
    the output goes anywhere but your own terminal, use --keys-only, which
    prints none, rather than redacting it yourself (nexus-6fvwo).
    """
    click.echo("Credentials  (env var takes precedence over config file)\n")

    path = _global_config_path()
    file_data: dict = {}
    if path.exists():
        file_data = yaml.safe_load(path.read_text()) or {}
    file_creds = file_data.get("credentials", {})

    for cred, env_var in CREDENTIALS.items():
        env_val = os.environ.get(env_var, "")
        file_val = file_creds.get(cred, "")

        unmasked = cred in NON_SECRET_CREDENTIALS and not keys_only
        if keys_only:
            source = f"env:{env_var}" if env_val else ("config.yml" if file_val else "")
            display = "set" if source else "not set"
        elif env_val:
            source = f"env:{env_var}"
            display = env_val if unmasked else _mask(env_val)
        elif file_val:
            source = "config.yml"
            display = file_val if unmasked else _mask(file_val)
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
                click.echo(f"  {section}.{k}" if keys_only else f"  {section}.{k:<18} {v}")
        else:
            click.echo(f"  {section}" if keys_only else f"  {section:<24} {values}")


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
            # Named, not masked: the wizard's screen is shared and pasted, and
            # a partial mask is how 8 characters leaked once (nexus-6fvwo).
            click.echo(f"  Already set via environment: {env_var}")
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
