# ruff: noqa: E501
"""Routine scheduling: first sighting, due runs, catch-up window, fan-out."""

import asyncio
from types import SimpleNamespace

from openjarvis.devtasks.routines import RoutineScheduler
from openjarvis.devtasks.store import TaskStore


class FakeRunner:
    def __init__(self, tmp_path, projects=("A", "B")):
        self.store = TaskStore(tmp_path / "tasks.db")
        self.registry = SimpleNamespace(
            list=lambda: [SimpleNamespace(name=p) for p in projects]
        )
        self.submitted = []

    async def submit(self, kind, project, prompt, title="", routine=""):
        self.submitted.append((kind, project, title, routine))
        return SimpleNamespace(to_dict=lambda: {"project": project})


def _routine(**kw):
    base = dict(
        name="Resumo",
        cron="0 8 * * *",
        kind="resumo",
        project="",
        prompt="p",
        enabled=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_routine_waits_for_schedule_then_fires_once(tmp_path):
    runner = FakeRunner(tmp_path)
    now = [1_800_000_000.0]
    sched = RoutineScheduler(runner, [_routine()], "UTC", clock=lambda: now[0])
    asyncio.run(sched.tick())
    assert runner.submitted == []  # first sighting only records the time
    nxt = sched.next_fire(sched.routines[0])
    now[0] = nxt + 60
    asyncio.run(sched.tick())
    asyncio.run(sched.tick())  # same window: no duplicate
    assert runner.submitted == [("resumo", "", "Resumo", "Resumo")]


def test_missed_by_more_than_12h_is_skipped(tmp_path):
    runner = FakeRunner(tmp_path)
    now = [1_800_000_000.0]
    sched = RoutineScheduler(runner, [_routine()], "UTC", clock=lambda: now[0])
    asyncio.run(sched.tick())
    now[0] = sched.next_fire(sched.routines[0]) + 13 * 3600
    asyncio.run(sched.tick())
    assert runner.submitted == []


def test_star_project_fans_out_and_disabled_never_runs(tmp_path):
    runner = FakeRunner(tmp_path)
    sched = RoutineScheduler(
        runner,
        [
            _routine(name="Rev", kind="revisao", project="*"),
            _routine(name="Off", enabled=False),
        ],
        "UTC",
    )
    asyncio.run(sched.fire(sched.find("rev")))
    assert [s[1] for s in runner.submitted] == ["A", "B"]
    assert runner.submitted[0][2] == "Rev — A"
    desc = {d["name"]: d for d in sched.describe()}
    assert desc["Off"]["next_fire"] is None


def test_skipped_routine_is_reported(tmp_path):
    class Refusing(FakeRunner):
        async def submit(self, *a, **kw):
            raise RuntimeError("Teto diário atingido")

    runner = Refusing(tmp_path)
    sched = RoutineScheduler(runner, [_routine(project="X")], "UTC")
    skipped = []

    async def on_skip(name, project, reason):
        skipped.append((name, project, reason))

    sched.on_skip = on_skip
    assert asyncio.run(sched.fire(sched.routines[0])) == []
    assert skipped == [("Resumo", "X", "Teto diário atingido")]
