"""Fixed application invariants, paths, regular expressions, and templates."""

import re
from pathlib import Path
from typing import TypedDict

SUDO_SCOPE_DESTRUCTIVE = "destructive"

PROBLEM_ID_RULE_MESSAGE = (
    "invalid problem id. Use <owner>/<slug> with lowercased words separated by dash "
    "(64 characters max). "
    "Examples: alice/books, team-7/minimal-spanning-tree"
)
USERNAME_RULE_MESSAGE = (
    "invalid username. Use 3-16 letters or digits, separated by dash. "
    "Examples: alice, Qingyu, team-7, judge-admin"
)

USER_IDENT_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 16
PROBLEM_IDENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*$")
PROBLEM_ID_MAX_LEN = 64
SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{20,256}$")
ROOT_PROBLEMS_PATH_RE = re.compile(r"^/problems(?:/import(?:/slug-hint)?)?$")
ROOT_CONTESTS_PATH_RE = re.compile(
    r"^/contests(?:/(?:create|import(?:/(?:review|confirm))?))?$"
)
RUN_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX_32_RE = re.compile(r"^[0-9a-f]{32}$")

CONTEST_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

GENERAL_CONFIG_REL = Path("config/problem.json")
BUILD_CONFIG_REL = Path("config/build.json")
GENERAL_MODE_VALUES = ("pass-fail", "interactive")
class GeneralConfigDefaults(TypedDict):
    """Canonical scalar types for a newly-created problem configuration."""

    time_limit_ms: int
    memory_limit_mb: int
    mode: str
    pass_limit: int


GENERAL_CONFIG_DEFAULTS: GeneralConfigDefaults = {
    "time_limit_ms": 2000,
    "memory_limit_mb": 1024,
    "mode": "pass-fail",
    "pass_limit": 1,
}
TESTS_SPEC_MAX_ITEMS = 4096
TESTS_SPEC_GEN_COMMAND_MAX_CHARS = 1024
TESTS_SPEC_ID_RE = re.compile(r"^[0-9]{3,12}$")
RUN_PLACEHOLDER_VERIFICATION_ID = "pending"
CORE_SOURCE_TARGETS = [
    {"label": "Checker", "path": "checkers/checker.cpp", "kind": "checker"},
    {"label": "Interactor", "path": "interactors/interactor.cpp", "kind": "interactor"},
    {"label": "Validator", "path": "validators/validator.cpp", "kind": "validator"},
    {
        "label": "Accepted Solution",
        "path": "solutions/accepted.cpp",
        "kind": "solution",
    },
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
TOOLCHAIN_JAVA_MAIN_CLASS_RE = re.compile(
    r"\bpublic\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
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
TOOLCHAIN_CPP_CXXFLAGS = ("-O2", "-std=gnu++20", "-pipe", "-static")
STANDARD_CHECKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STANDARD_CHECKER_ROOT = (
    Path(__file__).resolve().parents[1]
    / "third_party"
    / "testlib"
    / "checkers"
).resolve()
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
