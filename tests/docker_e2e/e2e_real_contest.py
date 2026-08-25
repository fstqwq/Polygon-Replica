"""Transient Contest PDF portion of the deployed real-Judgehost E2E journey."""

import io
import json
import os
import sqlite3
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from runner import _assert_artifact_refs, _assert_tasks


CONTEST = "e2e-real-contest"
CONTEST_TITLE = "E2E Real Contest"
PostRedirect = Callable[
    [httpx.Client, str, dict[str, str]],
    httpx.Response,
]


def start_contest_pdf(
    client: httpx.Client,
    *,
    post_redirect: PostRedirect,
    problem: str,
) -> httpx.Response:
    post_redirect(
        client,
        "/contests/create",
        {"contest_slug": CONTEST, "contest_title": CONTEST_TITLE},
    )
    post_redirect(
        client,
        f"/contests/{CONTEST}/problems/add",
        {"problem_slugs": problem, "q": ""},
    )
    response = client.get(
        f"/contests/{CONTEST}/statements/pdf",
        params={"source": "native_package", "language": "english"},
        timeout=300.0,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "Contest PDF Preview returned "
            f"{response.status_code}: {response.text[:500]}"
        )
    return response


def assert_contest_pdf(
    response: httpx.Response,
    connection: sqlite3.Connection,
    *,
    problem: str,
    username: str,
    expected_head: str,
    expected_solution_verdicts: dict[str, dict[str, str]],
) -> tuple[str, str]:
    row = connection.execute(
        """
        SELECT sp.id,sp.status,sp.output_kind,sp.source_kind,sp.language,
               sp.summary_json,c.id AS contest_id,title_property.value AS title,
               u.username AS actor_username
        FROM statement_previews sp
        JOIN contests c ON c.id=sp.contest_id
        JOIN users u ON u.id=sp.actor_user_id
        JOIN contest_properties title_property
          ON title_property.contest_id=c.id AND title_property.key='title'
        WHERE c.slug=? AND sp.subject_kind='contest' AND sp.output_kind='pdf'
          AND sp.source_kind='native_package' AND sp.language='english'
          AND sp.status='ok'
        ORDER BY sp.created_at DESC,sp.id DESC
        LIMIT 1
        """,
        [CONTEST],
    ).fetchone()
    if row is None:
        raise RuntimeError("Contest PDF Preview metadata is missing")
    preview_id = str(row["id"])
    summary = json.loads(str(row["summary_json"] or "{}"))
    if (
        str(row["title"]) != CONTEST_TITLE
        or str(row["actor_username"]) != username
        or str(row["status"]) != "ok"
        or str(row["output_kind"]) != "pdf"
        or str(row["source_kind"]) != "native_package"
        or str(row["language"]) != "english"
        or summary.get("job_type") != "preview-pdf"
        or summary.get("pdf") != "pdf/statement.pdf"
        or summary.get("totals") != {"total": 1, "success": 1, "failed": 0}
    ):
        raise RuntimeError(f"Contest PDF Preview metadata is inconsistent: {dict(row)!r}")

    materialization = connection.execute(
        """
        SELECT m.source_commit,m.verification_id
        FROM contest_problems cp
        JOIN problems p ON p.id=cp.problem_id
        JOIN problem_package_materializations m ON m.problem_id=p.id
        WHERE cp.contest_id=? AND p.slug=? AND m.status='available'
        ORDER BY m.revision_number DESC,m.created_at DESC LIMIT 1
        """,
        [int(row["contest_id"]), problem],
    ).fetchone()
    if materialization is None or str(materialization["source_commit"]) != expected_head:
        detail = None if materialization is None else dict(materialization)
        raise RuntimeError(f"Contest PDF source Native Package is wrong: {detail!r}")
    verification_id = str(materialization["verification_id"] or "")
    verification = connection.execute(
        "SELECT kind,status,sanity_status,fail_reason FROM verifications WHERE id=?",
        [verification_id],
    ).fetchone()
    if (
        verification is None
        or str(verification["kind"]) != "all"
        or str(verification["status"]) != "ok"
        or str(verification["sanity_status"]) != "passed"
        or str(verification["fail_reason"] or "")
    ):
        detail = None if verification is None else dict(verification)
        raise RuntimeError(f"Native Package Verification is inconsistent: {detail!r}")
    _assert_tasks(
        connection,
        verification_id,
        special_verdicts=expected_solution_verdicts,
    )
    _assert_artifact_refs(
        connection,
        verification_id,
        expected_input=b"1\n7\n42\n",
        expected_answer=b"49\n",
    )

    content_type = response.headers.get("content-type", "")
    content_disposition = response.headers.get("content-disposition", "")
    if (
        response.status_code != 200
        or not content_type.startswith("application/pdf")
        or "inline" not in content_disposition.lower()
        or f"{CONTEST}-english-statements.pdf" not in content_disposition
        or len(response.content) < 100
        or not response.content.startswith(b"%PDF-")
    ):
        raise RuntimeError(
            f"Contest PDF Preview is invalid: status={response.status_code} "
            f"content_type={content_type!r} "
            f"content_disposition={content_disposition!r} "
            f"size={len(response.content)}"
        )
    cache_root = Path(os.environ["POLYGON_REPLICA_E2E_CACHE_ROOT"]).resolve()
    preview_root = cache_root / "artifacts" / "previews" / preview_id
    if (preview_root / "pdf" / "statement.pdf").read_bytes() != response.content:
        raise RuntimeError("Contest PDF Preview cache and HTTP payload differ")
    compile_log = (preview_root / "logs" / "contest-pdf.log").read_text(
        encoding="utf-8",
        errors="replace",
    )
    if (
        "== xelatex pass 1 ==" not in compile_log
        or "== xelatex pass 2 ==" not in compile_log
        or compile_log.count("returncode: 0") < 2
    ):
        raise RuntimeError("Contest PDF Preview did not complete two XeLaTeX passes")
    if (preview_root / "contest-pdf-src").exists() or (
        preview_root / "contest-sources"
    ).exists():
        raise RuntimeError("Contest PDF Preview retained an intermediate source tree")
    return verification_id, preview_id


def assert_contest_package_download(
    client: httpx.Client,
    connection: sqlite3.Connection,
    *,
    problem: str,
    expected_head: str,
) -> None:
    origin_headers = {"Origin": str(client.base_url).rstrip("/")}
    rejected = client.post(
        f"/contests/{CONTEST}/packages/download",
        data={"package_format": "qoj"},
        headers=origin_headers,
        timeout=300.0,
    )
    if rejected.status_code != 409:
        raise RuntimeError(
            "Contest package download did not reject the stale Native Package: "
            f"status={rejected.status_code} body={rejected.text[:500]}"
        )
    rejected_detail = str(rejected.json().get("detail") or "")
    if "Packages are not ready" not in rejected_detail or problem not in rejected_detail:
        raise RuntimeError(
            f"Contest package readiness error is inaccurate: {rejected_detail!r}"
        )

    build = client.post(
        f"/contests/{CONTEST}/packages/build-all",
        headers=origin_headers,
    )
    if build.status_code != 303:
        raise RuntimeError(
            f"Build All Packages failed: {build.status_code} {build.text[:500]}"
        )
    deadline = time.monotonic() + 300.0
    job = None
    while time.monotonic() < deadline:
        job = connection.execute(
            """
            SELECT j.status,j.error,j.materialization_id
            FROM export_jobs j
            JOIN problems p ON p.id=j.problem_id
            WHERE p.slug=? AND j.export_type='native' AND j.source_commit=?
            ORDER BY j.created_at DESC,j.id DESC LIMIT 1
            """,
            [problem, expected_head],
        ).fetchone()
        if job is not None and str(job["status"]) in {"succeeded", "failed"}:
            break
        time.sleep(0.1)
    if job is None or str(job["status"]) != "succeeded":
        detail = None if job is None else dict(job)
        raise RuntimeError(f"Build All Packages did not finish successfully: {detail!r}")

    materialization = connection.execute(
        """
        SELECT m.id
        FROM contest_problems cp
        JOIN contests c ON c.id=cp.contest_id
        JOIN problems p ON p.id=cp.problem_id
        JOIN problem_package_materializations m ON m.problem_id=p.id
        WHERE c.slug=? AND p.slug=? AND m.status='available'
          AND m.source_commit=?
        ORDER BY m.revision_number DESC,m.created_at DESC LIMIT 1
        """,
        [CONTEST, problem, expected_head],
    ).fetchone()
    if materialization is None:
        raise RuntimeError("Contest package Native Package is missing")
    native_package_id = str(materialization["id"])

    def export_count(package_format: str) -> int:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM exports
            WHERE materialization_id=? AND export_type=?
            """,
            [native_package_id, package_format],
        ).fetchone()
        return int(row["count"])

    def export_job_count(package_format: str) -> int:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM export_jobs
            WHERE materialization_id=? AND export_type=?
            """,
            [native_package_id, package_format],
        ).fetchone()
        return int(row["count"])

    qoj_before = export_count("qoj")
    qoj_jobs_before = export_job_count("qoj")
    first_qoj = client.post(
        f"/contests/{CONTEST}/packages/download",
        data={"package_format": "qoj"},
        headers=origin_headers,
        timeout=300.0,
    )
    qoj_after_first = export_count("qoj")
    qoj_jobs_after_first = export_job_count("qoj")
    second_qoj = client.post(
        f"/contests/{CONTEST}/packages/download",
        data={"package_format": "qoj"},
        headers=origin_headers,
        timeout=300.0,
    )
    qoj_after_second = export_count("qoj")
    qoj_jobs_after_second = export_job_count("qoj")
    if (
        qoj_after_first != qoj_before + 1
        or qoj_after_second != qoj_after_first
        or qoj_jobs_after_first != qoj_jobs_before + 1
        or qoj_jobs_after_second != qoj_jobs_after_first
    ):
        raise RuntimeError(
            "Contest package download did not create and reuse one QOJ export: "
            f"before={qoj_before} first={qoj_after_first} "
            f"second={qoj_after_second} jobs_before={qoj_jobs_before} "
            f"jobs_first={qoj_jobs_after_first} jobs_second={qoj_jobs_after_second}"
        )
    for response in (first_qoj, second_qoj):
        if response.status_code != 200:
            raise RuntimeError(
                "Contest QOJ package returned "
                f"{response.status_code}: {response.text[:500]}"
            )
        with zipfile.ZipFile(io.BytesIO(response.content)) as outer:
            names = set(outer.namelist())
            child_name = "packages/A-e2e-sample.zip"
            if {"statements.en.pdf", child_name} != names:
                raise RuntimeError(
                    f"Contest QOJ bundle has unexpected members: {sorted(names)!r}"
                )
            if not outer.read("statements.en.pdf").startswith(b"%PDF-"):
                raise RuntimeError("Contest QOJ bundle statement PDF is invalid")
            with zipfile.ZipFile(io.BytesIO(outer.read(child_name))) as child:
                if "problem.conf" not in child.namelist():
                    raise RuntimeError("Contest QOJ child omitted problem.conf")

    domjudge_before = export_count("domjudge")
    domjudge = client.post(
        f"/contests/{CONTEST}/packages/download",
        data={"package_format": "domjudge"},
        headers=origin_headers,
        timeout=300.0,
    )
    if domjudge.status_code != 200 or export_count("domjudge") != domjudge_before + 1:
        raise RuntimeError(
            "Contest DOMjudge package did not prepare one external export: "
            f"status={domjudge.status_code} body={domjudge.text[:500]}"
        )
    with zipfile.ZipFile(io.BytesIO(domjudge.content)) as outer:
        child_name = "packages/A-e2e-sample.zip"
        with zipfile.ZipFile(io.BytesIO(outer.read(child_name))) as child:
            metadata = child.read("domjudge-problem.ini").decode()
    if "short-name = A\n" not in metadata or "color = #e6194b\n" not in metadata:
        raise RuntimeError(f"Contest DOMjudge placement is wrong: {metadata!r}")
