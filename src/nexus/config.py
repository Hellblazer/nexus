# SPDX-License-Identifier: AGPL-3.0-or-later
import copy
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.stranded_install import StrandedInstall

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
    #: nexus-1qdb9: the PDF pipeline may spawn a local MinerU server on
    #: demand when it routes a document to MinerU and none is running.
    #: False = operator manages the server out-of-band (launchctl, remote).
    mineru_autostart: bool = True
    mineru_table_enable: bool = False
    mineru_page_batch: int = 1
    # RDR-148 Gap 6: hard RLIMIT_AS address-space ceiling (MB) applied to the
    # MinerU worker. 0 = disabled (rely on the OS OOM-killer / jetsam). Opt-in
    # because too low a value turns healthy pages into spurious OOMs. Enforced
    # only on Linux — macOS does not honour RLIMIT_AS (see get_mineru helpers).
    # NB: RLIMIT_AS caps VIRTUAL address space, not physical RSS; PyTorch/MinerU
    # mmap model weights aggressively, so the address-space footprint can be 3-5x
    # the resident size — set this generously (e.g. several GB) to avoid spurious
    # OOMs on healthy pages.
    mineru_memory_ceiling_mb: int = 0
    # RDR-148 Gap 6: per-page wall-clock budget (seconds) for the worker,
    # replacing the old fixed batch-level 180s. The effective subprocess timeout
    # is this value times the number of pages in the range.
    mineru_page_timeout_s: int = 180


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
        mineru_autostart=bool(pdf.get("mineru_autostart", True)),
        mineru_table_enable=bool(pdf.get("mineru_table_enable", False)),
        mineru_page_batch=max(1, int(pdf.get("mineru_page_batch", 1))),
        mineru_memory_ceiling_mb=max(0, int(pdf.get("mineru_memory_ceiling_mb", 0))),
        mineru_page_timeout_s=max(1, int(pdf.get("mineru_page_timeout_s", 180))),
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
        from nexus._mineru_pid import (  # noqa: PLC0415 — circular-dep avoidance (_mineru_pid)
            is_process_alive,
            read_pid_file,
        )
    except Exception:  # noqa: BLE001 — best-effort PID probe; any import/read failure degrades to None
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


_MINERU_DEFAULT_URL = "http://127.0.0.1:8010"


def get_mineru_server_url(repo_root: Path | None = None) -> str:
    """Return the URL of the MinerU server to talk to.

    Resolution order (RDR-148 Gap 1 — explicit operator intent wins):
    1. An explicit, non-default ``pdf.mineru_server_url`` — when the
       operator has set the config to anything other than the built-in
       default ``http://127.0.0.1:8010``, that intent wins outright.
       This covers out-of-band server management (e.g. a launchctl
       service or a remote host on a fixed URL); a live local pid file
       must not silently hijack it.
    2. Live PID file (``~/.config/nexus/mineru.pid``) — the canonical
       source of truth when ``nx mineru start`` brought a server up on
       an ephemeral port and the config was left at the default.
       Validated via ``_is_process_alive``.
    3. Built-in default ``http://127.0.0.1:8010``.

    Documented heuristic limitation: the ``!=`` default check cannot
    distinguish "operator deliberately fixed local :8010" from "config
    never changed", so an operator who pins :8010 is still overridden by
    a live pid file. Both target 127.0.0.1, so this is harmless; a
    ``mineru_prefer_config`` flag can be added later if a concrete need
    arises.
    """
    configured = get_pdf_config(repo_root).mineru_server_url
    if configured != _MINERU_DEFAULT_URL:
        return configured
    live = _read_live_mineru_port()
    if live is not None:
        return f"http://127.0.0.1:{live}"
    return configured


def mineru_server_provisioned(repo_root: Path | None = None) -> bool:
    """Return True when a MinerU server is actually provisioned.

    Provisioned means an explicit non-default ``pdf.mineru_server_url``
    (operator intent, RDR-148 Gap 1) OR a live pid-file server from
    ``nx mineru start``. False when ``get_mineru_server_url`` would fall
    through to the built-in default — i.e. nothing was ever set up.

    nexus-9xfx5: ``nx doctor`` uses this to render an unprovisioned
    MinerU as a not-configured skip instead of a red ✗ probing the
    built-in default URL on every fresh install.
    """
    if get_pdf_config(repo_root).mineru_server_url != _MINERU_DEFAULT_URL:
        return True
    return _read_live_mineru_port() is not None


def get_mineru_configured_fixed_port(repo_root: Path | None = None) -> int | None:
    """Return the port from an explicit, non-default local ``mineru_server_url``.

    ``get_mineru_server_url`` treats a non-default ``pdf.mineru_server_url``
    as binding operator intent (RDR-148 Gap 1) — but that precedence was
    read-only: ``nx mineru start``'s own port default (``--port 0`` ==
    auto-assign) ignored config entirely, so an operator with a fixed local
    port in config who ran a bare ``nx mineru start`` got a live server on a
    *different* random port than what the rest of the system (``nx doctor``,
    the PDF pipeline) was told to look for — a silently disconnected server
    that reported success (nexus incident 2026-07-01).

    This gives the start path the same read: a non-default, ``127.0.0.1``/
    ``localhost``-hosted URL with a parseable port is "the operator already
    told us where to bind" and should be honored as the auto-assign default,
    not overridden by a fresh random port. Returns ``None`` for the default
    URL, an unparseable URL, or a non-local host (e.g. a remote/launchctl
    URL an operator manages out-of-band — nothing to bind here).
    """
    import urllib.parse  # noqa: PLC0415 — deferred; only needed on this rare path

    configured = get_pdf_config(repo_root).mineru_server_url
    if configured == _MINERU_DEFAULT_URL:
        return None
    parsed = urllib.parse.urlparse(configured)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return None
    return parsed.port

def get_mineru_table_enable(repo_root: Path | None = None) -> bool:
    return get_pdf_config(repo_root).mineru_table_enable

def get_mineru_page_batch(repo_root: Path | None = None) -> int:
    return get_pdf_config(repo_root).mineru_page_batch

def get_mineru_memory_ceiling_mb(repo_root: Path | None = None) -> int:
    return get_pdf_config(repo_root).mineru_memory_ceiling_mb

def get_mineru_page_timeout_s(repo_root: Path | None = None) -> int:
    return get_pdf_config(repo_root).mineru_page_timeout_s


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


# Detection table shared with conexus/hooks/scripts/read_verification_config.py.
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
    # RDR-166 managed onboarding (nexus-v3p0x): the operator-provisioned managed
    # endpoint + bearer. `nx config set service_url/service_token` persists them
    # to config.yml; the service resolvers consume them via get_credential, so
    # the env var still wins and config.yml is the durable fallback — the single
    # consume point the conexus issuance contract targets.
    "service_url":       "NX_SERVICE_URL",
    "service_token":     "NX_SERVICE_TOKEN",
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


def fastembed_cache_dir() -> Path:
    """Return the stable on-disk cache dir for the Tier-1 (bge-768) fastembed model.

    RDR-144 P1 (CA-1): without an explicit ``cache_dir`` fastembed downloads
    to a volatile ``tempfile.gettempdir()/fastembed_cache`` that the OS wipes
    on reboot, re-downloading the 768-dim model on every cold start and
    breaking offline-after-first-run. The sole embedding-function
    construction chokepoint (``LocalEmbeddingFunction._init_ef``) reads this
    resolver so the launchd-spawned daemon/MCP processes — which never see
    the ``nx init`` shell env (CRITICAL-1) — still land on a stable dir.

    Precedence:
      1. ``local.fastembed_cache_path`` in ``~/.config/nexus/config.yml``
      2. ``$XDG_DATA_HOME/nexus/fastembed_cache``
      3. ``~/.local/share/nexus/fastembed_cache``

    ``FASTEMBED_CACHE_PATH`` env is intentionally NOT consulted: it does not
    reach launchd-spawned daemon/MCP processes (the CRITICAL-1 root cause),
    so this resolver — read at the EF-construction chokepoint — owns the
    address and always passes an explicit ``cache_dir`` to fastembed.

    Nothing is created here — the construction site materialises the dir.
    """
    path = _global_config_path()
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        configured = (data.get("local") or {}).get("fastembed_cache_path", "")
        if configured:
            # expanduser so a hand-edited ``~/models`` resolves to $HOME, not
            # a literal ``./~/models`` created relative to the daemon's cwd.
            return Path(configured).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "nexus" / "fastembed_cache"
    return Path.home() / ".local" / "share" / "nexus" / "fastembed_cache"


def local_embed_model_choice() -> str | None:
    """Return the local embedder the user selected via ``nx init`` (RDR-144).

    Reads ``local.embed_model`` from ``~/.config/nexus/config.yml`` — the key
    ``nx init`` (P2) persists. ``None`` when no choice has been recorded, in
    which case ``LocalEmbeddingFunction`` keeps its legacy
    fastembed-availability auto-select.
    """
    path = _global_config_path()
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        value = (data.get("local") or {}).get("embed_model", "")
        return value or None
    return None


def catalog_path() -> Path:
    """Return the catalog directory path.

    Priority: NEXUS_CATALOG_PATH env → NEXUS_CONFIG_DIR/catalog/
    → ~/.config/nexus/catalog/
    """
    env = os.environ.get("NEXUS_CATALOG_PATH", "").strip()
    if env:
        return Path(env)
    return nexus_config_dir() / "catalog"


def detect_stranded_install_default() -> "StrandedInstall | None":
    """Run the stranded-install detector (nexus-gynt2) against the real
    path roots: config dir, local Chroma dir, catalog dir.

    The single assembler every entry point (``nx init``, CLI startup, MCP
    startup, ``nx doctor``) calls, so the path-resolution knowledge stays
    here with the resolvers. Near-zero cost while the detector is
    disarmed (``stranded_install.LAST_MIGRATION_CAPABLE is None`` — every
    migration-capable release): the leaf short-circuits before touching
    the filesystem.
    """
    from nexus.stranded_install import detect_stranded_install  # noqa: PLC0415 — leaf module, deferred to keep config import-light

    return detect_stranded_install(
        nexus_config_dir(), _default_local_path(), catalog_path()
    )


def is_local_mode() -> bool:
    """Return True if nexus should use the local T3 backend.

    Decision logic (precedence, highest first):
      - ``NX_LOCAL=1`` → True  (explicit opt-in)
      - ``NX_LOCAL=0`` → False (explicit opt-out)
      - ``service_url`` present (``NX_SERVICE_URL`` env or config.yml) → False —
        a managed 6.0 user serves every tier from a remote service and is NOT
        local (nexus-3k43p: the legacy heuristic below mis-detected a greenfield
        managed user — service_url set, no chroma/voyage key — as local). This
        mirrors ``_resolve_init_mode``'s precedence (NX_LOCAL wins over
        service_url, which wins over the rest). Wins over ``pg_credentials``
        too: a migrated local→managed install keeps the old file on disk.
      - ``pg_credentials`` present in the config dir → True — the EXPLICIT
        positive record that a local service was provisioned (``nx init``
        local mode; the same signal health.py gates its service checks on).
        RDR-188 P3.1 (nexus-9o6y2.13): mode comes from explicit install
        state, not key inference.
      - Otherwise (legacy, pre-service Chroma era): True when CHROMA_API_KEY
        is absent. The voyage clause is DELETED (RDR-188 Gap 3): the client
        no longer consumes the voyage key for anything, so its presence or
        absence must have ZERO mode influence — a chroma-key-without-voyage
        install is a half-configured CLOUD install whose missing key should
        surface loudly, never a silent flip to local.
    """
    nx_local = os.environ.get("NX_LOCAL", "").strip()
    if nx_local == "1":
        return True
    if nx_local == "0":
        return False
    if (get_credential("service_url") or "").strip():
        return False
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — leaf constant, deferred to keep config import-light

    if (nexus_config_dir() / CREDENTIALS_FILENAME).is_file():
        # AMBIGUOUS CORNER (reviewer Critical, T2 [21057]): a chroma key next
        # to pg_credentials is indistinguishable between (a) a migrated
        # local-service install retaining its legacy keys as the immutable
        # migration source (the LIVE population — must resolve local; the
        # deprecation window keeps those keys on disk until RDR-155 P4b) and
        # (b) a former-local install hand-reconfigured to the deprecated
        # direct-Chroma cloud posture with the stale file left behind.
        # pg_credentials wins — (a) is current and common, (b) is a legacy
        # corner with explicit escape hatches — but the resolution is
        # surfaced loudly, never silent (tracked for an explicit mode
        # record: bead nexus-x3ugg).
        if get_credential("chroma_api_key"):
            _warn_ambiguous_mode_once()
        return True
    # Auto-detect (legacy): a Chroma-Cloud key marks a cloud install.
    return not get_credential("chroma_api_key")


_ambiguous_mode_warned: bool = False


def _warn_ambiguous_mode_once() -> None:
    """One-shot per process: pg_credentials + chroma key with no service_url
    resolved LOCAL; name the overrides so a genuinely-cloud user can escape."""
    global _ambiguous_mode_warned
    if _ambiguous_mode_warned:
        return
    _ambiguous_mode_warned = True
    import structlog  # noqa: PLC0415 — deferred; config must stay import-light

    structlog.get_logger(__name__).warning(
        "mode_ambiguous_resolved_local",
        reason="pg_credentials (local service provisioned) and chroma_api_key "
               "(legacy cloud) are both present with no service_url",
        resolution="local (the provisioned service wins; legacy keys are "
                   "treated as migration-source material)",
        override="set NX_LOCAL=0 or configure a managed service_url if this "
                 "install is genuinely cloud",
    )


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
    # RDR-182: Claude-assisted upgrade forensics / remediation. DEFAULT-OFF —
    # the MCP surface is autonomously agent-invocable, so the durable opt-in
    # is enforced at the tool boundary itself (the tool refuses before
    # emitting content when this is false). Enable:
    #   nx config set claude_assisted_remediation.enabled true
    # NOTE: that write path stores the STRING "true"; consumers must parse
    # strictly (see nexus.mcp.core._remediation_opt_in), never truthiness.
    # CONSENT-PROVENANCE EXCEPTION (critic-p3 Critical, 2026-07-12): unlike
    # every other flag, the gate does NOT honor this key from the merged
    # load_config() view — a repo-local .nexus.yml (which arrives via git
    # pull) is not a human consent gesture. _remediation_opt_in reads the
    # GLOBAL config.yml only; this default exists for documentation and
    # `nx config list` visibility.
    "claude_assisted_remediation": {
        "enabled": False,
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

    A non-dict value at an intermediate key (e.g. a hand-written flat
    ``claude_assisted_remediation: true`` when setting
    ``claude_assisted_remediation.enabled``) is REPLACED by the nested form —
    the dotted command expresses explicit intent for the section shape, and the
    RDR-182 refusal text names this command as the remedy for exactly that flat
    shape (nexus-s4a98). The replacement is logged with the discarded value.
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
        for i, part in enumerate(parts[:-1]):
            existing = node.get(part)
            if not isinstance(existing, dict):
                if existing is not None:
                    _log.warning(
                        "config_scalar_section_replaced",
                        key=".".join(parts[: i + 1]),
                        discarded_value=existing,
                        dotted_key=dotted_key,
                    )
                existing = {}
                node[part] = existing
            node = existing
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


def unset_credential(name: str) -> bool:
    """Remove credential *name* from ``~/.config/nexus/config.yml``.

    The teardown counterpart of :func:`set_credential` (RDR-165 nexus-a11ge —
    the managed-config clear in ``nx uninstall``). Returns ``True`` when the key
    was present and removed, ``False`` when it was already absent (idempotent —
    a teardown must not error on an already-clean config). Raises ``ValueError``
    for an unknown credential name, mirroring :func:`set_credential`.

    NOTE: this clears only the persisted ``config.yml`` value. An environment
    variable (e.g. ``NX_SERVICE_TOKEN``) overrides config.yml in
    :func:`get_credential` and CANNOT be unset from the parent shell here — the
    caller is responsible for warning the user to unset the export.
    """
    if name not in CREDENTIALS:
        known = ", ".join(sorted(CREDENTIALS))
        raise ValueError(f"Unknown credential '{name}'. Known: {known}")
    path = _global_config_path()
    if not path.exists():
        return False
    with _config_lock:
        data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        creds = data.get("credentials")
        if not isinstance(creds, dict) or name not in creds:
            return False
        del creds[name]
        if not creds:
            data.pop("credentials", None)
        content = yaml.dump(data, default_flow_style=False)
        # Atomic write: unique temp file → os.replace() (0o600), mirroring
        # set_credential so a torn write never leaves a half-cleared config.
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
    return True


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
