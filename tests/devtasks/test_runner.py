# ruff: noqa: E501 — the fake claude script below emits long JSON lines
"""End-to-end runner tests with a fake ``claude`` binary and a bare remote."""

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openjarvis.core.config import DevTasksConfig
from openjarvis.devtasks import gitops
from openjarvis.devtasks.runner import BudgetError, DevTaskRunner

FAKE_CLAUDE = textwrap.dedent(
    """\
    #!{python}
    import json, os, subprocess, sys, time
    prompt = sys.stdin.read()
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_MODE", "edit")
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({{"args": args, "prompt": prompt, "cwd": os.getcwd()}}) + "\\n")
    def out(obj):
        print(json.dumps(obj), flush=True)
    out({{"type": "system", "subtype": "init", "session_id": "sess-1"}})
    if mode == "slow":
        time.sleep(30)
    if mode == "switch":  # a disobedient agent that commits on its own branch
        subprocess.run(["git", "switch", "-q", "-c", "minha-branch"], check=True)
        with open("NOVO.md", "w") as f:
            f.write("novo\\n")
        subprocess.run(["git", "add", "NOVO.md"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "novo"], check=True)
    result_text = "Criei o README."
    if mode == "check_ok":
        result_text = "Todos os 3 testes passaram.\\nSTATUS: OK"
    if mode == "check_fail":
        result_text = "test_x falhou: AssertionError.\\nSTATUS: FALHOU"
    if mode == "summary":
        result_text = "Resumo: nada mudou."
    if mode == "edit":
        out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Write", "input": {{"file_path": os.path.join(os.getcwd(), "README.md")}}}}]}}}})
        with open("README.md", "w") as f:
            f.write("# Projeto\\n" + prompt + "\\n")
        out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}}]}}}})
    out({{"type": "result", "subtype": "success", "is_error": False, "result": result_text, "session_id": "sess-1", "total_cost_usd": 0.12, "num_turns": 2}})
    """
)


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    ident = ["-c", "user.name=T", "-c", "user.email=t@t"]
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "app.py").write_text("print('oi')\n")
    _git("add", ".", cwd=repo)
    _git(*ident, "commit", "-q", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(remote), cwd=repo)
    _git("push", "-q", "-u", "origin", "main", cwd=repo)

    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setattr(gitops, "gh_ready", lambda: False)

    def make(mode="edit", **over):
        cfg = DevTasksConfig(
            claude_binary=str(fake),
            worktree_root=str(tmp_path / "wt"),
            reports_dir=str(tmp_path / "reports"),
            db_path=str(tmp_path / "tasks.db"),
            projects=[
                {
                    "name": "Demo",
                    "path": str(repo.resolve()),
                    "base_branch": "main",
                    "env": {"FAKE_MODE": mode, "FAKE_LOG": str(log)},
                }
            ],
        )
        for k, v in over.items():
            setattr(cfg, k, v)
        return DevTaskRunner(cfg)

    return {"make": make, "repo": repo, "remote": remote, "log": log}


async def _wait(runner, task_id, statuses, timeout=20):
    for _ in range(int(timeout / 0.1)):
        task = runner.store.get(task_id)
        if task.status in statuses:
            return task
        await asyncio.sleep(0.1)
    raise AssertionError(f"status still {runner.store.get(task_id).status}")


def test_code_task_branch_then_approve_pushes(env):
    async def scenario():
        runner = env["make"]()
        await runner.start()
        try:
            task = await runner.submit("codigo", "demo", "adiciona um README")
            done = await _wait(runner, task.id, {"awaiting_approval", "failed"})
            assert done.status == "awaiting_approval", done.error
            assert done.branch.startswith("jarvis/adiciona-um-readme-")
            assert done.files == ["README.md"] and done.cost_usd == 0.12
            assert done.session_id == "sess-1" and "README" in done.summary
            # the user's checkout is untouched
            assert not (env["repo"] / "README.md").exists()
            assert _git("branch", "--show-current", cwd=env["repo"]).strip() == "main"
            kinds = [e["kind"] for e in runner.store.events(task.id)]
            assert kinds[:2] == ["tool_start", "tool_end"]
            # nothing pushed before approval
            assert done.branch not in _git("branch", cwd=env["remote"])

            published = await runner.approve(task.id)
            assert published.status == "done"
            assert done.branch in _git("branch", cwd=env["remote"])
            assert not Path(done.worktree).exists()
        finally:
            await runner.stop()

    asyncio.run(scenario())

    call = json.loads(env["log"].read_text().splitlines()[0])
    args = call["args"]
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "Bash(git push:*)" in args and "Bash(gh:*)" in args
    assert call["prompt"] == "adiciona um README"


def test_deny_keeps_branch_local_and_followup_resumes(env):
    async def scenario():
        runner = env["make"]()
        await runner.start()
        try:
            task = await runner.submit("codigo", "Demo", "adiciona um README")
            await _wait(runner, task.id, {"awaiting_approval"})
            await runner.followup(task.id, "agora adiciona uma licença")
            again = await _wait(runner, task.id, {"awaiting_approval", "failed"})
            assert again.status == "awaiting_approval"
            assert again.cost_usd == pytest.approx(0.24)
            denied = await runner.deny(task.id)
            assert denied.status == "done" and "negada" in denied.error
            assert again.branch not in _git("branch", cwd=env["remote"])
            assert again.branch in _git("branch", cwd=env["repo"])
        finally:
            await runner.stop()

    asyncio.run(scenario())
    second = json.loads(env["log"].read_text().splitlines()[1])
    assert second["args"][-2:] == ["--resume", "sess-1"]
    assert second["prompt"] == "agora adiciona uma licença"


def test_cancel_and_timeout(env):
    async def scenario():
        runner = env["make"]("slow", timeout_s=1)
        await runner.start()
        try:
            t1 = await runner.submit("codigo", "demo", "tarefa lenta")
            failed = await _wait(runner, t1.id, {"failed"}, timeout=15)
            assert "Tempo limite" in failed.error
            runner.cfg.timeout_s = 60
            t2 = await runner.submit("codigo", "demo", "outra lenta")
            await _wait(runner, t2.id, {"running"})
            await asyncio.sleep(0.5)
            cancelled = await runner.cancel(t2.id)
            assert cancelled.status == "cancelled"
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_rejects_unknown_project_and_budget(env):
    async def scenario():
        runner = env["make"](daily_budget_usd=0.0)
        await runner.start()
        try:
            with pytest.raises(Exception, match="não está cadastrado"):
                await runner.submit("codigo", "outro", "x")
            with pytest.raises(BudgetError):
                await runner.submit("codigo", "demo", "x")
            forced = await runner.submit("codigo", "demo", "x", force=True)
            assert forced.status in ("queued", "running")
            with pytest.raises(KeyError):
                await runner.approve("nope")
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_agent_switching_branch_still_publishes_its_work(env):
    async def scenario():
        runner = env["make"]("switch")
        await runner.start()
        try:
            task = await runner.submit("codigo", "demo", "cria NOVO.md")
            done = await _wait(runner, task.id, {"awaiting_approval", "failed"})
            assert done.status == "awaiting_approval", done.error
            assert "NOVO.md" in done.files
            await runner.approve(task.id)
            log = _git("log", "--format=%s", done.branch, cwd=env["remote"])
            assert "novo" in log.splitlines()
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_approve_refuses_branch_without_commits(env):
    async def scenario():
        runner = env["make"]()
        await runner.start()
        try:
            task = await runner.submit("codigo", "demo", "adiciona README")
            done = await _wait(runner, task.id, {"awaiting_approval"})
            wt = Path(done.worktree)
            _git("reset", "-q", "--hard", "main", cwd=wt)  # simulate lost work
            with pytest.raises(ValueError, match="sem commits|não tem commits"):
                await runner.approve(task.id)
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_check_task_is_silent_when_tests_pass_and_saves_report(env):
    async def scenario():
        runner = env["make"]("check_ok")
        await runner.start()
        try:
            task = await runner.submit(
                "checagem", "demo", "rode os testes", routine="Checagem"
            )
            done = await _wait(runner, task.id, {"done", "failed"})
            assert done.status == "done" and done.silent and done.routine == "Checagem"
            assert not Path(done.worktree).exists()  # temporary checkout removed
            report = Path(done.report)
            assert report.is_file() and "STATUS: OK" in report.read_text()
            assert not done.branch  # no branch for checks
        finally:
            await runner.stop()

    asyncio.run(scenario())
    args = json.loads(env["log"].read_text().splitlines()[0])["args"]
    assert "Edit" in args[args.index("--disallowedTools") :]


def test_failed_check_is_not_silent(env):
    async def scenario():
        runner = env["make"]("check_fail")
        await runner.start()
        try:
            task = await runner.submit("checagem", "demo", "rode os testes")
            done = await _wait(runner, task.id, {"done", "failed"})
            assert done.status == "done" and not done.silent
        finally:
            await runner.stop()

    asyncio.run(scenario())


def test_summary_gets_git_context_and_project_dirs(env):
    async def scenario():
        runner = env["make"]("summary")
        await runner.start()
        try:
            task = await runner.submit("resumo", "demo", "resumo diário")
            done = await _wait(runner, task.id, {"done", "failed"})
            assert done.status == "done" and Path(done.report).is_file()
        finally:
            await runner.stop()

    asyncio.run(scenario())
    call = json.loads(env["log"].read_text().splitlines()[0])
    assert "--add-dir" in call["args"]
    assert "Contexto de git" in call["prompt"] and "### Demo" in call["prompt"]
