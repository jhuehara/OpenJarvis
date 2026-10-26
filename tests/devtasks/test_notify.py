# ruff: noqa: E501
"""Phase 4: macOS + Telegram notices and approvals from the phone."""

from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace

import pytest

from openjarvis.devtasks import notify as notify_mod
from openjarvis.devtasks.notify import TaskNotifier, TelegramSettings
from openjarvis.devtasks.store import TaskStore

TOKEN = "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-ra"


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, data=None, files=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json or data or {}))
        result = {"username": "jarvis_bot"} if method == "getMe" else []
        return SimpleNamespace(json=lambda: {"ok": True, "result": result})

    def sent(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


class FakeRunner:
    def __init__(self, tmp_path):
        self.store = TaskStore(tmp_path / "tasks.db")
        self.decisions = []

    async def approve(self, task_id):
        self.decisions.append(("approve", task_id))
        return self.store.update(
            task_id, status="done", pr_url="https://github.com/x/pull/1"
        )

    async def deny(self, task_id):
        self.decisions.append(("deny", task_id))
        return self.store.update(task_id, status="done")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runner = FakeRunner(tmp_path)
    http = FakeHttp()
    clients = {"n": 0}
    mac = []
    monkeypatch.setattr(
        notify_mod, "macos_notify", lambda t, x: mac.append((t, x)) or True
    )
    notifier = TaskNotifier(
        runner,
        settings=TelegramSettings(tmp_path / "telegram.json"),
        http=http,
        clients=lambda: clients["n"],
    )
    return SimpleNamespace(
        runner=runner,
        http=http,
        notifier=notifier,
        clients=clients,
        mac=mac,
        tmp=tmp_path,
    )


def _msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def test_configure_validates_and_stores_token_privately(setup):
    async def go():
        with pytest.raises(ValueError):
            await setup.notifier.configure("not-a-token")
        status = await setup.notifier.configure(TOKEN)
        setup.notifier._poller.cancel()
        return status

    status = asyncio.run(go())
    assert status["telegram"]["configured"] and not status["telegram"]["paired"]
    assert len(status["telegram"]["pair_code"]) == 6
    path = setup.tmp / "telegram.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_pairing_only_with_code_then_strangers_ignored(setup):
    n = setup.notifier
    n.settings.save({"token": TOKEN, "bot": "jarvis_bot", "chat_id": ""})
    n.pair_code = "424242"

    async def go():
        await n._handle_update(_msg(111, "000000"))  # wrong code
        assert n.settings.load()["chat_id"] == ""
        await n._handle_update(_msg(222, "424242"))
        assert n.settings.load()["chat_id"] == "222"
        before = len(setup.http.calls)
        await n._handle_update(_msg(999, "/tarefas"))  # stranger
        assert len(setup.http.calls) == before

    asyncio.run(go())
    assert any("Pareado" in t for t in setup.http.sent())


def test_approval_by_button_and_by_reply(setup):
    n = setup.notifier
    n.settings.save({"token": TOKEN, "bot": "b", "chat_id": "222"})
    t1 = setup.runner.store.create(kind="codigo", project="P", prompt="a", title="A")
    t2 = setup.runner.store.create(kind="codigo", project="P", prompt="b", title="B")
    for t in (t1, t2):
        setup.runner.store.update(t.id, status="awaiting_approval")

    async def go():
        await n._handle_update(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb",
                    "data": f"ap:{t1.id}",
                    "message": {"chat": {"id": 222}},
                },
            }
        )
        await n._handle_update(_msg(222, f"nao {t2.id[:4]}"))
        await n._handle_update(
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cb2",
                    "data": f"ap:{t2.id}",
                    "message": {"chat": {"id": 999}},
                },
            }
        )

    asyncio.run(go())
    assert setup.runner.decisions == [("approve", t1.id), ("deny", t2.id)]
    assert any("Publicado" in t and "pull/1" in t for t in setup.http.sent())


def test_notices_on_transitions_only(setup):
    n = setup.notifier
    n.settings.save({"token": TOKEN, "bot": "b", "chat_id": "222"})
    base = {
        "id": "abcd1234",
        "kind": "codigo",
        "project": "P",
        "title": "Botão novo",
        "summary": "feito",
        "files": ["a.py"],
        "branch": "jarvis/x",
        "report": "",
        "routine": "",
        "silent": False,
        "error": "",
    }

    async def go():
        n._seen["abcd1234"] = "running"
        await n.on_task({**base, "status": "awaiting_approval"})
        await n.on_task({**base, "status": "awaiting_approval"})  # repeat: no notice
        n._seen["c1"] = "running"
        await n.on_task(
            {
                **base,
                "id": "c1",
                "kind": "checagem",
                "status": "done",
                "silent": True,
                "summary": "STATUS: OK",
            }
        )
        setup.clients["n"] = 1  # galaxy open: no macOS notice
        n._seen["c2"] = "running"
        await n.on_task(
            {
                **base,
                "id": "c2",
                "kind": "checagem",
                "status": "done",
                "summary": "test_x falhou\nSTATUS: FALHOU",
            }
        )

    asyncio.run(go())
    sent = setup.http.sent()
    assert len(sent) == 2
    assert "Aprovação necessária" in sent[0] and "SIM abcd" in sent[0]
    buttons = [p for m, p in setup.http.calls if m == "sendMessage"][0]["reply_markup"][
        "inline_keyboard"
    ]
    assert buttons[0][0]["callback_data"] == "ap:abcd1234"
    assert "Testes quebrados" in sent[1] and "STATUS" not in sent[1]
    assert len(setup.mac) == 1  # only the first one (no page connected then)


def test_macos_notify_uses_argv_not_shell(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notify_mod.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0),
    )
    assert notify_mod.macos_notify('Título "x"', "texto; rm -rf ~")
    argv = calls[0]
    assert argv[0] == "osascript" and argv[-1] == "texto; rm -rf ~"
