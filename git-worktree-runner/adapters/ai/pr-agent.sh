#!/usr/bin/env bash
# PR-Agent AI adapter — pr-agent review/improve/describe
#
# Usage: git gtr ai <worktree> --ai pr-agent -- <pr-agent args...>
#   or via config: git gtr config set gtr.ai.default pr-agent
#
# The adapter expects pr-agent to be installed (pip install pr-agent or
# available at the root-level orchestrator path).

_PR_AGENT_DIR="${GTR_PR_AGENT_DIR:-${GTR_DIR}/../pr-agent}"

_find_pr_agent() {
  # Prefer orchestrator-level venv
  if [ -x "${GTR_DIR}/../.venv/bin/pr-agent" ]; then
    echo "${GTR_DIR}/../.venv/bin/pr-agent"
    return 0
  fi
  # Fall back to PATH
  if command -v pr-agent >/dev/null 2>&1; then
    command -v pr-agent
    return 0
  fi
  # Try pr-agent project directly
  if [ -x "$_PR_AGENT_DIR/.venv/bin/python" ]; then
    echo "$_PR_AGENT_DIR/.venv/bin/python -m pr_agent.cli"
    return 0
  fi
  return 1
}

ai_can_start() {
  _find_pr_agent >/dev/null 2>&1
}

ai_start() {
  local path="$1"
  shift

  local pr_agent_cmd
  pr_agent_cmd="$(_find_pr_agent)" || {
    log_error "pr-agent not found. Install with: pip install pr-agent"
    log_info "Or set GTR_PR_AGENT_DIR to the pr-agent project path"
    return 1
  }

  if [ ! -d "$path" ]; then
    log_error "Directory not found: $path"
    return 1
  fi

  # Determine the PR URL from git remote
  local pr_url=""
  if [ -n "${GTR_PR_URL:-}" ]; then
    pr_url="$GTR_PR_URL"
  else
    # Try to infer from current branch's open PR
    local branch
    branch=$(cd "$path" && git rev-parse --abbrev-ref HEAD 2>/dev/null) || true
    if [ -n "$branch" ] && [ "$branch" != "HEAD" ]; then
      local remote_url
      remote_url=$(cd "$path" && git config --get remote.origin.url 2>/dev/null) || true
      if [ -n "$remote_url" ]; then
        # Extract owner/repo from remote URL
        local repo_path
        repo_path=$(echo "$remote_url" | sed -E 's|.*[:/]([^/]+/[^/]+)(\.git)?$|\1|')
        pr_url="https://github.com/${repo_path}/pull/$(cd "$path" && git rev-parse --abbrev-ref HEAD 2>/dev/null)"
      fi
    fi
  fi

  if [ -z "$pr_url" ]; then
    log_error "No PR URL found. Set GTR_PR_URL or open a PR first."
    return 1
  fi

  log_step "Running pr-agent on: $pr_url"
  (cd "$path" && $pr_agent_cmd --pr_url="$pr_url" "$@")
}