# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

# ── Credential registry ───────────────────────────────────────────────────────
# Maps config-file key → environment variable name
CREDENTIALS: dict[str, str] = {
    "chroma_api_key":    "CHROMA_API_KEY",
    "chroma_tenant":     "CHROMA_TENANT",
    "chroma_database":   "CHROMA_DATABASE",
    "voyage_api_key":    "VOYAGE_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "mxbai_api_key":     "MXBAI_API_KEY",
}

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    "server": {
        "port": 7890,
        "headPollInterval": 10,
        "ignorePatterns": [],
    },
    "embeddings": {
        "codeModel": "voyage-code-3",
        "docsModel": "voyage-4",
        "rerankerModel": "rerank-2.5",
    },
    "pm": {
        "archiveTtl": 90,
    },
    "mxbai": {
        "stores": [],
    },
    "chromadb": {
        "tenant": "",
        "database": "",
    },
    "client": {
        "host": "localhost",
    },
}

# Env var → (section, key, type) mapping
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "NX_SERVER_PORT": ("server", "port", int),
    "NX_SERVER_HEAD_POLL_INTERVAL": ("server", "headPollInterval", int),
    "NX_EMBEDDINGS_CODE_MODEL": ("embeddings", "codeModel", str),
    "NX_EMBEDDINGS_DOCS_MODEL": ("embeddings", "docsModel", str),
    "NX_EMBEDDINGS_RERANKER_MODEL": ("embeddings", "rerankerModel", str),
    "NX_PM_ARCHIVE_TTL": ("pm", "archiveTtl", int),
    "NX_CLIENT_HOST": ("client", "host", str),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: override applied on top of base (deep for nested dicts)."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    for env_var, (section, key, cast) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is not None:
            result.setdefault(section, {})[key] = cast(raw)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def _global_config_path() -> Path:
    return Path.home() / ".config" / "nexus" / "config.yml"


def get_credential(name: str) -> str:
    """Return the credential value for *name*.

    Precedence: environment variable > ``~/.config/nexus/config.yml``.
    Returns ``""`` when not set in either location.
    """
    env_var = CREDENTIALS.get(name, name.upper())
    env_val = os.environ.get(env_var, "")
    if env_val:
        return env_val
    path = _global_config_path()
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        return data.get("credentials", {}).get(name, "")
    return ""


def set_credential(name: str, value: str) -> None:
    """Persist *name*=*value* under ``credentials`` in ``~/.config/nexus/config.yml``."""
    path = _global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("credentials", {})[name] = value
    content = yaml.dump(data, default_flow_style=False)
    # Atomic write: write to temp file in same directory, then os.replace()
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".config_")
    try:
        with os.fdopen(tmp_fd, "w") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_config(repo_root: Path | None = None) -> dict[str, Any]:
    """Load and merge configuration.

    Precedence (highest last wins):
      1. Built-in defaults
      2. Global config   — ``~/.config/nexus/config.yml``
      3. Per-repo config — ``<repo_root>/.nexus.yml`` (or cwd if repo_root is None)
      4. Env vars        — ``NX_*`` variables
    """
    config = copy.deepcopy(_DEFAULTS)

    # Global config
    global_path = Path.home() / ".config" / "nexus" / "config.yml"
    if global_path.exists():
        with global_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        config = _deep_merge(config, data)

    # Per-repo config
    repo_config_path = (repo_root or Path.cwd()) / ".nexus.yml"
    if repo_config_path.exists():
        with repo_config_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        config = _deep_merge(config, data)

    # Env var overrides
    config = _apply_env_overrides(config)

    return config
