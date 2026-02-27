# PROGRESS

Last updated: 2026-02-27

## Code-Verified Status

1. Top-level `Problems / Contests` navigation is implemented.
Evidence: `app/routes/root_auth_routes.py:69`, `app/routes/root_auth_routes.py:76`, `app/templates/_topbar_common.html:17`, `app/templates/_topbar_common.html:18`.

2. Problem editing pages/routes exist for general/files/generators/checker/validator/interactor/tests/solutions.
Evidence: `app/routes/problem_editor_routes.py:10`, `app/routes/problem_editor_routes.py:22`, `app/routes/problem_editor_routes.py:38`, `app/routes/problem_editor_routes.py:70`, `app/routes/problem_editor_routes.py:86`, `app/routes/problem_editor_routes.py:102`, `app/routes/problem_editor_routes.py:107`, `app/routes/build_preview_routes.py:11`.

3. Invocation list/new/details/execute pages are wired.
Evidence: `app/routes/run_export_routes.py:11`, `app/routes/run_export_routes.py:17`, `app/routes/run_export_routes.py:23`, `app/routes/run_export_routes.py:29`.

4. Invocation execute supports multi-solution + selected tests.
Evidence: `app/templates/run_execute.html:18`, `app/templates/run_execute.html:43`, `app/impl/run_export.py:123`, `app/impl/run_export.py:160`, `app/impl/run_export.py:231`.

5. Package export entry exists and is ICPC-only by code.
Evidence: `app/routes/run_export_routes.py:34`, `app/routes/run_export_routes.py:40`, `app/impl/run_export.py:306`, `app/impl/run_export.py:307`.

6. Tests are managed by `tests/spec.json` + payload directories (`tests/manual`, `tests/generator`).
Evidence: `app/services/tests_spec.py:35`, `app/services/tests_spec.py:36`, `app/services/tests_spec.py:37`, `app/services/tests_spec.py:78`, `app/services/tests_spec.py:83`.

7. Tests editing is decoupled from explicit build trigger (`build_run` is intentionally disabled).
Evidence: `app/impl/build_preview.py:435`, `app/impl/build_preview.py:438`.

8. Async queue usage is confirmed for `run batch`, `verification`, `export`.
Evidence: `app/impl/workspace.py`, `app/services/worker_queue_service.py`.

9. `preview.run` route is currently synchronous (does not enqueue).
Evidence: `app/impl/build_preview.py:572`, `app/impl/build_preview.py:591`.

10. Preview async worker helper exists but is not used by `preview_run` route.
Evidence: `app/impl/workspace.py:2896`, `app/impl/workspace.py:2922`.

11. Sandbox backend is fixed to `native-sandbox`; startup is fail-closed when root switch probe fails.
Evidence: `app/services/sandbox/factory.py`, `app/services/sandbox/native_backend.py`, `app/impl/config.py`.

12. Linux host installer script exists and performs dependency install + userns + bwrap probe.
Evidence: `scripts/install_host.sh`.

13. Runtime constants now flow through runtime config mapping (no large hand-written alias block in handlers).
Evidence: `app/runtime_values.py`, `app/impl/config.py`.

14. UI test modules use explicit imports from `tests.ui_support` (no star-import).
Evidence: `tests/test_ui_auth.py`, `tests/test_ui_components.py`, `tests/test_ui_preview_export.py`, `tests/test_ui_run.py`, `tests/test_ui_workspace.py`.

## Code-Verified Gaps (Still Open)

1. Preview path remains synchronous and still blocks request until compile finishes.
Evidence: `app/impl/build_preview.py`.

2. Worker queue durability/backpressure are not yet implemented in current service (in-memory queue/history).
Evidence: `app/services/worker_queue_service.py`.

3. Sandbox hardening depth remains limited (mount/seccomp/cgroup still pending hardening iterations).
Evidence baseline: `app/services/sandbox/native_backend.py` current policy scope.

## Validation Command

1. Canonical local regression command:
`source .venv/bin/activate && ./scripts/test.sh`

## Notes

1. This file is now restricted to claims that can be traced to current repository code.
2. Subjective progress statements are intentionally removed.
3. Latest full local regression (`./scripts/test.sh`) passed on this revision.
