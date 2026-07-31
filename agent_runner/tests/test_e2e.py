"""End-to-end smoke tests: real gtr against a scratch repo, no network.

The agent (``claude``), ``gh`` and ``pr-agent`` are replaced by fake
executables on PATH; the remote is a local bare repo. This exercises the
full pipeline — create → launch → wait_for_pr → review → improve — plus
failure cleanup, catching vendor-dialect bugs that mocked unit tests can't.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_runner.orchestrator import run_pipeline

_VENDORED_GTR = (
    Path(__file__).resolve().parent.parent.parent
    / "vendor"
    / "git-worktree-runner"
    / "bin"
    / "git-gtr"
)


def _gtr_available() -> bool:
    return _VENDORED_GTR.is_file() or shutil.which("git-gtr") is not None


pytestmark = pytest.mark.skipif(not _gtr_available(), reason="git-gtr not installed")

_FAKE_CLAUDE = """#!/bin/sh
# Fake agent: commit + push on the current branch, then mark the PR ready.
if [ -n "$GAR_FAKE_AGENT_FAIL" ]; then
  echo "fake agent failed" >&2
  exit 1
fi
echo "worked: $*" >> agent-work.txt
git add agent-work.txt
git -c user.email=e2e@test -c user.name=e2e commit -m "agent work" >/dev/null
branch=$(git rev-parse --abbrev-ref HEAD)
git push -u origin "$branch" >/dev/null 2>&1
marker=$(echo "$branch" | tr '/' '_')
echo "https://fake.test/pr/$branch" > "$GAR_FAKE_STATE/pr-$marker"
"""

_FAKE_GH = """#!/bin/sh
# Fake gh: only `gh pr list --head <branch> ...` — prints the URL if the
# agent's marker file exists, nothing otherwise.
head=""
while [ $# -gt 0 ]; do
  case "$1" in
    --head) head="$2"; shift 2 ;;
    *) shift ;;
  esac
done
marker=$(echo "$head" | tr '/' '_')
if [ -f "$GAR_FAKE_STATE/pr-$marker" ]; then
  cat "$GAR_FAKE_STATE/pr-$marker"
fi
exit 0
"""

_FAKE_PR_AGENT = """#!/bin/sh
echo "fake pr-agent output: $*"
exit 0
"""


def _git(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _write_exe(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture()
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    # Fake executables.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "claude", _FAKE_CLAUDE)
    _write_exe(bin_dir / "gh", _FAKE_GH)
    _write_exe(bin_dir / "pr-agent", _FAKE_PR_AGENT)

    # Local "remote" + working repo on main.
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    _git("init", "-b", "main", str(work))
    _git("remote", "add", "origin", str(remote), cwd=work)
    (work / "README.md").write_text("scratch\n")
    _git("add", "README.md", cwd=work)
    _git("-c", "user.email=e2e@test", "-c", "user.name=e2e", "commit", "-m", "init", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)

    # Deterministic + fast: no retries in e2e.
    (work / "agent-runner.toml").write_text("retry_attempts = 1\n")

    # Hermetic environment.
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GAR_FAKE_STATE", str(state))
    monkeypatch.setenv("AGENT_RUNNER_HISTORY", str(tmp_path / "runs.jsonl"))
    monkeypatch.delenv("GAR_FAKE_AGENT_FAIL", raising=False)

    return {"work": work, "remote": remote, "state": state, "tmp": tmp_path}


def test_e2e_full_pipeline_happy_path(scratch: dict[str, Path]) -> None:
    ctx = run_pipeline(task="e2e happy", repo=scratch["work"])

    assert ctx.errors == []
    assert ctx.branch_name.startswith("agent/e2e-happy-")
    assert Path(ctx.worktree_path).is_dir()
    assert ctx.pr_url == f"https://fake.test/pr/{ctx.branch_name}"
    assert "review" in ctx.review_result
    assert "improve" in ctx.review_result

    # The agent's commit landed on the remote branch.
    refs = _git("--git-dir", str(scratch["remote"]), "branch", "--list", "agent/*")
    assert ctx.branch_name in refs

    # Run history records a clean run_end.
    history = scratch["tmp"] / "runs.jsonl"
    records = [json.loads(line) for line in history.read_text().splitlines()]
    end = next(r for r in records if r["event"] == "run_end")
    assert end["extra"]["ok"] is True


def test_e2e_failed_agent_cleans_up_worktree(
    scratch: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GAR_FAKE_AGENT_FAIL", "1")
    ctx = run_pipeline(task="e2e fail", repo=scratch["work"], cleanup=True)

    assert ctx.errors  # launch failure is still reported
    # Cleanup removed the worktree but kept the branch for diagnosis.
    assert ctx.worktree_path
    assert not Path(ctx.worktree_path).exists()
    branches = _git("branch", "--list", "agent/e2e-fail*", cwd=scratch["work"])
    assert ctx.branch_name in branches
    assert any("cleanup" in w for w in ctx.warnings)
