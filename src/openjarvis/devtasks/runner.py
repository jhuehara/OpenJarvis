"""Queue + executor for dev tasks: one headless Claude Code agent per task.

Each coding task runs in its own git worktree on a ``jarvis/<slug>-<id>``
branch, so the user's working copy is never touched. The agent may edit, test
and commit; it can never push or open PRs — the server does that, and only
after the user approves (voice, button or another channel).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.devtasks import gitops
from openjarvis.devtasks.projects import Project, ProjectError, ProjectRegistry
from openjarvis.devtasks.steps import parse_line
from openjarvis.devtasks.store import Task, TaskStore

logger = logging.getLogger(__name__)

KINDS = ("codigo", "revisao", "pesquisa", "resumo", "checagem")
KIND_LABELS = {
    "codigo": "código",
    "revisao": "revisão",
    "pesquisa": "pesquisa",
    "resumo": "resumo",
    "checagem": "checagem de testes",
}
READ_TOOLS = ("Read", "Glob", "Grep")

# Commands a coding agent may run (prefix match, per sub-command). Deny rules
# below take precedence. This is a guardrail against mistakes and prompt
# injection, not a sandbox: language runtimes are allowed so tests can run.
SAFE_BASH = (
    "git",
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "grep",
    "rg",
    "sed",
    "awk",
    "sort",
    "uniq",
    "diff",
    "cut",
    "tr",
    "mkdir",
    "touch",
    "mv",
    "cp",
    "echo",
    "printf",
    "pwd",
    "which",
    "test",
    "true",
    "tree",
    "jq",
    "python",
    "python3",
    "pytest",
    "uv",
    "ruff",
    "mypy",
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "make",
    "cargo",
    "go",
)
DENY = (
    "Bash(git push:*)",
    "Bash(git remote:*)",
    "Bash(git config:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git worktree:*)",
    "Bash(git checkout:*)",
    "Bash(git switch:*)",
    "Bash(gh:*)",
    "Bash(rm:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(sudo:*)",
    "Bash(npm publish:*)",
    "Bash(uv publish:*)",
    "Bash(open:*)",
)
CODE_TOOLS = (
    "Read",
    "Edit",
    "MultiEdit",
    "Write",
    "Glob",
    "Grep",
    "TodoWrite",
    "WebSearch",
)
REVIEW_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git status:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
)
RESEARCH_TOOLS = ("WebSearch", "WebFetch", "Write", "Read", "Glob")

_RULES = {
    "codigo": (
        "Você é um agente de desenvolvimento trabalhando para o Jonatan, delegado "
        "pelo Jarvis. Trabalhe somente neste diretório (um git worktree numa branch"
        " própria). Entenda o código antes de mudar, faça a mudança pedida de forma"
        " mínima e limpa, rode os testes/lint relevantes quando existirem{test}, e "
        "registre o trabalho em commits com mensagens claras. Você NÃO pode fazer "
        "push nem abrir PR — o Jarvis cuida disso depois da aprovação do usuário. "
        "Ao terminar, responda em português do Brasil com um resumo curto: o que "
        "mudou, arquivos principais e o resultado dos testes. No terminal, use "
        "comandos simples, um por chamada (sem subshells com parênteses, $( ), "
        "nem cadeias longas com && ou |): comandos complexos são negados "
        "automaticamente. Se um comando for negado, tente uma forma mais simples — "
        "o terminal continua disponível para git, testes e ferramentas comuns. "
        "Você já está na branch certa da tarefa: não crie nem troque de branch. Não "
        "toque em caminhos fora deste diretório (são bloqueados)."
    ),
    "revisao": (
        "Você é um revisor de código experiente trabalhando para o Jonatan. Não "
        "altere nada. Revise o que foi pedido (branch, diff, commits ou PR), "
        "procurando bugs, riscos de segurança, casos de borda e simplificações. "
        "Responda em português do Brasil com uma revisão em Markdown: resumo, "
        "problemas por gravidade (com arquivo:linha) e sugestões."
    ),
    "pesquisa": (
        "Você é um pesquisador trabalhando para o Jonatan. Pesquise na internet com"
        " cuidado, cruze fontes e escreva o relatório completo em Markdown no "
        "arquivo relatorio.md deste diretório, com as fontes no fim. Depois "
        "responda em português do Brasil com um resumo curto (3 a 5 frases) das "
        "conclusões."
    ),
    "resumo": (
        "Você é o assistente de engenharia do Jonatan. Com base no contexto de git "
        "fornecido (gerado pelo Jarvis) e, se preciso, lendo arquivos dos projetos, "
        "escreva em português do Brasil um resumo curto e útil: o que mudou, o que "
        "está pendente (branches não publicadas, alterações sem commit) e o que "
        "merece atenção hoje. Use tópicos curtos por projeto. Não altere nada."
    ),
    "checagem": (
        "Você verifica a saúde de um projeto do Jonatan numa cópia temporária. "
        "Rode os testes{test} — se a suíte inteira for lenta demais, rode as pastas "
        "mais relevantes. Não altere arquivos. Use comandos simples, um por chamada. "
        "Responda em português do Brasil com um resumo curto do resultado e, se algo "
        "falhou, a causa provável de cada falha. Não toque em caminhos fora deste "
        "diretório (o comando de teste já está no PATH; use-o como está). Termine "
        "com uma linha exatamente 'STATUS: OK' se tudo passou, 'STATUS: FALHOU' se "
        "algum teste falhou, ou 'STATUS: NAO_RODOU' se não conseguiu rodar os testes."
    ),
}


class BudgetError(RuntimeError):
    """Daily spending cap reached."""


def _local_midnight() -> float:
    now = _dt.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class DevTaskRunner:
    def __init__(
        self,
        cfg: Any,
        *,
        store: Optional[TaskStore] = None,
        registry: Optional[ProjectRegistry] = None,
        bus: Any = None,
        approvals: Any = None,
        audit: Any = None,
        memory_backend: Any = None,
    ) -> None:
        self.cfg = cfg
        self.store = store or TaskStore(cfg.db_path)
        self.registry = registry or ProjectRegistry(cfg.projects)
        self.bus = bus
        self.approvals = approvals
        self.audit = audit
        self.memory_backend = memory_backend
        self.paused = False
        self._queue: Optional[asyncio.Queue] = None
        self._workers: List[asyncio.Task] = []
        self._procs: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._gh_ready: Optional[bool] = None

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        # Tasks interrupted by a server restart cannot be resumed mid-run.
        for task in self.store.list(limit=200):
            if task.status in ("queued", "running"):
                self._update(
                    task.id,
                    status="failed",
                    error="Interrompida: o servidor foi reiniciado.",
                    ended_at=time.time(),
                )
        for _ in range(max(1, int(self.cfg.max_concurrent))):
            self._workers.append(asyncio.create_task(self._worker()))
        self._gh_ready = await asyncio.to_thread(gitops.gh_ready)

    async def stop(self) -> None:
        for task_id in list(self._procs):
            await self._kill(task_id)
        for worker in self._workers:
            worker.cancel()
        self._workers.clear()

    # -- public API -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "paused": self.paused,
            "cost_today": round(self.store.cost_since(_local_midnight()), 4),
            "daily_budget": self.cfg.daily_budget_usd,
            "gh_ready": bool(self._gh_ready),
            "max_concurrent": self.cfg.max_concurrent,
        }

    async def submit(
        self,
        kind: str,
        project: str,
        prompt: str,
        title: str = "",
        routine: str = "",
    ) -> Task:
        kind = (kind or "codigo").strip().lower()
        if kind not in KINDS:
            raise ValueError(f"tipo inválido: {kind} (use {', '.join(KINDS)})")
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("pedido vazio")
        name = project
        if kind not in ("pesquisa", "resumo") or project:
            name = self.registry.resolve(project).name  # raises ProjectError
        self._check_budget()
        task = self.store.create(
            kind=kind, project=name or "", prompt=prompt, title=title, routine=routine
        )
        self._audit("create", task)
        self._publish_update(task)
        await self._enqueue(task.id)
        return task

    async def followup(self, task_id: str, prompt: str) -> Task:
        task = self._require(task_id)
        if task.status in ("queued", "running"):
            raise ValueError("a tarefa ainda está em andamento")
        if not task.session_id:
            raise ValueError("esta tarefa não tem sessão para retomar")
        if task.kind == "codigo" and not (
            task.worktree and Path(task.worktree).is_dir()
        ):
            raise ValueError("o worktree desta tarefa já foi removido")
        self._check_budget()
        if task.approval_id and self.approvals is not None:
            self.approvals.update_status(task.approval_id, "superseded")
        task = self._update(
            task_id, followup=prompt.strip(), status="queued", error="", approval_id=""
        )
        self._emit(task_id, "followup", {"text": prompt.strip()})
        await self._enqueue(task_id)
        return task

    async def cancel(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.status == "queued":
            self._cancelled.add(task_id)
        elif task.status == "running":
            self._cancelled.add(task_id)
            await self._kill(task_id)
        elif task.status == "awaiting_approval":
            return await self.deny(task_id)
        else:
            return task
        self._audit("cancel", task)
        return self._update(
            task_id, status="cancelled", ended_at=time.time(), current_step=""
        )

    async def killswitch(self) -> List[str]:
        self.paused = True
        stopped = []
        for task in self.store.list(limit=200):
            if task.status in ("queued", "running"):
                await self.cancel(task.id)
                stopped.append(task.id)
        self._audit_raw("killswitch", f"{len(stopped)} tarefas paradas")
        return stopped

    def resume(self) -> None:
        self.paused = False

    async def approve(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.status != "awaiting_approval":
            raise ValueError("esta tarefa não está aguardando aprovação")
        project = self.registry.resolve(task.project)  # re-validate before git
        worktree = Path(task.worktree)
        if not worktree.is_dir() or not worktree.resolve().is_relative_to(
            Path(self.cfg.worktree_root).expanduser().resolve()
        ):
            raise ValueError("worktree inválido")
        await asyncio.to_thread(gitops.ensure_branch, worktree, task.branch)
        ahead = await asyncio.to_thread(gitops.commits_ahead, worktree, project.pr_base)
        if ahead == 0:
            raise ValueError("a branch não tem commits novos para publicar")
        self._update(task_id, current_step="publicando")
        body = (
            f"{task.summary}\n\n---\nPedido: {task.prompt}\n\n"
            "Gerado por um agente do Jarvis (Claude Code) e aprovado pelo usuário."
        )
        try:
            result = await asyncio.to_thread(
                gitops.publish,
                worktree,
                project.path,
                task.branch,
                project.pr_base,
                task.title[:120],
                body,
            )
        except gitops.GitError as exc:
            self._audit("publish_failed", task)
            return self._update(task_id, error=str(exc), current_step="")
        if self.approvals is not None and task.approval_id:
            self.approvals.update_status(task.approval_id, "approved")
        url = result.get("pr_url") or result.get("compare_url", "")
        self._emit(task_id, "published", result)
        self._audit("publish", task, extra=url)
        await asyncio.to_thread(gitops.remove_worktree, project.path, worktree)
        task = self._update(
            task_id,
            status="done",
            pr_url=url,
            error=result.get("pr_error", ""),
            current_step="",
            ended_at=time.time(),
        )
        self._remember(
            task, f"Publicou a branch {task.branch}" + (f" — PR: {url}" if url else "")
        )
        return task

    async def deny(self, task_id: str) -> Task:
        task = self._require(task_id)
        if task.status != "awaiting_approval":
            raise ValueError("esta tarefa não está aguardando aprovação")
        if self.approvals is not None and task.approval_id:
            self.approvals.update_status(task.approval_id, "denied")
        project = self.registry.find(task.project)
        if project and task.worktree and Path(task.worktree).is_dir():
            await asyncio.to_thread(
                gitops.remove_worktree, project.path, Path(task.worktree)
            )
        self._audit("deny", task)
        task = self._update(
            task_id,
            status="done",
            current_step="",
            error=(
                f"Publicação negada; a branch {task.branch} ficou só no seu computador."
            ),
            ended_at=time.time(),
        )
        self._remember(task, "Publicação negada pelo usuário")
        return task

    def diff_file(self, task_id: str, file: str) -> str:
        task = self._require(task_id)
        if not task.worktree or not Path(task.worktree).is_dir():
            raise ValueError("worktree indisponível")
        return gitops.diff_file(Path(task.worktree), task.base, file)

    # -- internals --------------------------------------------------------

    def _require(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _check_budget(self) -> None:
        spent = self.store.cost_since(_local_midnight())
        if spent >= self.cfg.daily_budget_usd:
            raise BudgetError(
                f"Teto diário atingido (US$ {spent:.2f} de "
                f"US$ {self.cfg.daily_budget_usd:.2f})"
            )

    async def _enqueue(self, task_id: str) -> None:
        if self._queue is None:
            raise RuntimeError("runner not started")
        self._cancelled.discard(task_id)
        await self._queue.put(task_id)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            task_id = await self._queue.get()
            try:
                while self.paused and task_id not in self._cancelled:
                    await asyncio.sleep(1)
                if task_id in self._cancelled:
                    continue
                await self._run(task_id)
            except Exception as exc:  # noqa: BLE001 — a task must never kill the worker
                logger.exception("devtask %s crashed", task_id)
                self._update(
                    task_id,
                    status="failed",
                    error=str(exc)[:500],
                    ended_at=time.time(),
                    current_step="",
                )
            finally:
                self._queue.task_done()

    def _clean_env(self, project: Optional[Project]) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "USER": os.environ.get("USER", ""),
            "LANG": os.environ.get("LANG", "pt_BR.UTF-8"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        env.update(
            gitops.identity_env(self.cfg.git_author_name, self.cfg.git_author_email)
        )
        if project:
            env.update({k: str(v) for k, v in project.env.items()})
        return env

    def build_argv(
        self, task: Task, project: Optional[Project], resume: bool
    ) -> List[str]:
        if task.kind == "codigo":
            allowed = [*CODE_TOOLS, *(f"Bash({c}:*)" for c in SAFE_BASH)]
            if project and project.test_cmd:
                allowed.append(f"Bash({project.test_cmd}:*)")
            deny = [*DENY, "WebFetch"]
            test = (
                f" (comando de teste: {project.test_cmd})"
                if project and project.test_cmd
                else ""
            )
            rules = _RULES["codigo"].format(test=test)
        elif task.kind == "revisao":
            allowed = list(REVIEW_TOOLS)
            deny = [d for d in DENY if d != "Bash(gh:*)"] + [
                "Edit",
                "Write",
                "MultiEdit",
            ]
            rules = _RULES["revisao"]
        elif task.kind == "checagem":
            allowed = [*READ_TOOLS, "TodoWrite", *(f"Bash({c}:*)" for c in SAFE_BASH)]
            if project and project.test_cmd:
                allowed.append(f"Bash({project.test_cmd}:*)")
            deny = [*DENY, "Edit", "Write", "MultiEdit", "WebFetch", "WebSearch"]
            test = (
                f" com {project.test_cmd}"
                if project and project.test_cmd
                else " (descubra como o projeto é testado)"
            )
            rules = _RULES["checagem"].format(test=test)
        elif task.kind == "resumo":
            allowed = list(READ_TOOLS)
            deny = [*DENY, "Edit", "Write", "MultiEdit", "Bash", "WebFetch"]
            rules = _RULES["resumo"]
        else:
            allowed = list(RESEARCH_TOOLS)
            deny = [*DENY, "Edit", "MultiEdit", "Bash"]
            rules = _RULES["pesquisa"]
        argv = [
            self.cfg.claude_binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.cfg.model,
            "--permission-mode",
            "dontAsk",
            "--max-budget-usd",
            f"{float(self.cfg.budget_usd):.2f}",
            "--strict-mcp-config",
            "--allowedTools",
            *allowed,
            "--disallowedTools",
            *deny,
            "--append-system-prompt",
            rules,
        ]
        if task.kind == "resumo":
            for p in self._scope(task):
                argv += ["--add-dir", str(p.path)]
        if resume and task.session_id:
            argv += ["--resume", task.session_id]
        return argv

    async def _prepare_workspace(self, task: Task, project: Optional[Project]) -> Path:
        if task.worktree and Path(task.worktree).is_dir():
            return Path(task.worktree)
        if task.kind == "codigo":
            assert project is not None
            root = Path(self.cfg.worktree_root).expanduser()
            self._update(task.id, current_step="preparando a branch")
            path, branch = await asyncio.to_thread(
                gitops.create_worktree,
                project.path,
                root,
                project.name,
                task.id,
                gitops.slugify(task.title or task.prompt),
                project.base_branch,
            )
            self._update(
                task.id, worktree=str(path), branch=branch, base=project.base_branch
            )
            return path
        if task.kind == "revisao":
            assert project is not None
            return project.path  # read-only tools: the checkout is never modified
        if task.kind == "checagem":
            assert project is not None
            self._update(task.id, current_step="preparando uma cópia temporária")
            path = await asyncio.to_thread(
                gitops.create_detached_worktree,
                project.path,
                Path(self.cfg.worktree_root).expanduser(),
                project.name,
                task.id,
                project.base_branch,
            )
            self._update(task.id, worktree=str(path), base=project.base_branch)
            return path
        path = Path(self.cfg.reports_dir).expanduser() / task.id
        path.mkdir(parents=True, exist_ok=True)
        self._update(task.id, worktree=str(path))
        return path

    async def _run(self, task_id: str) -> None:
        task = self._require(task_id)
        project = self.registry.resolve(task.project) if task.project else None
        resume = bool(task.followup and task.session_id)
        prompt = task.followup if resume else task.prompt
        if not resume and task.kind in ("resumo", "revisao"):
            prompt += await asyncio.to_thread(self._git_context, task)
        self._update(
            task_id,
            status="running",
            started_at=task.started_at or time.time(),
            current_step="começando",
            error="",
        )
        self._audit("start", task)
        cwd = await self._prepare_workspace(task, project)
        task = self._require(task_id)

        argv = self.build_argv(task, project, resume)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._clean_env(project),
            start_new_session=True,  # own process group: cancel kills the whole tree
            limit=16 * 1024 * 1024,
        )
        self._procs[task_id] = proc
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        result: Dict[str, Any] = {}
        steps = task.steps
        tools: Dict[str, str] = {}
        timed_out = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(self.cfg.timeout_s)
        try:
            while True:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), max(deadline - loop.time(), 0.01)
                )
                if not raw:
                    break
                for kind, payload in parse_line(
                    raw.decode("utf-8", "replace"), str(cwd)
                ):
                    if kind == "init":
                        self._update(task_id, session_id=payload["session_id"])
                    elif kind == "tool_start":
                        steps += 1
                        tools[payload["id"]] = payload["tool"]
                        self._update(task_id, steps=steps, current_step=payload["step"])
                        self._emit(task_id, kind, payload)
                    elif kind == "tool_end":
                        payload["tool"] = tools.get(payload["id"], "")
                        if payload["tool"] != "Bash":
                            payload["output"] = payload["output"][-300:]
                        self._emit(task_id, kind, payload)
                    elif kind in ("text", "denied"):
                        self._emit(task_id, kind, payload)
                    elif kind == "result":
                        result = payload
            await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), 0.01))
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill(task_id)
        finally:
            self._procs.pop(task_id, None)

        if task_id in self._cancelled:
            return  # cancel() already recorded the final state
        stderr = (
            (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
        )
        cost = (task.cost_usd or 0.0) + float(result.get("cost_usd") or 0.0)
        fields: Dict[str, Any] = {
            "cost_usd": round(cost, 4),
            "steps": steps,
            "current_step": "",
            "followup": "",
        }
        if result.get("session_id"):
            fields["session_id"] = result["session_id"]
        if result.get("summary"):
            fields["summary"] = result["summary"][:8000]

        if timed_out or not result or result.get("is_error"):
            reason = (
                f"Tempo limite de {int(self.cfg.timeout_s // 60)} min atingido."
                if timed_out
                else (
                    result.get("summary")
                    or stderr.strip()[-400:]
                    or "o agente terminou sem resultado"
                )
            )
            if result.get("subtype", "").startswith("error_max_budget"):
                reason = (
                    f"Orçamento da tarefa (US$ {self.cfg.budget_usd:.2f}) esgotado."
                )
            self._update(
                task_id,
                status="failed",
                error=str(reason)[:800],
                ended_at=time.time(),
                **fields,
            )
            self._audit("fail", self._require(task_id))
            return

        if task.kind == "codigo":
            await self._finish_code(task_id, Path(cwd), project, fields)
        else:
            summary = fields.get("summary", "")
            body = summary
            if task.kind == "pesquisa":
                full = Path(cwd) / "relatorio.md"
                if full.is_file():
                    body = full.read_text(encoding="utf-8", errors="replace")
            if task.kind == "checagem":
                fields["silent"] = "STATUS: OK" in summary.upper() and (
                    "FALHOU" not in summary.upper()
                )
                if project is not None:
                    await asyncio.to_thread(gitops.remove_worktree, project.path, cwd)
            if body:
                fields["report"] = self._save_report(task, body)
            self._update(task_id, status="done", ended_at=time.time(), **fields)
            done = self._require(task_id)
            self._audit("end", done)
            self._remember(done, done.summary)

    async def _finish_code(
        self,
        task_id: str,
        worktree: Path,
        project: Optional[Project],
        fields: Dict[str, Any],
    ) -> None:
        assert project is not None
        task = self._require(task_id)
        env = gitops.identity_env(self.cfg.git_author_name, self.cfg.git_author_email)
        # The agent may have switched branches; the task branch must hold its work.
        if await asyncio.to_thread(gitops.ensure_branch, worktree, task.branch):
            self._emit(
                task_id,
                "text",
                {
                    "text": "(o agente trocou de branch; o trabalho foi "
                    f"para {task.branch})"
                },
            )
        await asyncio.to_thread(
            gitops.commit_pending, worktree, f"jarvis: {task.title[:72]}", env
        )
        ahead = await asyncio.to_thread(gitops.commits_ahead, worktree, task.base)
        stat, files = await asyncio.to_thread(gitops.diff_stat, worktree, task.base)
        fields.update(diff_stat=stat[-4000:], files=files[:200])
        if ahead == 0:
            await asyncio.to_thread(gitops.remove_worktree, project.path, worktree)
            self._update(task_id, status="done", ended_at=time.time(), **fields)
            done = self._require(task_id)
            self._audit("end", done)
            self._remember(done, done.summary or "Nenhuma alteração foi necessária.")
            return
        approval_id = ""
        if self.approvals is not None:
            action = self.approvals.queue_action(
                action_type="devtask_push_pr",
                description=(
                    f"Publicar {task.branch} e abrir PR em {project.name} "
                    f"({len(files)} arquivos)"
                ),
                payload={
                    "task_id": task_id,
                    "branch": task.branch,
                    "project": project.name,
                },
                permission_key=f"devtask:{project.name}",
                tier="high",
                ttl_hours=72,
            )
            approval_id = action.id
        self._update(
            task_id, status="awaiting_approval", approval_id=approval_id, **fields
        )
        waiting = self._require(task_id)
        self._audit("await_approval", waiting)
        self._remember(waiting, waiting.summary)

    def _scope(self, task: Task) -> List[Project]:
        """Projects a resumo covers: the named one, or all registered."""
        if task.project:
            found = self.registry.find(task.project)
            return [found] if found else []
        return self.registry.list()

    def _git_context(self, task: Task) -> str:
        from openjarvis.devtasks.context import repo_digest

        projects = (
            self._scope(task)
            if task.kind == "resumo"
            else ([self.registry.find(task.project)] if task.project else [])
        )
        digests = [repo_digest(p) for p in projects if p is not None]
        if not digests:
            return ""
        return "\n\n## Contexto de git (gerado pelo Jarvis)\n" + "\n\n".join(digests)

    def _save_report(self, task: Task, body: str) -> str:
        """Keep the result as Markdown in reports_dir; returns the file path."""
        folder = Path(self.cfg.reports_dir).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        now = _dt.datetime.now()
        path = folder / (
            f"{now:%Y-%m-%d-%H%M}-{task.kind}-{gitops.slugify(task.title)}.md"
        )
        where = f" · {task.project}" if task.project else ""
        origin = f" · rotina “{task.routine}”" if task.routine else ""
        path.write_text(
            f"# {task.title}\n\n_{KIND_LABELS.get(task.kind, task.kind)}{where}"
            f"{origin} · {now:%d/%m/%Y %H:%M} · tarefa {task.id}_\n\n{body.strip()}\n",
            encoding="utf-8",
        )
        return str(path)

    async def _kill(self, task_id: str) -> None:
        proc = self._procs.get(task_id)
        if proc is None or proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    # -- events, audit, memory -------------------------------------------

    def _update(self, task_id: str, **fields: Any) -> Task:
        task = self.store.update(task_id, **fields)
        assert task is not None
        self._publish_update(task)
        return task

    def _publish_update(self, task: Task) -> None:
        if self.bus is None:
            return
        from openjarvis.core.events import EventType

        with contextlib.suppress(Exception):
            self.bus.publish(EventType.DEVTASK_UPDATED, {"task": task.to_dict()})

    def _emit(self, task_id: str, kind: str, payload: Dict[str, Any]) -> None:
        seq = self.store.add_event(task_id, kind, payload)
        if self.bus is None:
            return
        from openjarvis.core.events import EventType

        with contextlib.suppress(Exception):
            self.bus.publish(
                EventType.DEVTASK_EVENT,
                {
                    "task_id": task_id,
                    "seq": seq,
                    "kind": kind,
                    "payload": payload,
                    "ts": time.time(),
                },
            )

    def _audit(self, action: str, task: Task, extra: str = "") -> None:
        self._audit_raw(
            action,
            f"devtask.{action} id={task.id} tipo={task.kind} projeto={task.project} "
            f"branch={task.branch} custo={task.cost_usd:.4f} {extra}".strip(),
        )

    def _audit_raw(self, action: str, preview: str) -> None:
        if self.audit is None:
            return
        try:
            from openjarvis.security.types import SecurityEvent, SecurityEventType

            self.audit.log(
                SecurityEvent(
                    event_type=SecurityEventType.TOOL_EXECUTED,
                    timestamp=time.time(),
                    content_preview=preview[:500],
                    action_taken=f"devtask.{action}",
                )
            )
        except Exception:  # noqa: BLE001 — auditing must not break tasks
            logger.debug("devtask audit failed", exc_info=True)

    def _remember(self, task: Task, outcome: str) -> None:
        """Log the task in the searchable activity memory (see galaxy.py)."""
        if self.memory_backend is None:
            return
        try:
            from openjarvis.server.galaxy import ACTIVITY_SOURCE, _local_now

            when = _local_now().strftime("%d/%m/%Y %H:%M")
            where = f" no projeto {task.project}" if task.project else ""
            self.memory_backend.store(
                f"Em {when}, um agente do Jarvis fez a tarefa ({task.kind}){where}: "
                f"{' '.join(task.prompt.split())[:400]} — resultado: "
                f"{' '.join((outcome or '').split())[:600]}"
                + (f" [branch {task.branch}]" if task.branch else ""),
                source=ACTIVITY_SOURCE,
                metadata={"kind": "devtask", "task_id": task.id, "status": task.status},
            )
        except Exception:  # noqa: BLE001
            logger.debug("devtask remember failed", exc_info=True)


__all__ = ["BudgetError", "DevTaskRunner", "KINDS", "ProjectError"]
