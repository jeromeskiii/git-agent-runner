"""Tests for config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runner.config import load_config


def test_defaults(tmp_path: Path) -> None:
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.default_agent == "claude"
    assert cfg.poll_interval == 10
    assert cfg.default_timeout == 300
    assert cfg.subprocess_timeout == 120
    assert "claude" in cfg.agents
    assert "pr-agent" in cfg.agents
    assert cfg.config_path is None


def test_pyproject_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.agent-runner]\n"
        'default_agent = "codex"\n'
        "poll_interval = 5\n"
        "subprocess_timeout = 60\n"
        "retry_attempts = 2\n"
        "retry_backoff = 2.0\n"
        "\n"
        "[tool.agent-runner.agents.kiro]\n"
        'command = ["kiro", "--yes"]\n'
        'description = "Kiro CLI"\n'
    )
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.default_agent == "codex"
    assert cfg.poll_interval == 5
    assert cfg.subprocess_timeout == 60
    assert cfg.retry_attempts == 2
    assert cfg.retry_backoff == 2.0
    assert "kiro" in cfg.agents
    assert cfg.agents["kiro"].command == ["kiro", "--yes"]
    assert cfg.config_path == tmp_path / "pyproject.toml"


def test_agent_runner_toml_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / "agent-runner.toml").write_text('default_agent = "cursor"\n')
    (tmp_path / "pyproject.toml").write_text('[tool.agent-runner]\ndefault_agent = "codex"\n')
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.default_agent == "cursor"
    assert cfg.config_path == tmp_path / "agent-runner.toml"


def test_env_overrides(tmp_path: Path) -> None:
    cfg = load_config(
        project_root=tmp_path,
        env={"AGENT_RUNNER_AGENT": "codex", "AGENT_RUNNER_TIMEOUT": "600"},
    )
    assert cfg.default_agent == "codex"
    assert cfg.default_timeout == 600


def test_env_int_invalid_falls_back(tmp_path: Path) -> None:
    cfg = load_config(
        project_root=tmp_path,
        env={"AGENT_RUNNER_TIMEOUT": "notanumber"},
    )
    assert cfg.default_timeout == 300


def test_get_agent_unknown_raises() -> None:
    cfg = load_config(project_root=Path("/tmp"), env={})
    with pytest.raises(KeyError, match="Unknown agent"):
        cfg.get_agent("nope")


def test_missing_config_file_is_fine(tmp_path: Path) -> None:
    # No pyproject.toml / agent-runner.toml → still works.
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.config_path is None


def test_malformed_toml_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "agent-runner.toml").write_text("this is = = invalid [toml")
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.default_agent == "claude"  # fell back to default
