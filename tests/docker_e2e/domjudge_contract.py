"""DOMjudge 9.0.1 Judgehost wire facts used by the Docker E2E mock.

The source verifier proves these facts against the pinned official sources before
the mock is allowed to contact Polygon-Replica.  Keep this module dependency-free
so both the verifier and the mock consume exactly the same declaration.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


UPSTREAM_REPOSITORY = "https://github.com/DOMjudge/domjudge.git"
UPSTREAM_TAG = "9.0.1"
UPSTREAM_PEELED_COMMIT = "90bbb727906efb438ac2ec7512c09f17824cfc41"
JUDGEDAEMON_SOURCE = Path("judge/judgedaemon.main.php")
JUDGEHOST_CONTROLLER_SOURCE = Path(
    "webapp/src/Controller/API/JudgehostController.php"
)

APPROVAL_FILENAME = "domjudge-contract-approved.json"
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

# A mock behavior is approved only when all of its literals occur in the named
# source file at the pinned commit.  The daemon proves what an actual Judgehost
# sends and how it consumes responses; the controller proves what the official
# DOMjudge server means by those responses.  Polygon-Replica-specific ACK and
# diagnostic semantics are exercised by the black-box mock, not attributed to
# the upstream controller.
SOURCE_REQUIREMENTS: dict[Path, dict[str, tuple[str, ...]]] = {
    JUDGEDAEMON_SOURCE: {
        "basic-auth": (
            "CURLOPT_HTTPAUTH, CURLAUTH_BASIC",
            "CURLOPT_USERPWD, $restuser . \":\" . $restpass",
        ),
        "http-success-is-any-2xx": (
            "$status < 200 || $status >= 300",
            "if ($response !== null)",
        ),
        "array-post-is-multipart": (
            "if (is_array($data))",
            "Content-Type: multipart/form-data",
            "curl_setopt($curl_handle, CURLOPT_POSTFIELDS, $data);",
        ),
        "bootstrap": (
            "request('config', 'GET')",
            "request('languages', 'GET')",
            "request('judgehosts', 'POST', 'hostname=' . urlencode($myhost), false)",
            "$id = $language['id']",
            "$langexts[$id] = $language['extensions']",
        ),
        "config-values": (
            "djconfig_get_value('diskspace_error')",
            "djconfig_get_value('output_storage_limit')",
            "djconfig_get_value('script_timelimit')",
            "djconfig_get_value('script_memory_limit')",
            "djconfig_get_value('script_filesize_limit')",
            "djconfig_get_value('timelimit_overshoot')",
        ),
        "fetch-work-multipart": (
            "request('judgehosts/fetch-work', 'POST', ['hostname' => $myhost], false)",
        ),
        "fetch-work-shape": (
            "$row[0]['type']",
            "$row[0]['uuid']",
            "$judgeTask['judgetaskid']",
            "$judgeTask['jobid']",
            "$judgeTask['submitid']",
            "$judgeTask['compile_script_id']",
            "$judgeTask['run_script_id']",
            "$judgeTask['compare_script_id']",
            "$judgeTask['testcase_id']",
            "$judgeTask['testcase_hash']",
            "$judgeTask['compile_config']",
            "$judgeTask['run_config']",
            "$judgeTask['compare_config']",
        ),
        "downloads": (
            "request(sprintf('judgehosts/get_files/%s/%s', $type, $execid), 'GET')",
            "$url = sprintf('judgehosts/get_files/source/%s', $judgeTask['submitid'])",
            "request(sprintf('judgehosts/get_files/testcase/%s', $testcase_id), 'GET', '', false)",
            "foreach (['input', 'output'] as $inout)",
            "base64_decode($source['content'])",
            "fn($file) => $file['hash'] . $file['filename'] . $file['is_executable']",
            "if ($hash !== $computedHash)",
        ),
        "version-telemetry": (
            "request('judgehosts/get_version_commands/' . $judgeTaskId, 'GET')",
            "request('judgehosts/check_versions/' . $judgeTaskId, 'PUT', $args)",
            "&$type=\" . urlencode(base64_encode($versions[$type]))",
        ),
        "compile-report": (
            "'compile_success=' . $compile_success",
            "'&output_compile=' . urlencode(rest_encode_file($workdir . '/compile.out', $output_storage_limit))",
            "'&compile_metadata=' . urlencode(rest_encode_file($workdir . '/compile.meta', false))",
            "$url = sprintf('judgehosts/update-judging/%s/%s', urlencode($myhost), urlencode((string)$judgeTask['judgetaskid']));\n"
            "    request($url, 'PUT', $args);",
            "if (! $compile_success)",
            "return false;",
        ),
        "final-report": (
            "'runresult' => urlencode($result)",
            "'start_time' => urlencode((string)$startTime)",
            "'end_time' => urlencode((string)microtime(true))",
            "'runtime' => urlencode((string)$runtime)",
            "'output_run' => rest_encode_file($passdir . '/program.out', $output_storage_limit)",
            "'output_error' => rest_encode_file($passdir . '/program.err', $output_storage_limit)",
            "'output_system' => rest_encode_file($passdir . '/system.out', $output_storage_limit)",
            "'metadata' => rest_encode_file($passdir . '/program.meta', false)",
            "'output_diff' => rest_encode_file($passdir . '/feedback/judgemessage.txt', $output_storage_limit)",
            "'hostname' => $myhost",
            "'testcasedir' => $testcasedir",
            "'compare_metadata' => rest_encode_file($passdir . '/compare.meta', false)",
            "sprintf('judgehosts/add-judging-run/%s/%s', urlencode($myhost),\n"
            "                urlencode((string)$judgeTask['judgetaskid'])),\n"
            "            'POST',\n"
            "            $new_judging_run,\n"
            "            false",
            "sprintf('judgehosts/add-judging-run/%s/%s', $new_judging_run['hostname'],\n"
            "                urlencode((string)$judgeTaskId)),\n"
            "            'POST',\n"
            "            $new_judging_run,\n"
            "            false",
        ),
        "async-final-needs-http-success": (
            "$response = request(",
            "if ($response !== null)",
            "$success = true;",
            "Final attempt of uploading jt$judgeTaskId was unsuccessful",
        ),
        "incorrect-final-uses-response-body": (
            "$needsMoreWork = request(",
            "$ret = (bool)$needsMoreWork;",
        ),
        "debug-info-upload": (
            "if ($type == 'debug_info')",
            "'judgehosts/add-debug-info/%s/%s'",
            "'POST',",
            "['full_debug' => rest_encode_file($tmpfile, false)]",
            "['output_run' => rest_encode_file($testcasedir . '/program.out', false)]",
        ),
        "internal-error-upload": (
            "'&judgehostlog=' . urlencode(base64_encode($judgehostlog))",
            "'&disabled=' . urlencode($disabled)",
            "$args .= '&judgetaskid=' . urlencode((string)$judgeTaskId);",
            "request('judgehosts/internal-error', 'POST', $args)",
        ),
    },
    JUDGEHOST_CONTROLLER_SOURCE: {
        "official-final-response": (
            "#[Rest\\Post('/add-judging-run/{hostname}/{judgeTaskId<\\d+>}')]",
            "$hasFinalResult = $this->addSingleJudgingRun(",
            "return (int)$hasFinalResult;",
            "return $judging->getResult() === null || $judging->getJudgeCompletely()",
        ),
        "official-debug-storage": (
            "#[Rest\\Post('/add-debug-info/{hostname}/{judgeTaskId<\\d+>}')]",
            "'output_run',",
            "'full_debug',",
            "$debug_package = new DebugPackage();",
            "->setFilename($tempFilename);",
            "$judgingRunOutput->setOutputRun($outputRun);",
        ),
        "official-internal-error-record": (
            "#[Rest\\Post('/internal-error')]",
            "$required = ['description', 'judgehostlog', 'disabled'];",
            "$error = new InternalError();",
            "$this->em->persist($error);",
            "$judging?->setInternalError($error);",
        ),
    },
}


def verified_behaviors() -> list[str]:
    """Return stable source-qualified behavior identities for approval JSON."""

    return sorted(
        f"{source.as_posix()}:{behavior}"
        for source, requirements in SOURCE_REQUIREMENTS.items()
        for behavior in requirements
    )


def state_dir() -> Path:
    """Return the shared E2E state directory from the container environment."""

    return Path(os.environ["POLYGON_REPLICA_E2E_STATE_DIR"]).resolve()


def require_approval() -> dict[str, object]:
    """Load a successful verification record for the one permitted upstream."""

    approval_path = state_dir() / APPROVAL_FILENAME
    try:
        raw = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("DOMjudge source contract has not been approved") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("DOMjudge source approval has an invalid shape")
    approval = dict(raw)
    expected = {
        "approved": True,
        "repository": UPSTREAM_REPOSITORY,
        "tag": UPSTREAM_TAG,
        "commit": UPSTREAM_PEELED_COMMIT,
    }
    mismatches = {
        key: (expected_value, approval.get(key))
        for key, expected_value in expected.items()
        if approval.get(key) != expected_value
    }
    source_digests = approval.get("source_sha256s")
    expected_sources = {path.as_posix() for path in SOURCE_REQUIREMENTS}
    if not isinstance(source_digests, dict) or set(source_digests) != expected_sources:
        mismatches["source_sha256s"] = (
            f"digests for {sorted(expected_sources)!r}",
            source_digests,
        )
    elif any(
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in source_digests.values()
    ):
        mismatches["source_sha256s"] = (
            "64 lowercase hex characters for every source",
            source_digests,
        )
    if approval.get("verified_behaviors") != verified_behaviors():
        mismatches["verified_behaviors"] = (
            verified_behaviors(),
            approval.get("verified_behaviors"),
        )
    if mismatches:
        raise RuntimeError(f"DOMjudge source approval does not match the pinned contract: {mismatches!r}")
    return approval
