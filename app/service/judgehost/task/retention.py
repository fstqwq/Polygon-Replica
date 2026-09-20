from typing import TypedDict

from app.service.judgehost.task.model import PreparedTest, TaskPayload


class RetainedTest(TypedDict, total=False):
    name: str
    answer_name: str


def compact_payload_for_retention(payload: TaskPayload) -> TaskPayload:
    compact = payload.copy()
    compact.pop("precomputed", None)
    verification_payload = payload.get("verification_payload")
    if isinstance(verification_payload, dict):
        compact_verification_payload = verification_payload.copy()
        compact_verification_payload.pop("source_files", None)
        compact_tests: list[RetainedTest] = []
        tests_obj = verification_payload.get("tests")
        if isinstance(tests_obj, list):
            for test_obj in tests_obj:
                if isinstance(test_obj, PreparedTest):
                    compact_tests.append({"name": test_obj.name, "answer_name": test_obj.answer_name})
                    continue
                if not isinstance(test_obj, dict):
                    continue
                compact_test: RetainedTest = {}
                name, answer_name = test_obj.get("name"), test_obj.get("answer_name")
                if isinstance(name, str):
                    compact_test["name"] = name
                if isinstance(answer_name, str):
                    compact_test["answer_name"] = answer_name
                if compact_test:
                    compact_tests.append(compact_test)
        compact_verification_payload["tests"] = compact_tests
        compact["verification_payload"] = compact_verification_payload
    return compact
