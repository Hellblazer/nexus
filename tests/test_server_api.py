"""T2: Flask HTTP API — /health, /repos GET/POST/DELETE routes."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import nexus.server as server_module
from nexus.server import app


@pytest.fixture(autouse=True)
def reset_registry_instance():
    """Reset the module-level registry singleton between tests."""
    server_module._registry_instance = None
    yield
    server_module._registry_instance = None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── /health ───────────────────────────────────────────────────────────────────

def test_health_returns_ok(client) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# ── GET /repos ─────────────────────────────────────────────────────────────────

def test_list_repos_empty(client) -> None:
    resp = client.get("/repos")
    assert resp.status_code == 200
    assert resp.get_json() == {"repos": {}}


def test_list_repos_shows_registered(client, tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.all_info.return_value = {str(repo): {"name": "myrepo", "status": "registered"}}
    with patch.object(server_module, "_get_registry", return_value=mock_reg):
        resp = client.get("/repos")
    assert str(repo) in resp.get_json()["repos"]


# ── POST /repos ────────────────────────────────────────────────────────────────

def test_add_repo_success(client, tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_git = MagicMock(returncode=0)
    with patch.object(server_module, "_get_registry", return_value=mock_reg):
        with patch("nexus.server.subprocess.run", return_value=mock_git):
            resp = client.post("/repos", json={"path": str(repo)})
    assert resp.status_code == 201
    mock_reg.add.assert_called_once_with(repo)


def test_add_repo_path_not_found(client, tmp_path: Path) -> None:
    resp = client.post("/repos", json={"path": str(tmp_path / "nonexistent")})
    assert resp.status_code == 404


def test_add_repo_rejects_non_git_directory(client, tmp_path: Path) -> None:
    """POST /repos returns 400 when the path exists but is not a git repository."""
    repo = tmp_path / "notgit"
    repo.mkdir()
    resp = client.post("/repos", json={"path": str(repo)})
    assert resp.status_code == 400
    assert "git" in resp.get_json()["error"].lower()


def test_add_repo_accepts_git_repository(client, tmp_path: Path) -> None:
    """POST /repos accepts a directory that contains a .git repo."""
    import subprocess as _sp
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _sp.run(["git", "init", str(repo)], capture_output=True)
    mock_reg = MagicMock()
    with patch.object(server_module, "_get_registry", return_value=mock_reg):
        resp = client.post("/repos", json={"path": str(repo)})
    assert resp.status_code == 201
    mock_reg.add.assert_called_once_with(repo)


def test_add_repo_malformed_json(client) -> None:
    resp = client.post("/repos", data="not json", content_type="application/json")
    assert resp.status_code == 400
    assert "JSON" in resp.get_json()["error"]


def test_add_repo_missing_path_key(client) -> None:
    resp = client.post("/repos", json={"other": "value"})
    assert resp.status_code == 400


def test_add_repo_null_path(client) -> None:
    resp = client.post("/repos", json={"path": None})
    assert resp.status_code == 400


# ── DELETE /repos ──────────────────────────────────────────────────────────────

def test_delete_repo_success(client, tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}
    with patch.object(server_module, "_get_registry", return_value=mock_reg):
        resp = client.delete(f"/repos{repo}")
    assert resp.status_code == 200
    mock_reg.remove.assert_called_once_with(repo)


def test_delete_repo_not_registered(client, tmp_path: Path) -> None:
    repo = tmp_path / "unknown"
    mock_reg = MagicMock()
    mock_reg.get.return_value = None
    with patch.object(server_module, "_get_registry", return_value=mock_reg):
        resp = client.delete(f"/repos{repo}")
    assert resp.status_code == 404
    mock_reg.remove.assert_not_called()


def test_delete_repo_path_is_canonicalized(client, tmp_path: Path) -> None:
    """DELETE /repos canonicalizes the path so registry lookup uses the real path."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"collection": "code__myrepo"}
    with patch.object(server_module, "_get_registry", return_value=mock_reg):
        resp = client.delete(f"/repos{repo}")
    assert resp.status_code == 200
    # The path passed to reg.get must be the resolved canonical path
    called_path = mock_reg.get.call_args[0][0]
    assert called_path == repo.resolve()
