"""Complete definitions for admin-editable system configuration."""

from app.config.model import ConfigDefinition, ConfigKind, ConfigPolicy, TextPolicy

_DEFAULT_POLICY = ConfigPolicy()


def _bounds(
    minimum: int | float,
    maximum: int | float,
    *,
    restart: bool = False,
) -> ConfigPolicy:
    return ConfigPolicy(
        minimum=minimum,
        maximum=maximum,
        restart_required=restart,
    )


def _text(
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    policy: TextPolicy = TextPolicy.PRINTABLE_ASCII,
    restart: bool = False,
) -> ConfigPolicy:
    return ConfigPolicy(
        minimum=minimum,
        maximum=maximum,
        text_policy=policy,
        restart_required=restart,
    )


def _int(
    key: str,
    default: int,
    category: str,
    description: str,
    policy: ConfigPolicy,
) -> ConfigDefinition:
    return ConfigDefinition(
        key=key,
        kind=ConfigKind.INT,
        default=default,
        category=category,
        description=description,
        policy=policy,
    )


def _float(
    key: str,
    default: float,
    category: str,
    description: str,
    policy: ConfigPolicy,
) -> ConfigDefinition:
    return ConfigDefinition(
        key=key,
        kind=ConfigKind.FLOAT,
        default=default,
        category=category,
        description=description,
        policy=policy,
    )


def _bool(key: str, default: bool, category: str, description: str) -> ConfigDefinition:
    return ConfigDefinition(
        key=key,
        kind=ConfigKind.BOOL,
        default=default,
        category=category,
        description=description,
    )


def _str(
    key: str,
    default: str,
    category: str,
    description: str,
    policy: ConfigPolicy = _DEFAULT_POLICY,
) -> ConfigDefinition:
    return ConfigDefinition(
        key=key,
        kind=ConfigKind.STR,
        default=default,
        category=category,
        description=description,
        policy=policy,
    )


UI_AND_LIMIT_DEFINITIONS = (
    _int(
        "WORKSPACE_FILE_LIST_LIMIT",
        1024,
        "UI",
        "Max files listed in file browser.",
        _bounds(16, 10000),
    ),
    _int(
        "WORKSPACE_FILE_VIEW_CHAR_LIMIT",
        262144,
        "UI",
        "Max characters loaded in file editor preview.",
        _bounds(1024, 2097152),
    ),
    _int(
        "TEXTAREA_MAX_BYTES",
        262144,
        "Misc",
        "Shared UTF-8 byte limit for textarea form submissions.",
        _bounds(1024, 16777216),
    ),
    _int(
        "STATEMENT_SAMPLE_MAX_BYTES",
        32768,
        "Limits",
        "Max combined UTF-8 bytes for one statement sample input and output.",
        _bounds(1024, 262144),
    ),
    _int(
        "UPLOAD_MAX_BYTES",
        256 * 1024 * 1024,
        "Misc",
        "Shared raw-byte limit for uploads and Judgehost program input/output.",
        _bounds(1024, 1024 * 1024 * 1024),
    ),
    _int(
        "UI_LOG_TEXT_CHAR_LIMIT",
        131072,
        "UI",
        "Max log text rendered in UI.",
        _bounds(1024, 2097152),
    ),
    _int(
        "RUN_DETAIL_TEST_LIST_LIMIT",
        999,
        "Judging",
        "Max tests shown in run details.",
        _bounds(1, 5000),
    ),
    _int(
        "RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT",
        200,
        "Judging",
        "Max diagnostics shown in run details.",
        _bounds(1, 5000),
    ),
    _int(
        "RUN_TEST_FEEDBACK_FILE_LIST_LIMIT",
        32,
        "Judging",
        "Max feedback files listed per test.",
        _bounds(1, 1024),
    ),
    _int(
        "RUN_DETAIL_PREVIEW_MAX_BYTES",
        256,
        "Judging",
        "Max preview bytes for artifact snippets.",
        _bounds(32, 65536),
    ),
    _int(
        "RUN_TEST_SELECTOR_LIMIT",
        600,
        "Judging",
        "Max test options shown in run form.",
        _bounds(1, 10000),
    ),
    _int(
        "PREVIEW_LOG_REF_LIST_LIMIT",
        200,
        "UI",
        "Max statement log references parsed.",
        _bounds(1, 5000),
    ),
    _int(
        "API_PROBLEMS_LIST_LIMIT",
        200,
        "Misc",
        "Max problems/contests returned per list API.",
        _bounds(1, 10000),
    ),
    _int(
        "DIAGNOSTIC_MESSAGE_CHAR_LIMIT",
        4096,
        "Misc",
        "Diagnostic message truncation limit.",
        _bounds(256, 65536),
    ),
    _int(
        "UI_JSON_CHAR_LIMIT",
        1048576,
        "UI",
        "json parse limit in UI.",
        _bounds(1024, 16777216),
    ),
    _int(
        "WORKSPACE_HISTORY_LIMIT",
        120,
        "UI",
        "Max commit rows shown on the Backup & Restore page.",
        _bounds(1, 5000),
    ),
    _int(
        "SOLUTION_LIST_LIMIT", 256, "UI", "Max solution files listed.", _bounds(1, 5000)
    ),
    _int(
        "SOLUTION_NOTE_CHAR_LIMIT",
        4096,
        "UI",
        "Max solution metadata note length.",
        _bounds(0, 65536),
    ),
    _str(
        "UI_BRAND_NAME",
        "not polygon",
        "UI",
        "Brand name shown in the application header.",
        _text(minimum=1, maximum=80, policy=TextPolicy.ANY),
    ),
    _str(
        "UI_BRAND_TAGLINE",
        "Unprofessional way to prepare programming contest problems",
        "UI",
        "Optional tagline shown beside the brand name.",
        _text(minimum=0, maximum=200, policy=TextPolicy.ANY),
    ),
    _str(
        "UI_BROWSER_TITLE",
        "Polygon-Replica",
        "UI",
        "Browser title suffix used throughout the application.",
        _text(minimum=1, maximum=120, policy=TextPolicy.ANY),
    ),
)


AUTH_DEFINITIONS = (
    _int(
        "AUTH_COOKIE_MAX_AGE",
        30 * 24 * 60 * 60,
        "Auth",
        "Session cookie max age in seconds.",
        _bounds(60, 31536000),
    ),
    _str(
        "AUTH_COOKIE_NAME",
        "polygon_replica_session",
        "Auth",
        "Session cookie name; takes effect after restart.",
        _text(minimum=1, maximum=128, policy=TextPolicy.COOKIE_NAME, restart=True),
    ),
    _bool("AUTH_COOKIE_SECURE", True, "Auth", "Require HTTPS-only auth cookies."),
    _str(
        "AUTH_EMAIL_ALLOW_REGEX",
        r"^[a-z0-9_-]+@(?:gmail\.com|(?:[a-z0-9-]+\.)*sjtu\.edu\.cn)$",
        "Auth",
        "Full-match regex for allowed registration email addresses.",
        _text(minimum=1, maximum=512, policy=TextPolicy.REGEX),
    ),
    _int(
        "AUTH_REGISTER_PENDING_TTL_SEC",
        30 * 60,
        "Auth",
        "Pending email verification lifetime in seconds.",
        _bounds(60, 86400),
    ),
    _int(
        "AUTH_REGISTER_SUBMIT_WINDOW_SEC",
        60 * 60,
        "Auth",
        "Global registration submission rate-limit window in seconds.",
        _bounds(60, 86400),
    ),
    _int(
        "AUTH_REGISTER_SUBMIT_MAX",
        20,
        "Auth",
        "Max global registration submissions in one window.",
        _bounds(1, 10000),
    ),
    _int(
        "AUTH_REGISTER_VERIFY_FAIL_WINDOW_SEC",
        60 * 60,
        "Auth",
        "Global failed registration verification rate-limit window in seconds.",
        _bounds(60, 86400),
    ),
    _int(
        "AUTH_REGISTER_VERIFY_FAIL_MAX",
        20,
        "Auth",
        "Max global failed registration verification attempts in one window.",
        _bounds(1, 10000),
    ),
    _int(
        "AUTH_REGISTER_EMAIL_GLOBAL_WINDOW_SEC",
        24 * 60 * 60,
        "Auth",
        "Global registration email send rate-limit window in seconds.",
        _bounds(60, 604800),
    ),
    _int(
        "AUTH_REGISTER_EMAIL_GLOBAL_MAX",
        100,
        "Auth",
        "Max global registration emails sent in one window.",
        _bounds(1, 10000),
    ),
    _int(
        "AUTH_REGISTER_EMAIL_SEND_WINDOW_SEC",
        5,
        "Auth",
        "Per-email registration email send cooldown window in seconds.",
        _bounds(1, 3600),
    ),
    _int(
        "AUTH_REGISTER_EMAIL_SEND_MAX",
        1,
        "Auth",
        "Max registration emails sent to one email in the cooldown window.",
        _bounds(1, 100),
    ),
    _int(
        "SUDO_COOKIE_MAX_AGE",
        5 * 60,
        "Misc",
        "Sudo-mode token max age in seconds.",
        _bounds(30, 86400),
    ),
    _str(
        "SUDO_COOKIE_NAME",
        "polygon_replica_sudo_session",
        "Auth",
        "Sudo cookie name; takes effect after restart.",
        _text(minimum=1, maximum=128, policy=TextPolicy.COOKIE_NAME, restart=True),
    ),
    _int(
        "FLASH_COOKIE_MAX_AGE",
        24 * 60 * 60,
        "Auth",
        "Flash cookie max age in seconds.",
        _bounds(60, 31536000),
    ),
    _str(
        "FLASH_COOKIE_NAME",
        "polygon_replica_flash_queue",
        "Auth",
        "Flash message cookie name; takes effect after restart.",
        _text(minimum=1, maximum=128, policy=TextPolicy.COOKIE_NAME, restart=True),
    ),
    _int(
        "FLASH_QUEUE_MAX_ITEMS",
        16,
        "Auth",
        "Max queued flash messages.",
        _bounds(1, 256),
    ),
    _int(
        "FLASH_MESSAGE_MAX_LEN",
        512,
        "Auth",
        "Per-flash message max length.",
        _bounds(16, 4096),
    ),
    _int(
        "PASSWORD_FORM_CSRF_TTL_SEC",
        900,
        "Security",
        "Password form CSRF token lifetime in seconds.",
        _bounds(60, 86400),
    ),
    _str(
        "PASSWORD_FORM_CSRF_SECRET",
        "",
        "Security",
        "Password form CSRF signing secret (empty means random-at-startup).",
    ),
    _float(
        "LOGIN_RATE_LIMIT_WINDOW_SEC",
        300.0,
        "Security",
        "Login rate-limit observation window in seconds.",
        _bounds(1.0, 86400.0),
    ),
    _float(
        "LOGIN_RATE_LIMIT_BLOCK_SEC",
        300.0,
        "Security",
        "Login rate-limit block duration in seconds.",
        _bounds(1.0, 86400.0),
    ),
    _int(
        "LOGIN_RATE_LIMIT_MAX_FAILURES",
        8,
        "Security",
        "Max failed login attempts before blocking.",
        _bounds(1, 1024),
    ),
    _int(
        "PASSWORD_HASH_ITERS",
        240000,
        "Security",
        "PBKDF2 iteration count.",
        _bounds(10000, 10000000),
    ),
)


PROBLEM_DEFINITIONS = (
    _int(
        "CONTEST_TITLE_MAX_LEN",
        128,
        "Limits",
        "Max contest title length.",
        _bounds(1, 2048),
    ),
    _int(
        "CONTEST_MAX_PROBLEMS",
        26,
        "Limits",
        "Maximum problems admitted to a contest.",
        _bounds(1, 64, restart=True),
    ),
    _int(
        "GENERAL_TIME_LIMIT_MIN_MS",
        100,
        "Limits",
        "Lower bound for problem TL (ms).",
        _bounds(1, 60000),
    ),
    _int(
        "GENERAL_TIME_LIMIT_MAX_MS",
        30000,
        "Limits",
        "Upper bound for problem TL (ms).",
        _bounds(1, 300000),
    ),
    _int(
        "GENERAL_MEMORY_LIMIT_MIN_MB",
        1,
        "Limits",
        "Lower bound for memory limit (MB).",
        _bounds(1, 65536),
    ),
    _int(
        "GENERAL_MEMORY_LIMIT_MAX_MB",
        2048,
        "Limits",
        "Upper bound for memory limit (MB).",
        _bounds(1, 262144),
    ),
    _int(
        "GENERAL_PASS_LIMIT_MIN",
        1,
        "Limits",
        "Lower bound for pass limit.",
        _bounds(1, 1024),
    ),
    _int(
        "GENERAL_PASS_LIMIT_MAX",
        64,
        "Limits",
        "Upper bound for pass limit.",
        _bounds(1, 1024),
    ),
    _int(
        "TESTS_SPEC_ROWS_LIMIT",
        256,
        "Limits",
        "Max tests rows loaded from tests/spec.json.",
        _bounds(1, 10000),
    ),
    _int(
        "TESTS_SPEC_PREVIEW_CHARS",
        200,
        "Limits",
        "Chars shown in tests/spec previews.",
        _bounds(16, 65536),
    ),
    _int(
        "TESTS_SPEC_PREVIEW_LINES",
        4,
        "Limits",
        "Lines shown in tests/spec previews.",
        _bounds(1, 1024),
    ),
    _int(
        "TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES",
        16384,
        "Limits",
        "Max bytes for inline manual test edits.",
        _bounds(128, 10485760),
    ),
    _int(
        "TESTS_SPEC_MANUAL_PREVIEW_BYTES",
        256,
        "Limits",
        "Bytes shown for manual test payload preview.",
        _bounds(16, 65536),
    ),
    _int(
        "PROBLEM_ZIP_MAX_EXPANDED_BYTES",
        256 * 1024 * 1024,
        "Limits",
        "Maximum consumed expanded bytes in one problem archive.",
        _bounds(64 * 1024 * 1024, 4 * 1024 * 1024 * 1024, restart=True),
    ),
)


TOOLCHAIN_DEFINITIONS = (
    _str(
        "TOOLCHAIN_CPP_COMPILER",
        "g++",
        "Toolchain",
        "C++ compiler executable for source compilation (for example: g++, clang++).",
    ),
    _str(
        "TOOLCHAIN_PYTHON_EXECUTABLE",
        "python3",
        "Toolchain",
        "Python executable used for source compile-check.",
    ),
    _str(
        "TOOLCHAIN_JAVA_COMPILER",
        "javac",
        "Toolchain",
        "Java compiler executable used for source compilation (for example: javac).",
    ),
    _str(
        "TOOLCHAIN_JUDGEHOST_CPP_COMPILE_FLAGS",
        "-x c++ -Wall -O2 -std=gnu++20 -static -pipe -DDOMJUDGE",
        "Toolchain",
        "Judgehost C++ compile flags used in DOMjudge-compatible compile script.",
    ),
    _str(
        "TOOLCHAIN_JUDGEHOST_JAVA_COMPILE_FLAGS",
        "",
        "Toolchain",
        "Judgehost Java compile flags used in DOMjudge-compatible compile script.",
    ),
    _str(
        "TOOLCHAIN_JUDGEHOST_PYTHON_COMPILE_FLAGS",
        "",
        "Toolchain",
        "Judgehost Python interpreter flags used before -m py_compile in compile script.",
    ),
    _int(
        "TOOLCHAIN_COMPILE_TIMEOUT_SEC",
        120,
        "Toolchain",
        "Compilation timeout in seconds.",
        _bounds(5, 1800),
    ),
    _int(
        "TOOLCHAIN_COMPILE_MEMORY_MB",
        2048,
        "Toolchain",
        "Compilation memory limit in MB.",
        _bounds(64, 262144),
    ),
    _int(
        "AUX_DISPLAY_TEXT_LIMIT_BYTES",
        2048,
        "Misc",
        (
            "Unified byte cap for front-end-facing auxiliary text such as "
            "compile, error, feedback, and diagnostic messages."
        ),
        _bounds(256, 1048576),
    ),
    _int(
        "TOOLCHAIN_COMPILE_OUTPUT_KB",
        262144,
        "Toolchain",
        (
            "Judgehost compile/compare sandbox file size cap in KiB; this is "
            "not the saved or displayed log limit."
        ),
        _bounds(1024, 1048576),
    ),
    _int(
        "RUN_EXEC_PROCESS_LIMIT",
        1024,
        "Judging",
        "Run-time sandbox process limit.",
        _bounds(1, 4096),
    ),
    _int(
        "JUDGEHOST_STORED_LOG_LIMIT_BYTES",
        65536,
        "Judgehost",
        (
            "Max bytes of judgehost auxiliary compile output and compile "
            "metadata stored server-side before truncation."
        ),
        _bounds(1024, 16777216),
    ),
    _int(
        "RUN_WALL_TIME_SLACK_PASS_FAIL_SEC",
        1,
        "Judging",
        "Wall-time slack seconds for pass-fail runs (effective timeout = 2*TL + slack).",
        _bounds(0, 300),
    ),
    _int(
        "RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC",
        15,
        "Judging",
        (
            "Wall-time slack seconds for pass-limit runs with pass_limit > 1 "
            "(effective timeout = 2*TL + slack)."
        ),
        _bounds(0, 300),
    ),
    _int(
        "RUN_WALL_TIME_SLACK_INTERACTIVE_SEC",
        15,
        "Judging",
        "Wall-time slack seconds for interactive runs (effective timeout = 2*TL + slack).",
        _bounds(0, 300),
    ),
    _int(
        "PREVIEW_TEX_TIMEOUT_SEC",
        120,
        "UI",
        "TeX compile timeout in seconds.",
        _bounds(5, 1800),
    ),
    _int(
        "PREVIEW_TEX_MEMORY_MB",
        1024,
        "UI",
        "TeX compile memory limit in MB.",
        _bounds(16, 262144),
    ),
    _int(
        "PREVIEW_TEX_PROCESS_LIMIT",
        256,
        "UI",
        "TeX compile process limit.",
        _bounds(1, 4096),
    ),
    _int(
        "PREVIEW_TEX_OUTPUT_KB",
        131072,
        "UI",
        "TeX compile output cap in KB.",
        _bounds(64, 1048576),
    ),
    _int(
        "PREVIEW_HTML_TIMEOUT_SEC",
        30,
        "UI",
        "Pandoc statement conversion timeout in seconds.",
        _bounds(5, 300),
    ),
    _int(
        "PREVIEW_HTML_MEMORY_MB",
        1024,
        "UI",
        "Pandoc statement conversion address-space limit in MB.",
        _bounds(64, 8192),
    ),
    _int(
        "PREVIEW_HTML_PROCESS_LIMIT",
        256,
        "UI",
        "Pandoc statement conversion process limit.",
        _bounds(1, 256),
    ),
    _int(
        "PREVIEW_HTML_OUTPUT_KB",
        65536,
        "UI",
        "Pandoc statement conversion output cap in KB.",
        _bounds(64, 262144),
    ),
)


WORKER_DEFINITIONS = (
    _int(
        "WORKER_QUEUE_THREADS",
        4,
        "Queue",
        "Worker queue thread count.",
        _bounds(1, 64, restart=True),
    ),
    _int(
        "WORKER_QUEUE_HISTORY_LIMIT",
        1024,
        "Queue",
        "In-memory worker queue history row cap.",
        _bounds(32, 10000, restart=True),
    ),
    _int(
        "WORKER_QUEUE_CAPACITY",
        512,
        "Queue",
        "Worker queue pending capacity.",
        _bounds(1, 100000, restart=True),
    ),
    _int(
        "WORKER_QUEUE_DURABLE_HISTORY_LIMIT",
        20000,
        "Queue",
        "Worker durable event replay limit.",
        _bounds(256, 200000, restart=True),
    ),
    _bool(
        "DB_SQL_TRACE_ENABLED",
        False,
        "Misc",
        "Enable per-statement SQLite trace logging (heavy; for debugging only).",
    ),
)


JUDGEHOST_DEFINITIONS = (
    _bool(
        "JUDGEHOST_ENABLE",
        False,
        "Judgehost",
        "Enable DOMserver-like judgehost queue APIs for verification execution.",
    ),
    _str(
        "JUDGEHOST_API_TOKEN",
        "",
        "Judgehost",
        "Bearer token for judgehost API authentication.",
        _text(policy=TextPolicy.VISIBLE_ASCII),
    ),
    _str(
        "JUDGEHOST_API_USERNAME",
        "judgehost",
        "Judgehost",
        "Basic-auth username for DOMjudge judgehost compatibility API.",
        _text(policy=TextPolicy.VISIBLE_ASCII),
    ),
    _int(
        "JUDGEHOST_FETCH_BATCH_SIZE",
        2,
        "Judgehost",
        "Default max tasks returned per judgehost fetch.",
        _bounds(1, 128),
    ),
    _int(
        "JUDGEHOST_WAIT_TIMEOUT_SEC",
        7200,
        "Judgehost",
        "Backend wait timeout for judgehost task completion (seconds).",
        _bounds(5, 86400),
    ),
    _float(
        "JUDGEHOST_WAIT_POLL_SEC",
        0.5,
        "Judgehost",
        "Backend poll interval while waiting judgehost completion (seconds).",
        _bounds(0.05, 30.0),
    ),
    _int(
        "JUDGEHOST_ONLINE_WINDOW_SEC",
        120,
        "Judgehost",
        "Seconds a judgehost is considered online since last heartbeat/fetch/report event.",
        _bounds(5, 86400),
    ),
    _int(
        "JUDGEHOST_MAX_SUBMISSION_SOURCE_BYTES",
        262144,
        "Judgehost",
        "Maximum submission source size accepted for a judgehost task.",
        _bounds(1024, 16777216),
    ),
    _int(
        "JUDGEHOST_MAX_TESTS_PER_TASK",
        512,
        "Judgehost",
        "Maximum test cases attached when reconstructing a judgehost task.",
        _bounds(1, 10000),
    ),
    _int(
        "JUDGEHOST_MAX_COMPONENT_SOURCE_BYTES",
        8388608,
        "Judgehost",
        "Maximum size of each checker, validator, interactor, or testlib source file.",
        _bounds(1024, 134217728),
    ),
)


CONFIG_DEFINITIONS = (
    *UI_AND_LIMIT_DEFINITIONS,
    *AUTH_DEFINITIONS,
    *PROBLEM_DEFINITIONS,
    *TOOLCHAIN_DEFINITIONS,
    *WORKER_DEFINITIONS,
    *JUDGEHOST_DEFINITIONS,
)
