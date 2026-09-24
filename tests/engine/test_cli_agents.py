"""Tests for the CLI-backed engines (claude_code, codex) using fake binaries."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._base import EngineConnectionError
from openjarvis.engine._discovery import _make_engine
from openjarvis.engine.cli_agents import ClaudeCodeEngine, CodexEngine, _render_prompt

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="fake CLIs are POSIX shebang scripts"
)


def _fake_cli(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable Python script that records its argv and stdin."""
    path = tmp_path / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"_log = {str(tmp_path / (name + '.log'))!r}\n"
        "_stdin = sys.stdin.read()\n"
        "json.dump({'argv': sys.argv[1:], 'stdin': _stdin}, open(_log, 'w'))\n"
        + textwrap.dedent(body)
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _calls(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / f"{name}.log").read_text())


_CLAUDE_BODY = """
if "stream-json" in sys.argv:
    for text in ("Hel", "lo"):
        print(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text}}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "Hello", "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 3, "cache_read_input_tokens": 2000,
                            "output_tokens": 5}}))
"""

_CODEX_BODY = """
out = sys.argv[sys.argv.index("--output-last-message") + 1]
open(out, "w").write("Hi from codex")
print(json.dumps({"type": "item.completed",
                  "item": {"type": "agent_message", "text": "Hi from codex"}}))
print(json.dumps({"type": "turn.completed",
                  "usage": {"input_tokens": 1500, "output_tokens": 7}}))
"""

_MSGS = [
    Message(role=Role.SYSTEM, content="You are Jarvis."),
    Message(role=Role.USER, content="My name is Ana."),
    Message(role=Role.ASSISTANT, content="Hi Ana!"),
    Message(role=Role.USER, content="What is my name?"),
]


def test_render_prompt_single_turn_is_verbatim() -> None:
    system, prompt = _render_prompt([Message(role=Role.USER, content="oi")])
    assert (system, prompt) == ("", "oi")


def test_render_prompt_keeps_history() -> None:
    system, prompt = _render_prompt(_MSGS)
    assert system == "You are Jarvis."
    assert "User: My name is Ana." in prompt
    assert "Assistant: Hi Ana!" in prompt
    assert prompt.index("Ana.") < prompt.index("What is my name?")


def test_model_ids_and_routing(tmp_path: Path) -> None:
    eng = ClaudeCodeEngine(binary=_fake_cli(tmp_path, "claude", _CLAUDE_BODY))
    assert eng.health()
    assert "claudecode:sonnet" in eng.list_models()
    assert eng.can_serve("claudecode") and eng.can_serve("claudecode:opus")
    assert not eng.can_serve("qwen3.5:4b")
    assert not eng.can_serve("claude-sonnet-4-6")
    codex = CodexEngine(binary=_fake_cli(tmp_path, "codex", _CODEX_BODY))
    assert codex.can_serve("codex:gpt-5") and not codex.can_serve("claudecode")


def test_missing_binary_is_unhealthy(tmp_path: Path) -> None:
    eng = ClaudeCodeEngine(binary=str(tmp_path / "nope"))
    assert not eng.health()
    with pytest.raises(EngineConnectionError, match="not found"):
        eng.generate(_MSGS, model="claudecode")


def test_claude_generate(tmp_path: Path) -> None:
    eng = ClaudeCodeEngine(
        binary=_fake_cli(tmp_path, "claude", _CLAUDE_BODY),
        workspace=str(tmp_path / "ws"),
    )
    result = eng.generate(_MSGS, model="claudecode:haiku")
    assert result["content"] == "Hello"
    assert result["usage"]["prompt_tokens"] == 2003
    assert result["usage"]["completion_tokens"] == 5

    call = _calls(tmp_path, "claude")
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--system-prompt") + 1] == "You are Jarvis."
    assert argv[argv.index("--tools") + 1] == ""
    assert "--allowedTools" not in argv
    assert "What is my name?" in call["stdin"]
    assert (tmp_path / "ws").is_dir()


def test_claude_default_model_omits_flag(tmp_path: Path) -> None:
    eng = ClaudeCodeEngine(
        binary=_fake_cli(tmp_path, "claude", _CLAUDE_BODY), tools="WebSearch"
    )
    eng.generate([Message(role=Role.USER, content="oi")], model="claudecode")
    argv = _calls(tmp_path, "claude")["argv"]
    assert "--model" not in argv and "--system-prompt" not in argv
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"


def test_claude_error_result_raises(tmp_path: Path) -> None:
    body = """
print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                  "result": "Not logged in"}))
"""
    eng = ClaudeCodeEngine(binary=_fake_cli(tmp_path, "claude", body))
    with pytest.raises(EngineConnectionError, match="Not logged in"):
        eng.generate(_MSGS, model="claudecode")


def test_claude_stream(tmp_path: Path) -> None:
    eng = ClaudeCodeEngine(binary=_fake_cli(tmp_path, "claude", _CLAUDE_BODY))

    async def collect() -> list[str]:
        return [t async for t in eng.stream(_MSGS, model="claudecode")]

    assert asyncio.run(collect()) == ["Hel", "lo"]


def test_codex_generate(tmp_path: Path) -> None:
    eng = CodexEngine(binary=_fake_cli(tmp_path, "codex", _CODEX_BODY))
    result = eng.generate(_MSGS, model="codex:gpt-5")
    assert result["content"] == "Hi from codex"
    assert result["usage"]["prompt_tokens"] == 1500
    assert result["usage"]["completion_tokens"] == 7

    call = _calls(tmp_path, "codex")
    argv = call["argv"]
    assert argv[0] == "exec" and argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("--model") + 1] == "gpt-5"
    assert call["stdin"].startswith("You are Jarvis.")


def test_codex_failure_raises(tmp_path: Path) -> None:
    body = """
print(json.dumps({"type": "error", "message": "Not signed in"}))
sys.exit(1)
"""
    eng = CodexEngine(binary=_fake_cli(tmp_path, "codex", body))
    with pytest.raises(EngineConnectionError, match="Not signed in"):
        eng.generate(_MSGS, model="codex")


def test_make_engine_uses_config(tmp_path: Path) -> None:
    # conftest clears registries between tests; re-register for discovery.
    EngineRegistry.register_value("claude_code", ClaudeCodeEngine)
    EngineRegistry.register_value("codex", CodexEngine)
    cfg = JarvisConfig()
    cfg.engine.claude_code.binary = _fake_cli(tmp_path, "claude", _CLAUDE_BODY)
    cfg.engine.claude_code.tools = "WebFetch"
    cfg.engine.claude_code.timeout = 42
    eng = _make_engine("claude_code", cfg)
    assert isinstance(eng, ClaudeCodeEngine)
    assert eng.health() and eng._tools == "WebFetch" and eng._timeout == 42
    assert isinstance(_make_engine("codex", cfg), CodexEngine)
