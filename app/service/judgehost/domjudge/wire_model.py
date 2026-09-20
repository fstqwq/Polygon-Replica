from typing import Literal, TypedDict


class DomjudgeCompileConfig(TypedDict):
    hash: str
    toolchain_cmd_digest: str
    filter_compiler_files: bool
    language_extensions: list[str]
    script_timelimit: int
    script_memory_limit: int
    script_filesize_limit: int


class DomjudgeRunConfig(TypedDict):
    hash: str
    time_limit: float
    overshoot: float
    memory_limit: int
    output_limit: int
    process_limit: int
    entry_point: str | None
    pass_limit: int
    language_id: str


class DomjudgeCompareConfig(TypedDict):
    hash: str
    combined_run_compare: bool
    compare_args: str
    script_timelimit: int
    script_memory_limit: int
    script_filesize_limit: int


class DomjudgeWork(TypedDict):
    type: Literal["judging_run"]
    judgetaskid: int
    jobid: int
    uuid: str
    submitid: str
    contestid: str
    compile_script_id: str
    run_script_id: str
    compare_script_id: str
    testcase_id: str
    testcase_hash: str
    compile_config: str
    run_config: str
    compare_config: str


class DomjudgeWorkdir(TypedDict):
    jobid: int
    submitid: str


class DomjudgeConfiguration(TypedDict):
    diskspace_error: int
    output_storage_limit: int
    script_timelimit: int
    script_memory_limit: int
    script_filesize_limit: int
    timelimit_overshoot: str


class DomjudgeLanguage(TypedDict):
    id: str
    extensions: list[str]


class DomjudgeHost(TypedDict):
    hostname: str
    enabled: bool
    polltime: str
