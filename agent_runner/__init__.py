"""git-agent-runner — Dynamic agent pipeline orchestrator.

Wires git-worktree-runner (gtr) and pr-agent together:

    gtr creates worktree → AI agent works → pushes branch → opens PR
                                                        ↓
                                         pr-agent reviews PR
                                                        ↓
                                         agent fixes → iterate

Top-level re-exports so callers can do::

    from agent_runner import PipelineContext, PrActionResult, Config, RunHistory
"""

from __future__ import annotations

from .config import AgentSpec, Config, load_config
from .orchestrator import PrActionResult, improve_pr, review_pr, run_pipeline
from .pipeline import PipelineContext, run_full_pipeline
from .preflight import PreflightResult, run_preflight
from .runs import RunHistory, RunRecord, new_run_id

__version__ = "0.1.0"

__all__ = [
    "AgentSpec",
    "Config",
    "PipelineContext",
    "PrActionResult",
    "PreflightResult",
    "RunHistory",
    "RunRecord",
    "__version__",
    "improve_pr",
    "load_config",
    "new_run_id",
    "review_pr",
    "run_full_pipeline",
    "run_pipeline",
    "run_preflight",
]
