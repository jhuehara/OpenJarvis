"""Phase 6: remote access guard, "Meu dia" timeline, installable web app."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from openjarvis.server.galaxy import build_day, galaxy_router, parse_day
from openjarvis.server.tailnet_guard import TailnetGuard


def _guarded(allowed):
    app = FastAPI()

    @app.get("/v1/ping")
    def ping():
        return {"ok": True}

    @app.websocket("/v1/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("oi")
        await socket.close()

    app.add_middleware(TailnetGuard, allowed_logins=allowed)
    return TestClient(app)


def test_guard_allows_local_and_owner_only():
    client = _guarded(["Jhu.Uehara@gmail.com"])
    assert client.get("/v1/ping").status_code == 200  # local: no header
    owner = {"Tailscale-User-Login": "jhu.uehara@gmail.com"}
    assert client.get("/v1/ping", headers=owner).status_code == 200
    other = client.get("/v1/ping", headers={"Tailscale-User-Login": "x@y.com"})
    assert other.status_code == 403 and "não autorizado" in other.json()["detail"]
    with client.websocket_connect("/v1/ws", headers=owner) as ws:
        assert ws.receive_text() == "oi"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/v1/ws", headers={"Tailscale-User-Login": "x@y.com"}
        ) as ws:
            ws.receive_text()


def test_guard_closed_by_default():
    client = _guarded([])
    assert client.get("/v1/ping").status_code == 200
    assert (
        client.get("/v1/ping", headers={"Tailscale-User-Login": "me@x.com"}).status_code
        == 403
    )


def test_build_day_collects_conversations_tasks_and_facts(tmp_path):
    from openjarvis.devtasks.store import TaskStore
    from openjarvis.server.galaxy import _local_now

    today = _local_now().date()
    noon = dt.datetime.combine(
        today, dt.time(12), tzinfo=_local_now().tzinfo
    ).timestamp()
    tr = sqlite3.connect(tmp_path / "traces.db")
    tr.execute(
        "CREATE TABLE traces (trace_id TEXT, query TEXT, result TEXT, started_at REAL)"
    )
    tr.execute(
        "INSERT INTO traces VALUES ('t1', 'Toca Garota de Ipanema', ?, ?)",
        ("Aqui está. [[musica: https://youtu.be/x | Tom Jobim]]", noon),
    )
    tr.execute("INSERT INTO traces VALUES ('t0', 'ontem', 'x', ?)", (noon - 86400,))
    tr.commit()
    tr.close()
    store = TaskStore(tmp_path / "tasks.db")
    task = store.create(kind="codigo", project="P", prompt="botão", title="Botão novo")
    store.update(task.id, status="done", cost_usd=0.5)
    (tmp_path / "memory_facts.jsonl").write_text(
        json.dumps(
            {"text": "Gosta de bossa nova", "created_at": noon + 60, "trust": "auto"}
        )
        + "\n"
        + json.dumps({"text": "injetado", "created_at": noon, "trust": "untrusted"})
        + "\n"
    )
    day = build_day(today, config_dir=tmp_path)
    kinds = [it["type"] for it in day["items"]]
    assert (
        kinds.count("conversa") == 1
        and "tarefa" in kinds
        and kinds.count("aprendizado") == 1
    )
    conv = next(it for it in day["items"] if it["type"] == "conversa")
    assert conv["media"] == "música “Tom Jobim”"
    assert day["label"].startswith("hoje,") and day["next"] is None
    assert day["cost_usd"] == 0.5
    assert parse_day("ontem") == today - dt.timedelta(days=1)


def test_manifest_and_day_route(tmp_path, monkeypatch):
    monkeypatch.setattr("openjarvis.server.galaxy._config_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(galaxy_router)
    client = TestClient(app)
    manifest = client.get("/galaxy/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and data["start_url"] == "/galaxy"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/v1/galaxy/day?date=hoje").json()["items"] == []
    assert client.get("/v1/galaxy/day?date=amanha-talvez").status_code == 400
