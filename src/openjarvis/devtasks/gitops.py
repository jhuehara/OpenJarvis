"""Git plumbing for dev tasks: worktrees, residual commits, diffs, publishing.

All functions are synchronous (run them with ``asyncio.to_thread``) and use
argv lists — never a shell — so branch names and messages cannot inject.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class GitError(RuntimeError):
    pass


def slugify(text: str, max_len: int = 32) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (slug[:max_len].rstrip("-")) or "tarefa"


def git(
    args: List[str],
    cwd: Path | str,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:400]
        raise GitError(f"git {' '.join(args[:2])} falhou: {detail}")
    return proc.stdout


def identity_env(name: str, email: str) -> Dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }


def create_worktree(
    repo: Path, root: Path, project: str, task_id: str, slug: str, base: str
) -> Tuple[Path, str]:
    """``git worktree add -b jarvis/<slug>-<id> <root>/<project>/<id> <base>``."""
    branch = f"jarvis/{slug}-{task_id[:4]}"
    path = (root / slugify(project) / task_id).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-b", branch, str(path), base], cwd=repo)
    return path, branch


def create_detached_worktree(
    repo: Path, root: Path, project: str, task_id: str, base: str
) -> Path:
    """Throwaway checkout of ``base`` (no branch) for read-only checks."""
    path = (root / slugify(project) / task_id).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "--detach", str(path), base], cwd=repo)
    return path


def remove_worktree(repo: Path, path: Path | str) -> None:
    try:
        git(["worktree", "remove", "--force", str(path)], cwd=repo)
    except GitError:
        git(["worktree", "prune"], cwd=repo)


def commit_pending(worktree: Path, message: str, env: Dict[str, str]) -> bool:
    """Commit whatever the agent left uncommitted. True if a commit was made."""
    if not git(["status", "--porcelain"], cwd=worktree).strip():
        return False
    git(["add", "-A"], cwd=worktree)
    git(["commit", "-m", message], cwd=worktree, env=env)
    return True


def ensure_branch(worktree: Path, branch: str) -> bool:
    """Put HEAD on ``branch``, carrying the agent's commits with it.

    If the agent switched to (or created) another branch, ``checkout -B``
    moves the task branch to the current HEAD. Returns True if it had to fix it.
    """
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree).strip()
    if current == branch:
        return False
    git(["checkout", "-B", branch], cwd=worktree)
    return True


def commits_ahead(worktree: Path, base: str) -> int:
    out = git(["rev-list", "--count", f"{base}..HEAD"], cwd=worktree).strip()
    return int(out or 0)


def diff_stat(worktree: Path, base: str) -> Tuple[str, List[str]]:
    stat = git(["diff", "--stat", f"{base}...HEAD"], cwd=worktree).strip()
    files = [
        f
        for f in git(["diff", "--name-only", f"{base}...HEAD"], cwd=worktree).split(
            "\n"
        )
        if f
    ]
    return stat, files


def diff_file(worktree: Path, base: str, file: str) -> str:
    """Full diff of one changed file; the path must stay inside the worktree."""
    target = (worktree / file).resolve()
    if not target.is_relative_to(worktree.resolve()):
        raise GitError("arquivo fora do worktree")
    return git(["diff", f"{base}...HEAD", "--", file], cwd=worktree)[:200_000]


def github_compare_url(repo: Path, base: str, branch: str) -> str:
    try:
        url = git(["remote", "get-url", "origin"], cwd=repo).strip()
    except GitError:
        return ""
    m = re.match(
        r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/.]+?)(?:\.git)?$", url
    )
    return (
        f"https://github.com/{m.group(1)}/compare/{base}...{branch}?expand=1"
        if m
        else ""
    )


def gh_ready() -> bool:
    try:
        return (
            subprocess.run(
                ["gh", "auth", "status"], capture_output=True, timeout=15
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def publish(
    worktree: Path, repo: Path, branch: str, pr_base: str, title: str, body: str
) -> Dict[str, str]:
    """Push the branch and open a PR (if gh is logged in). Server-side only."""
    git(["push", "-u", "origin", branch], cwd=worktree, timeout=180)
    result = {
        "pushed": "true",
        "pr_url": "",
        "compare_url": github_compare_url(repo, pr_base, branch),
    }
    if gh_ready():
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                pr_base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            result["pr_url"] = (
                proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
            )
        else:
            result["pr_error"] = (proc.stderr or proc.stdout).strip()[:400]
    else:
        result["pr_error"] = "gh sem login (rode: gh auth login)"
    return result
