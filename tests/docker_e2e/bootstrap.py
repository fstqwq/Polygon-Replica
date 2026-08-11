"""Create a fresh, dirty authoring workspace for the Docker black-box run."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    os.environ.get("POLYGON_REPLICA_E2E_REPO_ROOT", "/opt/polygon-replica"),
)

from app.db import DB, now_iso  # noqa: E402
from app.service.statement.render import seed_statement_sources  # noqa: E402

from judgehost_protocol import BOOTSTRAP_FILENAME, state_dir  # noqa: E402


PROBLEM = "e2e/sample"
USERNAME = "e2e"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _configure_database() -> None:
    db = DB(Path(os.environ["POLYGON_REPLICA_DB"]))
    db.init()
    values: dict[str, object] = {
        "JUDGEHOST_ENABLE": True,
        "JUDGEHOST_API_USERNAME": "judgehost",
        "JUDGEHOST_API_TOKEN": os.environ["POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN"],
        "JUDGEHOST_FETCH_BATCH_SIZE": 8,
        "JUDGEHOST_WAIT_POLL_SEC": 0.1,
    }
    for key, value in values.items():
        db.execute(
            """
            INSERT INTO system_config(key,value_json,updated_at,updated_by_user_id)
            VALUES(?,?,?,NULL)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at,
                updated_by_user_id=NULL
            """,
            [key, json.dumps(value, ensure_ascii=True, separators=(",", ":")), now_iso()],
        )


def _seed_workspace() -> tuple[Path, int, int, int, str]:
    # RuntimeConfig must be imported only after the persisted Judgehost settings
    # exist; its service graph reads restart-required values during construction.
    from app.impl.runtime.config import config

    config.workspace_service.ensure_problem(PROBLEM)
    workspace = Path(config.workspace_service.ensure_workspace(PROBLEM, USERNAME))
    config.workspace_service.grant_repo_access(PROBLEM, USERNAME, "owner")

    for relative in (
        "config",
        "generators",
        "solutions",
        "validators",
        "tests/generator",
    ):
        (workspace / relative).mkdir(parents=True, exist_ok=True)
    seed_statement_sources(workspace)

    _write_json(
        workspace / "config/problem.json",
        {
            "input_file": "stdin",
            "memory_limit_mb": 4,
            "mode": "pass-fail",
            "output_file": "stdout",
            "pass_limit": 1,
            "time_limit_ms": 2000,
        },
    )
    _write_json(
        workspace / "config/build.json",
        {
            "accepted_solution_source": "solutions/main.cpp",
            "validator_source": "validators/validate.cpp",
            "checker_source": "",
            "generator_sources": ["generators/gen.py"],
        },
    )
    _write_json(
        workspace / "tests/spec.json",
        {"tests": [{"id": "001", "kind": "gen", "sample": True}]},
    )
    (workspace / "tests/generator/001.in").write_text("gen.py 7\n", encoding="utf-8")
    (workspace / "generators/gen.py").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print(sys.argv[1])\n",
        encoding="utf-8",
    )
    (workspace / "solutions/main.cpp").write_text(
        "#include <iostream>\n"
        "int main() { long long value = 0; std::cin >> value; "
        "std::cout << value * value << '\\n'; }\n",
        encoding="utf-8",
    )
    (workspace / "solutions/main.cpp.desc").write_text(
        "expected: accepted\n",
        encoding="utf-8",
    )
    (workspace / "solutions/re.py").write_text(
        "raise RuntimeError('intentional E2E runtime error')\n",
        encoding="utf-8",
    )
    (workspace / "solutions/re.py.desc").write_text(
        "expected: run_time_error\n",
        encoding="utf-8",
    )
    (workspace / "solutions/ce.cpp").write_text(
        "this is intentionally not valid C++\n",
        encoding="utf-8",
    )
    (workspace / "solutions/ce.cpp.desc").write_text(
        "expected: rejected\n",
        encoding="utf-8",
    )
    (workspace / "validators/validate.cpp").write_text(
        '#include "testlib.h"\n'
        "int main(int argc, char **argv) { registerValidation(argc, argv); "
        "inf.readLong(); inf.readEof(); }\n",
        encoding="utf-8",
    )

    user = config.workspace_service.ensure_user(USERNAME)
    user_id = int(user["id"])
    context = config.workspace_service.workspace_context(
        PROBLEM,
        USERNAME,
        include_recent=False,
    )
    problem_id = int(context["problem"]["id"])
    workspace_id = int(context["workspace"]["id"])
    session_token = config.auth_service.create_session_for_user(user_id)
    return workspace, user_id, problem_id, workspace_id, session_token


def main() -> None:
    _configure_database()
    workspace, user_id, problem_id, workspace_id, session_token = _seed_workspace()

    from app.impl.runtime.config import config

    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    _write_json(
        state / BOOTSTRAP_FILENAME,
        {
            "problem": PROBLEM,
            "username": USERNAME,
            "user_id": user_id,
            "problem_id": problem_id,
            "workspace_id": workspace_id,
            "workspace": str(workspace),
            "session_cookie_name": str(config.config_values.AUTH_COOKIE_NAME),
            "session_token": session_token,
        },
    )
    print(f"bootstrapped Docker E2E workspace problem={PROBLEM} user={USERNAME}")


if __name__ == "__main__":
    main()
