# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flask server for the Nexus persistent background service."""
import json
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

from nexus.registry import RepoRegistry

app = Flask(__name__)

_CONFIG_DIR = Path.home() / ".config" / "nexus"
_REGISTRY_PATH = _CONFIG_DIR / "repos.json"
_registry = RepoRegistry(_REGISTRY_PATH)
_poll_interval = 10  # seconds


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/repos", methods=["GET"])
def list_repos():
    return jsonify({"repos": _registry.all()})


@app.route("/repos", methods=["POST"])
def add_repo():
    data = request.get_json(force=True)
    path = Path(data.get("path", ""))
    if not path.exists():
        return jsonify({"error": "path not found"}), 404
    _registry.add(path)
    return jsonify({"added": str(path)}), 201


@app.route("/repos/<path:repo_path>", methods=["DELETE"])
def remove_repo(repo_path: str):
    _registry.remove(Path("/" + repo_path))
    return jsonify({"removed": "/" + repo_path})


def _poll_loop() -> None:
    """Background thread: poll all repos every _poll_interval seconds."""
    from nexus.polling import check_and_reindex

    while True:
        for repo_str in _registry.all():
            try:
                check_and_reindex(Path(repo_str), _registry)
            except Exception:
                pass
        time.sleep(_poll_interval)


def start_server(host: str = "127.0.0.1", port: int = 7474, poll_interval: int = 10) -> None:
    """Start Flask + poll thread via Waitress."""
    global _poll_interval
    _poll_interval = poll_interval

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()

    from waitress import serve  # type: ignore[import]

    serve(app, host=host, port=port)
