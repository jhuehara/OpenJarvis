"""Project registry validation and stream-json parsing."""

import subprocess
from pathlib import Path

import pytest

from openjarvis.devtasks.projects import ProjectError, ProjectRegistry, validate_path
from openjarvis.devtasks.steps import describe_step, parse_line

SAMPLE = Path(__file__).with_name("claude_stream_sample.jsonl")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_registry_accepts_only_valid_checkouts(tmp_path):
    good = _repo(tmp_path / "good")
    (tmp_path / "plain").mkdir()
    link = tmp_path / "link"
    link.symlink_to(good)
    reg = ProjectRegistry(
        [
            {"name": "Good", "path": str(good), "test_cmd": "pytest"},
            {"name": "plain", "path": str(tmp_path / "plain")},
            {"name": "rel", "path": "relative/path"},
            {"name": "link", "path": str(link)},
            {"name": "", "path": str(good)},
        ]
    )
    assert [p.name for p in reg.list()] == ["Good"]
    assert len(reg.errors) == 4
    assert reg.resolve("good").test_cmd == "pytest"  # case-insensitive
    with pytest.raises(ProjectError, match="não está cadastrado"):
        reg.resolve("other")


def test_validate_path_rejects_dotdot(tmp_path):
    good = _repo(tmp_path / "good")
    with pytest.raises(ProjectError):
        validate_path(str(good / ".." / "good"))


def test_parse_real_stream_sample():
    events = [ev for line in SAMPLE.read_text().splitlines() for ev in parse_line(line)]
    kinds = [k for k, _ in events]
    assert kinds[0] == "init"
    assert "denied" in kinds and "tool_start" in kinds and "tool_end" in kinds
    result = dict(events)["result"]
    assert result["session_id"] and result["cost_usd"] > 0 and not result["is_error"]
    write = [p for k, p in events if k == "tool_start" and p["tool"] == "Write"][0]
    assert write["step"] == "criando ola.txt"


@pytest.mark.parametrize(
    "tool,args,expected",
    [
        ("Edit", {"file_path": "/w/src/app.py"}, "editando src/app.py"),
        ("Bash", {"command": "uv run pytest -q tests"}, "rodando testes"),
        ("Bash", {"command": "git commit -m x"}, "registrando commit"),
        ("Grep", {"pattern": "def main"}, "procurando “def main”"),
        (
            "WebSearch",
            {"query": "fastapi websockets"},
            "pesquisando “fastapi websockets”",
        ),
    ],
)
def test_describe_step(tool, args, expected):
    assert describe_step(tool, args, "/w") == expected


def test_parse_ignores_garbage():
    assert parse_line("not json") == []
    assert parse_line("[1, 2]") == []
