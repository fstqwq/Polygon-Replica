from __future__ import annotations


class JudgehostCoreMixin:
    # Delegation layer; logic lives in app.service.judgehost.core.JudgehostCore
    def apply_runtime_values(self, constants):
        return self._core.apply_runtime_values(constants)

    def enabled(self):
        return self._core.enabled()

    def auth_token_configured(self):
        return self._core.auth_token_configured()

    def check_api_token(self, token):
        return self._core.check_api_token(token)

    def api_username(self):
        return self._core.api_username()

    def check_api_basic(self, username, password):
        return self._core.check_api_basic(username, password)

    def _normalize_run_id(self, run_id):
        return self._core.normalize_run_id(run_id)

    def _normalize_hostname(self, hostname):
        return self._core.normalize_hostname(hostname)

    def _task_status_counts(self):
        return self._core.task_status_counts()

    def _task_by_id(self, task_id):
        return self._core.task_by_id(task_id)

    def _task_payload(self, task_id):
        return self._core.task_payload(task_id)

    def _record_host_judging(self, hostname, *, label='-', updated_at=None):
        return self._core.record_host_judging(hostname, label=label, updated_at=updated_at)

    def bind_request_peer_hostname(self, peer_addr, hostname):
        return self._core.bind_request_peer_hostname(peer_addr, hostname)

    def hostname_for_request_peer(self, peer_addr):
        return self._core.hostname_for_request_peer(peer_addr)

    def _safe_workspace_source(self, workspace_root, submission_path):
        return self._core.safe_workspace_source(workspace_root, submission_path)

    def _safe_read_bytes(self, path, *, max_bytes, label):
        return self._core.safe_read_bytes(path, max_bytes=max_bytes, label=label)
