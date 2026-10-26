"""Scheduled dev tasks (``[[devtasks.routines]]``) fed into the same runner queue.

Routines never run alongside other tasks: they are submitted to the runner like
any delegated task, so the one-agent-at-a-time limit still applies. A run
missed while the Mac was asleep or the server was down is caught up once if it
is less than 12 hours late; older misses are skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from openjarvis.agents.scheduler import _next_cron_fire

logger = logging.getLogger(__name__)

CATCH_UP_S = 12 * 3600


class RoutineScheduler:
    def __init__(
        self,
        runner: Any,
        routines: List[Any],
        timezone: str = "",
        *,
        poll_s: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.runner = runner
        self.routines = [r for r in routines if getattr(r, "name", "")]
        self.timezone = timezone or None
        self.poll_s = poll_s
        self.clock = clock
        self._task: Optional[asyncio.Task] = None
        # async callback(routine_name, project, reason) when a run can't start
        self.on_skip: Optional[Callable[..., Any]] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def find(self, name: str) -> Any:
        for routine in self.routines:
            if routine.name.lower() == name.strip().lower():
                return routine
        raise KeyError(name)

    def next_fire(self, routine: Any) -> Optional[float]:
        try:
            last = self.runner.store.last_fire(routine.name) or self.clock()
            return _next_cron_fire(routine.cron, last, self.timezone)
        except Exception:  # noqa: BLE001 — invalid cron expression
            return None

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": r.name,
                "cron": r.cron,
                "kind": r.kind,
                "project": r.project,
                "enabled": r.enabled,
                "last_fire": self.runner.store.last_fire(r.name),
                "next_fire": self.next_fire(r) if r.enabled else None,
            }
            for r in self.routines
        ]

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — keep scheduling
                logger.exception("routine tick failed")
            await asyncio.sleep(self.poll_s)

    async def tick(self) -> List[Any]:
        """Submit every routine that is due. Returns the created tasks."""
        now = self.clock()
        created: List[Any] = []
        for routine in self.routines:
            if not routine.enabled or not routine.cron:
                continue
            store = self.runner.store
            last = store.last_fire(routine.name)
            if not last:  # first sighting: start counting from now
                store.set_last_fire(routine.name, now)
                continue
            due = self.next_fire(routine)
            if due is None or due > now:
                continue
            store.set_last_fire(routine.name, now)
            if now - due > CATCH_UP_S:
                logger.info(
                    "routine %r missed (%.0f h late); skipped",
                    routine.name,
                    (now - due) / 3600,
                )
                continue
            created += await self.fire(routine)
        return created

    async def fire(self, routine: Any) -> List[Any]:
        if routine.project == "*":
            targets = [p.name for p in self.runner.registry.list()]
        else:
            targets = [routine.project]
        created = []
        for project in targets:
            title = routine.name + (f" — {project}" if routine.project == "*" else "")
            try:
                created.append(
                    await self.runner.submit(
                        routine.kind,
                        project,
                        routine.prompt or routine.name,
                        title=title,
                        routine=routine.name,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — budget, unknown project...
                logger.warning(
                    "routine %r (%s) not started: %s", routine.name, project, exc
                )
                if self.on_skip is not None:
                    with contextlib.suppress(Exception):
                        await self.on_skip(routine.name, project, str(exc))
        return created
