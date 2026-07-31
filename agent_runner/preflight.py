"""Pre-flight validation — gate the pipeline before any stage runs.

Catching missing dependencies or bad auth *before* creating worktrees
and launching agents saves time and produces clear, actionable errors.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .process_utils import run

INDENT = "    "


@dataclass(slots=True)
class PreflightResult:
    """Aggregated pre-flight check results."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def errors(self) -> list[str]:
        return [c.message for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [c.message for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.ok)


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    message: str = ""
    severity: str = "error"  # "error" blocks the pipeline, "warning" doesn't
    detail: str = ""


def _check(name: str, ok: bool, message: str = "", **kwargs: str) -> CheckResult:
    return CheckResult(name=name, ok=ok, message=message, **kwargs)


def run_preflight(repo_root: Path) -> PreflightResult:
    """Run all pre-flight checks and return the results.

    Returns a ``PreflightResult`` — callers decide whether to proceed
    (``result.ok``) and can show results through the CLI.
    """
    checks: list[CheckResult] = []

    # ---- repo ----
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        checks.append(
            _check(
                "git repository",
                False,
                f"{repo_root} is not a git repository",
                detail="Create one with `git init` or point --repo at a git directory.",
            )
        )
    else:
        checks.append(_check("git repository", True))

    # ---- gtr ----
    gtr = _find_gtr(repo_root)
    if gtr is None:
        checks.append(
            _check(
                "git-worktree-runner (gtr)",
                False,
                "gtr CLI not found",
                detail="Run `make setup` or install from vendor/git-worktree-runner.",
            )
        )
    else:
        checks.append(_check("git-worktree-runner (gtr)", True, detail=gtr))

    # ---- gh ----
    gh = shutil.which("gh")
    if gh is None:
        checks.append(
            _check(
                "GitHub CLI (gh)",
                False,
                "gh CLI not found on $PATH",
                detail="Install from https://cli.github.com/ and run `gh auth login`.",
            )
        )
    else:
        auth = run(["gh", "auth", "status"], cwd=repo_root, timeout=15)
        if auth.ok:
            checks.append(_check("GitHub CLI (gh)", True, detail=f"{gh} (authenticated)"))
        else:
            checks.append(
                _check(
                    "GitHub CLI (gh)",
                    False,
                    "gh is not authenticated — run `gh auth login`",
                    detail=auth.stderr.strip() or auth.stdout.strip() or "unknown auth error",
                )
            )

    # ---- pr-agent ----
    pa = shutil.which("pr-agent")
    if pa is None:
        checks.append(
            _check(
                "pr-agent",
                False,
                "pr-agent not found on $PATH",
                detail="Run `make setup` to install.",
            )
        )
    else:
        checks.append(_check("pr-agent", True, detail=pa))

    # ---- working tree (warning only) ----
    dirty = _check_dirty(repo_root)
    if dirty:
        checks.append(
            _check(
                "clean working tree",
                False,
                "Working tree has uncommitted changes",
                severity="warning",
                detail="Changes won't be included in the agent's worktree.",
            )
        )
    else:
        checks.append(_check("clean working tree", True))

    return PreflightResult(checks=checks)


def _find_gtr(repo_root: Path) -> str | None:
    """Locate gtr: vendored paths only (fail-closed, no PATH fallback)."""
    vendor_root = Path(__file__).resolve().parent.parent

    for candidate in (
        vendor_root / "vendor" / "git-worktree-runner" / "bin" / "git-gtr",
        repo_root / "vendor" / "git-worktree-runner" / "bin" / "git-gtr",
        repo_root / "git-worktree-runner" / "bin" / "git-gtr",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _check_dirty(repo_root: Path) -> bool:
    """Return True if the working tree has uncommitted changes."""
    result = run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        timeout=10,
    )
    if not result.ok:
        return False  # can't determine, don't block
    return bool(result.stdout.strip())
