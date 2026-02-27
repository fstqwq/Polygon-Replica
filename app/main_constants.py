from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_FILE_LIST_LIMIT = 1024
WORKSPACE_FILE_VIEW_CHAR_LIMIT = 131072
UI_LOG_TEXT_CHAR_LIMIT = 131072
RUN_DETAIL_TEST_LIST_LIMIT = 200
RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT = 200
RUN_TEST_FEEDBACK_FILE_LIST_LIMIT = 32
RUN_DETAIL_PREVIEW_MAX_BYTES = 256
RUN_INVOCATION_LIST_SCAN_FACTOR = 8
RUN_INVOCATION_LIST_SUMMARY_ROW_CHAR_LIMIT = 65536
RUN_INVOCATION_LIST_SUMMARY_TOTAL_CHAR_BUDGET = 524288
RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS = 96
RUN_TEST_SELECTOR_LIMIT = 600
PREVIEW_LOG_REF_LIST_LIMIT = 200
STATEMENT_EDITOR_CHAR_LIMIT = 262144
API_PROBLEMS_LIST_LIMIT = 200
DIAGNOSTIC_MESSAGE_CHAR_LIMIT = 4096
SUMMARY_JSON_UI_CHAR_LIMIT = 1048576
WORKSPACE_HISTORY_LIMIT = 120
SOLUTION_LIST_LIMIT = 256
SOLUTION_NOTE_CHAR_LIMIT = 4096

AUTH_COOKIE_NAME = "polygonlike_session"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60
AUTH_COOKIE_SECURE = True
FLASH_COOKIE_NAME = "polygonlike_flash_queue"
FLASH_COOKIE_MAX_AGE = 24 * 60 * 60
FLASH_QUEUE_MAX_ITEMS = 16
FLASH_MESSAGE_MAX_LEN = 512
PASSWORD_FORM_CSRF_TTL_SEC = 900
LOGIN_RATE_LIMIT_WINDOW_SEC = 300.0
LOGIN_RATE_LIMIT_BLOCK_SEC = 300.0
LOGIN_RATE_LIMIT_MAX_FAILURES = 8

PROBLEM_ID_RULE_MESSAGE = (
    "invalid problem id. Use lowercased words, separated by dash. "
    "Examples: books, minimal-spanning-tree, stamps-3"
)
USERNAME_RULE_MESSAGE = (
    "invalid username. Use lowercased words, separated by dash. "
    "Examples: alice, team-7, judge-admin"
)

USER_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PROBLEM_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,256}$")
TOPLEVEL_USER_PATH_RE = re.compile(r"^/problems/(?P<user>[^/]+)/(?P<section>problems|contests)(?P<rest>/.*)?$")
PROBLEM_USER_PATH_RE = re.compile(r"^/problems/(?P<problem>[^/]+)/(?P<user>[^/]+)(?P<rest>/.*)?$")
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
GENERAL_MODE_VALUES = ("pass-fail", "interactive", "multi-pass")
GENERAL_CONFIG_DEFAULTS = {
    "input_file": "stdin",
    "output_file": "stdout",
    "time_limit_ms": 2000,
    "memory_limit_mb": 1024,
    "mode": "pass-fail",
}
TESTS_SPEC_ROWS_LIMIT = 256
TESTS_SPEC_PREVIEW_CHARS = 200
TESTS_SPEC_PREVIEW_LINES = 4
TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES = 16384
TESTS_SPEC_MANUAL_PREVIEW_BYTES = 256
TESTS_SPEC_VERSION = 2
TESTS_SPEC_MAX_ITEMS = 4096
TESTS_SPEC_MANUAL_MAX_CHARS = 262144
TESTS_SPEC_GEN_COMMAND_MAX_CHARS = 1024
TESTS_SPEC_ID_RE = re.compile(r"^[0-9]{3,12}$")
IMPLICIT_BUILD_DIRTY_REUSE_SEC = 60
RUN_PLACEHOLDER_BUILD_ID = "pending"

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
TOOLCHAIN_JAVA_JAVAC_FLAGS = ("-XX:CompressedClassSpaceSize=128m",)
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
TOOLCHAIN_CACHE_CLEANUP_LOCK = ".cleanup.lock"
TOOLCHAIN_CPP_CXXFLAGS = ("-O2", "-std=c++20", "-pipe", "-static")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STANDARD_CHECKER_ROOT = (Path(__file__).resolve().parents[1] / "third_party" / "upstream" / "testlib" / "checkers").resolve()
TESTS_MANUAL_BATCH_SPLIT_RE = re.compile(r"(?m)^\s*(?:---+|===+)\s*$")
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
    "UI_LOG_TEXT_CHAR_LIMIT": {"type": "int", "min": 1024, "max": 2097152, "description": "Max log text rendered in UI."},
    "RUN_DETAIL_TEST_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max tests shown in run details."},
    "RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max diagnostics shown in run details."},
    "RUN_TEST_FEEDBACK_FILE_LIST_LIMIT": {"type": "int", "min": 1, "max": 1024, "description": "Max feedback files listed per test."},
    "RUN_DETAIL_PREVIEW_MAX_BYTES": {"type": "int", "min": 32, "max": 65536, "description": "Max preview bytes for artifact snippets."},
    "RUN_INVOCATION_LIST_SCAN_FACTOR": {"type": "int", "min": 1, "max": 64, "description": "Run list scan multiplier over page size."},
    "RUN_INVOCATION_LIST_SUMMARY_ROW_CHAR_LIMIT": {"type": "int", "min": 256, "max": 2097152, "description": "Per-row summary char cap."},
    "RUN_INVOCATION_LIST_SUMMARY_TOTAL_CHAR_BUDGET": {"type": "int", "min": 1024, "max": 16777216, "description": "Total summary char budget in run list."},
    "RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS": {"type": "int", "min": 1, "max": 2048, "description": "Max summary rows shown for invocations."},
    "RUN_TEST_SELECTOR_LIMIT": {"type": "int", "min": 1, "max": 10000, "description": "Max test options shown in run form."},
    "PREVIEW_LOG_REF_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max statement log references parsed."},
    "STATEMENT_EDITOR_CHAR_LIMIT": {"type": "int", "min": 2048, "max": 4194304, "description": "Statement editor content limit."},
    "API_PROBLEMS_LIST_LIMIT": {"type": "int", "min": 1, "max": 10000, "description": "Max problems/contests returned per list API."},
    "DIAGNOSTIC_MESSAGE_CHAR_LIMIT": {"type": "int", "min": 256, "max": 65536, "description": "Diagnostic message truncation limit."},
    "SUMMARY_JSON_UI_CHAR_LIMIT": {"type": "int", "min": 1024, "max": 16777216, "description": "summary_json parse limit in UI."},
    "WORKSPACE_HISTORY_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max commit rows shown on history page."},
    "SOLUTION_LIST_LIMIT": {"type": "int", "min": 1, "max": 5000, "description": "Max solution files listed."},
    "SOLUTION_NOTE_CHAR_LIMIT": {"type": "int", "min": 0, "max": 65536, "description": "Max solution metadata note length."},
    "AUTH_COOKIE_MAX_AGE": {"type": "int", "min": 60, "max": 31536000, "description": "Session cookie max age in seconds."},
    "AUTH_COOKIE_SECURE": {"type": "bool", "description": "Require HTTPS-only auth cookies."},
    "FLASH_COOKIE_MAX_AGE": {"type": "int", "min": 60, "max": 31536000, "description": "Flash cookie max age in seconds."},
    "FLASH_QUEUE_MAX_ITEMS": {"type": "int", "min": 1, "max": 256, "description": "Max queued flash messages."},
    "FLASH_MESSAGE_MAX_LEN": {"type": "int", "min": 16, "max": 4096, "description": "Per-flash message max length."},
    "PASSWORD_FORM_CSRF_TTL_SEC": {"type": "int", "min": 60, "max": 86400, "description": "Password form CSRF token lifetime in seconds."},
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
    "TESTS_SPEC_ROWS_LIMIT": {"type": "int", "min": 1, "max": 10000, "description": "Max tests rows loaded from tests/spec.json."},
    "TESTS_SPEC_PREVIEW_CHARS": {"type": "int", "min": 16, "max": 65536, "description": "Chars shown in tests/spec previews."},
    "TESTS_SPEC_PREVIEW_LINES": {"type": "int", "min": 1, "max": 1024, "description": "Lines shown in tests/spec previews."},
    "TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES": {"type": "int", "min": 128, "max": 10485760, "description": "Max bytes for inline manual test edits."},
    "TESTS_SPEC_MANUAL_PREVIEW_BYTES": {"type": "int", "min": 16, "max": 65536, "description": "Bytes shown for manual test payload preview."},
    "IMPLICIT_BUILD_DIRTY_REUSE_SEC": {"type": "int", "min": 0, "max": 86400, "description": "Dirty implicit-build reuse window in seconds."},
}

ADMIN_CONFIG_DEFAULTS: dict[str, object] = {
    key: globals()[key] for key in ADMIN_CONFIG_SPECS
}
