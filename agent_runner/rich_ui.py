"""Rich terminal UI helpers for agent-runner CLI."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .preflight import PreflightResult
from .runs import RunRecord

console = Console()
INDENT = "    "


def print_result_header(label: str, success: bool, duration_s: float, attempts: int) -> None:
    status = (
        f"[bold green]✅[/] {label} completed" if success else f"[bold red]❌[/] {label} failed"
    )
    attrs = f"({duration_s:.2f}s, {attempts} attempt(s))"
    console.print(f"{status} {attrs}")
    if not success:
        pass  # caller handles stdout/stderr detail


def print_pipeline_success(
    run_id: str,
    branch_name: str,
    worktree_path: str,
    pr_url: str,
    warnings: list[str],
    dry_run: bool,
) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("run_id:", run_id)
    table.add_row("branch:", branch_name)
    table.add_row("worktree:", worktree_path)
    table.add_row("PR:", pr_url)
    if dry_run:
        table.add_row("", "[dim](dry-run)[/]")

    console.print("[bold green]✅ Pipeline completed successfully[/]")
    console.print(table)

    for w in warnings:
        console.print(f"  [yellow]⚠[/] {w}")


def print_pipeline_errors(run_id: str, errors: list[str]) -> None:
    console.print("[bold red]❌ Pipeline completed with errors:[/]")
    for err in errors:
        console.print(f"  [red]•[/] {err}")
    console.print(f"[dim]   run_id: {run_id}[/]")


def print_preflight(result: PreflightResult) -> None:
    table = Table(title="Pre-flight Checks", show_header=False, padding=(0, 1))
    table.add_column(no_wrap=True, style="bold")
    table.add_column()

    for check in result.checks:
        icon = (
            "[green]✅[/]"
            if check.ok
            else ("[yellow]⚠[/]" if check.severity == "warning" else "[red]❌[/]")
        )
        line = check.message
        if check.detail:
            line += f"\n[dim]{INDENT}{check.detail}[/]"
        table.add_row(f"{icon} {check.name}", line)
    console.print(table)


def print_status(gtr_ok: bool, pr_agent_ok: bool, default_agent: str, agents: list[str]) -> None:
    table = Table(title="git-agent-runner status")
    table.add_column("Component", style="bold")
    table.add_column("Status")

    table.add_row("gtr", "[green]✅ installed[/]" if gtr_ok else "[red]❌ not found[/]")
    table.add_row("pr-agent", "[green]✅ installed[/]" if pr_agent_ok else "[red]❌ not found[/]")
    table.add_row("default agent", default_agent)
    table.add_row("agents", ", ".join(agents))

    console.print(table)
    if gtr_ok and pr_agent_ok:
        console.print("[green]  ✅ All systems ready[/]")
    else:
        console.print("[red]  ❌ Run `make setup` to install missing components[/]")


def print_logs(starts: list[RunRecord], ends: dict[str, RunRecord]) -> None:
    if not starts:
        console.print("[dim]No pipeline runs recorded yet.[/]")
        return

    table = Table(title="Recent Pipeline Runs")
    table.add_column("Run ID", style="dim")
    table.add_column("Task")
    table.add_column("Agent", style="cyan")
    table.add_column("Branch", style="dim")
    table.add_column("Duration")
    table.add_column("Status")

    for start in starts:
        end = ends.get(start.run_id)
        if end is None:
            table.add_row(
                start.run_id,
                start.task,
                start.agent,
                start.branch or "-",
                "—",
                "[yellow]⚠ unfinished[/]",
            )
            continue
        when = end.extra.get("duration_s") if isinstance(end.extra, dict) else None
        ok = end.extra.get("ok") if isinstance(end.extra, dict) else None
        status = "[green]ok[/]" if ok else "[red]failed[/]"
        table.add_row(
            end.run_id,
            end.task,
            end.agent,
            end.branch or "-",
            f"{when}s" if when is not None else "—",
            status,
        )

    console.print(table)


def print_doctor(
    worktrees: list[dict[str, str]],
    statuses: dict[str, tuple[str | None, str]],
    orphans: int,
) -> None:
    if not worktrees:
        console.print("✅ no agent/* worktrees found")
        return

    table = Table(title="Agent Worktree Reconciliation")
    table.add_column("Branch", style="cyan")
    table.add_column("Path", style="dim")
    table.add_column("Status")

    for wt in worktrees:
        branch = wt["branch"]
        _, msg = statuses.get(branch, (None, "unknown"))
        status = (
            f"[red]⚠ {msg}[/]"
            if "orphan" in msg or "unfinished" in msg or "failed" in msg
            else f"[green]{msg}[/]"
        )
        table.add_row(branch, wt["path"], status)

    console.print(table)
    if orphans:
        console.print(f"[yellow]  {orphans} orphan(s). Remediation: agent-runner doctor --fix[/]")


def print_compact_result(n: int) -> None:
    console.print(f"[green]📦 Compacted {n} stage records into run_end summaries.[/]")


def print_prune_result(n: int, days: int) -> None:
    console.print(f"[green]🧹 Pruned {n} records older than {days} day(s).[/]")


def print_clean_result(path: str, dry_run: bool) -> None:
    if dry_run:
        console.print(f"[dim]Would truncate {path}[/]")
    else:
        console.print(f"[green]🧹 Removed run history: {path}[/]")
