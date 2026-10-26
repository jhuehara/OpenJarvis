# ruff: noqa: E501 — fake CLI script and payload literals
"""Phase 2: tool events in chat streaming, project code reading, project context."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from openjarvis.core.types import Message, Role

FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    sys.stdin.read()
    with open(os.environ["FAKE_LOG"], "w") as f:
        json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, f)
    def out(o): print(json.dumps(o), flush=True)
    out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Grep", "input": {{"pattern": "def main"}}}}]}}}})
    out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "content": "app.py:1"}}]}}}})
    out({{"type": "stream_event", "event": {{"delta": {{"type": "text_delta", "text": "Fica em app.py"}}}}}})
    out({{"type": "result", "subtype": "success", "result": "Fica em app.py", "usage": {{}}}})
    """
)


def test_engine_reports_tool_events_and_honours_workspace(tmp_path, monkeypatch):
    from openjarvis.engine.cli_agents import ClaudeCodeEngine

    fake = tmp_path / "claude"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    project = tmp_path / "proj"
    project.mkdir()

    engine = ClaudeCodeEngine(
        binary=str(fake), tools="WebSearch", workspace=str(tmp_path / "ws")
    )
    events: list = []

    async def run():
        out = []
        async for tok in engine.stream(
            [Message(role=Role.USER, content="onde fica main?")],
            model="claudecode",
            on_tool_event=events.append,
            workspace=str(project),
            tools="Read,Grep,Glob",
        ):
            out.append(tok)
        return "".join(out)

    assert asyncio.run(run()) == "Fica em app.py"
    assert [e["type"] for e in events] == ["start", "end"]
    assert events[0]["tool"] == "Grep" and events[0]["step"] == "procurando “def main”"
    call = json.loads(log.read_text())
    assert Path(call["cwd"]).resolve() == project.resolve()
    assert call["argv"][call["argv"].index("--tools") + 1] == "Read,Grep,Glob"


def _app(tmp_path, recorded: dict, projects=None):
    from openjarvis.core.config import JarvisConfig
    from openjarvis.server.app import create_app

    class FakeClaudeEngine:
        engine_id = "claude_code"

        def health(self):
            return True

        def list_models(self):
            return ["claudecode"]

        async def stream(
            self, messages, *, model, temperature=0.7, max_tokens=1024, **kw
        ):
            recorded.update(kw)
            if kw.get("on_tool_event"):
                kw["on_tool_event"](
                    {
                        "type": "start",
                        "id": "t1",
                        "tool": "Read",
                        "step": "lendo app.py",
                    }
                )
                kw["on_tool_event"]({"type": "end", "id": "t1", "ok": True})
            for tok in ["Fica ", "em app.py"]:
                yield tok

    cfg = JarvisConfig()
    cfg.analytics.enabled = False
    cfg.traces.enabled = False
    cfg.security.enabled = False
    cfg.devtasks.db_path = str(tmp_path / "tasks.db")
    cfg.devtasks.projects = projects or []
    return create_app(FakeClaudeEngine(), "claudecode", config=cfg)


def _post(client, **extra):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "claudecode",
            "stream": True,
            "messages": [{"role": "user", "content": "onde?"}],
            **extra,
        },
    )


def test_stream_emits_named_tool_events(tmp_path):
    from fastapi.testclient import TestClient

    recorded: dict = {}
    with TestClient(_app(tmp_path, recorded)) as client:
        body = _post(client).text
    assert "event: tool_call_start" in body and "event: tool_call_end" in body
    start = body.split("event: tool_call_start\ndata: ", 1)[1].split("\n", 1)[0]
    assert json.loads(start)["step"] == "lendo app.py"
    assert "Fica " in body and "em app.py" in body
    assert "workspace" not in recorded


def test_project_field_reads_registered_project_only(tmp_path):
    from fastapi.testclient import TestClient

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    recorded: dict = {}
    app = _app(
        tmp_path, recorded, projects=[{"name": "Demo", "path": str(repo.resolve())}]
    )
    with TestClient(app) as client:
        assert _post(client, project="outro").status_code == 400
        assert _post(client, project="demo").status_code == 200
    assert recorded["workspace"] == str(repo.resolve())
    assert recorded["tools"] == "Read,Grep,Glob,WebSearch,WebFetch"


def test_projects_context_summarises_git_state(tmp_path):
    from openjarvis.devtasks.context import _CACHE, projects_context
    from openjarvis.devtasks.projects import ProjectRegistry

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "primeiro",
        ],
        check=True,
    )
    (repo / "b.txt").write_text("y")
    _CACHE.clear()
    text = projects_context(
        ProjectRegistry(
            [{"name": "Demo", "path": str(repo.resolve()), "description": "teste"}]
        ).list()
    )
    assert "Demo (teste): branch atual main, 1 arquivo(s) alterado(s)" in text
    assert "primeiro" in text
