# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flask server for the Nexus persistent background service."""
import logging
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

from nexus.registry import RepoRegistry

app = Flask(__name__)

_log = logging.getLogger(__name__)

_poll_interval = 10  # seconds


def _registry() -> RepoRegistry:
    """Return the registry, using the current HOME at call time."""
    registry_path = Path.home() / ".config" / "nexus" / "repos.json"
    return RepoRegistry(registry_path)


# Module-level registry instance for request handlers; initialised lazily
# via _get_registry() so that Path.home() is evaluated at call time.
_registry_instance: RepoRegistry | None = None
_registry_lock = threading.Lock()


def _get_registry() -> RepoRegistry:
    """Return the shared registry instance, creating it on first call."""
    global _registry_instance
    if _registry_instance is None:
        with _registry_lock:
            if _registry_instance is None:
                _registry_instance = _registry()
    return _registry_instance


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/repos", methods=["GET"])
def list_repos():
    return jsonify({"repos": _get_registry().all()})


@app.route("/repos", methods=["POST"])
def add_repo():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    path_str = data.get("path")
    if not isinstance(path_str, str):
        return jsonify({"error": "'path' must be a non-null string"}), 400
    path = Path(path_str)
    if not path.exists():
        return jsonify({"error": "path not found"}), 404
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return jsonify({"error": "path is not a git repository"}), 400
    _get_registry().add(path)
    return jsonify({"added": str(path)}), 201


@app.route("/repos/<path:repo_path>", methods=["DELETE"])
def remove_repo(repo_path: str):
    full_path = Path("/" + repo_path).resolve()
    reg = _get_registry()
    if reg.get(full_path) is None:
        return jsonify({"error": "repo not registered"}), 404
    reg.remove(full_path)
    return jsonify({"removed": str(full_path)})


def _poll_loop() -> None:
    """Background thread: poll all repos every _poll_interval seconds."""
    from nexus.polling import check_and_reindex

    while True:
        try:
            for repo_str in _get_registry().all():
                try:
                    check_and_reindex(Path(repo_str), _get_registry())
                except Exception as exc:
                    _log.warning("Poll error for %s: %s", repo_str, exc, exc_info=True)
            time.sleep(_poll_interval)
        except Exception as exc:
            _log.exception("Poll loop body raised — restarting in %ss: %s", _poll_interval, exc)
            time.sleep(_poll_interval)


def start_server(host: str = "127.0.0.1", port: int = 7474, poll_interval: int = 10) -> None:
    """Start Flask + poll thread via Waitress."""
    global _poll_interval
    _poll_interval = poll_interval

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()

    from waitress import serve  # type: ignore[import]

    serve(app, host=host, port=port)
