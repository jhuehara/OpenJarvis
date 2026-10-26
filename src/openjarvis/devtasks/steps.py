"""Turn Claude Code ``stream-json`` lines into task events and human phrases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _rel(path: str, cwd: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(cwd).resolve()))
    except (ValueError, OSError):
        return Path(path).name or path


def describe_step(tool: str, args: Dict[str, Any], cwd: str = "") -> str:
    """Short pt-BR phrase for the HUD, e.g. "editando src/app.py"."""
    a = args or {}
    path = a.get("file_path") or a.get("path") or a.get("notebook_path") or ""
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        verb = "criando" if tool == "Write" else "editando"
        return f"{verb} {_rel(path, cwd)}" if path else verb
    if tool == "Read":
        return f"lendo {_rel(path, cwd)}" if path else "lendo arquivos"
    if tool == "Grep":
        return f"procurando “{_short(a.get('pattern', ''), 30)}”"
    if tool == "Glob":
        return f"listando {_short(a.get('pattern', ''), 30)}"
    if tool == "Bash":
        cmd = str(a.get("command", ""))
        low = cmd.lower()
        if any(
            k in low
            for k in (
                "pytest",
                "npm test",
                "npm run test",
                "vitest",
                "jest",
                "go test",
                "cargo test",
            )
        ):
            return "rodando testes"
        if "git commit" in low:
            return "registrando commit"
        if low.startswith(("git diff", "git status", "git log")):
            return "revisando mudanças"
        if any(k in low for k in ("ruff", "eslint", "lint", "mypy", "tsc")):
            return "checando o código"
        return f"executando {_short(cmd, 40)}"
    if tool == "WebSearch":
        return f"pesquisando “{_short(a.get('query', ''), 40)}”"
    if tool == "WebFetch":
        return f"lendo {_short(a.get('url', ''), 40)}"
    if tool in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "organizando o plano"
    if tool in ("Task", "Agent"):
        return "delegando a um sub-agente"
    return f"usando {tool}"


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def parse_line(line: str, cwd: str = "") -> List[Tuple[str, Dict[str, Any]]]:
    """Events from one stream-json line: ``[(kind, payload), ...]``.

    kinds: ``init`` (session_id), ``text``, ``tool_start``, ``tool_end``,
    ``result`` (session_id, cost, summary, is_error, turns).
    """
    try:
        msg = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(msg, dict):
        return []
    kind = msg.get("type")
    out: List[Tuple[str, Dict[str, Any]]] = []
    if kind == "system" and msg.get("subtype") == "init":
        out.append(("init", {"session_id": msg.get("session_id", "")}))
    elif kind == "system" and msg.get("subtype") == "permission_denied":
        out.append(
            (
                "denied",
                {"id": msg.get("tool_use_id", ""), "tool": msg.get("tool_name", "")},
            )
        )
    elif kind == "assistant":
        for block in (msg.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                out.append(("text", {"text": _short(block["text"], 2000)}))
            elif block.get("type") == "tool_use":
                args = block.get("input") or {}
                out.append(
                    (
                        "tool_start",
                        {
                            "id": block.get("id", ""),
                            "tool": block.get("name", ""),
                            "step": describe_step(block.get("name", ""), args, cwd),
                            "detail": _short(
                                args.get("command")
                                or args.get("file_path")
                                or args.get("pattern")
                                or args.get("query")
                                or args.get("url")
                                or "",
                                300,
                            ),
                        },
                    )
                )
    elif kind == "user":
        for block in (msg.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _result_text(block.get("content"))
                out.append(
                    (
                        "tool_end",
                        {
                            "id": block.get("tool_use_id", ""),
                            "ok": not block.get("is_error", False),
                            "output": text[-1500:],
                        },
                    )
                )
    elif kind == "result":
        out.append(
            (
                "result",
                {
                    "session_id": msg.get("session_id", ""),
                    "cost_usd": float(msg.get("total_cost_usd") or 0.0),
                    "summary": str(msg.get("result") or ""),
                    "is_error": bool(msg.get("is_error")),
                    "subtype": msg.get("subtype", ""),
                    "turns": int(msg.get("num_turns") or 0),
                },
            )
        )
    return out


def iter_lines(data: str) -> Iterator[str]:
    for line in data.splitlines():
        if line.strip():
            yield line
