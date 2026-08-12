from pathlib import Path

from app.impl.runtime.dependency import runtime
from app.main_util import safe_workspace_path
from app.service.problem.runtime_config import (
    PROBLEM_CONFIG_REL,
    ProblemConfig,
    load_problem_config,
    problem_config_limits,
)


def read_problem_config(
    workspace: Path,
) -> tuple[ProblemConfig, ProblemConfig, Path]:
    cfg_path = safe_workspace_path(workspace, PROBLEM_CONFIG_REL.as_posix())
    payload = load_problem_config(
        workspace,
        limits=problem_config_limits(runtime().config_values),
    )
    return (payload, dict(payload), cfg_path)
