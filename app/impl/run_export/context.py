from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.db import now_iso
from app.impl.auth.public import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.public import (
    allocate_verification_id,
    allocate_run_id,
    assert_workspace_artifact_access,
    assert_workspace_verification_access,
    audit,
    browser_file_response,
    build_run_detail_context,
    dedupe_preserve_order,
    export_download_filename,
    git_commit_count,
    latest_workspace_stage_verification,
    latest_workspace_committed_stage_verification,
    normalize_problem_mode,
    normalize_run_id_token,
    normalize_run_test_name_token,
    parse_verification_detail_id,
    parse_run_test_names,
    read_problem_config,
    record_async_run_failure,
    require_write_access,
    run_list_rows,
    run_solution_options_context,
    run_test_options_context,
    verification_record_run_ids,
    safe_artifact_path,
    safe_run_artifact_path,
    start_export_job,
    start_run_execute_batch,
    workspace_verification_id_for_run,
    workspace_run_artifact_root,
    page_ctx,
)
from app.main_util import (
    contains_symlink_component,
    normalize_optional_component_source_path,
    normalize_optional_component_source_path_safe,
    upload_compile_check_error,
    workspace_source_compile_check_error,
)
from app.service.importing.icpc import ICPCPackageImportService
from app.service.importing.polygon import PolygonPackageImportService
from app.service.problem.solution_metadata import (
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
)
from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec
from app.service.platform.process import is_canonical_artifact_id, run_cmd

_C = config.constants
_POLYGON_IMPORTER = PolygonPackageImportService()
_ICPC_IMPORTER = ICPCPackageImportService()
_POLYGON_LINUX_PACKAGE_SUFFIX_RE = re.compile(r"-\d+\$linux$", re.IGNORECASE)
_PROBLEM_SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _select_importer(package_format: str):
    token = str(package_format or "").strip().lower()
    if token == "polygon":
        return _POLYGON_IMPORTER
    if token == "icpc":
        return _ICPC_IMPORTER
    raise ValueError(f"unsupported package format: {package_format}")



