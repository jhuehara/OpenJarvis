"""HTTP API for dev tasks."""

import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.core.config import DevTasksConfig
from openjarvis.server.devtask_routes import setup_devtasks
from openjarvis.server.galaxy import delegation_rules


def _app(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cfg = DevTasksConfig(
        claude_binary="/nonexistent/claude",
        db_path=str(tmp_path / "tasks.db"),
        worktree_root=str(tmp_path / "wt"),
        projects=[
            {"name": "Demo", "path": str(repo.resolve()), "description": "teste"}
        ],
    )
    app = FastAPI()
    app.state.config = SimpleNamespace(devtasks=cfg)
    setup_devtasks(app)
    return app


def test_routes(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        projects = client.get("/v1/projects").json()
        assert [p["name"] for p in projects["projects"]] == ["Demo"]

        bad = client.post("/v1/tasks", json={"project": "outro", "prompt": "x"})
        assert bad.status_code == 400 and "cadastrado" in bad.json()["detail"]
        assert (
            client.post(
                "/v1/tasks", json={"kind": "x", "project": "Demo", "prompt": "y"}
            ).status_code
            == 409
        )

        created = client.post(
            "/v1/tasks", json={"project": "demo", "prompt": "faz algo"}
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        listing = client.get("/v1/tasks").json()
        assert listing["tasks"][0]["id"] == task_id and "cost_today" in listing
        assert client.get("/v1/tasks/nope").status_code == 404
        assert client.post(f"/v1/tasks/{task_id}/approve").status_code == 409
        assert client.post("/v1/tasks/killswitch").json()["paused"] is True


def test_delegation_rules_list_projects():
    text = delegation_rules([SimpleNamespace(name="MyOps", description="SaaS")])
    assert (
        "- MyOps: SaaS" in text
        and "[[tarefa:" in text
        and "[[abrir: atividades]]" in text
    )
