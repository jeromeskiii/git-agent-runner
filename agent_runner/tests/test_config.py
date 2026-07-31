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
    assert cfg.history_max_bytes == 10 * 1024 * 1024  # rotation on by default
    assert cfg.agents["claude"].args == ["-p", "--permission-mode", "acceptEdits"]
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


def test_default_timeout_toml_key(tmp_path: Path) -> None:
    """Regression: the documented `default_timeout` key was silently ignored."""
    (tmp_path / "agent-runner.toml").write_text("default_timeout = 600\n")
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.default_timeout == 600


def test_deprecated_timeout_alias_still_works(tmp_path: Path) -> None:
    (tmp_path / "agent-runner.toml").write_text("timeout = 999\n")
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.default_timeout == 999


def test_env_beats_toml_for_default_agent(tmp_path: Path) -> None:
    """README documents env as highest precedence — for every key."""
    (tmp_path / "agent-runner.toml").write_text('default_agent = "cursor"\n')
    cfg = load_config(project_root=tmp_path, env={"AGENT_RUNNER_AGENT": "codex"})
    assert cfg.default_agent == "codex"


def test_agent_timeout_default_and_env(tmp_path: Path) -> None:
    assert load_config(project_root=tmp_path, env={}).agent_timeout == 1800
    cfg = load_config(project_root=tmp_path, env={"AGENT_RUNNER_AGENT_TIMEOUT": "60"})
    assert cfg.agent_timeout == 60


def test_agent_args_from_toml(tmp_path: Path) -> None:
    (tmp_path / "agent-runner.toml").write_text(
        '[agents.claude]\ncommand = ["claude"]\nargs = ["-p", "--verbose"]\n'
    )
    cfg = load_config(project_root=tmp_path, env={})
    assert cfg.agents["claude"].args == ["-p", "--verbose"]


def test_unknown_toml_key_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    (tmp_path / "agent-runner.toml").write_text("bogus_key = 1\ndefault_timeout = 600\n")
    # agent_runner loggers set propagate=False, so attach caplog's handler
    # directly instead of relying on root-logger propagation.
    logger = logging.getLogger("agent_runner.config")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="agent_runner.config"):
            cfg = load_config(project_root=tmp_path, env={})
    finally:
        logger.removeHandler(caplog.handler)
    assert cfg.default_timeout == 600
    assert any("bogus_key" in r.getMessage() for r in caplog.records)
