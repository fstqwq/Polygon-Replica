"""Judgehost API wire shapes shared by the Docker E2E client and assertions."""

from __future__ import annotations

import os
from pathlib import Path


MOCK_STATE_FILENAME = "mock-judgehost.json"
MOCK_READY_FILENAME = "mock-ready"
BOOTSTRAP_FILENAME = "bootstrap.json"

ENDPOINTS = {
    "config": "config",
    "languages": "languages",
    "judgehosts": "judgehosts",
    "fetch_work": "judgehosts/fetch-work",
    "source": "judgehosts/get_files/source/{submitid}",
    "executable": "judgehosts/get_files/{file_type}/{script_id}",
    "testcase": "judgehosts/get_files/testcase/{testcase_id}",
    "version_commands": "judgehosts/get_version_commands/{judgetaskid}",
    "check_versions": "judgehosts/check_versions/{judgetaskid}",
    "update_judging": "judgehosts/update-judging/{hostname}/{judgetaskid}",
    "add_judging_run": "judgehosts/add-judging-run/{hostname}/{judgetaskid}",
    "add_debug_info": "judgehosts/add-debug-info/{hostname}/{judgetaskid}",
    "internal_error": "judgehosts/internal-error",
}

CONFIG_REQUIRED_FIELDS = (
    "diskspace_error",
    "output_storage_limit",
    "script_timelimit",
    "script_memory_limit",
    "script_filesize_limit",
    "timelimit_overshoot",
)

WORK_REQUIRED_FIELDS = (
    "type",
    "judgetaskid",
    "jobid",
    "uuid",
    "submitid",
    "compile_script_id",
    "run_script_id",
    "compare_script_id",
    "testcase_id",
    "testcase_hash",
    "compile_config",
    "run_config",
    "compare_config",
)

COMPILE_REPORT_FIELDS = (
    "compile_success",
    "output_compile",
    "compile_metadata",
)

FINAL_REPORT_FIELDS = (
    "runresult",
    "start_time",
    "end_time",
    "runtime",
    "output_run",
    "output_error",
    "output_system",
    "metadata",
    "output_diff",
    "hostname",
    "testcasedir",
    "compare_metadata",
)


def state_dir() -> Path:
    """Return the shared E2E state directory from the container environment."""

    return Path(os.environ["POLYGON_REPLICA_E2E_STATE_DIR"]).resolve()
