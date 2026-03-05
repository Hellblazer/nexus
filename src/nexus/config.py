# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import structlog
import yaml

_log = structlog.get_logger(__name__)

# Protects the read-modify-write sequence in set_credential() against concurrent
# calls within the same process.  Cross-process safety is provided by the atomic
# os.replace() at the end; in-process safety requires this lock.
_config_lock = threading.Lock()

# ── Model constants ───────────────────────────────────────────────────────────

# ── Credential registry ───────────────────────────────────────────────────────
# Maps config-file key → environment variable name
CREDENTIALS: dict[str, str] = {
    "chroma_api_key":    "CHROMA_API_KEY",
    "chroma_tenant":     "CHROMA_TENANT",
    "chroma_database":   "CHROMA_DATABASE",
    "voyage_api_key":    "VOYAGE_API_KEY",
}

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    "embeddings": {
        "rerankerModel": "rerank-2.5",
    },
    "chromadb": {
        "tenant": "",
        "database": "",
    },
    "client": {
        "host": "localhost",
    },
    "indexing": {
        "code_extensions": [],
        "prose_extensions": [],
        "rdr_paths": ["docs/rdr"],
        "include_untracked": False,
    },
    "voyageai": {
        "read_timeout_seconds": 120,
    },
}

# Env var → (section, key, type) mapping
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "NX_EMBEDDINGS_RERANKER_MODEL": ("embeddings", "rerankerModel", str),
    "NX_CLIENT_HOST": ("client", "host", str),
    "NX_VOYAGEAI_READ_TIMEOUT_SECONDS": ("voyageai", "read_timeout_seconds", int),
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
            try:
                result.setdefault(section, {})[key] = cast(raw)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Invalid value for {env_var!r}: cannot convert {raw!r} to {cast.__name__}: {exc}"
                ) from exc
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
    if name not in CREDENTIALS:
        known = ", ".join(sorted(CREDENTIALS))
        raise ValueError(f"Unknown credential '{name}'. Known: {known}")
    path = _global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock covers the entire read-modify-write unit so two concurrent calls in
    # the same process cannot silently drop each other's change.
    with _config_lock:
        data: dict[str, Any] = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        data.setdefault("credentials", {})[name] = value
        content = yaml.dump(data, default_flow_style=False)
        # Atomic write: unique temp file → os.replace() (0o600 permissions).
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".config_")
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                fh.write(content)
            os.chmod(tmp_path, 0o600)
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
        if not isinstance(data, dict):
            _log.warning("global config is not a dict, ignoring", path=str(global_path))
            data = {}
        config = _deep_merge(config, data)

    # Per-repo config
    repo_config_path = (repo_root or Path.cwd()) / ".nexus.yml"
    if repo_config_path.exists():
        with repo_config_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            _log.warning("repo config is not a dict, ignoring", path=str(repo_config_path))
            data = {}
        config = _deep_merge(config, data)

    # Env var overrides
    config = _apply_env_overrides(config)

    return config
