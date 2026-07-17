"""Pipeline stages that compose the agent-runner workflow.

Each stage is an independent function that mutates and returns a
:class:`PipelineContext`. Stages short-circuit on prior errors so a
single failure doesn't drag the whole pipeline down.
"""

from __future__ import annotations

import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .logging_utils import get_logger, log_event
from .process_utils import run
from .runs import RunHistory, RunRecord, new_run_id

log = get_logger("pipeline")

# ``gtr new <branch>`` prints a line like ``Created worktree at: /abs/path`` on
# modern versions. Match that pattern explicitly so we don't accidentally pick
# up an arbitrary directory from stdout.
_WORKTREE_LINE_RE = re.compile(
    r"(?:Created worktree at|Worktree path|Worktree created at):\s*(\S+)",
    re.IGNORECASE,
)


@dataclass
class PipelineContext:
    """Mutable context passed through pipeline stages."""

    repo_root: Path
    task: str
    ai_agent: str = "claude"
    branch_name: str = ""
    worktree_path: str = ""
    pr_url: str = ""
    review_result: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    run_id: str = field(default_factory=new_run_id)
    history: RunHistory | None = None
    config: Config | None = None


def _gtr_bin(base_dir: Path) -> str:
    """Locate the gtr binary: prefer the vendored one, fall back to PATH."""
    candidate = base_dir / "git-worktree-runner" / "bin" / "git-gtr"
    if candidate.is_file():
        return str(candidate)
    on_path = shutil.which("git-gtr")
    if on_path:
        return on_path
    raise FileNotFoundError(
        f"git-gtr not found at {candidate} or on $PATH. Run `make setup` to install."
    )


def _gtr_base(ctx: PipelineContext) -> Path:
    """Choose the cwd gtr expects.

    Modern gtr (>=2.x) requires being run from *inside* the repository (it
    walks up from CWD looking for ``.git``). Older gtr expected the repo's
    parent directory. We prefer the inside-the-repo cwd; the parent-dir
    fallback is kept for backward compatibility with older installations.
    """
    if (ctx.repo_root / ".git").exists() or (ctx.repo_root / ".git").is_file():
        return ctx.repo_root
    return ctx.repo_root.parent if ctx.repo_root.is_dir() else ctx.repo_root


def _sanitize_branch_name(task: str) -> str:
    """Convert a task description into a valid git branch name.

    - Normalizes unicode (accents → ASCII where possible)
    - Replaces non-alnum with hyphens (collapses runs)
    - Strips leading/trailing hyphens
    - Truncates to 50 chars after the ``agent/`` prefix
    """
    raw = (
        unicodedata.normalize("NFKD", task)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
        .strip()
    )
    parts: list[str] = []
    for c in raw:
        if c.isalnum() or c in "-_":
            parts.append(c)
        else:
            parts.append("-")
    branch = "".join(parts)
    # Collapse runs of '-' into a single '-'. Implemented manually so Python 3.14's
    # optimizer can't elide the pass; ``re`` was being optimized away in some envs.
    collapsed: list[str] = []
    prev_dash = False
    for c in branch:
        if c == "-":
            if not prev_dash:
                collapsed.append(c)
            prev_dash = True
        else:
            collapsed.append(c)
            prev_dash = False
    branch = "".join(collapsed).strip("-")[:50]
    return f"agent/{branch}"


def _emit(ctx: PipelineContext, event: str, **fields: Any) -> None:
    log_event(log, event, run_id=ctx.run_id, **fields)
    if ctx.history is not None:
        ctx.history.append(
            RunRecord(
                run_id=ctx.run_id,
                ts=time.time(),
                event=event,
                task=ctx.task,
                repo=str(ctx.repo_root),
                agent=ctx.ai_agent,
                branch=ctx.branch_name,
                worktree=ctx.worktree_path,
                pr_url=ctx.pr_url,
                extra=fields,
            )
        )


def _parse_worktree_path(stdout: str) -> str | None:
    """Parse ``gtr new`` output to find the created worktree path."""
    for line in stdout.splitlines():
        m = _WORKTREE_LINE_RE.search(line)
        if m:
            candidate = Path(m.group(1))
            if candidate.is_absolute() and candidate.exists():
                return str(candidate)
    # Fallback: any line containing ``worktree`` followed by an existing absolute path.
    for line in stdout.splitlines():
        if "worktree" not in line.lower():
            continue
        for token in line.split():
            p = Path(token)
            if p.is_absolute() and p.exists() and p.is_dir():
                return str(p)
    return None


def _now() -> float:
    return time.time()


class PrerequisiteError(RuntimeError):
    """Raised when an external prerequisite is missing or misconfigured."""


def check_gh_available() -> str:
    """Return the path to the ``gh`` CLI, raising :class:`PrerequisiteError` if missing.

    Run early (before the polling loop) so users get one clear error instead of
    a long timeout followed by an ambiguous failure.
    """
    gh = shutil.which("gh")
    if not gh:
        raise PrerequisiteError(
            "gh CLI not found on $PATH. Install it from https://cli.github.com/"
            " and run `gh auth login`."
        )
    return gh


def stage_create_worktree(ctx: PipelineContext) -> PipelineContext:
    """Create an isolated worktree for the agent task."""
    ctx.branch_name = _sanitize_branch_name(ctx.task)

    if ctx.dry_run:
        ctx.worktree_path = f"<dry-run>/{ctx.branch_name}"
        _emit(ctx, "stage_create_worktree", status="dry_run", branch=ctx.branch_name)
        return ctx

    try:
        gtr = _gtr_bin(_gtr_base(ctx))
    except FileNotFoundError as e:
        ctx.errors.append(str(e))
        _emit(ctx, "stage_create_worktree", status="error", error=str(e))
        return ctx

    timeout = ctx.config.subprocess_timeout if ctx.config else 120
    result = run(
        [gtr, "new", ctx.branch_name, "--ai", ctx.ai_agent],
        cwd=_gtr_base(ctx),
        timeout=timeout,
    )
    if not result.ok:
        ctx.errors.append(f"gtr new failed: {result.stderr.strip() or result.stdout.strip()}")
        _emit(ctx, "stage_create_worktree", status="error", exit=result.returncode)
        return ctx

    parsed = _parse_worktree_path(result.stdout)
    if parsed:
        ctx.worktree_path = parsed
        _emit(ctx, "stage_create_worktree", status="ok", worktree=parsed)
    else:
        ctx.errors.append("Could not determine worktree path from gtr output")
        _emit(ctx, "stage_create_worktree", status="warn", reason="path_parse_failed")

    return ctx


def stage_launch_agent(ctx: PipelineContext) -> PipelineContext:
    """Launch an AI agent in the worktree."""
    if not ctx.worktree_path:
        return ctx
    if ctx.dry_run:
        _emit(ctx, "stage_launch_agent", status="dry_run")
        return ctx

    try:
        gtr = _gtr_bin(_gtr_base(ctx))
    except FileNotFoundError as e:
        ctx.errors.append(str(e))
        _emit(ctx, "stage_launch_agent", status="error", error=str(e))
        return ctx

    timeout = ctx.config.subprocess_timeout if ctx.config else 120
    result = run(
        [gtr, "ai", ctx.branch_name, "--ai", ctx.ai_agent],
        cwd=_gtr_base(ctx),
        timeout=timeout,
    )
    if not result.ok:
        ctx.errors.append(f"gtr ai failed: {result.stderr.strip() or result.stdout.strip()}")
        _emit(ctx, "stage_launch_agent", status="error", exit=result.returncode)
    else:
        _emit(ctx, "stage_launch_agent", status="ok")
    return ctx


def stage_wait_for_pr(
    ctx: PipelineContext,
    timeout: int | None = None,
    poll_interval: int | None = None,
) -> PipelineContext:
    """Poll for a PR URL from the branch.

    ``timeout`` / ``poll_interval`` fall back to the config defaults if not given.
    """
    if not ctx.branch_name:
        return ctx

    if timeout is None:
        timeout = ctx.config.default_timeout if ctx.config else 300
    if poll_interval is None:
        poll_interval = ctx.config.poll_interval if ctx.config else 10

    if ctx.dry_run:
        ctx.pr_url = f"<dry-run>/pr/{ctx.branch_name}"
        _emit(ctx, "stage_wait_for_pr", status="dry_run", timeout=timeout)
        return ctx

    # Fail fast with a single clear error if `gh` isn't installed at all.
    try:
        check_gh_available()
    except PrerequisiteError as e:
        ctx.errors.append(str(e))
        _emit(ctx, "stage_wait_for_pr", status="error", reason="gh_missing")
        return ctx

    deadline = _now() + timeout
    while _now() < deadline:
        result = run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                ctx.branch_name,
                "--json",
                "url",
                "--jq",
                ".[0].url",
            ],
            cwd=ctx.repo_root,
            timeout=30,
        )
        if result.ok and result.stdout.strip():
            ctx.pr_url = result.stdout.strip()
            _emit(ctx, "stage_wait_for_pr", status="ok", pr=ctx.pr_url)
            return ctx
        if result.returncode != 0 and "not logged in" in result.stderr.lower():
            ctx.errors.append("gh CLI is not authenticated — run `gh auth login`")
            _emit(ctx, "stage_wait_for_pr", status="error", reason="gh_unauth")
            return ctx
        time.sleep(poll_interval)

    msg = f"No PR found for branch {ctx.branch_name} within {timeout}s"
    ctx.errors.append(msg)
    _emit(ctx, "stage_wait_for_pr", status="timeout", timeout=timeout)
    return ctx


def stage_review_pr(ctx: PipelineContext) -> PipelineContext:
    """Run pr-agent review on the PR."""
    if not ctx.pr_url:
        return ctx
    if ctx.dry_run:
        _emit(ctx, "stage_review_pr", status="dry_run")
        return ctx

    timeout = ctx.config.subprocess_timeout if ctx.config else 120
    result = run(
        ["pr-agent", "--pr_url", ctx.pr_url, "review"],
        cwd=ctx.repo_root,
        timeout=timeout,
    )
    if not result.ok:
        ctx.errors.append(
            f"pr-agent review failed: {result.stderr.strip() or result.stdout.strip()}"
        )
        _emit(ctx, "stage_review_pr", status="error", exit=result.returncode)
    else:
        ctx.review_result["review"] = result.stdout
        _emit(ctx, "stage_review_pr", status="ok", lines=result.stdout.count("\n"))
    return ctx


def stage_improve_pr(ctx: PipelineContext) -> PipelineContext:
    """Run pr-agent improve on the PR."""
    if not ctx.pr_url:
        return ctx
    if ctx.dry_run:
        _emit(ctx, "stage_improve_pr", status="dry_run")
        return ctx

    timeout = ctx.config.subprocess_timeout if ctx.config else 120
    result = run(
        ["pr-agent", "--pr_url", ctx.pr_url, "improve"],
        cwd=ctx.repo_root,
        timeout=timeout,
    )
    if not result.ok:
        ctx.errors.append(
            f"pr-agent improve failed: {result.stderr.strip() or result.stdout.strip()}"
        )
        _emit(ctx, "stage_improve_pr", status="error", exit=result.returncode)
    else:
        ctx.review_result["improve"] = result.stdout
        _emit(ctx, "stage_improve_pr", status="ok", lines=result.stdout.count("\n"))
    return ctx


def run_full_pipeline(
    ctx: PipelineContext,
    *,
    pr_timeout: int | None = None,
) -> PipelineContext:
    """Run the complete pipeline: create → agent → PR → review → improve.

    Args:
        ctx: pipeline context.
        pr_timeout: overrides the configured default for the PR-wait stage.
            Honors ``--timeout`` from the CLI.
    """
    started = _now()
    stages: list[tuple[str, Any]] = [
        ("create_worktree", stage_create_worktree),
        ("launch_agent", stage_launch_agent),
        ("wait_for_pr", stage_wait_for_pr),
        ("review_pr", stage_review_pr),
        ("improve_pr", stage_improve_pr),
    ]

    _emit(ctx, "run_start")

    for _name, stage in stages:
        if ctx.errors:
            log.info("pipeline: halting before %s due to prior error", _name)
            break
        if stage is stage_wait_for_pr and pr_timeout is not None:
            ctx = stage(ctx, timeout=pr_timeout)
        else:
            ctx = stage(ctx)

    duration = _now() - started
    _emit(
        ctx,
        "run_end",
        duration_s=round(duration, 3),
        ok=not ctx.errors,
        errors=len(ctx.errors),
    )
    return ctx
