"""Transient Contest PDF portion of the deployed real-Judgehost E2E journey."""

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
) -> str:
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
    response = client.post(
        f"/contests/{CONTEST}/statements/pdf",
        params={"source": "native_package", "language": "english"},
        data={},
        headers={"Origin": str(client.base_url).rstrip("/")},
        timeout=300.0,
    )
    if response.status_code != 303:
        raise RuntimeError(
            "Contest PDF Preview returned "
            f"{response.status_code}: {response.text[:500]}"
        )
    location = response.headers.get("location", "")
    preview_ids = parse_qs(urlparse(location).query).get("preview_id", [])
    if len(preview_ids) != 1 or not preview_ids[0]:
        raise RuntimeError(
            f"Contest PDF Preview did not return one preview id: {location!r}"
        )
    return preview_ids[0]


def assert_contest_pdf(
    client: httpx.Client,
    connection: sqlite3.Connection,
    *,
    problem: str,
    preview_id: str,
    expected_head: str,
    expected_solution_verdicts: dict[str, dict[str, str]],
) -> str:
    row = connection.execute(
        """
        SELECT sp.status,sp.output_kind,sp.source_kind,sp.language,sp.summary_json,
               c.id AS contest_id,c.title
        FROM statement_previews sp
        JOIN contests c ON c.id=sp.contest_id
        WHERE c.slug=? AND sp.id=?
        """,
        [CONTEST, preview_id],
    ).fetchone()
    if row is None:
        raise RuntimeError("Contest PDF Preview metadata is missing")
    summary = json.loads(str(row["summary_json"] or "{}"))
    if (
        str(row["title"]) != CONTEST_TITLE
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

    download = client.get(
        f"/contests/{CONTEST}/statements/pdf/file/{preview_id}"
    )
    if (
        download.status_code != 200
        or len(download.content) < 100
        or not download.content.startswith(b"%PDF-")
    ):
        raise RuntimeError(
            f"Contest PDF Preview is invalid: status={download.status_code} "
            f"size={len(download.content)}"
        )
    cache_root = Path(os.environ["POLYGON_REPLICA_E2E_CACHE_ROOT"]).resolve()
    preview_root = cache_root / "artifacts" / "previews" / preview_id
    if (preview_root / "pdf" / "statement.pdf").read_bytes() != download.content:
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
    durable_rows = connection.execute(
        "SELECT id FROM contest_artifacts WHERE contest_id=?",
        [int(row["contest_id"])],
    ).fetchall()
    if durable_rows:
        raise RuntimeError("Contest PDF Preview created a durable Contest artifact")
    return verification_id
