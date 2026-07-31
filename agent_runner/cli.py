"""agent-runner CLI — dynamic agent pipeline.

Usage:
    agent-runner run     --task "fix auth bug" --repo . --agent claude
    agent-runner review  --pr-url https://github.com/owner/repo/pull/42
    agent-runner improve --pr-url https://github.com/owner/repo/pull/42
    agent-runner status
    agent-runner logs    [--limit 10]
    agent-runner config  [--repo .]
    agent-runner clean   [--dry-run]
    agent-runner doctor  [--repo .] [--fix] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from rich.console import Console

from . import __version__
from .config import load_config
from .logging_utils import configure, get_logger
from .orchestrator import PrActionResult, improve_pr, review_pr, run_pipeline
from .pipeline import collect_agent_worktrees
from .preflight import run_preflight
from .rich_ui import (
    print_clean_result,
    print_compact_result,
    print_doctor,
    print_logs,
    print_pipeline_errors,
    print_pipeline_success,
    print_preflight,
    print_prune_result,
    print_result_header,
    print_status,
)
from .runs import RunHistory, RunRecord

console = Console()
log = get_logger("cli")


def _format_pr_result(label: str, result: PrActionResult) -> int:
    if result.success:
        print_result_header(label, True, result.duration_s, result.attempts)
        if result.stdout:
            console.print(result.stdout)
        return 0
    print_result_header(label, False, result.duration_s, result.attempts)
    if result.timed_out:
        console.print("[red]   reason: timed out[/]")
    if result.stderr.strip():
        console.print(f"[red]{result.stderr.strip()}[/]")
    elif result.stdout.strip():
        console.print(f"[red]{result.stdout.strip()}[/]")
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    ctx = run_pipeline(
        task=args.task,
        repo=args.repo,
        agent=args.agent,
        timeout=args.timeout,
        dry_run=args.dry_run,
        agent_timeout=args.agent_timeout,
        review_iterations=args.iterations,
        cleanup=True if args.cleanup else None,
        skip_preflight=args.skip_preflight,
    )

    if ctx.preflight:
        print_preflight(ctx.preflight)

    if ctx.errors:
        print_pipeline_errors(ctx.run_id, ctx.errors)
        return 1

    print_pipeline_success(
        ctx.run_id,
        ctx.branch_name,
        ctx.worktree_path,
        ctx.pr_url,
        ctx.warnings,
        ctx.dry_run,
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    return _format_pr_result("Review", review_pr(args.pr_url, args.repo or "."))


def _cmd_improve(args: argparse.Namespace) -> int:
    return _format_pr_result("Improve", improve_pr(args.pr_url, args.repo or "."))


def _cmd_status(args: argparse.Namespace) -> int:
    gtr_ok = shutil.which("git-gtr") is not None
    pr_agent_ok = shutil.which("pr-agent") is not None
    cfg = load_config(project_root=Path(args.repo or ".").resolve())
    print_status(gtr_ok, pr_agent_ok, cfg.default_agent, cfg.agent_names)
    return 0 if (gtr_ok and pr_agent_ok) else 1


def _cmd_logs(args: argparse.Namespace) -> int:
    history = RunHistory()
    had_side_effects = False

    if args.prune_days > 0:
        n = history.prune_older_than(args.prune_days)
        if not getattr(args, "json", False):
            print_prune_result(n, args.prune_days)
        had_side_effects = True

    if args.compact:
        n = history.compact()
        if not getattr(args, "json", False):
            print_compact_result(n)
        had_side_effects = True

    starts = history.latest(args.limit)
    if not starts:
        if getattr(args, "json", False):
            console.print("[]")
        elif not had_side_effects:
            console.print("[dim]No pipeline runs recorded yet.[/]")
        return 0

    wanted = {s.run_id for s in starts}
    ends: dict[str, RunRecord] = {}
    for record in history.iter_all():
        if record.run_id in wanted and record.event == "run_end":
            ends[record.run_id] = record

    if getattr(args, "json", False):
        records = [s.to_dict() for s in starts]
        print(json.dumps(records, indent=2))
    else:
        print_logs(starts, ends)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(project_root=Path(args.repo or ".").resolve())
    console.print(
        json.dumps(
            {
                "default_agent": cfg.default_agent,
                "poll_interval": cfg.poll_interval,
                "default_timeout": cfg.default_timeout,
                "subprocess_timeout": cfg.subprocess_timeout,
                "agent_timeout": cfg.agent_timeout,
                "retry_attempts": cfg.retry_attempts,
                "retry_backoff": cfg.retry_backoff,
                "config_path": str(cfg.config_path) if cfg.config_path else None,
                "agents": {
                    name: {
                        "command": list(spec.command),
                        "args": list(spec.args),
                        "description": spec.description,
                    }
                    for name, spec in cfg.agents.items()
                },
            },
            indent=2,
        )
    )
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    history = RunHistory()
    if args.dry_run:
        print_clean_result(str(history.path), dry_run=True)
        return 0
    if not history.path.exists():
        console.print("[dim]Nothing to clean.[/]")
        return 0
    history.path.unlink()
    print_clean_result(str(history.path), dry_run=False)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    repo = Path(args.repo or ".").resolve()
    is_json = getattr(args, "json", False)

    if not is_json:
        console.print(f"[bold]agent-runner doctor[/] — {repo}")

    if not (repo / ".git").exists():
        if is_json:
            print(json.dumps({"error": f"{repo} is not a git repository", "worktrees": []}))
        else:
            console.print(f"[red]❌ {repo} is not a git repository[/]")
        return 1

    history = RunHistory()
    latest_by_branch: dict[str, RunRecord] = {}
    for rec in history.iter_all():
        if rec.branch:
            latest_by_branch[rec.branch] = rec

    worktrees = collect_agent_worktrees(repo)
    if not worktrees:
        if is_json:
            print(json.dumps({"repo": str(repo), "orphans": 0, "worktrees": []}))
        else:
            console.print("  ✅ no agent/* worktrees found")
        return 0

    statuses: dict[str, tuple[str | None, str]] = {}
    orphans = 0
    orphan_branches: list[str] = []

    for wt in worktrees:
        branch = wt["branch"]
        record = latest_by_branch.get(branch)
        if record is None:
            msg = "orphan — no run history"
            statuses[branch] = (record.run_id if record else None, msg)
            orphans += 1
            orphan_branches.append(branch)
        elif record.event != "run_end":
            msg = f"unfinished run {record.run_id}"
            statuses[branch] = (record.run_id, msg)
            orphans += 1
            orphan_branches.append(branch)
        elif isinstance(record.extra, dict) and record.extra.get("ok") is False:
            statuses[branch] = (record.run_id, f"failed run {record.run_id}")
        else:
            statuses[branch] = (record.run_id, f"ok run {record.run_id}")

    if is_json:
        doc_data = {
            "repo": str(repo),
            "orphans": orphans,
            "worktrees": [
                {
                    "branch": wt["branch"],
                    "path": wt["path"],
                    "run_id": statuses[wt["branch"]][0],
                    "status": statuses[wt["branch"]][1],
                }
                for wt in worktrees
            ],
        }
        print(json.dumps(doc_data, indent=2))
    else:
        print_doctor(worktrees, statuses, orphans)

    if args.fix and orphan_branches:
        from .process_utils import run

        gtr = shutil.which("git-gtr")
        if not gtr:
            if not is_json:
                console.print("[red]gtr not found — cannot clean up orphans[/]")
            return 1

        for branch in orphan_branches:
            if args.dry_run:
                if not is_json:
                    console.print(f"[yellow]🔍 would remove worktree for {branch}[/]")
                continue
            result = run([gtr, "rm", branch, "--yes"], cwd=repo, timeout=60)
            if result.ok:
                if not is_json:
                    console.print(f"[green]✅ removed worktree for {branch}[/]")
            else:
                if not is_json:
                    console.print(
                        f"[red]❌ failed to remove {branch}:[/] {result.stderr.strip() or result.stdout.strip()}"
                    )

    return 1 if orphans else 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    repo = Path(args.repo or ".").resolve()
    console.print(f"[bold]agent-runner preflight[/] — {repo}")
    result = run_preflight(repo)
    print_preflight(result)
    if result.ok:
        console.print("[green]✅ All checks passed — ready to run[/]")
        return 0
    if result.errors:
        console.print("[red]❌ Pre-flight failed — fix the errors above before running[/]")
        return 1
    console.print("[yellow]⚠ Pre-flight warnings (non-blocking)[/]")
    return 0


def main() -> None:
    configure()
    parser = argparse.ArgumentParser(
        prog="agent-runner",
        description="Dynamic agent pipeline — wires gtr + pr-agent together",
        epilog="Run 'agent-runner <command> --help' for command-specific help.",
    )
    parser.add_argument("--version", action="version", version=f"agent-runner {__version__}")

    sub = parser.add_subparsers(dest="command", title="commands")

    # run
    run_p = sub.add_parser("run", help="Run the full agent pipeline")
    run_p.add_argument("--task", required=True, help="Task description for the AI agent")
    run_p.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    run_p.add_argument("--agent", default=None, help="AI agent name (overrides config default)")
    run_p.add_argument(
        "--timeout", type=int, default=None, help="Seconds to wait for PR (overrides config)"
    )
    run_p.add_argument(
        "--agent-timeout",
        type=int,
        default=None,
        help="Seconds to wait for the agent stage (overrides config agent_timeout)",
    )
    run_p.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Review→agent feedback rounds (overrides config review_iterations)",
    )
    run_p.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove the worktree if the run fails (branch is kept)",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the pipeline without executing any external commands",
    )
    run_p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight validation checks",
    )

    # review / improve
    for name, help_text in [
        ("review", "Run pr-agent review on a PR"),
        ("improve", "Run pr-agent improve on a PR"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--pr-url", required=True, help="Full PR URL")
        p.add_argument("--repo", default=None, help="Path to git repository")

    # preflight
    pf_p = sub.add_parser("preflight", help="Run pre-flight validation checks")
    pf_p.add_argument("--repo", default=".", help="Path to git repository (default: .)")

    # status / logs / config / clean
    status_p = sub.add_parser("status", help="Check if gtr and pr-agent are available")
    status_p.add_argument("--repo", default=".", help="Path to git repository (default: .)")

    logs_p = sub.add_parser("logs", help="Show recent pipeline runs")
    logs_p.add_argument("--limit", type=int, default=10, help="Number of runs to show (default 10)")
    logs_p.add_argument(
        "--prune-days",
        type=int,
        default=0,
        help="Drop records older than N days before listing (0 = no pruning)",
    )
    logs_p.add_argument(
        "--compact",
        action="store_true",
        help="Merge stage events of old runs into a single run_end record",
    )
    logs_p.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON array of log records instead of Rich formatted view",
    )

    cfg_p = sub.add_parser("config", help="Show resolved configuration as JSON")
    cfg_p.add_argument("--repo", default=".", help="Path to git repository (default: .)")

    clean_p = sub.add_parser("clean", help="Clear local pipeline-run history")
    clean_p.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed without removing"
    )

    doctor_p = sub.add_parser(
        "doctor", help="Reconcile agent/* worktrees on disk against run history"
    )
    doctor_p.add_argument("--repo", default=".", help="Path to git repository (default: .)")
    doctor_p.add_argument(
        "--fix", action="store_true", help="Remove orphaned worktrees (branch is kept)"
    )
    doctor_p.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed without removing"
    )
    doctor_p.add_argument(
        "--json", action="store_true", help="Output JSON structure of worktrees and health status"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "run": _cmd_run,
        "review": _cmd_review,
        "improve": _cmd_improve,
        "preflight": _cmd_preflight,
        "status": _cmd_status,
        "logs": _cmd_logs,
        "config": _cmd_config,
        "clean": _cmd_clean,
        "doctor": _cmd_doctor,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
