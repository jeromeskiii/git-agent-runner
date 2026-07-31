"""Orchestration engine — ties gtr + pr-agent + agent tools together.

Public functions in this module are the CLI's contract. They return
strongly-typed results and never raise for normal pipeline failures;
errors land in ``PipelineContext.errors`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, load_config
from .logging_utils import get_logger
from .pipeline import PipelineContext, run_full_pipeline, run_pr_agent
from .preflight import run_preflight
from .process_utils import CommandResult
from .runs import HistoryLimits, RunHistory

log = get_logger("orchestrator")


@dataclass(slots=True)
class PrActionResult:
    """Result of a standalone pr-agent review/improve invocation."""

    pr_url: str
    action: str
    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    duration_s: float = 0.0
    timed_out: bool = False
    attempts: int = 1
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_command(cls, pr_url: str, action: str, result: CommandResult) -> PrActionResult:
        errors: list[str] = []
        if not result.ok:
            msg = (result.stderr.strip() or result.stdout.strip() or "<no output>")[:500]
            errors.append(f"pr-agent {action} failed: {msg}")
        return cls(
            pr_url=pr_url,
            action=action,
            success=result.ok,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            duration_s=result.duration_s,
            timed_out=result.timed_out,
            attempts=result.attempts,
            errors=errors,
        )


def _resolve_config(repo: str | Path) -> Config:
    return load_config(project_root=Path(repo).resolve())


def run_pipeline(
    task: str,
    repo: str | Path,
    agent: str | None = None,
    timeout: int | None = None,
    *,
    dry_run: bool = False,
    agent_timeout: int | None = None,
    review_iterations: int | None = None,
    cleanup: bool | None = None,
    skip_preflight: bool = False,
) -> PipelineContext:
    """Run the full agent pipeline on a task.

    Args:
        task: Task description for the AI agent.
        repo: Path to the git repository.
        agent: AI agent name. Defaults to ``config.default_agent``.
        timeout: Seconds to wait for PR creation. Defaults to ``config.default_timeout``.
        dry_run: If True, skip every external command and only emit what would run.
        agent_timeout: Seconds to wait for the agent stage. Defaults to
            ``config.agent_timeout``.
        review_iterations: Review→agent feedback rounds. Defaults to
            ``config.review_iterations``.
        cleanup: Remove the worktree if the run fails. Defaults to
            ``config.cleanup_on_error``.
        skip_preflight: If True, skip pre-flight validation checks.

    Returns:
        PipelineContext with results and any errors.
    """
    cfg = _resolve_config(repo)
    if agent_timeout is not None:
        cfg.agent_timeout = agent_timeout
    if review_iterations is not None:
        cfg.review_iterations = review_iterations
    if cleanup is not None:
        cfg.cleanup_on_error = cleanup
    chosen_agent = agent or cfg.default_agent

    history = RunHistory(
        limits=HistoryLimits(
            max_bytes=cfg.history_max_bytes,
            max_age_days=cfg.history_max_age_days,
        )
    )
    if cfg.history_max_age_days > 0:
        # Auto-prune before writing anything new so we don't carry forward
        # ancient records into the next rotation.
        history.prune_older_than(cfg.history_max_age_days)

    ctx = PipelineContext(
        repo_root=Path(repo).resolve(),
        task=task,
        ai_agent=chosen_agent,
        config=cfg,
        dry_run=dry_run,
        history=history,
    )

    if chosen_agent not in cfg.agents:
        ctx.errors.append(
            f"Unknown agent {chosen_agent!r}. Available: {', '.join(cfg.agent_names)}"
        )
        return ctx

    if not dry_run and not skip_preflight:
        preflight = run_preflight(ctx.repo_root)
        ctx.preflight = preflight
        for err in preflight.errors:
            ctx.errors.append(err)
        if preflight.errors:
            return ctx

    return run_full_pipeline(ctx, pr_timeout=timeout)


def review_pr(pr_url: str, repo: str | Path) -> PrActionResult:
    """Run pr-agent review on an existing PR."""
    return _pr_action(pr_url, repo, "review")


def improve_pr(pr_url: str, repo: str | Path) -> PrActionResult:
    """Run pr-agent improve on an existing PR."""
    return _pr_action(pr_url, repo, "improve")


def _pr_action(pr_url: str, repo: str | Path, action: str) -> PrActionResult:
    cfg = _resolve_config(repo)
    result = run_pr_agent(pr_url, action, Path(repo).resolve(), cfg)
    return PrActionResult.from_command(pr_url, action, result)
