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
        logger.info(
            "devtasks: %d project(s) registered%s",
            len(runner.registry.list()),
            f"; ignored: {runner.registry.errors}" if runner.registry.errors else "",
        )

    @app.on_event("shutdown")
    async def _stop_devtasks() -> None:
        runner = getattr(app.state, "devtasks", None)
        if runner is not None:
            await runner.stop()


__all__ = ["router", "setup_devtasks"]
