# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import os
from pathlib import Path
from typing import Any

import yaml

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
