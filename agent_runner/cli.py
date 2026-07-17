"""agent-runner CLI — dynamic agent pipeline.

Usage:
    agent-runner run     --task "fix auth bug" --repo . --agent claude
    agent-runner review  --pr-url https://github.com/owner/repo/pull/42
    agent-runner improve --pr-url https://github.com/owner/repo/pull/42
    agent-runner status
    agent-runner logs    [--limit 10]
    agent-runner config  [--repo .]
    agent-runner clean   [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .logging_utils import configure, get_logger
from .orchestrator import PrActionResult, improve_pr, review_pr, run_pipeline
from .runs import RunHistory

log = get_logger("cli")


def _format_pr_result(label: str, result: PrActionResult) -> int:
    if result.success:
        print(f"✅ {label} completed ({result.duration_s:.2f}s, {result.attempts} attempt(s))")
        if result.stdout:
            print(result.stdout)
        return 0
    print(
        f"❌ {label} failed (exit={result.returncode}, attempts={result.attempts})",
        file=sys.stderr,
    )
    if result.timed_out:
        print("   reason: timed out", file=sys.stderr)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    elif result.stdout.strip():
        print(result.stdout.strip(), file=sys.stderr)
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    ctx = run_pipeline(
        task=args.task,
        repo=args.repo,
        agent=args.agent,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )

    if ctx.errors:
        print("❌ Pipeline completed with errors:", file=sys.stderr)
        for err in ctx.errors:
            print(f"  • {err}", file=sys.stderr)
        print(f"   run_id: {ctx.run_id}", file=sys.stderr)
        return 1

    print("✅ Pipeline completed successfully")
    print(f"   run_id:     {ctx.run_id}")
    print(f"   branch:     {ctx.branch_name}")
    print(f"   worktree:   {ctx.worktree_path}")
    print(f"   PR:         {ctx.pr_url}")
    if ctx.dry_run:
        print("   (dry-run)")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    return _format_pr_result("Review", review_pr(args.pr_url, args.repo or "."))


def _cmd_improve(args: argparse.Namespace) -> int:
    return _format_pr_result("Improve", improve_pr(args.pr_url, args.repo or "."))


def _cmd_status(args: argparse.Namespace) -> int:
    gtr_ok = shutil.which("git-gtr") is not None
    pr_agent_ok = shutil.which("pr-agent") is not None
    cfg = load_config(project_root=Path(".").resolve())

    print("git-agent-runner status:")
    print(f"  gtr:           {'✅' if gtr_ok else '❌ not found'}")
    print(f"  pr-agent:      {'✅' if pr_agent_ok else '❌ not found'}")
    print(f"  default agent: {cfg.default_agent}")
    print(f"  agents:        {', '.join(cfg.agent_names)}")
    print(f"  config:        {cfg.config_path or '<built-in defaults>'}")

    if gtr_ok and pr_agent_ok:
        print("  ✅ All systems ready")
        return 0
    return 1


def _cmd_logs(args: argparse.Namespace) -> int:
    history = RunHistory()
    if args.prune_days > 0:
        n = history.prune_older_than(args.prune_days)
        print(f"🧹 Pruned {n} records older than {args.prune_days} day(s).")
    if args.compact:
        n = history.compact()
        print(f"📦 Compacted {n} stage records into run_end summaries.")
    starts = history.latest(args.limit)
    if not starts and not (args.prune_days > 0 or args.compact):
        print("No pipeline runs recorded yet.")
        return 0
    for start in starts:
        records = history.iter_run(start.run_id)
        # Prefer the run_end record (carries duration_s + final state).
        end = next((r for r in reversed(records) if r.event == "run_end"), None)
        final = end or start
        when = final.extra.get("duration_s") if isinstance(final.extra, dict) else None
        ok = final.extra.get("ok") if isinstance(final.extra, dict) else None
        marker = "" if ok else "  ⚠"
        print(
            f"• {final.run_id}{marker}  task={final.task!r}  agent={final.agent}  "
            f"branch={final.branch or '-'}  pr={final.pr_url or '-'}  "
            f"duration={when}s"
        )
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config(project_root=Path(args.repo or ".").resolve())
    print(
        json.dumps(
            {
                "default_agent": cfg.default_agent,
                "poll_interval": cfg.poll_interval,
                "default_timeout": cfg.default_timeout,
                "subprocess_timeout": cfg.subprocess_timeout,
                "retry_attempts": cfg.retry_attempts,
                "retry_backoff": cfg.retry_backoff,
                "config_path": str(cfg.config_path) if cfg.config_path else None,
                "agents": {
                    name: {"command": list(spec.command), "description": spec.description}
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
        print(f"Would truncate {history.path}")
        return 0
    if not history.path.exists():
        print("Nothing to clean.")
        return 0
    history.path.unlink()
    print(f"🧹 Removed run history: {history.path}")
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
        "--dry-run",
        action="store_true",
        help="Plan the pipeline without executing any external commands",
    )

    # review / improve
    for name, help_text in [
        ("review", "Run pr-agent review on a PR"),
        ("improve", "Run pr-agent improve on a PR"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--pr-url", required=True, help="Full PR URL")
        p.add_argument("--repo", default=None, help="Path to git repository")

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

    cfg_p = sub.add_parser("config", help="Show resolved configuration as JSON")
    cfg_p.add_argument("--repo", default=".", help="Path to git repository (default: .)")

    clean_p = sub.add_parser("clean", help="Clear local pipeline-run history")
    clean_p.add_argument(
        "--dry-run", action="store_true", help="Show what would be removed without removing"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "run": _cmd_run,
        "review": _cmd_review,
        "improve": _cmd_improve,
        "status": _cmd_status,
        "logs": _cmd_logs,
        "config": _cmd_config,
        "clean": _cmd_clean,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
