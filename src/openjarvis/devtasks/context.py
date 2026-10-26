"""Short, cached status of each registered project for Jarvis' context.

Gives the chat model project awareness ("o MyOps tem 3 arquivos alterados na
branch X; último commit há 2 h…") without running an agent.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

_CACHE: Dict[str, Tuple[float, str]] = {}
TTL_S = 60.0


def _git(path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(path), capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def project_status(project: Any) -> str:
    """One compact line block for a project (cached for ``TTL_S`` seconds)."""
    key = str(project.path)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < TTL_S:
        return cached[1]
    path = Path(project.path)
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    dirty = [line for line in _git(path, "status", "--porcelain").splitlines() if line]
    commits = _git(path, "log", "-3", "--format=%h %s (%cr)").splitlines()
    open_branches = [
        b.strip()
        for b in _git(
            path, "branch", "--list", "jarvis/*", "--format=%(refname:short)"
        ).splitlines()
        if b.strip()
    ]
    lines = [
        f"- {project.name}"
        + (f" ({project.description})" if project.description else "")
        + f": branch atual {branch}, "
        + (
            f"{len(dirty)} arquivo(s) alterado(s) sem commit"
            if dirty
            else "sem alterações pendentes"
        )
    ]
    if commits:
        lines.append("  últimos commits: " + "; ".join(commits))
    if open_branches:
        lines.append(f"  branches de agentes: {', '.join(open_branches[:5])}")
    text = "\n".join(lines)
    _CACHE[key] = (time.time(), text)
    return text


def projects_context(projects: Iterable[Any]) -> str:
    items: List[str] = [project_status(p) for p in projects]
    if not items:
        return ""
    return "## Estado dos projetos (git, atualizado a cada minuto)\n" + "\n".join(items)


def repo_digest(project: Any, since_hours: int = 24) -> str:
    """Facts an agent needs to summarise or review a repository.

    Computed by the server (plain ``git``) so read-only agents don't need
    shell access: current branch, uncommitted changes, recent commits on every
    branch and local branches not merged into the base, with commits ahead.
    """
    path = Path(project.path)
    base = getattr(project, "base_branch", "main") or "main"
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    dirty = [line for line in _git(path, "status", "--porcelain").splitlines() if line]
    recent = _git(
        path,
        "log",
        "--all",
        f"--since={since_hours} hours ago",
        "--format=%h %ad [%D] %s",
        "--date=format:%d/%m %H:%M",
    ).splitlines()
    branches = []
    for name in _git(path, "branch", "--format=%(refname:short)").splitlines():
        name = name.strip()
        if not name or name == base:
            continue
        ahead = _git(path, "rev-list", "--count", f"{base}..{name}") or "?"
        if ahead == "0":
            continue
        last = _git(path, "log", "-1", "--format=%cr: %s", name)
        branches.append(
            f"  - {name}: {ahead} commit(s) à frente de {base} (último {last})"
        )
    lines = [
        f"### {project.name} ({path})",
        f"Branch atual: {branch}. Base: {base}.",
        f"Alterações sem commit: {len(dirty)}"
        + (
            " — " + ", ".join(d.split(maxsplit=1)[-1] for d in dirty[:15])
            if dirty
            else ""
        ),
        f"Commits nas últimas {since_hours} h ({len(recent)}):",
        *(f"  - {c}" for c in recent[:40]),
        f"Branches não incorporadas à {base} ({len(branches)}):",
        *branches[:30],
    ]
    return "\n".join(lines)
