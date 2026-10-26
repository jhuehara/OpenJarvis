"""HTTP API for delegated dev tasks (``/v1/tasks``) and registered projects.

The runner itself lives in :mod:`openjarvis.devtasks.runner`; this module wires
it into the FastAPI app (startup/shutdown) and exposes it to the galaxy UI.
Live progress is pushed over ``/v1/agents/events`` (``devtask_updated`` and
``devtask_event``); these endpoints serve snapshots and actions.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from openjarvis.devtasks.projects import ProjectError
from openjarvis.devtasks.runner import BudgetError, DevTaskRunner

logger = logging.getLogger(__name__)

router = APIRouter(tags=["devtasks"])


class CreateTaskRequest(BaseModel):
    kind: str = "codigo"
    project: str = ""
    prompt: str = Field(min_length=1, max_length=8000)
    title: str = Field(default="", max_length=200)


class TelegramSetupRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class FollowupRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


def _runner(request: Request) -> DevTaskRunner:
    runner = getattr(request.app.state, "devtasks", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Tarefas desativadas")
    return runner


def _guard(fn: Any) -> Any:
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada") from exc
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BudgetError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _aguard(coro: Any) -> Any:
    try:
        return await coro
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada") from exc
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BudgetError as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/v1/projects")
def list_projects(request: Request):
    runner = _runner(request)
    return {
        "projects": [p.to_dict() for p in runner.registry.list()],
        "errors": runner.registry.errors,
    }


@router.get("/v1/tasks")
def list_tasks(request: Request, limit: int = 30):
    runner = _runner(request)
    tasks = runner.store.list(limit=max(1, min(limit, 200)))
    return {"tasks": [t.to_dict() for t in tasks], **runner.status()}


@router.post("/v1/tasks", status_code=201)
async def create_task(req: CreateTaskRequest, request: Request):
    runner = _runner(request)
    task = await _aguard(runner.submit(req.kind, req.project, req.prompt, req.title))
    return task.to_dict()


@router.get("/v1/tasks/{task_id}")
def get_task(task_id: str, request: Request, after: int = 0):
    runner = _runner(request)
    task = runner.store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return {"task": task.to_dict(), "events": runner.store.events(task_id, after=after)}


@router.get("/v1/tasks/{task_id}/diff", response_class=PlainTextResponse)
def task_diff(task_id: str, file: str, request: Request):
    runner = _runner(request)
    return _guard(lambda: runner.diff_file(task_id, file))


@router.post("/v1/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    return (await _aguard(_runner(request).cancel(task_id))).to_dict()


@router.post("/v1/tasks/{task_id}/followup")
async def followup_task(task_id: str, req: FollowupRequest, request: Request):
    return (await _aguard(_runner(request).followup(task_id, req.prompt))).to_dict()


@router.post("/v1/tasks/{task_id}/approve")
async def approve_task(task_id: str, request: Request):
    return (await _aguard(_runner(request).approve(task_id))).to_dict()


@router.post("/v1/tasks/{task_id}/deny")
async def deny_task(task_id: str, request: Request):
    return (await _aguard(_runner(request).deny(task_id))).to_dict()


@router.get("/v1/tasks/{task_id}/report", response_class=PlainTextResponse)
def task_report(task_id: str, request: Request):
    runner = _runner(request)
    task = runner.store.get(task_id)
    if task is None or not task.report:
        raise HTTPException(status_code=404, detail="Sem relatório")
    from pathlib import Path

    path = Path(task.report).resolve()
    root = Path(runner.cfg.reports_dir).expanduser().resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="Relatório indisponível")
    return path.read_text(encoding="utf-8", errors="replace")


@router.get("/v1/routines")
def list_routines(request: Request):
    scheduler = getattr(request.app.state, "devtask_routines", None)
    return {"routines": scheduler.describe() if scheduler else []}


@router.post("/v1/routines/{name}/run")
async def run_routine(name: str, request: Request):
    scheduler = getattr(request.app.state, "devtask_routines", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Rotinas desativadas")
    try:
        routine = scheduler.find(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Rotina não encontrada") from exc
    tasks = await scheduler.fire(routine)
    return {"tasks": [t.to_dict() for t in tasks]}


def _notifier(request: Request) -> Any:
    notifier = getattr(request.app.state, "devtask_notifier", None)
    if notifier is None:
        raise HTTPException(status_code=503, detail="Notificações desativadas")
    return notifier


@router.get("/v1/notify/status")
def notify_status(request: Request):
    return _notifier(request).status()


@router.post("/v1/notify/telegram")
async def notify_telegram_setup(req: TelegramSetupRequest, request: Request):
    try:
        return await _notifier(request).configure(req.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:  # Telegram rejected the token
        raise HTTPException(status_code=400, detail=f"Telegram: {exc}") from exc


@router.post("/v1/notify/telegram/test")
async def notify_telegram_test(request: Request):
    notifier = _notifier(request)
    if not notifier.status()["telegram"]["paired"]:
        raise HTTPException(status_code=409, detail="Telegram ainda não pareado")
    await notifier.send_test()
    return {"sent": True}


@router.delete("/v1/notify/telegram")
def notify_telegram_remove(request: Request):
    _notifier(request).unpair()
    return _notifier(request).status()


@router.post("/v1/tasks/killswitch")
async def killswitch(request: Request):
    stopped = await _runner(request).killswitch()
    return {"stopped": stopped, "paused": True}


@router.post("/v1/tasks/resume")
def resume(request: Request):
    _runner(request).resume()
    return {"paused": False}


def setup_devtasks(app: Any) -> None:
    """Create the runner on startup (needs the event loop) and stop it on shutdown."""
    config = getattr(app.state, "config", None)
    if config is None:
        from openjarvis.core.config import load_config

        config = load_config()
    cfg = getattr(config, "devtasks", None)
    app.include_router(router)
    if cfg is None or not cfg.enabled:
        return

    @app.on_event("startup")
    async def _start_devtasks() -> None:
        approvals = None
        try:
            from openjarvis.tools.approval_store import ApprovalStore

            approvals = ApprovalStore()
        except Exception:  # noqa: BLE001
            logger.warning("devtasks: approval store unavailable", exc_info=True)
        runner = DevTaskRunner(
            cfg,
            bus=getattr(app.state, "bus", None),
            approvals=approvals,
            audit=getattr(app.state, "audit_logger", None),
            memory_backend=getattr(app.state, "memory_backend", None),
        )
        await runner.start()
        app.state.devtasks = runner
        from openjarvis.devtasks.routines import RoutineScheduler

        scheduler = RoutineScheduler(runner, cfg.routines, cfg.timezone)
        await scheduler.start()
        app.state.devtask_routines = scheduler
        from openjarvis.devtasks.notify import TaskNotifier
        from openjarvis.server.ws_bridge import connected_clients

        notifier = TaskNotifier(
            runner,
            bus=getattr(app.state, "bus", None),
            macos=cfg.notify_macos,
            clients=connected_clients,
        )
        await notifier.start()
        app.state.devtask_notifier = notifier
        logger.info(
            "devtasks: %d project(s) registered%s",
            len(runner.registry.list()),
            f"; ignored: {runner.registry.errors}" if runner.registry.errors else "",
        )

    @app.on_event("shutdown")
    async def _stop_devtasks() -> None:
        notifier = getattr(app.state, "devtask_notifier", None)
        if notifier is not None:
            await notifier.stop()
        scheduler = getattr(app.state, "devtask_routines", None)
        if scheduler is not None:
            await scheduler.stop()
        runner = getattr(app.state, "devtasks", None)
        if runner is not None:
            await runner.stop()


__all__ = ["router", "setup_devtasks"]
