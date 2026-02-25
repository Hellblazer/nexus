"""Flask server route tests."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.server import app


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the module-level registry singleton between tests."""
    import nexus.server
    nexus.server._registry_instance = None
    yield
    nexus.server._registry_instance = None


# ── /health ──────────────────────────────────────────────────────────────────

def test_health_endpoint(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


# ── GET /repos ───────────────────────────────────────────────────────────────

def test_list_repos_empty(client) -> None:
    mock_reg = MagicMock()
    mock_reg.all_info.return_value = {}
    with patch("nexus.server._get_registry", return_value=mock_reg):
        response = client.get("/repos")

    assert response.status_code == 200
    assert response.get_json() == {"repos": {}}


def test_list_repos_with_entries(client) -> None:
    mock_reg = MagicMock()
    mock_reg.all_info.return_value = {
        "/home/user/project": {"name": "project", "status": "ready"}
    }
    with patch("nexus.server._get_registry", return_value=mock_reg):
        response = client.get("/repos")

    data = response.get_json()
    assert "/home/user/project" in data["repos"]


# ── POST /repos ──────────────────────────────────────────────────────────────

def test_add_repo_success(client, tmp_path: Path) -> None:
    """Valid git repo path is added and returns 201."""
    mock_reg = MagicMock()
    with (
        patch("nexus.server._get_registry", return_value=mock_reg),
        patch("nexus.server.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=str(tmp_path))
        response = client.post("/repos", json={"path": str(tmp_path)})

    assert response.status_code == 201
    mock_reg.add.assert_called_once()


def test_add_repo_missing_body(client) -> None:
    response = client.post("/repos", data="not json", content_type="text/plain")
    assert response.status_code == 400


def test_add_repo_missing_path(client) -> None:
    response = client.post("/repos", json={"wrong_key": "value"})
    assert response.status_code == 400
    assert "path" in response.get_json()["error"]


def test_add_repo_nonexistent_path(client) -> None:
    response = client.post("/repos", json={"path": "/nonexistent/repo"})
    assert response.status_code == 404


def test_add_repo_not_git(client, tmp_path: Path) -> None:
    """Non-git directory returns 400."""
    with patch("nexus.server.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="not a git repository")
        response = client.post("/repos", json={"path": str(tmp_path)})

    assert response.status_code == 400
    assert "not a git repository" in response.get_json()["error"]


def test_add_repo_git_timeout(client, tmp_path: Path) -> None:
    """Git timeout returns 504."""
    import subprocess

    with patch("nexus.server.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 10)):
        response = client.post("/repos", json={"path": str(tmp_path)})

    assert response.status_code == 504


# ── DELETE /repos ────────────────────────────────────────────────────────────

def test_remove_repo_success(client) -> None:
    mock_reg = MagicMock()
    mock_reg.get.return_value = {"name": "project", "status": "ready"}
    with patch("nexus.server._get_registry", return_value=mock_reg):
        response = client.delete("/repos/home/user/project")

    assert response.status_code == 200
    mock_reg.remove.assert_called_once()


def test_remove_repo_not_registered(client) -> None:
    mock_reg = MagicMock()
    mock_reg.get.return_value = None
    with patch("nexus.server._get_registry", return_value=mock_reg):
        response = client.delete("/repos/home/user/project")

    assert response.status_code == 404
    assert "not registered" in response.get_json()["error"]
