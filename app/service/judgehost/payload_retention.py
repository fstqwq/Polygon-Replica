from __future__ import annotations


_HEAVY_PAYLOAD_KEYS = {
    "domjudge_precomputed",
    "extra_sources_b64",
    "source_b64",
}
_HEAVY_VERIFICATION_PAYLOAD_KEYS = {
    "binaries_b64",
    "sources_b64",
}
_RETAINED_TEST_KEYS = ("name", "answer_name")


def compact_payload_for_retention(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    compact = {
        key: value
        for key, value in payload.items()
        if str(key) not in _HEAVY_PAYLOAD_KEYS
    }
    verification_payload = payload.get("verification_payload")
    if isinstance(verification_payload, dict):
        compact_verification_payload = {
            key: value
            for key, value in verification_payload.items()
            if str(key) not in _HEAVY_VERIFICATION_PAYLOAD_KEYS
        }
        compact_tests: list[dict[str, object]] = []
        tests_obj = verification_payload.get("tests")
        if isinstance(tests_obj, list):
            for test_obj in tests_obj:
                if not isinstance(test_obj, dict):
                    continue
                compact_test = {
                    key: test_obj[key]
                    for key in _RETAINED_TEST_KEYS
                    if key in test_obj
                }
                if compact_test:
                    compact_tests.append(compact_test)
        compact_verification_payload["tests"] = compact_tests
        compact["verification_payload"] = compact_verification_payload
    return compact


def compact_task_row_payload(row: dict[str, object]) -> None:
    payload = row.get("payload")
    if isinstance(payload, dict):
        row["payload"] = compact_payload_for_retention(payload)
