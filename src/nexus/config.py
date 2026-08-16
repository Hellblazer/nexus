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

    # nexus-yq3vk: IDENTITY, not just existence. The pid file is shared by every
    # process reading this config dir, and until now the only check was "is that
    # pid alive". A develop checkout pinning mineru 3.1.11 therefore routed
    # extraction to a server an installed tool had started under 3.4.4 — so
    # uv.lock did not control the code that produced the text, and the same PDF
    # yielded different output depending on which server happened to be
    # registered. Different extraction is a data-correctness difference, not a
    # performance one, so a KNOWN mismatch refuses rather than degrading.
    recorded = info.get("mineru_version")
    if recorded is not None:
        try:
            from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — deferred

            ours = _pkg_version("mineru")
        except Exception:  # noqa: BLE001 — cannot compare: fall through rather than block
            ours = None
        if ours is not None and ours != recorded:
            _log.warning(
                "mineru_server_version_skew",
                registered_version=recorded,
                our_version=ours,
                registered_python=info.get("python"),
                pid=pid,
                port=port,
                remedy="nx mineru restart  (or set pdf.mineru_server_url explicitly)",
            )
            return None
    # A pid file without the field predates nexus-yq3vk. Say so once rather than
    # silently trusting it — absent identity is not matching identity.
    elif info.get("python") is None:
        _log.debug(
            "mineru_server_identity_unrecorded",
            pid=pid, port=port,
            detail="pid file predates version stamping; cannot verify the "
                   "server runs the mineru this environment pins",
        )
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
    "voyage_api_key":    "VOYAGE_API_KEY",
    "migrated":          "NX_MIGRATED",
    # RDR-166 managed onboarding (nexus-v3p0x): the operator-provisioned managed
    # endpoint + bearer. `nx config set service_url/service_token` persists them
    # to config.yml; the service resolvers consume them via get_credential, so
    # the env var still wins and config.yml is the durable fallback — the single
    # consume point the conexus issuance contract targets.
    "service_url":       "NX_SERVICE_URL",
    "service_token":     "NX_SERVICE_TOKEN",
    # RDR-005 2a self-minting (nexus-wrwb7): a scope=mint or scope=mint-locked
    # credential. When configured, nexus.db.data_token.DataTokenManager
    # self-mints short-TTL data tokens (POST /v1/data-tokens/mint) instead of
    # presenting the static service_token on engine calls. Unconfigured =
    # zero behavior change (the manager is inert; see the module docstring).
    "mint_token":        "NX_MINT_TOKEN",
    # nexus-ssqk9 (RDR-005 2a follow-up): the TENANT stamped in the mint
    # request body, overriding the caller-passed tenant (which every
    # Http*Store defaults to DEFAULT_TENANT="default"). A scope=mint-locked
    # credential is bound to its OWN tenant server-side (DataTokenHandler
    # 403s the mint the moment the body tenant differs) -- a real deployed
    # credential is routinely bound to something other than the client's
    # "default" convention (e.g. "nexus"), so mint_token and mint_tenant
    # travel as a PAIR: set mint_tenant to the credential's actual bound
    # tenant whenever it is not literally "default". Resolved via the same
    # env-wins-over-config.yml machinery as every other CREDENTIALS entry,
    # but it is NOT a secret -- see NON_SECRET_CREDENTIALS below, which
    # keeps `nx config get/list` from masking it.
    "mint_tenant":       "NX_MINT_TENANT",
}

#: Entries in CREDENTIALS that are not actually secrets -- a tenant slug, a
#: URL -- and so must display UNMASKED from `nx config get`/`nx config list`
#: (nexus-ssqk9). Deliberately narrow: only ``mint_tenant`` opts in today;
#: `service_url` stays masked by its pre-existing behavior, unchanged here.
NON_SECRET_CREDENTIALS: frozenset[str] = frozenset({"mint_tenant"})


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


#: nexus-4922x: the historical ladder rung name that recorded the pre-PG
#: data migration at the pinned last-migration-capable release
#: (``stranded_install.LAST_MIGRATION_CAPABLE``, currently ``6.18.1``).
#: ``RUNG_SUBSTRATE_ETL`` was deleted from ``upgrade_ladder.registry`` at
#: RDR-155 P4b (the rung no longer WALKS on the current release), but the
#: completion FACT it recorded is engine-side (``nexus.ladder_completions``
#: via ``HttpLadderStore``, RDR-186 .12) and OUTLIVES the client package
#: version: the SAME local PG database serves both the pin and the current
#: release across a package-only up/downgrade, so a verified row survives
#: hop 3. Hardcoded (not imported) because the registry constant naming it
#: is gone — this string is frozen historical fact, not a live symbol.
_MIGRATION_RUNG_NAME = "substrate-etl"

#: nexus-4922x code-review [22008] Important: this probe runs at CLI
#: STARTUP for every box that ever carried pre-PG artifacts (bounded
#: population, but every invocation of theirs). ``HttpLadderStore``'s
#: default (``RefreshableHttpStoreMixin._DEFAULT_TIMEOUT_S`` = 30.0s, with
#: up to ~2x that on a self-heal retry) is sized for a foreground data
#: operation, not a best-effort startup diagnostic — a resolvable-but-
#: unresponsive engine would hang CLI startup for up to ~60s. Every
#: comparable best-effort diagnostic probe in ``health.py`` uses a short
#: explicit timeout (2.0-5.0s); 3.0s matches that convention.
_LADDER_PROBE_TIMEOUT_S = 3.0


def _ladder_migration_verified() -> bool | None:
    """nexus-4922x: query the engine-side upgrade-ladder for a verified
    record of the pre-PG data migration — the signal the CURRENT two-hop
    remedy (``nx upgrade`` at the pin) actually produces, replacing the
    legacy ``<config>/migration-reports/*.json`` format that neither ``nx
    upgrade`` (the ladder) nor the hidden ``nx guided-upgrade`` writes any
    more (see ``stranded_install._has_verified_migration_report``'s
    docstring for the full trace — only the separate, unadvertised ``nx
    storage migrate*`` family writes that format).

    Returns ``True`` when the rung is present and verified: the caller
    de-strands. Returns ``None`` on ANY failure to reach or query the
    engine — unresolvable endpoint, connection error, HTTP error, timeout,
    or any other exception — and on the ordinary "rung not recorded" case.
    Deliberate fail-CLOSED degradation (no silent fallbacks for
    correctness decisions): an unreachable engine is the EXPECTED state at
    ``nx init`` time on a genuinely stranded box (nothing has been
    provisioned yet), so treating "can't tell" as "verified" would
    silently de-strand a box that was never migrated at all. The caller
    (:func:`nexus.stranded_install.detect_stranded_install`) treats
    ``None`` the same as ``False`` — stay stranded — and this function
    logs a structured warning so the degradation is LOUD, not silent, even
    though it never raises: a raise here would crash the UNWRAPPED ``nx
    init`` call site (deliberately not try/except'd — see
    ``commands/init.py``), which must abort loud only on a genuine
    detector bug, never on the ordinary "engine isn't up yet" case.

    ``timeout=_LADDER_PROBE_TIMEOUT_S`` (nexus-4922x review [22008]): a
    SHORT explicit timeout — ``httpx.Client(timeout=<float>)`` applies a
    scalar uniformly to connect/read/write/pool, so this bounds BOTH the
    connect and the read/response phases, not just one. Without it,
    ``RefreshableHttpStoreMixin``'s 30.0s default (sized for a foreground
    data operation) would let a resolvable-but-unresponsive engine hang
    this best-effort startup diagnostic for up to ~60s with the mixin's
    self-heal retry — every comparable probe in ``health.py`` uses 2-5s.
    """
    try:
        from nexus.upgrade_ladder.http_store import HttpLadderStore  # noqa: PLC0415 — deferred to avoid import cost on cold CLI start

        with HttpLadderStore(timeout=_LADDER_PROBE_TIMEOUT_S) as store:
            verified = store.verified_rungs()
    except Exception as exc:  # noqa: BLE001 — engine-down/unresolvable is the EXPECTED case at nx init time; must degrade, never raise or crash the caller
        _log.warning("stranded_install_ladder_check_unreachable", error=str(exc))
        return None
    return _MIGRATION_RUNG_NAME in verified


def detect_stranded_install_default() -> "StrandedInstall | None":
    """Run the stranded-install detector (nexus-gynt2) against the real
    path roots: config dir, local Chroma dir, catalog dir.

    The single assembler every entry point (``nx init``, CLI startup, MCP
    startup, ``nx doctor``) calls, so the path-resolution knowledge stays
    here with the resolvers. Near-zero cost while the detector is
    disarmed (``stranded_install.LAST_MIGRATION_CAPABLE is None``): the
    leaf short-circuits before touching the filesystem, and
    ``_ladder_migration_verified`` (network) is never even referenced.

    SCOPE CONSISTENCY (nexus-rjod2, found at the 7.0.0 gate where it
    reddened 60 tests + two E2E legs): every probed root must resolve
    from the SAME scope. When ``NEXUS_CONFIG_DIR`` is overridden (sandbox
    / test / multi-profile) and ``NX_LOCAL_CHROMA_PATH`` is not, the
    legacy-chroma probe must NOT fall back to the user-global default —
    that mixes roots: artifacts found under the real HOME while the
    migration-report suppression is consulted under the override, so a
    healthy box's sandbox sees a phantom stranded banner. Under an
    override the probe anchors at ``<override>/chroma``; a sandbox that
    wants to exercise detection seeds that path or sets
    ``NX_LOCAL_CHROMA_PATH`` explicitly.

    PRIMARY de-strand signal (nexus-4922x), GATED to ``is_local_mode()``
    (nexus-cmtpa, critique [22009] Critical): ``_ladder_migration_verified``
    is wired in as ``detect_stranded_install``'s ``ladder_migration_verified``
    probe ONLY in local mode. It only ever runs when pre-PG artifacts are
    actually present on disk (``detect_stranded_install`` short-circuits
    before it otherwise), so the added network round-trip is bounded to the
    rare once-stranded LOCAL population, not every CLI invocation on a
    healthy box.

    WHY THE GATE (empirically investigated, not assumed): the engine-side
    completion row is keyed ``(tenant_id, rung_name)`` ONLY — see
    ``service/src/main/resources/db/changelog/ladder-001-baseline.xml``'s
    ``ladder_completions`` table — with NO machine/host/config-dir identity.
    In LOCAL mode there is exactly one bundled PG per install, so
    "verified for this tenant" and "verified for this machine" coincide by
    construction (the module docstring's "same local PG serves both pin
    and current release" claim). In MANAGED/cloud mode, the SAME tenant can
    be shared across multiple machines pointed at the SAME remote engine —
    machine A migrating would flip the tenant-wide row, and an ungated
    check would falsely de-strand machine B's own, distinct, genuinely
    unmigrated local pre-PG files. This is NOT a hypothetical the fix can
    paper over: it is exactly the silent-data-loss class the detector
    exists to prevent, just shifted from "no signal" to "the WRONG signal".

    NOT A DEAD END for cloud users, verified against the v6.18.1 pin's own
    ``upgrade_ladder/rungs/substrate_etl.py``: ``SubstrateEtlRung.detect()``
    classifies the LOCAL Chroma footprint via ``self._plan()`` — it does
    NOT trust the tenant-wide completion row to decide "nothing to do".
    ``LadderRunner._run_rung`` only short-circuits to ALREADY_RECORDED when
    BOTH ``detect()`` says locally-converged AND the store has the rung —
    machine B's own unmigrated files make ``detect()`` return
    ``converged=False`` regardless of A's completion row, so ``nx upgrade``
    at the pin on machine B still genuinely re-migrates B's own data (an
    idempotent re-upsert of the same tenant-wide row, harmless). The
    two-hop remedy itself is sound in cloud mode; only THIS detector's
    de-strand SIGNAL was unsafe to trust there.

    SCOPE OF THIS FIX, STATED HONESTLY: gating to ``is_local_mode()`` means
    cloud/managed-mode installs fall back to EXACTLY the pre-nexus-4922x
    behavior (the legacy ``_has_verified_migration_report`` file check
    only) — i.e. the ORIGINAL nexus-4922x infinite-hop-3-loop bug remains
    UNFIXED for cloud-mode users. nexus-4922x's own empirical proof (the
    ``--stranded`` rehearsal) only ever exercised LOCAL mode (bundled PG at
    the pin), so this gate aligns the fix's claimed scope with what was
    actually tested rather than overclaiming a cloud-mode fix that was
    never verified.

    UPDATE (nexus-cmtpa, Hal decision 2026-08-09): the cloud-mode signal
    ships in THIS function, not deferred — ``check_local_ack`` (below)
    replaces the legacy-report-only fallback with an explicit consented
    marker.

    CLOUD-MODE primary signal (nexus-cmtpa): ``check_local_ack=True``
    when NOT local mode — trusts a matching ``nx stranded ack`` consent
    marker (:func:`nexus.stranded_install.write_ack_marker` /
    :func:`nexus.stranded_install._has_matching_ack`), a LOCAL,
    machine-scoped, EXPLICITLY CONSENTED signal independent of the
    tenant-shared engine. Mutually exclusive with the ladder probe by
    construction here (exactly one of ``ladder_probe`` /
    ``check_local_ack`` is active, keyed on the same ``is_local_mode()``
    read) — local mode keeps the stronger, engine-VERIFIED ladder signal
    undiluted by a weaker self-attested one; cloud mode gets the
    consent-marker escape instead of being stranded forever.
    """
    config_dir, chroma_dir, catalog_dir = _resolve_stranded_paths()
    # nexus-cmtpa: the tenant-scoped ladder signal is sound ONLY when this
    # install owns its own PG (local mode). Cloud/managed mode gets the
    # consented local ack-marker signal instead -- see the docstring above.
    local = is_local_mode()
    ladder_probe = _ladder_migration_verified if local else None
    from nexus.stranded_install import detect_stranded_install  # noqa: PLC0415 — leaf module, deferred to keep config import-light

    return detect_stranded_install(
        config_dir, chroma_dir, catalog_dir,
        ladder_migration_verified=ladder_probe,
        check_local_ack=not local,
    )


def _resolve_stranded_paths() -> tuple[Path, Path, Path]:
    """The three path roots the stranded-install detector probes: config
    dir, local Chroma dir, catalog dir.

    Factored out of :func:`detect_stranded_install_default` (nexus-cmtpa)
    so ``nx stranded ack`` (:mod:`nexus.commands.stranded_cmd`) resolves
    the IDENTICAL roots detection uses -- the same nexus-rjod2 scope-
    consistency contract applies to both: an ack computed against a
    different scope than detection probes would fingerprint the wrong
    files entirely. See :func:`detect_stranded_install_default`'s SCOPE
    CONSISTENCY note for the override precedence this mirrors exactly.
    """
    import os  # noqa: PLC0415 — stdlib, branch-local

    from nexus.stranded_install import legacy_chroma_dir  # noqa: PLC0415 — leaf module, deferred to keep config import-light

    config_dir = nexus_config_dir()
    if os.environ.get("NX_LOCAL_CHROMA_PATH") or not os.environ.get("NEXUS_CONFIG_DIR"):
        chroma_dir = legacy_chroma_dir()
    else:
        chroma_dir = config_dir / "chroma"
    return config_dir, chroma_dir, catalog_path()


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
      - Otherwise → True. RDR-155 P4b: the CHROMA_API_KEY inference died
        with the chroma credential map — there is no direct-Chroma cloud
        posture left to infer; cloud is service_url, everything else is
        local.
    """
    nx_local = os.environ.get("NX_LOCAL", "").strip()
    if nx_local == "1":
        return True
    if nx_local == "0":
        return False
    # nexus-x3ugg: the EXPLICIT mode record (config.yml ``install.mode``,
    # stamped by ``nx init`` / managed onboarding at resolution time). Record
    # beats artifact inference; a configured service_url beats a stale
    # ``local`` record (the endpoint is the operationally safer read,
    # nexus-3k43p posture) — but that contradiction is surfaced LOUDLY.
    recorded = str(
        load_config().get("install", {}).get("mode", "") or ""
    ).strip().lower()
    service_url_set = bool((get_credential("service_url") or "").strip())
    if service_url_set:
        if recorded == "local":
            _warn_mode_record_contradiction_once()
        return False
    if recorded == "local":
        return True
    if recorded == "managed":
        return False
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — leaf constant, deferred to keep config import-light

    if (nexus_config_dir() / CREDENTIALS_FILENAME).is_file():
        return True
    # RDR-155 P4b: the legacy CHROMA_API_KEY inference is gone — with no
    # record, no service_url, and no pg_credentials, this is a local box.
    return True


def backfill_install_mode_record() -> str | None:
    """Backfill a missing ``install.mode`` record (nexus-g7ijj).

    ``set_config_value("install.mode", ...)`` is written only at ``nx init``
    time — local provisioning stamps ``"local"``, managed onboarding stamps
    ``"managed"``. An install that reached its current state purely via
    ``nx upgrade`` (never re-running ``nx init``) never gets the record, so
    :func:`is_local_mode` falls through to ``pg_credentials`` artifact
    inference forever, and ``nx doctor``'s service checks report a
    misleading "pg_credentials absent" skip on a genuinely managed box.

    Precedence mirrors :func:`is_local_mode`'s own reading of the record,
    with two deliberate divergences because this is a DURABLE, PERMANENT
    stamp rather than a per-invocation read:

    - ``NX_LOCAL`` (``"1"`` or ``"0"``) SKIPS stamping entirely (returns
      ``None``). ``is_local_mode`` gives it top precedence for the current
      session, but an env override is session-scoped evidence of nothing
      durable — stamping from it would permanently contradict the record
      the moment the operator unsets the env var. Known residual: a box
      whose every ``nx upgrade`` runs under a durable ``NX_LOCAL`` wrapper
      (shell profile, e2e scripts) never backfills — that population keeps
      the artifact-inference behavior until ``nx init`` re-stamps.
    - ``service_url`` evidence is read from the PERSISTED config.yml
      credentials section only (never the ``NX_SERVICE_URL`` env overlay
      that :func:`get_credential` applies). A transiently-exported
      ``NX_SERVICE_URL`` at upgrade time is session-scoped, not durable
      intent; stamping "managed" from it would break T3 ops on a genuinely
      local box the next time the env var is absent. ``is_local_mode``'s
      own runtime read is UNCHANGED — the env var still wins for that
      session; only the STAMP requires file-backed evidence.

    Otherwise: a valid ``local``/``managed`` record already present is left
    untouched (no-op) — this is a backfill, not a re-stamp of a live
    record. A garbage/invalid recorded value is treated as unrecorded and
    re-stamped, the same fall-through :func:`is_local_mode` applies. A
    virgin box (neither signal present) is left unstamped — ``nx init``
    owns first stamping.

    Returns the mode that was stamped (``"local"`` or ``"managed"``), or
    ``None`` when nothing was written (already recorded, env-overridden
    session, or a virgin box). Never raises: the entire body is
    exception-safe — an unwritable config dir, or a malformed config.yml
    (``yaml.YAMLError`` from ``load_config``/credential reads), must not
    break an upgrade (same tolerance as ``upgrade_finish.py``'s finish-pass
    legs) even when the caller does not wrap the call itself.
    """
    try:
        nx_local = os.environ.get("NX_LOCAL", "").strip()
        if nx_local in ("1", "0"):
            return None  # session override; not durable intent, do not stamp

        recorded = str(
            load_config().get("install", {}).get("mode", "") or ""
        ).strip().lower()
        if recorded in ("local", "managed"):
            return None

        if _persisted_service_url().strip():
            mode = "managed"
        else:
            from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — leaf constant, deferred to keep config import-light

            if not (nexus_config_dir() / CREDENTIALS_FILENAME).is_file():
                return None  # virgin box; nx init owns first stamping
            mode = "local"

        set_config_value("install.mode", mode)
    except Exception:  # noqa: BLE001 — the docstring's "never raises" contract must hold from THIS function, not a caller wrapper (nexus-g7ijj); malformed config.yml (yaml.YAMLError) or an unwritable dir must not break an upgrade
        _log.warning("install_mode_backfill_failed", exc_info=True)
        return None
    _log.info("install_mode_backfilled", mode=mode, source="upgrade-convergence")
    return mode


def _persisted_service_url() -> str:
    """Read ``service_url`` from the PERSISTED ``config.yml`` only.

    Unlike :func:`get_credential`, this bypasses the ``NX_SERVICE_URL`` env
    overlay entirely. Used solely by :func:`backfill_install_mode_record`:
    a durable stamp needs durable (file-backed) evidence, never a
    transient env override for the current session.
    """
    path = _global_config_path()
    if not path.exists():
        return ""
    data = yaml.safe_load(path.read_text()) or {}
    return str(data.get("credentials", {}).get("service_url", "") or "")


_mode_record_contradiction_warned: bool = False


def _warn_mode_record_contradiction_once() -> None:
    """One-shot per process: install.mode=local recorded but a service_url is
    configured — the endpoint wins; tell the user how to reconcile."""
    global _mode_record_contradiction_warned
    if _mode_record_contradiction_warned:
        return
    _mode_record_contradiction_warned = True
    import structlog  # noqa: PLC0415 — deferred; config must stay import-light

    structlog.get_logger(__name__).warning(
        "mode_record_contradicts_service_url",
        recorded="local",
        resolution="managed (the configured service_url wins)",
        remedy="re-run `nx init` to re-stamp the record, or remove the stale "
               "service_url / set NX_LOCAL=1 if this box is genuinely local",
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
