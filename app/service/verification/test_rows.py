from __future__ import annotations

from typing import cast

VerificationTestPassRow = dict[str, object]
VerificationTestRow = dict[str, object]


def build_verification_test_pass_row(
    *,
    verdict: str,
    time_ms: int | None = None,
    time_user_ms: int | None = None,
    time_wall_ms: int | None = None,
    memory_kb: int | None = None,
    feedback: str = "",
    output_ref: str = "",
    runresult: str = "",
    pass_number: int = 1,
) -> VerificationTestPassRow:
    resolved_time_ms = max(0, int(0 if time_ms is None else time_ms))
    resolved_time_user_ms = max(0, int(resolved_time_ms if time_user_ms is None else time_user_ms))
    resolved_time_wall_ms = max(0, int(resolved_time_user_ms if time_wall_ms is None else time_wall_ms))
    resolved_memory_kb = max(0, int(0 if memory_kb is None else memory_kb))
    row: VerificationTestPassRow = {
        "pass": max(1, int(pass_number)),
        "verdict": verdict or "",
        "time_ms": resolved_time_ms,
        "time_user_ms": resolved_time_user_ms,
        "time_wall_ms": resolved_time_wall_ms,
        "memory_kb": resolved_memory_kb,
        "feedback": feedback or "",
        "output_ref": output_ref or "",
    }
    if runresult:
        row["runresult"] = runresult
    return row


def build_verification_test_row(
    *,
    test_name: str,
    verdict: str = "",
    time_ms: int | None = None,
    time_user_ms: int | None = None,
    time_wall_ms: int | None = None,
    memory_kb: int | None = None,
    message: str = "",
    output_ref: str = "",
    feedback_files: list[str] | None = None,
    passes: list[VerificationTestPassRow] | None = None,
    runresult: str = "",
) -> VerificationTestRow:
    canonical_passes = list(passes or [])
    if not canonical_passes:
        canonical_passes = [
            build_verification_test_pass_row(
                verdict=verdict,
                time_ms=time_ms,
                time_user_ms=time_user_ms,
                time_wall_ms=time_wall_ms,
                memory_kb=memory_kb,
                feedback=message,
                output_ref=output_ref,
                runresult=runresult,
            )
        ]
    final_pass = canonical_passes[-1]
    resolved_verdict = verdict or str(final_pass.get("verdict") or "")
    resolved_time_ms = max(
        0,
        int(final_pass.get("time_ms") if time_ms is None else time_ms),
    )
    resolved_time_user_ms = max(
        0,
        int(final_pass.get("time_user_ms") if time_user_ms is None else time_user_ms),
    )
    resolved_time_wall_ms = max(
        0,
        int(final_pass.get("time_wall_ms") if time_wall_ms is None else time_wall_ms),
    )
    resolved_memory_kb = max(
        0,
        int(final_pass.get("memory_kb") if memory_kb is None else memory_kb),
    )
    resolved_message = message or str(final_pass.get("feedback") or "")
    resolved_output_ref = output_ref or str(final_pass.get("output_ref") or "")
    row: VerificationTestRow = {
        "test": test_name,
        "verdict": resolved_verdict,
        "time_ms": resolved_time_ms,
        "time_user_ms": resolved_time_user_ms,
        "time_wall_ms": resolved_time_wall_ms,
        "memory_kb": resolved_memory_kb,
        "message": resolved_message,
        "output_ref": resolved_output_ref,
        "feedback_files": list(feedback_files or []),
        "passes": canonical_passes,
    }
    if runresult:
        row["runresult"] = runresult
    return row


def canonicalize_verification_test_rows(raw_tests: list[dict[str, object]]) -> list[VerificationTestRow]:
    rows: list[VerificationTestRow] = []
    for item in raw_tests:
        test_name = str(item.get("test") or "")
        if not test_name:
            continue
        raw_passes = cast(list[dict[str, object]], item.get("passes") or [])
        passes: list[VerificationTestPassRow] = []
        for index, pass_item in enumerate(raw_passes, start=1):
            passes.append(
                build_verification_test_pass_row(
                    verdict=str(pass_item.get("verdict") or ""),
                    time_ms=pass_item.get("time_ms"),
                    time_user_ms=pass_item.get("time_user_ms"),
                    time_wall_ms=pass_item.get("time_wall_ms"),
                    memory_kb=pass_item.get("memory_kb"),
                    feedback=str(pass_item.get("feedback") or pass_item.get("message") or ""),
                    output_ref=str(
                        pass_item.get("output_ref")
                        or pass_item.get("output_artifact")
                        or pass_item.get("output_rel")
                        or ""
                    ),
                    runresult=str(pass_item.get("runresult") or ""),
                    pass_number=index,
                )
            )
        rows.append(
            build_verification_test_row(
                test_name=test_name,
                verdict=str(item.get("verdict") or ""),
                time_ms=item.get("time_ms"),
                time_user_ms=item.get("time_user_ms"),
                time_wall_ms=item.get("time_wall_ms"),
                memory_kb=item.get("memory_kb"),
                message=str(item.get("message") or item.get("feedback") or ""),
                output_ref=str(item.get("output_ref") or item.get("output_artifact") or item.get("output_rel") or ""),
                feedback_files=list(cast(list[str], item.get("feedback_files") or [])),
                passes=passes,
                runresult=str(item.get("runresult") or ""),
            )
        )
    rows.sort(key=lambda item: str(item.get("test") or ""))
    return rows


def upsert_verification_test_row(
    summary: dict[str, object],
    *,
    test_row: VerificationTestRow,
    tests_total: int = 0,
    selected_tests_count: int = 0,
) -> dict[str, object]:
    existing_tests = canonicalize_verification_test_rows(list(summary.get("tests") or []))
    updated_tests: list[VerificationTestRow] = []
    test_name = str(test_row.get("test") or "")
    replaced = False
    for item in existing_tests:
        if str(item.get("test") or "") == test_name:
            updated_tests.append(test_row)
            replaced = True
            continue
        updated_tests.append(item)
    if not replaced:
        updated_tests.append(test_row)
    updated_tests.sort(key=lambda item: str(item.get("test") or ""))
    summary["tests"] = updated_tests
    summary["tests_total"] = max(
        len(updated_tests),
        int(summary.get("tests_total") or 0),
        max(0, int(tests_total)),
        max(0, int(selected_tests_count)),
    )
    usage = dict(summary.get("usage") or {})
    summary["usage"] = {
        "tests": len(updated_tests),
        "time_ms_total": max(int(test_row.get("time_user_ms") or 0), int(usage.get("time_ms_total") or 0)),
        "time_user_ms_total": max(int(test_row.get("time_user_ms") or 0), int(usage.get("time_user_ms_total") or 0)),
        "time_wall_ms_total": max(int(test_row.get("time_wall_ms") or 0), int(usage.get("time_wall_ms_total") or 0)),
        "memory_kb_peak": max(int(test_row.get("memory_kb") or 0), int(usage.get("memory_kb_peak") or 0)),
    }
    return summary
