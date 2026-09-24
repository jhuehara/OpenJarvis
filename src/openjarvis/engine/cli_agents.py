"""Engines backed by coding-agent CLIs already installed on this computer.

``claude_code`` drives the Claude Code CLI (``claude -p``) and ``codex``
drives the OpenAI Codex CLI (``codex exec``). Both reuse whatever login the
CLI already has (a Claude or ChatGPT subscription, or an API key), so
OpenJarvis needs no API key of its own. Inference still happens on the
provider's servers; only the CLI process runs locally.

Model ids are ``claudecode`` / ``claudecode:<model>`` and ``codex`` /
``codex:<model>``; the part after the colon is passed to the CLI's
``--model`` flag. The ids deliberately avoid the ``claude-`` / ``gpt-``
prefixes, which the server routes straight to the cloud APIs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._base import (
    EngineConnectionError,
    InferenceEngine,
    estimate_prompt_tokens,
)

logger = logging.getLogger(__name__)


def _render_prompt(messages: Sequence[Message]) -> Tuple[str, str]:
    """Split *messages* into ``(system_prompt, prompt)`` for a one-shot CLI.

    A single user turn is passed through verbatim; longer conversations are
    rendered as a transcript so the CLI sees the whole history.
    """
    system = "\n\n".join(
        m.content or "" for m in messages if m.role == Role.SYSTEM and m.content
    )
    turns = [m for m in messages if m.role != Role.SYSTEM]
    if len(turns) == 1 and turns[0].role == Role.USER:
        return system, turns[0].content or ""

    labels = {Role.USER: "User", Role.ASSISTANT: "Assistant", Role.TOOL: "Tool"}
    lines = ["Conversation so far:"]
    for m in turns:
        label = labels.get(m.role, m.role.value)
        if m.role == Role.TOOL and m.name:
            label = f"Tool result ({m.name})"
        lines.append(f"{label}: {m.content or ''}")
    lines.append("Reply to the last User message as the Assistant.")
    return system, "\n\n".join(lines)


def _usage(
    messages: Sequence[Message], prompt_tokens: int, completion_tokens: int
) -> Dict[str, int]:
    prompt_tokens = max(prompt_tokens, estimate_prompt_tokens(messages))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


class _CliAgentEngine(InferenceEngine):
    """Shared plumbing: locate the binary, run it, map model ids."""

    _binary_name: str = ""
    _model_prefix: str = ""
    _extra_models: Tuple[str, ...] = ()

    def __init__(
        self,
        *,
        binary: str = "",
        timeout: int = 300,
        workspace: str = "",
        extra_args: str = "",
    ) -> None:
        self._binary = binary or self._binary_name
        self._timeout = timeout
        self._workspace = Path(
            workspace or Path.home() / ".openjarvis" / "cli_workspace"
        ).expanduser()
        self._extra_args = shlex.split(extra_args) if extra_args else []

    # -- InferenceEngine ------------------------------------------------

    def health(self) -> bool:
        return self._resolve_binary() is not None

    def list_models(self) -> List[str]:
        return [self._model_prefix] + [
            f"{self._model_prefix}:{m}" for m in self._extra_models
        ]

    def can_serve(self, model: str) -> bool:
        return model == self._model_prefix or model.startswith(f"{self._model_prefix}:")

    # -- helpers --------------------------------------------------------

    def _resolve_binary(self) -> Optional[str]:
        return shutil.which(os.path.expanduser(self._binary))

    def _cli_model(self, model: str) -> str:
        """Return the model to pass to ``--model`` ("" = the CLI's default)."""
        if model.startswith(f"{self._model_prefix}:"):
            return model.split(":", 1)[1]
        return ""

    def _prepare_run(self) -> Tuple[str, str]:
        binary = self._resolve_binary()
        if binary is None:
            raise EngineConnectionError(
                f"'{self._binary}' CLI not found on PATH. Install it and log in "
                f"once (run '{self._binary_name}' interactively) before using the "
                f"{self.engine_id!r} engine."
            )
        self._workspace.mkdir(parents=True, exist_ok=True)
        return binary, str(self._workspace)

    def _run(self, argv: List[str], stdin: str) -> subprocess.CompletedProcess:
        _, cwd = self._prepare_run()
        try:
            return subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=cwd,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineConnectionError(
                f"{self._binary_name} timed out after {self._timeout}s"
            ) from exc


@EngineRegistry.register("claude_code")
class ClaudeCodeEngine(_CliAgentEngine):
    """Inference through the Claude Code CLI (``claude -p``)."""

    engine_id = "claude_code"
    _binary_name = "claude"
    _model_prefix = "claudecode"
    _extra_models = ("sonnet", "opus", "haiku")

    def __init__(self, *, tools: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Built-in Claude Code tools to enable ("" = plain chat, no tools).
        self._tools = tools

    def _argv(self, binary: str, model: str, system: str, stream: bool) -> List[str]:
        argv = [binary, "-p", "--no-session-persistence", "--tools", self._tools]
        if self._tools:
            argv += ["--allowedTools", self._tools]
        if stream:
            argv += [
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
            ]
        else:
            argv += ["--output-format", "json"]
        cli_model = self._cli_model(model)
        if cli_model:
            argv += ["--model", cli_model]
        if system:
            argv += ["--system-prompt", system]
        return argv + self._extra_args

    @staticmethod
    def _parse_result(
        data: Dict[str, Any], messages: Sequence[Message], model: str
    ) -> Dict[str, Any]:
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            raise EngineConnectionError(
                f"claude returned an error: {data.get('result') or data.get('subtype')}"
            )
        usage = data.get("usage") or {}
        prompt_tokens = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        return {
            "content": data.get("result") or "",
            "usage": _usage(messages, prompt_tokens, usage.get("output_tokens", 0)),
            "model": model,
            "finish_reason": "stop",
            "cost_usd": data.get("total_cost_usd", 0.0),
        }

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        binary, _ = self._prepare_run()
        system, prompt = _render_prompt(messages)
        proc = self._run(self._argv(binary, model, system, stream=False), prompt)
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout).strip()[:500]
            raise EngineConnectionError(
                f"claude exited with code {proc.returncode}: {detail}"
            ) from exc
        return self._parse_result(data, messages, model)

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        binary, cwd = self._prepare_run()
        system, prompt = _render_prompt(messages)
        proc = await asyncio.create_subprocess_exec(
            *self._argv(binary, model, system, stream=True),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            limit=16 * 1024 * 1024,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        streamed = False
        result: Optional[Dict[str, Any]] = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        try:
            while True:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), max(deadline - loop.time(), 0.001)
                )
                if not raw:
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        streamed = True
                        yield delta["text"]
                elif event.get("type") == "result":
                    result = event
            await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), 0.001))
        except asyncio.TimeoutError as exc:
            raise EngineConnectionError(
                f"claude timed out after {self._timeout}s"
            ) from exc
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

        if result is None:
            stderr = (await proc.stderr.read()).decode("utf-8", "replace")
            raise EngineConnectionError(
                f"claude exited with code {proc.returncode}: {stderr.strip()[:500]}"
            )
        parsed = self._parse_result(result, messages, model)
        if not streamed and parsed["content"]:
            yield parsed["content"]


@EngineRegistry.register("codex")
class CodexEngine(_CliAgentEngine):
    """Inference through the OpenAI Codex CLI (``codex exec``)."""

    engine_id = "codex"
    _binary_name = "codex"
    _model_prefix = "codex"

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        binary, _ = self._prepare_run()
        system, prompt = _render_prompt(messages)
        if system:
            prompt = f"{system}\n\n{prompt}"

        with tempfile.TemporaryDirectory(prefix="openjarvis-codex-") as tmp:
            last_message = Path(tmp) / "last_message.txt"
            argv = [
                binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-last-message",
                str(last_message),
            ]
            cli_model = self._cli_model(model)
            if cli_model:
                argv += ["--model", cli_model]
            argv += self._extra_args + ["-"]
            proc = self._run(argv, prompt)
            content = (
                last_message.read_text(encoding="utf-8").strip()
                if last_message.exists()
                else ""
            )

        usage: Dict[str, Any] = {}
        messages_out: List[str] = []
        error = ""
        for raw in proc.stdout.splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "turn.completed":
                usage = event.get("usage") or {}
            elif kind == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    messages_out.append(item["text"])
            elif kind in ("error", "turn.failed"):
                error = event.get("message") or str(event.get("error") or event)

        content = content or (messages_out[-1] if messages_out else "")
        if not content:
            detail = error or (proc.stderr or proc.stdout).strip()[:500]
            raise EngineConnectionError(
                f"codex exited with code {proc.returncode}: {detail}"
            )
        return {
            "content": content,
            "usage": _usage(
                messages,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
            ),
            "model": model,
            "finish_reason": "stop",
        }

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        # codex exec emits whole messages, not token deltas.
        result = await asyncio.to_thread(
            self.generate,
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        yield result["content"]


__all__ = ["ClaudeCodeEngine", "CodexEngine"]
