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

from .logging_utils import get_logger

log = get_logger("config")

DEFAULT_AGENTS: dict[str, dict[str, Any]] = {
    "claude": {
        "command": ["claude"],
        # Headless print mode + auto-accept file edits inside the worktree
        # (other prompts auto-deny in -p mode). Use
        # "--dangerously-skip-permissions" instead only if you accept fully
        # unattended actions with your credentials.
        "args": ["-p", "--permission-mode", "acceptEdits"],
        "description": "Anthropic Claude Code (default)",
    },
    "cursor": {
        "command": ["cursor"],
        "args": [],
        "description": "Cursor agent",
    },
    "codex": {
        "command": ["codex"],
        # Headless exec mode: edits in the worktree + network (push/gh),
        # no full-sandbox bypass.
        "args": [
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
        ],
        "description": "OpenAI Codex CLI",
    },
    "pr-agent": {
        "command": ["pr-agent"],
        "args": [],
        "description": "pr-agent review tool (used in review/improve stages)",
    },
}

# Top-level keys understood from agent-runner.toml / [tool.agent-runner].
# Anything else is warned about and ignored — silent dead keys have already
# caused one misconfiguration bug (``default_timeout`` vs ``timeout``).
_KNOWN_KEYS = frozenset(
    {
        "default_agent",
        "poll_interval",
        "default_timeout",
        "timeout",  # deprecated alias of default_timeout
        "subprocess_timeout",
        "agent_timeout",
        "retry_attempts",
        "retry_backoff",
        "review_iterations",
        "cleanup_on_error",
        "history_max_bytes",
        "history_max_age_days",
        "agents",
    }
)


@dataclass(slots=True)
class AgentSpec:
    """A registered AI agent.

    ``args`` are passed to the agent (after gtr's ``--`` pass-through) right
    before the task text, e.g. ``claude -p "<task>"`` for headless operation.
    """

    name: str
    command: list[str]
    args: list[str] = field(default_factory=list)
    description: str = ""


@dataclass(slots=True)
class Config:
    """Resolved runtime configuration."""

    default_agent: str = "claude"
    poll_interval: int = 10
    default_timeout: int = 300
    subprocess_timeout: int = 120
    agent_timeout: int = 1800
    retry_attempts: int = 3
    retry_backoff: float = 1.5
    review_iterations: int = 1
    cleanup_on_error: bool = False
    history_max_bytes: int = 10 * 1024 * 1024  # rotate runs.jsonl at 10 MB
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
    def _str_list(value: Any) -> list[str] | None:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(v) for v in value]
        return None

    merged: dict[str, AgentSpec] = {}
    for name, spec in DEFAULT_AGENTS.items():
        merged[name] = AgentSpec(name=name, **spec)
    for name, spec in user_agents.items():
        if not isinstance(spec, dict):
            continue
        cmd = _str_list(spec.get("command"))
        if cmd is None:
            continue
        merged[name] = AgentSpec(
            name=name,
            command=cmd,
            args=_str_list(spec.get("args")) or [],
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

    for key in toml_data:
        if key not in _KNOWN_KEYS:
            log.warning(
                "config: ignoring unknown key %r in %s",
                key,
                cfg_path,
            )

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

    def _env_bool(name: str, default: bool) -> bool:
        raw = env.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    return Config(
        # Env vars take precedence over TOML for every key (README order).
        default_agent=str(
            env.get("AGENT_RUNNER_AGENT") or toml_data.get("default_agent", "claude")
        ),
        poll_interval=_env_int(
            "AGENT_RUNNER_POLL_INTERVAL", int(toml_data.get("poll_interval", 10))
        ),
        default_timeout=_env_int(
            "AGENT_RUNNER_TIMEOUT",
            int(toml_data.get("default_timeout", toml_data.get("timeout", 300))),
        ),
        subprocess_timeout=_env_int(
            "AGENT_RUNNER_SUBPROCESS_TIMEOUT", int(toml_data.get("subprocess_timeout", 120))
        ),
        agent_timeout=_env_int(
            "AGENT_RUNNER_AGENT_TIMEOUT", int(toml_data.get("agent_timeout", 1800))
        ),
        retry_attempts=_env_int(
            "AGENT_RUNNER_RETRY_ATTEMPTS", int(toml_data.get("retry_attempts", 3))
        ),
        retry_backoff=_env_float(
            "AGENT_RUNNER_RETRY_BACKOFF", float(toml_data.get("retry_backoff", 1.5))
        ),
        review_iterations=_env_int(
            "AGENT_RUNNER_REVIEW_ITERATIONS", int(toml_data.get("review_iterations", 1))
        ),
        cleanup_on_error=_env_bool(
            "AGENT_RUNNER_CLEANUP_ON_ERROR", bool(toml_data.get("cleanup_on_error", False))
        ),
        history_max_bytes=_env_int(
            "AGENT_RUNNER_HISTORY_MAX_BYTES",
            int(toml_data.get("history_max_bytes", 10 * 1024 * 1024)),
        ),
        history_max_age_days=_env_int(
            "AGENT_RUNNER_HISTORY_MAX_AGE_DAYS",
            int(toml_data.get("history_max_age_days", 0)),
        ),
        agents=agents,
        config_path=cfg_path,
    )
