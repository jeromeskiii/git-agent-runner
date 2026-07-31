#!/usr/bin/env bash
# OpenAI Codex CLI adapter

# Find the actual codex executable (not a shell function)
# Returns the path to the executable, or empty string if not found
find_codex_executable() {
  local cmd

  for cmd in codex; do
    local exe_path
    exe_path="$(type -P "$cmd" 2>/dev/null)" && [ -n "$exe_path" ] && {
      echo "$exe_path"
      return 0
    }
  done

  # Fallback: use type -t to check if it's an executable file
  if [ "$(type -t codex 2>/dev/null)" = "file" ]; then
    command -v codex
    return 0
  fi

  return 1
}

# Check if Codex CLI is available
ai_can_start() {
  find_codex_executable >/dev/null 2>&1
}

# Start Codex in a directory
# Usage: ai_start path [args...]
# Expected args for headless pipeline use (from the agent registry):
#   exec --skip-git-repo-check --sandbox workspace-write \
#     -c sandbox_workspace_write.network_access=true "<task>"
ai_start() {
  local path="$1"
  shift
  local configured_args=("${GTR_AI_CMD_ARGS[@]}")

  local codex_cmd
  codex_cmd="$(find_codex_executable)"

  if [ -z "$codex_cmd" ]; then
    log_error "Codex CLI not found. Install from https://github.com/openai/codex"
    log_info "The CLI is called 'codex'"
    return 1
  fi

  if [ ! -d "$path" ]; then
    log_error "Directory not found: $path"
    return 1
  fi

  # stdin closed: codex exec reads extra input from stdin when it's not a TTY.
  (cd "$path" && "$codex_cmd" "${configured_args[@]}" "$@" </dev/null)
}
