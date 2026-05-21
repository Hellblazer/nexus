# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
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


@dataclass(frozen=True)
class PDFConfig:
    """PDF extraction settings from ``[pdf]`` config section."""

    extractor: str = "auto"
    mineru_server_url: str = "http://127.0.0.1:8010"
    mineru_table_enable: bool = False
    mineru_page_batch: int = 1


def get_pdf_config(repo_root: Path | None = None) -> PDFConfig:
    """Load PDF config. Invalid ``extractor`` falls back to ``"auto"``."""
    pdf = load_config(repo_root=repo_root).get("pdf", {})
    extractor = pdf.get("extractor", "auto")
    if extractor not in ("auto", "docling", "mineru"):
        _log.warning("invalid pdf.extractor config", value=extractor)
        extractor = "auto"
    return PDFConfig(
        extractor=extractor,
        mineru_server_url=pdf.get("mineru_server_url", "http://127.0.0.1:8010"),
        mineru_table_enable=bool(pdf.get("mineru_table_enable", False)),
        mineru_page_batch=max(1, int(pdf.get("mineru_page_batch", 1))),
    )


@dataclass(frozen=True)
class TelemetryConfig:
    """Opt-outs for the RDR-087 search observability surfaces.

    - ``search_enabled``: Phase 2.2 hot-path ``INSERT OR IGNORE`` into
      ``search_telemetry``. When False, ``search_cross_corpus`` skips
      the write even when a telemetry store is injected.
    - ``stderr_silent_zero``: Phase 1.2 silent-zero stderr note. When
      False, ``nx search`` never emits the "candidates dropped..."
      diagnostic.

    Both default ``True`` — feature-on. Opt-out is project-scoped via
    ``.nexus.yml#telemetry``.
    """

    search_enabled: bool = True
    stderr_silent_zero: bool = True


def _coerce_bool(value: Any, *, key: str, default: bool) -> bool:
    """Coerce ``value`` to bool, warn + fall back to ``default`` on malformed input."""
    if isinstance(value, bool):
        return value
    _log.warning(
        "telemetry_config_malformed",
        key=key,
        value=value,
        fell_back_to=default,
    )
    return default


def get_telemetry_config(
    repo_root: Path | None = None,
    *,
    cfg: dict | None = None,
) -> TelemetryConfig:
    """Load the ``telemetry`` config section into a typed struct.

    Malformed boolean values coerce to the default with a structured
    warning so a stray string in ``.nexus.yml`` never silently disables
    the feature (or silently enables it).

    Pass *cfg* to reuse an already-loaded config dict and skip the disk
    read — required on the search hot path.
    """
    tel = (cfg if cfg is not None else load_config(repo_root=repo_root)).get("telemetry", {})
    return TelemetryConfig(
        search_enabled=_coerce_bool(
            tel.get("search_enabled", True),
            key="telemetry.search_enabled",
            default=True,
        ),
        stderr_silent_zero=_coerce_bool(
            tel.get("stderr_silent_zero", True),
            key="telemetry.stderr_silent_zero",
            default=True,
        ),
    )


# Backward-compatible accessors — thin wrappers for existing callers.
def get_pdf_extractor(repo_root: Path | None = None) -> str:
    return get_pdf_config(repo_root).extractor

def _read_live_mineru_port() -> int | None:
    """Return the port of the currently-alive MinerU server, or None.

    Source of truth is the PID file written by ``nx mineru start`` /
    ``_restart_mineru_server`` at ``~/.config/nexus/mineru.pid``. The
    file persists across the spawning process tree's lifetime but is
    cleaned up by ``nx mineru stop``; ``_is_process_alive`` guards
    against stale rows when the server crashes without cleanup.

    nexus-oa7r: previously the live port was written to
    ``~/.config/nexus/config.yml``'s ``pdf.mineru_server_url``. That
    persistent record drifted across reboots: when the server died,
    the config still pointed at the dead port, and every subsequent
    session silently fell through to the OOM-prone in-process
    subprocess. PID file is the canonical source — it's correct by
    construction (only present when the server is up).
    """
    # nexus-8g79.10 (V4): import from the lower-layer module instead of
    # reaching up into commands/. The CLI module re-exports under the
    # legacy private names.
    try:
        from nexus._mineru_pid import (  # noqa: PLC0415
            is_process_alive,
            read_pid_file,
        )
    except Exception:
        return None
    info = read_pid_file()
    if not info:
        return None
    pid = info.get("pid")
    port = info.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return None
    if not is_process_alive(pid):
        return None
    return port


def get_mineru_server_url(repo_root: Path | None = None) -> str:
    """Return the URL of the live MinerU server, falling back to the
    configured default when no live server is found.

    Resolution order:
    1. Live PID file (``~/.config/nexus/mineru.pid``) — the canonical
       source of truth when a server is running. Validated via
       ``_is_process_alive``.
    2. Configured ``pdf.mineru_server_url`` — the static fallback for
       installs where the operator manages the server out-of-band
       (e.g. a launchctl service on a fixed port).
    3. Built-in default ``http://127.0.0.1:8010``.
    """
    live = _read_live_mineru_port()
    if live is not None:
        return f"http://127.0.0.1:{live}"
    return get_pdf_config(repo_root).mineru_server_url

def get_mineru_table_enable(repo_root: Path | None = None) -> bool:
    return get_pdf_config(repo_root).mineru_table_enable

def get_mineru_page_batch(repo_root: Path | None = None) -> int:
    return get_pdf_config(repo_root).mineru_page_batch


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


# ── Config directory helper ──────────────────────────────────────────────────


def nexus_config_dir() -> Path:
    """Return the Nexus config / data directory, respecting ``NEXUS_CONFIG_DIR``.

    Single source of truth for every path under ``.config/nexus/`` so sandbox
    runs, tests, and multi-profile installs can redirect the entire T2 +
    catalog + session + log footprint with one environment variable.

    Precedence:
      1. ``NEXUS_CONFIG_DIR`` env var (explicit override)
      2. ``~/.config/nexus`` (default)

    Nothing is created here — callers either read or ``mkdir(parents=True,
    exist_ok=True)`` as needed.
    """
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


def default_db_path() -> Path:
    """Return the default path to the T2 SQLite database.

    nexus-8g79.10: promoted from ``commands/_helpers.py`` so non-CLI
    modules (``mcp_infra``, ``health``, ``collection_health``,
    ``collection_audit``, ``context``, ``operators/aspect_sql``,
    ``merge_candidates``, ``console/routes/health``) can resolve the
    canonical T2 path without reaching up to the CLI presentation
    layer. The original location remains as a re-export for
    backwards compatibility with CLI command modules.

    Respects ``NEXUS_CONFIG_DIR`` via :func:`nexus_config_dir` so
    sandbox / test / multi-profile runs can redirect T2 writes away
    from the user's production ``memory.db``.
    """
    return nexus_config_dir() / "memory.db"


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

    Priority: NEXUS_CATALOG_PATH env → NEXUS_CONFIG_DIR/catalog/
    → ~/.config/nexus/catalog/
    """
    env = os.environ.get("NEXUS_CATALOG_PATH", "").strip()
    if env:
        return Path(env)
    return nexus_config_dir() / "catalog"


#: RDR-120 P0.B — accepted values for ``NX_STORAGE_MODE``. ``direct``
#: is the only mode wired today; ``daemon`` is reserved for the P3
#: cutover and is rejected at P0 with an explicit not-yet-supported
#: error so operators do not silently land in a half-built state.
VALID_STORAGE_MODES: tuple[str, ...] = ("direct", "daemon")


class StorageModeError(click.ClickException):
    """Raised when ``NX_STORAGE_MODE`` is set to an unsupported value.

    Subclasses ``click.ClickException`` so a CLI invocation surfaces
    the message cleanly. Constructor signature matches the parent so
    downstream code can catch it as either.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


def storage_mode() -> str:
    """Return the validated ``NX_STORAGE_MODE`` value.

    RDR-120 P0.B scaffolding. Single source of truth for the storage
    backend mode flag. Currently:

    - unset / empty / whitespace -> ``"direct"`` (default)
    - ``"direct"`` (any case) -> ``"direct"``
    - ``"daemon"`` (any case) -> raises ``StorageModeError`` with
      "not yet supported at phase 0"
    - anything else -> raises ``StorageModeError`` naming the bad
      value and listing :data:`VALID_STORAGE_MODES`

    The function exists at P0 to lock the env-var name and the
    validation contract before any client wires the mode to actual
    behavior. P3 / P4 will switch the daemon branch from rejection
    to a daemon-client construction.
    """
    raw = os.environ.get("NX_STORAGE_MODE", "")
    normalized = raw.strip().lower()
    if not normalized:
        return "direct"
    if normalized == "direct":
        return "direct"
    if normalized == "daemon":
        raise StorageModeError(
            "NX_STORAGE_MODE=daemon is not yet supported at phase 0 of "
            "RDR-120 (storage substrate split). Set NX_STORAGE_MODE=direct "
            "or unset the variable to use the current library-mode T2/T3 "
            "backend. Daemon mode lands in P3."
        )
    raise StorageModeError(
        f"NX_STORAGE_MODE={raw!r} is not a recognized value. "
        f"Valid values: {', '.join(VALID_STORAGE_MODES)}."
    )


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


# RDR-101 Phase 5c (nexus-o6aa.13) removed ``is_catalog_event_sourced``.
# The Phase 5a/5b flag was a transitional gate for the deprecated chunk-
# metadata fields; with the schema now enforcing the drop unconditionally
# (corpus / store_type / git_meta gone from ALLOWED_TOP_LEVEL), no flag
# is needed. ``NEXUS_CATALOG_EVENT_SOURCED`` env var no longer consulted.


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
    "taxonomy": {
        # Glob patterns for collections to exclude from auto-discover
        # in LOCAL MODE ONLY. Local mode uses MiniLM which clusters
        # poorly on source code. Cloud mode uses voyage-code-3 and
        # is unaffected. Set to [] to enable taxonomy for all collections.
        "local_exclude_collections": ["code__*"],
        # Auto-label topics with Claude haiku after discover.
        # Requires `claude` CLI on PATH. Set False to keep c-TF-IDF labels.
        "auto_label": True,
        # RDR-085: project vocabulary for glossary-aware labeling.
        # When set, each term expansion is prepended to the labeler
        # prompt so Claude resolves project acronyms correctly (e.g.
        # SSMF → SelfSimilarMaskingField, not "Single Mode Fiber").
        # Empty dict disables — labeler behaves as pre-RDR-085.
        "glossary": {},
    },
    "plans": {
        # RDR-084: Auto-save successful ad-hoc plans for this many days.
        # Set 0 to disable grown-plan persistence entirely (library stays
        # at the seed-template set).
        "ad_hoc_ttl": 30,
    },
    "search": {
        "hybrid_default": False,
        "hnsw_ef": 256,
        # Post-RDR-059 recalibrated thresholds (Voyage embedding fix).
        # code=0.45 is intentionally inert (all code results <0.43) — guards
        # future model changes.  knowledge/docs/rdr=0.65 per RF-21 empirical
        # measurement: relevant cluster ends ~0.59, noise starts ~0.67.
        "distance_threshold": {
            "code": 0.45,
            "knowledge": 0.65,
            "docs": 0.65,
            "rdr": 0.65,
            "default": 0.55,
        },
        "cluster_by": None,
        "contradiction_check": True,
        "query_sanitizer": True,
    },
    "voyageai": {
        "read_timeout_seconds": 120,
    },
    # RDR-109 Phase 5: salience-boost feature flag.
    # Phase 4b measurements (2026-05-11) saw the boost ship Pareto-clean
    # on code + docs (+1/+2 hits at w=0.025) but regress 2 baseline-hits
    # on the knowledge corpus, so default-on is rejected per the bead
    # acceptance criterion. The mechanism ships; the default does not.
    # Operators opt in via ``.nexus.yml``:
    #   attention_guided_v1:
    #     enabled: true
    #     weight: 0.025
    "attention_guided_v1": {
        "enabled": False,
        "weight": 0.025,
    },
    # RDR-087: search-observability opt-outs. Default-on.
    "telemetry": {
        "search_enabled": True,       # Phase 2.2 hot-path INSERT OR IGNORE.
        "stderr_silent_zero": True,   # Phase 1.2 silent-zero stderr note.
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
    return nexus_config_dir() / "config.yml"


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
    global_path = nexus_config_dir() / "config.yml"
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
