# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

_log = structlog.get_logger(__name__)

# Protects the read-modify-write sequence in set_credential() against concurrent
# calls within the same process.  Cross-process safety is provided by the atomic
# os.replace() at the end; in-process safety requires this lock.
_config_lock = threading.Lock()

# ── TuningConfig ─────────────────────────────────────────────────────────────


@dataclass
class TuningConfig:
    """Tunable constants for the indexing and search pipeline.

    All fields default to the values previously hard-coded in the respective
    modules.  Users can override via the ``[tuning]`` section of ``.nexus.yml``
    without changing source code.

    Sections mirror the ``[tuning]`` YAML structure::

        tuning:
          scoring:
            vector_weight: 0.7
            frecency_weight: 0.3
            file_size_threshold: 30
          frecency:
            decay_rate: 0.01
          chunking:
            code_chunk_lines: 150
            pdf_chunk_chars: 1500
          timeouts:
            git_log: 30
            ripgrep: 10
    """

    # scoring.py constants
    vector_weight: float = 0.7
    frecency_weight: float = 0.3
    file_size_threshold: int = 30

    # frecency.py constants
    decay_rate: float = 0.01

    # chunker.py / pdf_chunker.py constants
    code_chunk_lines: int = 150
    pdf_chunk_chars: int = 1500

    # timeout constants
    git_log_timeout: int = 30
    ripgrep_timeout: int = 10


def _tuning_from_dict(raw: dict[str, Any]) -> TuningConfig:
    """Construct a TuningConfig from the raw ``tuning`` section of the config dict.

    Unknown keys are silently ignored.  Invalid numeric types raise ValueError.
    """
    scoring = raw.get("scoring", {})
    frecency = raw.get("frecency", {})
    chunking = raw.get("chunking", {})
    timeouts = raw.get("timeouts", {})

    def _float(section: dict, section_name: str, key: str, default: float) -> float:
        val = section.get(key, default)
        try:
            return float(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"tuning.{section_name}.{key}: expected a number, got {val!r}"
            ) from exc

    def _int(section: dict, section_name: str, key: str, default: int) -> int:
        val = section.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"tuning.{section_name}.{key}: expected an integer, got {val!r}"
            ) from exc

    _d = TuningConfig()  # source of defaults — single source of truth
    return TuningConfig(
        vector_weight=_float(scoring, "scoring", "vector_weight", _d.vector_weight),
        frecency_weight=_float(scoring, "scoring", "frecency_weight", _d.frecency_weight),
        file_size_threshold=_int(scoring, "scoring", "file_size_threshold", _d.file_size_threshold),
        decay_rate=_float(frecency, "frecency", "decay_rate", _d.decay_rate),
        code_chunk_lines=_int(chunking, "chunking", "code_chunk_lines", _d.code_chunk_lines),
        pdf_chunk_chars=_int(chunking, "chunking", "pdf_chunk_chars", _d.pdf_chunk_chars),
        git_log_timeout=_int(timeouts, "timeouts", "git_log", _d.git_log_timeout),
        ripgrep_timeout=_int(timeouts, "timeouts", "ripgrep", _d.ripgrep_timeout),
    )


def get_pdf_extractor(repo_root: Path | None = None) -> str:
    """Return the configured PDF extractor backend.

    Reads ``pdf.extractor`` from the merged config.  Defaults to ``"auto"``.
    Valid values: ``"auto"``, ``"docling"``, ``"mineru"``.
    """
    cfg = load_config(repo_root=repo_root)
    value = cfg.get("pdf", {}).get("extractor", "auto")
    if value not in ("auto", "docling", "mineru"):
        _log.warning("invalid pdf.extractor config", value=value)
        return "auto"
    return value


def get_mineru_server_url(repo_root: Path | None = None) -> str:
    """Return the configured MinerU server URL (default http://127.0.0.1:8010)."""
    cfg = load_config(repo_root=repo_root)
    return cfg.get("pdf", {}).get("mineru_server_url", "http://127.0.0.1:8010")


def get_mineru_table_enable(repo_root: Path | None = None) -> bool:
    """Return whether MinerU table extraction is enabled (default False)."""
    cfg = load_config(repo_root=repo_root)
    return bool(cfg.get("pdf", {}).get("mineru_table_enable", False))


def get_mineru_page_batch(repo_root: Path | None = None) -> int:
    """Return the configured MinerU page batch size (default 1)."""
    cfg = load_config(repo_root=repo_root)
    return max(1, int(cfg.get("pdf", {}).get("mineru_page_batch", 1)))


def get_tuning_config(repo_root: Path | None = None) -> TuningConfig:
    """Return a TuningConfig loaded from the merged configuration.

    Reads ``load_config(repo_root)`` and extracts the ``[tuning]`` section.
    Missing keys fall back to TuningConfig defaults (identical to previous
    hard-coded values — no behavioral change for repos without ``[tuning]``).
    """
    cfg = load_config(repo_root=repo_root)
    return _tuning_from_dict(cfg.get("tuning", {}))


def get_verification_config(repo_root: Path | None = None) -> dict[str, Any]:
    """Return the merged verification config section.

    Does not perform auto-detection of ``test_command``; call
    :func:`detect_test_command` separately when ``test_command`` is empty.
    """
    cfg = load_config(repo_root=repo_root)
    defaults = _DEFAULTS["verification"]
    section = cfg.get("verification", {})
    return {**defaults, **section}


# Detection table shared with nx/hooks/scripts/read_verification_config.py.
# Keep both tables identical — a cross-validation test enforces this.
_DETECT_TABLE: list[tuple[str, str]] = [
    ("pom.xml",          "mvn test"),
    ("build.gradle",     "./gradlew test"),
    ("build.gradle.kts", "./gradlew test"),
    ("pyproject.toml",   "uv run pytest"),
    ("package.json",     "npm test"),
    ("Cargo.toml",       "cargo test"),
    ("Makefile",         "make test"),
    ("go.mod",           "go test ./..."),
]


def detect_test_command(repo_root: Path | None = None) -> str:
    """Auto-detect test command from project marker files.

    Detection order (first match wins):
      pom.xml            → "mvn test"
      build.gradle /
      build.gradle.kts   → "./gradlew test"
      pyproject.toml     → "uv run pytest"
      package.json       → "npm test"
      Cargo.toml         → "cargo test"
      Makefile           → "make test"
      go.mod             → "go test ./..."

    Returns "" if no marker file found.
    """
    base = Path(repo_root or Path.cwd())
    for marker, command in _DETECT_TABLE:
        if (base / marker).exists():
            return command
    return ""


# ── Credential registry ───────────────────────────────────────────────────────
# Maps config-file key → environment variable name
CREDENTIALS: dict[str, str] = {
    "chroma_api_key":    "CHROMA_API_KEY",
    "chroma_tenant":     "CHROMA_TENANT",
    "chroma_database":   "CHROMA_DATABASE",
    "voyage_api_key":    "VOYAGE_API_KEY",
    "migrated":          "NX_MIGRATED",
}


# ── Local mode helpers ───────────────────────────────────────────────────────


def _default_local_path() -> Path:
    """Return the default local ChromaDB PersistentClient path.

    Precedence:
      1. ``NX_LOCAL_CHROMA_PATH`` env var (explicit override)
      2. ``$XDG_DATA_HOME/nexus/chroma``
      3. ``~/.local/share/nexus/chroma``
    """
    override = os.environ.get("NX_LOCAL_CHROMA_PATH")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "nexus" / "chroma"
    return Path.home() / ".local" / "share" / "nexus" / "chroma"


def catalog_path() -> Path:
    """Return the catalog directory path.

    Priority: NEXUS_CATALOG_PATH env → default ~/.config/nexus/catalog/
    """
    env = os.environ.get("NEXUS_CATALOG_PATH", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".config" / "nexus" / "catalog"


def is_local_mode() -> bool:
    """Return True if nexus should use the local T3 backend.

    Decision logic:
      - ``NX_LOCAL=1`` → True  (explicit opt-in)
      - ``NX_LOCAL=0`` → False (explicit opt-out)
      - Otherwise: True when **either** CHROMA_API_KEY or VOYAGE_API_KEY is absent
    """
    nx_local = os.environ.get("NX_LOCAL", "").strip()
    if nx_local == "1":
        return True
    if nx_local == "0":
        return False
    # Auto-detect: local mode when either cloud credential is missing
    chroma_key = get_credential("chroma_api_key")
    voyage_key = get_credential("voyage_api_key")
    return not (chroma_key and voyage_key)

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
    "pdf": {
        "extractor": "auto",
        "mineru_server_url": "http://127.0.0.1:8010",
        "mineru_table_enable": False,
        "mineru_page_batch": 1,
    },
    "search": {
        "hybrid_default": False,
        "hnsw_ef": 256,
    },
    "voyageai": {
        "read_timeout_seconds": 120,
    },
    "verification": {
        "on_stop": False,
        "on_close": False,
        "test_command": "",
        "lint_command": "",
        "test_timeout": 120,
    },
    # Derived from TuningConfig() at module load — single source of truth.
    # Do not edit values here; change TuningConfig field defaults instead.
    "tuning": (lambda _tc: {
        "scoring": {
            "vector_weight": _tc.vector_weight,
            "frecency_weight": _tc.frecency_weight,
            "file_size_threshold": _tc.file_size_threshold,
        },
        "frecency": {
            "decay_rate": _tc.decay_rate,
        },
        "chunking": {
            "code_chunk_lines": _tc.code_chunk_lines,
            "pdf_chunk_chars": _tc.pdf_chunk_chars,
        },
        "timeouts": {
            "git_log": _tc.git_log_timeout,
            "ripgrep": _tc.ripgrep_timeout,
        },
    })(TuningConfig()),
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


def set_config_value(dotted_key: str, value: str) -> None:
    """Persist a dotted config key in ``~/.config/nexus/config.yml``.

    Example: ``set_config_value("pdf.extractor", "mineru")`` writes::

        pdf:
          extractor: mineru
    """
    path = _global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = dotted_key.split(".")
    with _config_lock:
        data: dict[str, Any] = {}
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
        # Build nested dict from dotted path
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        content = yaml.dump(data, default_flow_style=False)
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
                pass  # intentional: cleanup after re-raise
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
