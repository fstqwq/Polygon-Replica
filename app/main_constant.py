from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_FILE_LIST_LIMIT = 1024
WORKSPACE_FILE_VIEW_CHAR_LIMIT = 262144
TEXTAREA_MAX_BYTES = 262144
UPLOAD_MAX_BYTES = 256 * 1024 * 1024
UI_LOG_TEXT_CHAR_LIMIT = 131072
RUN_DETAIL_TEST_LIST_LIMIT = 200
RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT = 200
RUN_TEST_FEEDBACK_FILE_LIST_LIMIT = 32
RUN_DETAIL_PREVIEW_MAX_BYTES = 256
RUN_TEST_SELECTOR_LIMIT = 600
PREVIEW_LOG_REF_LIST_LIMIT = 200
API_PROBLEMS_LIST_LIMIT = 200
DIAGNOSTIC_MESSAGE_CHAR_LIMIT = 4096
UI_JSON_CHAR_LIMIT = 1048576
WORKSPACE_HISTORY_LIMIT = 120
SOLUTION_LIST_LIMIT = 256
SOLUTION_NOTE_CHAR_LIMIT = 4096

AUTH_COOKIE_NAME = "polygonlike_session"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
AUTH_COOKIE_SECURE = True
SUDO_COOKIE_NAME = "polygonlike_sudo_session"
SUDO_COOKIE_MAX_AGE = 5 * 60
SUDO_SCOPE_DESTRUCTIVE = "destructive"
FLASH_COOKIE_NAME = "polygonlike_flash_queue"
FLASH_COOKIE_MAX_AGE = 24 * 60 * 60
FLASH_QUEUE_MAX_ITEMS = 16
FLASH_MESSAGE_MAX_LEN = 512
PASSWORD_FORM_CSRF_TTL_SEC = 900
LOGIN_RATE_LIMIT_WINDOW_SEC = 300.0
LOGIN_RATE_LIMIT_BLOCK_SEC = 300.0
LOGIN_RATE_LIMIT_MAX_FAILURES = 8

PROBLEM_ID_RULE_MESSAGE = (
    "invalid problem id. Use <owner>/<slug> with lowercased words separated by dash (64 characters max). "
    "Examples: alice/books, team-7/minimal-spanning-tree"
)
USERNAME_RULE_MESSAGE = (
    "invalid username. Use 3-16 lowercased characters, separated by dash. "
    "Examples: alice, team-7, judge-admin"
)

USER_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 16
PROBLEM_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")
PROBLEM_ID_MAX_LEN = 64
SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,256}$")
ROOT_PROBLEMS_PATH_RE = re.compile(r"^/problems(?:/import(?:/slug-hint)?)?$")
ROOT_CONTESTS_PATH_RE = re.compile(r"^/contests(?:/(?:create|import(?:/(?:review|confirm))?))?$")
RUN_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")

PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 256
PASSWORD_HASH_ITERS = 240000
CONTEST_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CONTEST_TITLE_MAX_LEN = 128
PROBLEM_NAME_MAX_LEN = 128

GENERAL_CONFIG_REL = Path("config/problem.json")
BUILD_CONFIG_REL = Path("config/build.json")
GENERAL_TIME_LIMIT_MIN_MS = 100
GENERAL_TIME_LIMIT_MAX_MS = 30000
GENERAL_MEMORY_LIMIT_MIN_MB = 8
GENERAL_MEMORY_LIMIT_MAX_MB = 2048
GENERAL_MODE_VALUES = ("pass-fail", "interactive")
GENERAL_PASS_LIMIT_MIN = 1
GENERAL_PASS_LIMIT_MAX = 64
GENERAL_CONFIG_DEFAULTS = {
    "time_limit_ms": 2000,
    "memory_limit_mb": 1024,
    "mode": "pass-fail",
    "pass_limit": 1,
}
TESTS_SPEC_ROWS_LIMIT = 256
TESTS_SPEC_PREVIEW_CHARS = 200
TESTS_SPEC_PREVIEW_LINES = 4
TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES = 16384
TESTS_SPEC_MANUAL_PREVIEW_BYTES = 256
TESTS_SPEC_MAX_ITEMS = 4096
TESTS_SPEC_GEN_COMMAND_MAX_CHARS = 1024
TESTS_SPEC_ID_RE = re.compile(r"^[0-9]{3,12}$")
RUN_PLACEHOLDER_VERIFICATION_ID = "pending"
WORKER_QUEUE_THREADS = 4
WORKER_QUEUE_HISTORY_LIMIT = 1024
WORKER_QUEUE_CAPACITY = 512
WORKER_QUEUE_DURABLE_HISTORY_LIMIT = 20000
WORKER_QUEUE_DURABLE_LOG = ""
DB_SQL_TRACE_ENABLED = False
JUDGEHOST_ENABLE = False
JUDGEHOST_API_TOKEN = ""
JUDGEHOST_API_USERNAME = "judgehost"
JUDGEHOST_FETCH_BATCH_SIZE = 1
JUDGEHOST_LEASE_SEC = 120
JUDGEHOST_WAIT_TIMEOUT_SEC = 7200
JUDGEHOST_WAIT_POLL_SEC = 0.5
JUDGEHOST_ONLINE_WINDOW_SEC = 120
JUDGEHOST_MAX_INLINE_SOURCE_BYTES = 262144
JUDGEHOST_MAX_TESTS_PER_TASK = 512
JUDGEHOST_INCLUDE_BUILD_PAYLOAD = True
JUDGEHOST_MAX_BINARY_PAYLOAD_BYTES = 8388608
TOOLCHAIN_COMPILE_TIMEOUT_SEC = 120
TOOLCHAIN_COMPILE_MEMORY_MB = 2048
TOOLCHAIN_COMPILE_PROCESS_LIMIT = 0
AUX_DISPLAY_TEXT_LIMIT_BYTES = 2048
# Compile/compare sandbox file cap in KiB. This is not a UI or persisted log
# display limit; it must stay large enough for normal compiler/checker output.
TOOLCHAIN_COMPILE_OUTPUT_KB = 262144
TOOLCHAIN_CACHE_CLEANUP_INTERVAL_SEC = 600
TOOLCHAIN_CACHE_MAX_BYTES = 2147483648
TOOLCHAIN_CACHE_MAX_ENTRIES = 0
VERIFICATION_EXEC_MEMORY_MB = 1024
VERIFICATION_EXEC_PROCESS_LIMIT = 64
RUN_EXEC_MEMORY_MB = 1024
RUN_EXEC_PROCESS_LIMIT = 64
# Judgehost run-stage stdout cap in KiB. Compile/compare script file caps use
# TOOLCHAIN_COMPILE_OUTPUT_KB instead.
RUN_EXEC_OUTPUT_KB = 65536
# Persisted/in-memory judgehost auxiliary log cap in bytes. Compile output and
# compile metadata are truncated to this size before base64 storage.
JUDGEHOST_STORED_LOG_LIMIT_BYTES = 65536
RUN_WALL_TIME_SLACK_PASS_FAIL_SEC = 1
RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC = 15
RUN_WALL_TIME_SLACK_INTERACTIVE_SEC = 15
PREVIEW_TEX_TIMEOUT_SEC = 120
PREVIEW_TEX_MEMORY_MB = 1024
PREVIEW_TEX_PROCESS_LIMIT = 64
PREVIEW_TEX_OUTPUT_KB = 131072
PASSWORD_FORM_CSRF_SECRET = ""

CORE_SOURCE_TARGETS = [
    {"label": "Checker", "path": "checkers/checker.cpp", "kind": "checker"},
    {"label": "Interactor", "path": "interactors/interactor.cpp", "kind": "interactor"},
    {"label": "Validator", "path": "validators/validator.cpp", "kind": "validator"},
    {"label": "Accepted Solution", "path": "solutions/accepted.cpp", "kind": "solution"},
]
FILE_TEMPLATES = {
    "generator": """#include "testlib.h"

int main(int argc, char* argv[]) {
    registerGen(argc, argv, 1);
    println(1);
    println(1);
    return 0;
}
""",
    "checker": """#include "testlib.h"

int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);
    const std::string jury = ans.readString();
    const std::string team = ouf.readString();
    if (jury == team) {
        quitf(_ok, "ok");
    }
    quitf(_wa, "expected '%s', found '%s'", jury.c_str(), team.c_str());
}
""",
    "interactor": """#include "testlib.h"

int main(int argc, char* argv[]) {
    registerInteraction(argc, argv);
    quitf(_fail, "interactor template: implement protocol");
}
""",
    "validator": """#include "testlib.h"

int main(int argc, char* argv[]) {
    registerValidation(argc, argv);
    inf.readEof();
}
""",
    "solution": """#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);
    return 0;
}
""",
}
CPP_SOURCE_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++"}
SOLUTION_SOURCE_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".py", ".java"}
TOOLCHAIN_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)
TOOLCHAIN_JAVA_MAIN_CLASS_RE = re.compile(r"\bpublic\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\b")
TOOLCHAIN_JAVA_JAVAC_FLAGS = (
    "-Xms16m",
    "-Xmx256m",
    "-XX:MaxMetaspaceSize=64m",
    "-XX:CompressedClassSpaceSize=32m",
)
TOOLCHAIN_JAVA_RUNTIME_FLAGS = (
    "-XX:+UseSerialGC",
    "-XX:TieredStopAtLevel=1",
    "-XX:ActiveProcessorCount=1",
    "-Xss256k",
    "-XX:-UseCompressedClassPointers",
)
TOOLCHAIN_JAVA_RUNTIME_DEFAULT_HEAP_MB = 256
TOOLCHAIN_JAVA_RUNTIME_MIN_HEAP_MB = 64
TOOLCHAIN_JAVA_RUNTIME_INITIAL_HEAP_MB = 16
TOOLCHAIN_CPP_COMPILER = "g++"
TOOLCHAIN_PYTHON_EXECUTABLE = "python3"
TOOLCHAIN_JAVA_COMPILER = "javac"
TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS = "-x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE"
TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS = ""
TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS = ""
TOOLCHAIN_CPP_CXXFLAGS = ("-O2", "-std=gnu++20", "-pipe", "-static")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[1] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
STANDARD_CHECKER_DESCRIPTIONS = {
    "acmp.cpp": "single double, absolute error <= 1.5e-6",
    "caseicmp.cpp": "Case i: <int64>, one integer per case",
    "casencmp.cpp": "Case i: <int64...>, integer sequence per case",
    "casewcmp.cpp": "Case i: <token...>, token sequence per case",
    "dcmp.cpp": "single double, absolute or relative error <= 1e-6",
    "fcmp.cpp": "compare files as sequence of full lines (exact)",
    "hcmp.cpp": "single signed huge integer (string-level exact match)",
    "icmp.cpp": "single signed int comparison",
    "lcmp.cpp": "line-by-line, compare tokens inside each line",
    "ncmp.cpp": "ordered sequence of signed int64 numbers",
    "nyesno.cpp": "multiple YES/NO tokens, case-insensitive",
    "pointscmp.cpp": "example scored checker using quitp(...)",
    "pointsinfo.cpp": "example checker with points_info via quitpi(...)",
    "rcmp.cpp": "single double, absolute error <= 1.5e-6",
    "rcmp4.cpp": "double sequence, abs/rel error <= 1e-4",
    "rcmp6.cpp": "double sequence, abs/rel error <= 1e-6",
    "rcmp9.cpp": "double sequence, abs/rel error <= 1e-9",
    "rncmp.cpp": "double sequence, absolute error <= 1.5e-5",
    "uncmp.cpp": "unordered sequence of signed int64 numbers",
    "wcmp.cpp": "ordered sequence of tokens",
    "yesno.cpp": "single YES/NO token, case-insensitive",
}

# System-admin editable runtime constants shown on the settings admin panel.
# Only scalar values are included so they can be validated and patched safely.
ADMIN_CONFIG_SPECS: dict[str, dict[str, object]] = {
    "WORKSPACE_FILE_LIST_LIMIT": {"type": "int", "min": 16, "max": 10000, "description": "Max files listed in file browser."},
    "WORKSPACE_FILE_VIEW_CHAR_LIMIT": {"type": "int", "min": 1024, "max": 2097152, "description": "Max characters loaded in file editor preview."},
    "TEXTAREA_MAX_BYTES": {"type": "int", "min": 1024, "max": 16777216, "description": "Shared UTF-8 byte limit for textarea form submissions."},
    "UPLOAD_MAX_BYTES": {"type": "int", "min": 1024, "max": 1073741824, "description": "Shared raw-byte limit for uploaded files."},
    "UI_LOG_TEXT_CHAR_LIMIT": {"type": "int", "min": 1024, "max": 2097152, "description": "Max log text rendered in UI."},
    "RUN_DETAIL_TEST_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max tests shown in run details."},
    "RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max diagnostics shown in run details."},
    "RUN_TEST_FEEDBACK_FILE_LIST_LIMIT": {"type": "int", "min": 1, "max": 1024, "description": "Max feedback files listed per test."},
    "RUN_DETAIL_PREVIEW_MAX_BYTES": {"type": "int", "min": 32, "max": 65536, "description": "Max preview bytes for artifact snippets."},
    "RUN_TEST_SELECTOR_LIMIT": {"type": "int", "min": 1, "max": 10000, "description": "Max test options shown in run form."},
    "PREVIEW_LOG_REF_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max statement log references parsed."},
    "API_PROBLEMS_LIST_LIMIT": {"type": "int", "min": 1, "max": 10000, "description": "Max problems/contests returned per list API."},
    "DIAGNOSTIC_MESSAGE_CHAR_LIMIT": {"type": "int", "min": 256, "max": 65536, "description": "Diagnostic message truncation limit."},
    "UI_JSON_CHAR_LIMIT": {"type": "int", "min": 1024, "max": 16777216, "description": "json parse limit in UI."},
    "WORKSPACE_HISTORY_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max commit rows shown on history page."},
    "SOLUTION_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max solution files listed."},
    "SOLUTION_NOTE_CHAR_LIMIT": {"type": "int", "min": 0, "max": 65536, "description": "Max solution metadata note length."},
    "AUTH_COOKIE_MAX_AGE": {"type": "int", "min": 60, "max": 31536000, "description": "Session cookie max age in seconds."},
    "AUTH_COOKIE_SECURE": {"type": "bool", "description": "Require HTTPS-only auth cookies."},
    "SUDO_COOKIE_MAX_AGE": {"type": "int", "min": 30, "max": 86400, "description": "Sudo-mode token max age in seconds."},
    "FLASH_COOKIE_MAX_AGE": {"type": "int", "min": 60, "max": 31536000, "description": "Flash cookie max age in seconds."},
    "FLASH_QUEUE_MAX_ITEMS": {"type": "int", "min": 1, "max": 256, "description": "Max queued flash messages."},
    "FLASH_MESSAGE_MAX_LEN": {"type": "int", "min": 16, "max": 4096, "description": "Per-flash message max length."},
    "PASSWORD_FORM_CSRF_TTL_SEC": {"type": "int", "min": 60, "max": 86400, "description": "Password form CSRF token lifetime in seconds."},
    "PASSWORD_FORM_CSRF_SECRET": {"type": "str", "description": "Password form CSRF signing secret (empty means random-at-startup).", "restart_required": False, "impact": "runtime"},
    "LOGIN_RATE_LIMIT_WINDOW_SEC": {"type": "float", "min": 1.0, "max": 86400.0, "description": "Login rate-limit observation window in seconds."},
    "LOGIN_RATE_LIMIT_BLOCK_SEC": {"type": "float", "min": 1.0, "max": 86400.0, "description": "Login rate-limit block duration in seconds."},
    "LOGIN_RATE_LIMIT_MAX_FAILURES": {"type": "int", "min": 1, "max": 1024, "description": "Max failed login attempts before blocking."},
    "PASSWORD_MIN_LEN": {"type": "int", "min": 1, "max": 512, "description": "Minimum user password length."},
    "PASSWORD_MAX_LEN": {"type": "int", "min": 1, "max": 4096, "description": "Maximum user password length."},
    "PASSWORD_HASH_ITERS": {"type": "int", "min": 10000, "max": 10000000, "description": "PBKDF2 iteration count."},
    "CONTEST_TITLE_MAX_LEN": {"type": "int", "min": 1, "max": 2048, "description": "Max contest title length."},
    "PROBLEM_NAME_MAX_LEN": {"type": "int", "min": 1, "max": 2048, "description": "Max problem name length."},
    "GENERAL_TIME_LIMIT_MIN_MS": {"type": "int", "min": 1, "max": 60000, "description": "Lower bound for problem TL (ms)."},
    "GENERAL_TIME_LIMIT_MAX_MS": {"type": "int", "min": 1, "max": 300000, "description": "Upper bound for problem TL (ms)."},
    "GENERAL_MEMORY_LIMIT_MIN_MB": {"type": "int", "min": 1, "max": 65536, "description": "Lower bound for memory limit (MB)."},
    "GENERAL_MEMORY_LIMIT_MAX_MB": {"type": "int", "min": 1, "max": 262144, "description": "Upper bound for memory limit (MB)."},
    "GENERAL_PASS_LIMIT_MIN": {"type": "int", "min": 1, "max": 1024, "description": "Lower bound for pass limit."},
    "GENERAL_PASS_LIMIT_MAX": {"type": "int", "min": 1, "max": 1024, "description": "Upper bound for pass limit."},
    "TESTS_SPEC_ROWS_LIMIT": {"type": "int", "min": 1, "max": 10000, "description": "Max tests rows loaded from tests/spec.json."},
    "TESTS_SPEC_PREVIEW_CHARS": {"type": "int", "min": 16, "max": 65536, "description": "Chars shown in tests/spec previews."},
    "TESTS_SPEC_PREVIEW_LINES": {"type": "int", "min": 1, "max": 1024, "description": "Lines shown in tests/spec previews."},
    "TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES": {"type": "int", "min": 128, "max": 10485760, "description": "Max bytes for inline manual test edits."},
    "TESTS_SPEC_MANUAL_PREVIEW_BYTES": {"type": "int", "min": 16, "max": 65536, "description": "Bytes shown for manual test payload preview."},
    "TOOLCHAIN_CPP_COMPILER": {"type": "str", "description": "C++ compiler executable for source compilation (for example: g++, clang++)."},
    "TOOLCHAIN_PYTHON_EXECUTABLE": {"type": "str", "description": "Python executable used for source compile-check."},
    "TOOLCHAIN_JAVA_COMPILER": {"type": "str", "description": "Java compiler executable used for source compilation (for example: javac)."},
    "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS": {"type": "str", "description": "Judgehost C++ compile flags used in DOMjudge-compatible compile script."},
    "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS": {"type": "str", "description": "Judgehost Java compile flags used in DOMjudge-compatible compile script."},
    "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS": {"type": "str", "description": "Judgehost Python interpreter flags used before -m py_compile in compile script."},
    "TOOLCHAIN_COMPILE_TIMEOUT_SEC": {"type": "int", "min": 5, "max": 1800, "description": "Compilation timeout in seconds.", "restart_required": False, "impact": "runtime"},
    "TOOLCHAIN_COMPILE_MEMORY_MB": {"type": "int", "min": 64, "max": 262144, "description": "Compilation memory limit in MB.", "restart_required": False, "impact": "runtime"},
    "TOOLCHAIN_COMPILE_PROCESS_LIMIT": {"type": "int", "min": 0, "max": 4096, "description": "Compilation process count limit (0 disables RLIMIT_NPROC).", "restart_required": False, "impact": "runtime"},
    "AUX_DISPLAY_TEXT_LIMIT_BYTES": {"type": "int", "min": 256, "max": 1048576, "description": "Unified byte cap for front-end-facing auxiliary text such as compile, error, feedback, and diagnostic messages.", "restart_required": False, "impact": "runtime"},
    "TOOLCHAIN_COMPILE_OUTPUT_KB": {"type": "int", "min": 1024, "max": 1048576, "description": "Judgehost compile/compare sandbox file size cap in KiB; this is not the saved or displayed log limit.", "restart_required": False, "impact": "runtime"},
    "TOOLCHAIN_CACHE_CLEANUP_INTERVAL_SEC": {"type": "int", "min": 0, "max": 86400, "description": "Compile cache cleanup interval in seconds.", "restart_required": False, "impact": "runtime"},
    "TOOLCHAIN_CACHE_MAX_BYTES": {"type": "int", "min": 0, "max": 1125899906842624, "description": "Compile cache size cap in bytes.", "restart_required": False, "impact": "runtime"},
    "TOOLCHAIN_CACHE_MAX_ENTRIES": {"type": "int", "min": 0, "max": 10000000, "description": "Compile cache entry cap (0 disables entry-count eviction).", "restart_required": False, "impact": "runtime"},
    "VERIFICATION_EXEC_MEMORY_MB": {"type": "int", "min": 16, "max": 262144, "description": "Verification-stage sandbox memory limit in MB.", "restart_required": False, "impact": "runtime"},
    "VERIFICATION_EXEC_PROCESS_LIMIT": {"type": "int", "min": 1, "max": 4096, "description": "Verification-stage sandbox process limit.", "restart_required": False, "impact": "runtime"},
    "RUN_EXEC_MEMORY_MB": {"type": "int", "min": 16, "max": 262144, "description": "Run-time sandbox memory limit in MB.", "restart_required": False, "impact": "runtime"},
    "RUN_EXEC_PROCESS_LIMIT": {"type": "int", "min": 1, "max": 4096, "description": "Run-time sandbox process limit.", "restart_required": False, "impact": "runtime"},
    "RUN_EXEC_OUTPUT_KB": {"type": "int", "min": 64, "max": 1048576, "description": "Judgehost run-stage stdout cap in KiB; compile/compare sandbox output uses TOOLCHAIN_COMPILE_OUTPUT_KB.", "restart_required": False, "impact": "runtime"},
    "JUDGEHOST_STORED_LOG_LIMIT_BYTES": {"type": "int", "min": 1024, "max": 16777216, "description": "Max bytes of judgehost auxiliary compile output and compile metadata stored server-side before truncation.", "restart_required": False, "impact": "runtime"},
    "RUN_WALL_TIME_SLACK_PASS_FAIL_SEC": {"type": "int", "min": 0, "max": 300, "description": "Wall-time slack seconds for pass-fail runs (effective timeout = 2*TL + slack).", "restart_required": False, "impact": "runtime"},
    "RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC": {"type": "int", "min": 0, "max": 300, "description": "Wall-time slack seconds for pass-limit runs with pass_limit > 1 (effective timeout = 2*TL + slack).", "restart_required": False, "impact": "runtime"},
    "RUN_WALL_TIME_SLACK_INTERACTIVE_SEC": {"type": "int", "min": 0, "max": 300, "description": "Wall-time slack seconds for interactive runs (effective timeout = 2*TL + slack).", "restart_required": False, "impact": "runtime"},
    "PREVIEW_TEX_TIMEOUT_SEC": {"type": "int", "min": 5, "max": 1800, "description": "TeX compile timeout in seconds.", "restart_required": False, "impact": "runtime"},
    "PREVIEW_TEX_MEMORY_MB": {"type": "int", "min": 16, "max": 262144, "description": "TeX compile memory limit in MB.", "restart_required": False, "impact": "runtime"},
    "PREVIEW_TEX_PROCESS_LIMIT": {"type": "int", "min": 1, "max": 4096, "description": "TeX compile process limit.", "restart_required": False, "impact": "runtime"},
    "PREVIEW_TEX_OUTPUT_KB": {"type": "int", "min": 64, "max": 1048576, "description": "TeX compile output cap in KB.", "restart_required": False, "impact": "runtime"},
    "WORKER_QUEUE_THREADS": {"type": "int", "min": 1, "max": 64, "description": "Worker queue thread count.", "restart_required": True, "impact": "restart"},
    "WORKER_QUEUE_HISTORY_LIMIT": {"type": "int", "min": 32, "max": 10000, "description": "In-memory worker queue history row cap.", "restart_required": True, "impact": "restart"},
    "WORKER_QUEUE_CAPACITY": {"type": "int", "min": 1, "max": 100000, "description": "Worker queue pending capacity.", "restart_required": True, "impact": "restart"},
    "WORKER_QUEUE_DURABLE_HISTORY_LIMIT": {"type": "int", "min": 256, "max": 200000, "description": "Worker durable event replay limit.", "restart_required": True, "impact": "restart"},
    "WORKER_QUEUE_DURABLE_LOG": {"type": "str", "description": "Worker durable event log file path (empty uses default cache path).", "restart_required": True, "impact": "restart"},
    "DB_SQL_TRACE_ENABLED": {"type": "bool", "description": "Enable per-statement SQLite trace logging (heavy; for debugging only).", "restart_required": False, "impact": "runtime"},
    "JUDGEHOST_ENABLE": {"type": "bool", "description": "Enable DOMserver-like judgehost queue APIs for verification execution."},
    "JUDGEHOST_API_TOKEN": {"type": "str", "ascii": "visible", "description": "Bearer token for judgehost API authentication."},
    "JUDGEHOST_API_USERNAME": {"type": "str", "ascii": "visible", "description": "Basic-auth username for DOMjudge judgehost compatibility API."},
    "JUDGEHOST_FETCH_BATCH_SIZE": {"type": "int", "min": 1, "max": 128, "description": "Default max tasks returned per judgehost fetch."},
    "JUDGEHOST_LEASE_SEC": {"type": "int", "min": 5, "max": 86400, "description": "Task lease duration for judgehost workers (seconds)."},
    "JUDGEHOST_WAIT_TIMEOUT_SEC": {"type": "int", "min": 5, "max": 86400, "description": "Backend wait timeout for judgehost task completion (seconds)."},
    "JUDGEHOST_WAIT_POLL_SEC": {"type": "float", "min": 0.05, "max": 30.0, "description": "Backend poll interval while waiting judgehost completion (seconds)."},
    "JUDGEHOST_ONLINE_WINDOW_SEC": {"type": "int", "min": 5, "max": 86400, "description": "Seconds a judgehost is considered online since last heartbeat/fetch/report event."},
    "JUDGEHOST_MAX_INLINE_SOURCE_BYTES": {"type": "int", "min": 1024, "max": 16777216, "description": "Max submission source bytes embedded in judgehost task payload."},
    "JUDGEHOST_MAX_TESTS_PER_TASK": {"type": "int", "min": 1, "max": 10000, "description": "Max tests embedded per judgehost task payload."},
    "JUDGEHOST_INCLUDE_BUILD_PAYLOAD": {"type": "bool", "description": "Include selected test payload and checker binaries in judgehost task payload."},
    "JUDGEHOST_MAX_BINARY_PAYLOAD_BYTES": {"type": "int", "min": 1024, "max": 134217728, "description": "Per-binary payload byte cap for embedded checker/interactor."},
}

# Explicitly reference dynamically-consumed defaults so dead-code scanners
# do not mark them as unused.
(
    PASSWORD_MIN_LEN,
    PASSWORD_MAX_LEN,
    TOOLCHAIN_COMPILE_PROCESS_LIMIT,
    AUX_DISPLAY_TEXT_LIMIT_BYTES,
    TOOLCHAIN_COMPILE_OUTPUT_KB,
    TOOLCHAIN_CACHE_CLEANUP_INTERVAL_SEC,
    TOOLCHAIN_CACHE_MAX_BYTES,
    TOOLCHAIN_CACHE_MAX_ENTRIES,
    VERIFICATION_EXEC_MEMORY_MB,
    VERIFICATION_EXEC_PROCESS_LIMIT,
    PREVIEW_TEX_TIMEOUT_SEC,
    PREVIEW_TEX_MEMORY_MB,
    PREVIEW_TEX_PROCESS_LIMIT,
    PREVIEW_TEX_OUTPUT_KB,
    JUDGEHOST_STORED_LOG_LIMIT_BYTES,
)

ADMIN_CONFIG_DEFAULTS: dict[str, object] = {
    key: globals()[key] for key in ADMIN_CONFIG_SPECS
}
