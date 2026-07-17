"""Configuration loading for agent-runner.

Supports two layers, merged in increasing precedence:

1. Built-in defaults (``DEFAULT_CONFIG``)
2. Project-local ``agent-runner.toml`` (or ``[tool.agent-runner]`` in ``pyproject.toml``)
3. Environment variables (``AGENT_RUNNER_*``)

The config object also doubles as the **agent registry** — agents are not
hardcoded in the CLI anymore; they're declared in config so new agents can
be added without code changes.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_AGENTS: dict[str, dict[str, Any]] = {
    "claude": {
        "command": ["claude"],
        "description": "Anthropic Claude Code (default)",
    },
    "cursor": {
        "command": ["cursor"],
        "description": "Cursor agent",
    },
    "codex": {
        "command": ["codex"],
        "description": "OpenAI Codex CLI",
    },
    "pr-agent": {
        "command": ["pr-agent"],
        "description": "pr-agent review tool (used in review/improve stages)",
    },
}


@dataclass(slots=True)
class AgentSpec:
    """A registered AI agent."""

    name: str
    command: list[str]
    description: str = ""


@dataclass(slots=True)
class Config:
    """Resolved runtime configuration."""

    default_agent: str = "claude"
    poll_interval: int = 10
    default_timeout: int = 300
    subprocess_timeout: int = 120
    retry_attempts: int = 3
    retry_backoff: float = 1.5
    history_max_bytes: int = 0
    history_max_age_days: int = 0
    agents: dict[str, AgentSpec] = field(default_factory=dict)
    config_path: Path | None = None

    def get_agent(self, name: str) -> AgentSpec:
        if name not in self.agents:
            raise KeyError(f"Unknown agent {name!r}. Registered: {', '.join(sorted(self.agents))}")
        return self.agents[name]

    @property
    def agent_names(self) -> list[str]:
        return sorted(self.agents)


def _load_toml(path: Path) -> dict[str, Any]:
    """Load config from a TOML file.

    Two layouts are supported:

    - ``agent-runner.toml`` — top-level keys (``default_agent = "..."``)
    - ``pyproject.toml`` — ``[tool.agent-runner]`` table

    The caller decides which file it loaded; this helper just extracts the
    relevant section. For ``agent-runner.toml`` the whole file is the section.
    For ``pyproject.toml`` we read ``[tool.agent-runner]``.
    """
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data


def _merge_agents(user_agents: dict[str, Any]) -> dict[str, AgentSpec]:
    merged: dict[str, AgentSpec] = {}
    for name, spec in DEFAULT_AGENTS.items():
        merged[name] = AgentSpec(name=name, **spec)
    for name, spec in user_agents.items():
        if not isinstance(spec, dict):
            continue
        cmd = spec.get("command")
        if isinstance(cmd, str):
            cmd = [cmd]
        elif not isinstance(cmd, list):
            continue
        merged[name] = AgentSpec(
            name=name,
            command=[str(c) for c in cmd],
            description=str(spec.get("description", "")),
        )
    return merged


def load_config(
    project_root: Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Config:
    """Resolve configuration from disk + env.

    Args:
        project_root: directory to search for ``agent-runner.toml`` / ``pyproject.toml``.
            Falls back to ``.`` if None.
        env: override for ``os.environ`` (useful for tests).
    """
    env = env if env is not None else dict(os.environ)
    root = (project_root or Path(".")).resolve()
    cfg_path: Path | None = None
    toml_data: dict[str, Any] = {}

    candidate = root / "agent-runner.toml"
    if candidate.is_file():
        cfg_path = candidate
        # Standalone config file: keys live at the top level.
        toml_data = _load_toml(candidate)
    else:
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            cfg_path = pyproject
            data = _load_toml(pyproject)
            tool = data.get("tool") if isinstance(data, dict) else None
            toml_data = tool.get("agent-runner", {}) if isinstance(tool, dict) else {}
        else:
            toml_data = {}

    agents = _merge_agents(toml_data.get("agents", {}))

    def _env_int(name: str, default: int) -> int:
        raw = env.get(name)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    def _env_float(name: str, default: float) -> float:
        raw = env.get(name)
        try:
            return float(raw) if raw is not None else default
        except ValueError:
            return default

    return Config(
        default_agent=str(toml_data.get("default_agent", env.get("AGENT_RUNNER_AGENT", "claude"))),
        poll_interval=_env_int(
            "AGENT_RUNNER_POLL_INTERVAL", int(toml_data.get("poll_interval", 10))
        ),
        default_timeout=_env_int("AGENT_RUNNER_TIMEOUT", int(toml_data.get("timeout", 300))),
        subprocess_timeout=_env_int(
            "AGENT_RUNNER_SUBPROCESS_TIMEOUT", int(toml_data.get("subprocess_timeout", 120))
        ),
        retry_attempts=_env_int(
            "AGENT_RUNNER_RETRY_ATTEMPTS", int(toml_data.get("retry_attempts", 3))
        ),
        retry_backoff=_env_float(
            "AGENT_RUNNER_RETRY_BACKOFF", float(toml_data.get("retry_backoff", 1.5))
        ),
        history_max_bytes=_env_int(
            "AGENT_RUNNER_HISTORY_MAX_BYTES",
            int(toml_data.get("history_max_bytes", 0)),
        ),
        history_max_age_days=_env_int(
            "AGENT_RUNNER_HISTORY_MAX_AGE_DAYS",
            int(toml_data.get("history_max_age_days", 0)),
        ),
        agents=agents,
        config_path=cfg_path,
    )
