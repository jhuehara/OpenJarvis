"""Registry of the repositories agents may work in.

Only projects listed under ``[[devtasks.projects]]`` are usable, and each one
is validated: absolute path, no symlinks, an existing git checkout. Anything
else is refused, so a prompt can never point an agent at another directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


class ProjectError(ValueError):
    """Unknown or invalid project."""


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    base_branch: str = "main"
    pr_base: str = "main"
    test_cmd: str = ""
    description: str = ""
    env: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "base_branch": self.base_branch,
            "pr_base": self.pr_base,
            "test_cmd": self.test_cmd,
            "description": self.description,
        }


def _get(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def validate_path(raw: str) -> Path:
    """Return the checkout path, or raise :class:`ProjectError`."""
    if not raw:
        raise ProjectError("path is empty")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProjectError(f"path must be absolute: {raw}")
    if ".." in path.parts:
        raise ProjectError(f"path must not contain '..': {raw}")
    if path.resolve() != path:
        raise ProjectError(f"path must not go through symlinks: {raw}")
    if not path.is_dir():
        raise ProjectError(f"not a directory: {raw}")
    if not (path / ".git").exists():
        raise ProjectError(f"not a git checkout: {raw}")
    return path


class ProjectRegistry:
    """Validated, case-insensitive lookup of registered projects."""

    def __init__(self, projects: Iterable[Any] = ()) -> None:
        self._projects: Dict[str, Project] = {}
        self.errors: List[str] = []
        for item in projects:
            name = str(_get(item, "name") or "").strip()
            try:
                if not name:
                    raise ProjectError("project without a name")
                path = validate_path(str(_get(item, "path") or ""))
            except ProjectError as exc:
                self.errors.append(f"{name or '?'}: {exc}")
                logger.warning("devtasks: ignoring project %r: %s", name, exc)
                continue
            base = str(_get(item, "base_branch") or "main")
            self._projects[name.lower()] = Project(
                name=name,
                path=path,
                base_branch=base,
                pr_base=str(_get(item, "pr_base") or base),
                test_cmd=str(_get(item, "test_cmd") or ""),
                description=str(_get(item, "description") or ""),
                env=dict(_get(item, "env", {}) or {}),
            )

    def list(self) -> List[Project]:
        return list(self._projects.values())

    def find(self, name: str) -> Optional[Project]:
        return self._projects.get((name or "").strip().lower())

    def resolve(self, name: str) -> Project:
        """Registered project by name, re-validated on every call."""
        project = self.find(name)
        if project is None:
            known = ", ".join(p.name for p in self.list()) or "nenhum"
            raise ProjectError(
                f"Projeto '{name}' não está cadastrado (cadastrados: {known})"
            )
        validate_path(str(project.path))  # the checkout may have moved since
        return project
