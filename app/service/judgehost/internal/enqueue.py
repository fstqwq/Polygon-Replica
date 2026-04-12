from __future__ import annotations


class JudgehostEnqueueMixin:
    _JAVA_CLASS_DECL_RE = None
    _JAVA_MAIN_METHOD_RE = None

    @staticmethod
    def _normalize_text(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._normalize_text(*args, **kwargs)

    @staticmethod
    def _normalize_text_with_default(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._normalize_text_with_default(*args, **kwargs)

    @staticmethod
    def _normalize_status(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._normalize_status(*args, **kwargs)

    def _verification_artifact_ref(self, *args, **kwargs):
        return self._enqueue._verification_artifact_ref(*args, **kwargs)

    @staticmethod
    def _json_object(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._json_object(*args, **kwargs)

    @staticmethod
    def _normalize_list(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._normalize_list(*args, **kwargs)

    @staticmethod
    def _strip_java_noncode(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._strip_java_noncode(*args, **kwargs)

    @classmethod
    def _java_top_level_classes(cls, *args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._java_top_level_classes(*args, **kwargs)

    @classmethod
    def _java_class_has_main(cls, *args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._java_class_has_main(*args, **kwargs)

    @classmethod
    def _detect_java_entry_point(cls, *args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._detect_java_entry_point(*args, **kwargs)

    def _normalize_submission_source(self, *args, **kwargs):
        return self._enqueue._normalize_submission_source(*args, **kwargs)

    @staticmethod
    def _payload_verification_tests(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._payload_verification_tests(*args, **kwargs)

    @staticmethod
    def _payload_test_names(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._payload_test_names(*args, **kwargs)

    def _merge_existing_task_payload(self, *args, **kwargs):
        return self._enqueue._merge_existing_task_payload(*args, **kwargs)

    def _append_cases_to_existing_task(self, *args, **kwargs):
        return self._enqueue._append_cases_to_existing_task(*args, **kwargs)

    def _restore_existing_task_work_root(self, *args, **kwargs):
        return self._enqueue._restore_existing_task_work_root(*args, **kwargs)

    @staticmethod
    def _verification_id(*args, **kwargs):
        from app.service.judgehost.enqueue import TaskEnqueue
        return TaskEnqueue._verification_id(*args, **kwargs)

    def _collect_verification_payload(self, *args, **kwargs):
        return self._enqueue._collect_verification_payload(*args, **kwargs)

    def _build_task_payload(self, *args, **kwargs):
        return self._enqueue._build_task_payload(*args, **kwargs)

    def _domjudge_precomputed_fields_from_payload(self, *args, **kwargs):
        return self._enqueue._domjudge_precomputed_fields_from_payload(*args, **kwargs)

    def prepare_enqueue_payload(self, *args, **kwargs):
        return self._enqueue.prepare_enqueue_payload(*args, **kwargs)

    def _initial_summary(self, *args, **kwargs):
        return self._enqueue._initial_summary(*args, **kwargs)

    def enqueue_task(self, *args, **kwargs):
        return self._enqueue.enqueue_task(*args, **kwargs)

    def enqueue_compile_only_task(self, *args, **kwargs):
        return self._enqueue.enqueue_compile_only_task(*args, **kwargs)
