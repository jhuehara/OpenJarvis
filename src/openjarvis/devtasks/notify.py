# ruff: noqa: E501 — user-facing pt-BR message templates read better unwrapped
"""Notifications for dev tasks outside the galaxy page: macOS and Telegram.

Only three things notify — a task finished, needs approval, or failed — and
silent tasks (e.g. a passing test check) never do. macOS notifications are
shown only when no galaxy page is connected (the page has its own toasts).

Telegram talks to the Bot API directly (no extra dependency): it sends the
notices (with the report attached and Approve/Deny buttons) and long-polls for
the user's replies. Only the paired chat is ever obeyed. The bot token lives
in ``~/.openjarvis/telegram.json`` (mode 600), written by the setup endpoint,
so it never has to appear in a chat or in config.toml.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
_REPLY_RE = re.compile(
    r"^\s*(sim|s|aprovo|aprovar|ok|nao|não|n|nego|negar)\s+#?([0-9a-f]{4,8})\s*$", re.I
)
_KIND = {
    "codigo": "código",
    "revisao": "revisão",
    "pesquisa": "pesquisa",
    "resumo": "resumo",
    "checagem": "checagem de testes",
}


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _short(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# -- macOS --------------------------------------------------------------------


def macos_notify(title: str, text: str) -> bool:
    """``display notification`` via osascript (argv only: no shell, no injection)."""
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv) "
        'sound name "Glass"\n'
        "end run"
    )
    try:
        return (
            subprocess.run(
                ["osascript", "-e", script, title[:120], text[:400]],
                capture_output=True,
                timeout=10,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


# -- Telegram settings ----------------------------------------------------------


class TelegramSettings:
    """Token/chat id persisted with owner-only permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


# -- notifier -----------------------------------------------------------------


class TaskNotifier:
    def __init__(
        self,
        runner: Any,
        *,
        bus: Any = None,
        settings: Optional[TelegramSettings] = None,
        macos: bool = True,
        clients: Any = None,
        http: Any = None,
    ) -> None:
        self.runner = runner
        self.bus = bus
        self.settings = settings or TelegramSettings(
            Path.home() / ".openjarvis" / "telegram.json"
        )
        self.macos = macos
        self._clients = clients or (lambda: 0)
        self._http = http  # injectable for tests (async .post(url, ...))
        self._seen: Dict[str, str] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._poller: Optional[asyncio.Task] = None
        self.pair_code = ""
        self.bot_name = ""

    # lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        for task in self.runner.store.list(limit=200):
            self._seen[task.id] = task.status
        if self.bus is not None:
            from openjarvis.core.events import EventType

            self.bus.subscribe(EventType.DEVTASK_UPDATED, self._on_event)
        if self.settings.load().get("token"):
            self._start_polling()

    async def stop(self) -> None:
        if self._poller:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poller
        if self._http is not None and hasattr(self._http, "aclose"):
            await self._http.aclose()

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=70)
        return self._http

    def _start_polling(self) -> None:
        if self._poller is None or self._poller.done():
            self._poller = asyncio.create_task(self._poll_loop())

    # status / setup -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        data = self.settings.load()
        return {
            "macos": self.macos,
            "telegram": {
                "configured": bool(data.get("token")),
                "paired": bool(data.get("chat_id")),
                "bot": data.get("bot", ""),
                "pair_code": self.pair_code
                if data.get("token") and not data.get("chat_id")
                else "",
            },
        }

    async def configure(self, token: str) -> Dict[str, Any]:
        """Validate a bot token, store it and start pairing with a 6-digit code."""
        token = token.strip()
        if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,64}", token):
            raise ValueError("token inválido (formato do @BotFather: 123456:ABC…)")
        me = await self._api("getMe", token=token)
        bot = me.get("username", "")
        self.settings.save({"token": token, "bot": bot, "chat_id": ""})
        self.pair_code = f"{secrets.randbelow(10**6):06d}"
        self.bot_name = bot
        if self._poller:
            self._poller.cancel()
            self._poller = None
        self._start_polling()
        return self.status()

    def unpair(self) -> None:
        self.settings.clear()
        self.pair_code = ""
        if self._poller:
            self._poller.cancel()
            self._poller = None

    async def send_test(self) -> None:
        await self.telegram(
            "🛰️ Jarvis conectado. Vou te avisar por aqui quando um agente terminar, falhar ou precisar da sua aprovação."
        )

    # telegram API ---------------------------------------------------------

    async def _api(
        self, method: str, *, token: str = "", files: Any = None, **params: Any
    ) -> Any:
        token = token or self.settings.load().get("token", "")
        if not token:
            raise RuntimeError("Telegram não configurado")
        url = API.format(token=token, method=method)
        if files:
            resp = await self._client().post(url, data=params, files=files)
        else:
            resp = await self._client().post(url, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "erro do Telegram"))
        return data.get("result")

    async def telegram(
        self, text: str, *, buttons: Any = None, document: Optional[Path] = None
    ) -> bool:
        chat = self.settings.load().get("chat_id")
        if not chat:
            return False
        try:
            params: Dict[str, Any] = {
                "chat_id": chat,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if buttons:
                params["reply_markup"] = {"inline_keyboard": buttons}
            await self._api("sendMessage", **params)
            if document and document.is_file() and document.stat().st_size < 1_000_000:
                with open(document, "rb") as fh:
                    await self._api(
                        "sendDocument",
                        files={"document": (document.name, fh.read(), "text/markdown")},
                        chat_id=chat,
                    )
            return True
        except Exception as exc:  # noqa: BLE001 — a notice must never break tasks
            logger.warning("telegram notice failed: %s", exc)
            return False

    async def _poll_loop(self) -> None:
        offset = 0
        backoff = 2
        while True:
            try:
                updates = await self._api(
                    "getUpdates",
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
                backoff = 2
                for update in updates or []:
                    offset = update["update_id"] + 1
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — network blips, 409, ...
                logger.debug("telegram poll error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle_update(self, update: Dict[str, Any]) -> None:
        data = self.settings.load()
        chat_id = str(data.get("chat_id") or "")
        if "callback_query" in update:
            cq = update["callback_query"]
            from_chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            if not chat_id or from_chat != chat_id:
                return
            with contextlib.suppress(Exception):
                await self._api("answerCallbackQuery", callback_query_id=cq["id"])
            action, _, task_id = str(cq.get("data", "")).partition(":")
            await self._decide(task_id, approve=(action == "ap"))
            return
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        from_chat = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id:
            # Pairing: the first chat that sends the code shown in the galaxy wins.
            if self.pair_code and text == self.pair_code:
                data["chat_id"] = from_chat
                self.settings.save(data)
                self.pair_code = ""
                await self.telegram(
                    "✅ Pareado! A partir de agora este chat recebe os avisos do Jarvis.\nComandos: /tarefas, /ajuda"
                )
            return
        if from_chat != chat_id:
            return  # strangers are ignored
        low = text.lower()
        if low in ("/start", "/ajuda", "/help"):
            await self.telegram(
                "Eu aviso quando um agente termina, falha ou precisa de aprovação.\n"
                "• Aprove com os botões ou respondendo <b>SIM abcd</b> / <b>NAO abcd</b>\n"
                "• /tarefas — o que os agentes estão fazendo"
            )
        elif low == "/tarefas":
            await self.telegram(self._tasks_text())
        elif m := _REPLY_RE.match(text):
            word, short = m.group(1).lower(), m.group(2).lower()
            task_id = next(
                (
                    t.id
                    for t in self.runner.store.list(limit=200)
                    if t.id.startswith(short)
                ),
                "",
            )
            await self._decide(
                task_id, approve=word in ("sim", "s", "aprovo", "aprovar", "ok")
            )

    async def _decide(self, task_id: str, *, approve: bool) -> None:
        task = self.runner.store.get(task_id) if task_id else None
        if task is None:
            await self.telegram("Não encontrei essa tarefa.")
            return
        if task.status != "awaiting_approval":
            await self.telegram(
                f"A tarefa <b>{_esc(task.title)}</b> não está aguardando aprovação."
            )
            return
        try:
            if approve:
                done = await self.runner.approve(task.id)
                where = done.pr_url or done.branch
                await self.telegram(
                    f"🚀 Publicado: <b>{_esc(done.title)}</b>\n{_esc(where)}"
                    + (f"\n⚠️ {_esc(done.error)}" if done.error else "")
                )
            else:
                await self.runner.deny(task.id)
                await self.telegram(
                    f"Tudo bem, não publiquei <b>{_esc(task.title)}</b>. A branch ficou só no computador."
                )
        except Exception as exc:  # noqa: BLE001
            await self.telegram(f"Não consegui: {_esc(str(exc))}")

    def _tasks_text(self) -> str:
        tasks = self.runner.store.list(limit=12)
        active = [
            t for t in tasks if t.status in ("queued", "running", "awaiting_approval")
        ]
        label = {
            "queued": "na fila",
            "running": "trabalhando",
            "awaiting_approval": "aguardando aprovação",
        }
        if not active:
            return "Nenhum agente trabalhando agora."
        return "\n".join(
            f"• <b>{_esc(t.title)}</b> ({_esc(t.project or _KIND.get(t.kind, t.kind))}) — {label[t.status]}"
            + (
                f": {_esc(t.current_step)}"
                if t.status == "running" and t.current_step
                else ""
            )
            + (f" — responda SIM {t.id[:4]}" if t.status == "awaiting_approval" else "")
            for t in active
        )

    async def routine_skipped(self, name: str, project: str, reason: str) -> None:
        where = f" ({project})" if project else ""
        text = f"A rotina “{name}”{where} não rodou: {reason}"
        if self.macos and self._clients() == 0:
            await asyncio.to_thread(macos_notify, "Jarvis — rotina não rodou", text)
        await self.telegram(f"⚠️ {_esc(text)}")

    # task events ----------------------------------------------------------

    def _on_event(self, event: Any) -> None:
        task = (event.data or {}).get("task") or {}
        if not task or self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.on_task(task))
        )

    async def on_task(self, task: Dict[str, Any]) -> None:
        prev = self._seen.get(task["id"])
        status = task["status"]
        self._seen[task["id"]] = status
        if prev == status:
            return
        notice = self.compose(task, prev)
        if notice is None:
            return
        title, text, tg, buttons = notice
        if self.macos and self._clients() == 0:
            await asyncio.to_thread(macos_notify, f"Jarvis — {title}", text)
        report = Path(task["report"]) if task.get("report") else None
        await self.telegram(tg, buttons=buttons, document=report)

    @staticmethod
    def compose(task: Dict[str, Any], prev: Optional[str]) -> Optional[tuple]:
        """(title, plain text, telegram HTML, buttons) or None for no notice."""
        status, kind = task["status"], task.get("kind", "")
        name = task.get("project") or _KIND.get(kind, kind)
        title_txt = task.get("title", "")
        summary = re.sub(
            r"\n?STATUS:.*$", "", task.get("summary") or "", flags=re.S | re.I
        ).strip()
        short_id = task["id"][:4]
        if status == "awaiting_approval":
            n = len(task.get("files") or [])
            text = f"{title_txt} ({name}) está na branch, {n} arquivo(s) alterado(s). Aprovar push/PR?"
            tg = (
                f"🟡 <b>Aprovação necessária</b>\n<b>{_esc(title_txt)}</b> ({_esc(name)})\n"
                f"{n} arquivo(s) na branch <code>{_esc(task.get('branch', ''))}</code>\n\n"
                f"{_esc(_short(summary, 700))}\n\nResponda <b>SIM {short_id}</b> / <b>NAO {short_id}</b> ou use os botões."
            )
            buttons = [
                [
                    {
                        "text": "✅ Aprovar e abrir PR",
                        "callback_data": f"ap:{task['id']}",
                    },
                    {"text": "❌ Negar", "callback_data": f"dn:{task['id']}"},
                ]
            ]
            return ("Aprovação necessária", text, tg, buttons)
        if status == "failed" and prev in ("running", "queued", None):
            err = task.get("error") or "erro desconhecido"
            return (
                "Tarefa falhou",
                f"{title_txt} ({name}) falhou: {err}",
                f"🔴 <b>Falhou:</b> {_esc(title_txt)} ({_esc(name)})\n{_esc(_short(err, 600))}",
                None,
            )
        if status == "done" and prev == "running":
            if task.get("silent"):
                return None
            if kind == "checagem":
                didnt_run = "NAO_RODOU" in (task.get("summary") or "").upper()
                head = "Checagem não rodou" if didnt_run else "Testes quebrados"
                return (
                    head,
                    f"{head} — {name}: {_short(summary, 200)}",
                    f"🔴 <b>{head}</b> — {_esc(name)}\n{_esc(_short(summary, 900))}",
                    None,
                )
            head = "Rotina concluída" if task.get("routine") else "Tarefa concluída"
            return (
                head,
                f"{title_txt} ({name}) está pronta.",
                f"✅ <b>{_esc(title_txt)}</b> ({_esc(name)})\n\n{_esc(_short(summary, 1500))}",
                None,
            )
        return None
