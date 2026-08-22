"""Transient Contest PDF portion of the deployed real-Judgehost E2E journey."""

import json
import os
import sqlite3
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
) -> str:
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
    return verification_id
