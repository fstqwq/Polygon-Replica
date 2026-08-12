"""Contest PDF portion of the deployed real-Judgehost E2E journey."""

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from runner import (
    _assert_artifact_refs,
    _assert_tasks,
)


CONTEST = "e2e-real-contest"
CONTEST_TITLE = "E2E Real Contest"
PostRedirect = Callable[
    [httpx.Client, str, dict[str, str]],
    httpx.Response,
]


def _resolved_child(root: Path, child: Path) -> Path:
    resolved_root = root.resolve()
    resolved_child = child.resolve()
    if resolved_child == resolved_root or resolved_root not in resolved_child.parents:
        raise RuntimeError(f"path escaped expected E2E root: {resolved_child}")
    return resolved_child


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
    response = post_redirect(
        client,
        f"/contests/{CONTEST}/packages/build/start",
        {"outputs": "statement_pdf", "language": "english"},
    )
    location = response.headers.get("location", "")
    job_ids = parse_qs(urlparse(location).query).get("job_id", [])
    if len(job_ids) != 1 or not job_ids[0]:
        raise RuntimeError(
            f"contest PDF start did not return one job id: {location!r}"
        )
    return job_ids[0]


def wait_for_contest_job(
    client: httpx.Client,
    job_id: str,
    *,
    timeout_sec: float = 300.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_sec
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"/contests/{CONTEST}/packages/jobs/status",
            params={"job_id": job_id},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("contest job status response is not an object")
        latest = dict(payload)
        if latest.get("job_id") != job_id:
            raise RuntimeError(f"contest job status changed identity: {latest!r}")
        if latest.get("running") is False:
            return latest
        time.sleep(0.1)
    raise RuntimeError(f"contest PDF job did not finish: {latest!r}")


def _contest_job_root(job_id: str) -> Path:
    artifacts_root = Path(
        os.environ["POLYGON_REPLICA_E2E_ARTIFACTS_ROOT"]
    ).resolve()
    return _resolved_child(
        artifacts_root,
        artifacts_root / "contests" / CONTEST / job_id,
    )


def _assert_contest_database(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    artifact_id: str,
    expected_head: str,
    pdf_payload: bytes,
) -> str:
    row = connection.execute(
        """
        SELECT
            c.id AS contest_id,
            c.title,
            cj.job_type,
            cj.status AS job_status,
            cj.finished_at,
            cp.label,
            i.source_commit,
            i.revision_number,
            i.materialization_id,
            i.archive_sha256,
            m.status AS materialization_status,
            m.source_commit AS materialization_commit,
            m.verification_id
        FROM contests c
        JOIN contest_jobs cj ON cj.contest_id=c.id
        JOIN contest_build_items i ON i.job_id=cj.id
        JOIN contest_problems cp ON cp.id=i.contest_problem_id
        JOIN problem_package_materializations m ON m.id=i.materialization_id
        WHERE c.slug=? AND cj.id=?
        """,
        [CONTEST, job_id],
    ).fetchone()
    if row is None:
        raise RuntimeError("contest PDF job did not persist its frozen revision")
    if (
        str(row["title"]) != CONTEST_TITLE
        or str(row["job_type"]) != "build"
        or str(row["job_status"]) != "ok"
        or not str(row["finished_at"] or "")
        or str(row["label"]) != "A"
        or str(row["source_commit"]) != expected_head
        or int(row["revision_number"]) != 1
        or str(row["materialization_status"]) != "available"
        or str(row["materialization_commit"]) != expected_head
        or not str(row["materialization_id"] or "")
        or not str(row["archive_sha256"] or "")
    ):
        raise RuntimeError(f"contest PDF persisted inconsistent identities: {dict(row)!r}")

    verification_id = str(row["verification_id"] or "")
    verification = connection.execute(
        """
        SELECT kind,status,sanity_status,fail_reason
        FROM verifications
        WHERE id=?
        """,
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
        raise RuntimeError(
            f"contest materialization did not use a full verification: {detail!r}"
        )
    _assert_tasks(
        connection,
        verification_id,
        special_verdicts={
            "solutions/wa.cpp": "WA",
            "solutions/ce.cpp": "CE",
        },
    )
    _assert_artifact_refs(connection, verification_id)

    artifact = connection.execute(
        """
        SELECT artifact_type,filename,sha256,size_bytes
        FROM contest_artifacts
        WHERE contest_id=? AND job_id=? AND id=?
        """,
        [int(row["contest_id"]), job_id, artifact_id],
    ).fetchone()
    if (
        artifact is None
        or str(artifact["artifact_type"]) != "contest-pdf"
        or not str(artifact["filename"] or "").endswith(".pdf")
        or str(artifact["sha256"] or "") != _sha256(pdf_payload)
        or int(artifact["size_bytes"] or 0) != len(pdf_payload)
    ):
        detail = None if artifact is None else dict(artifact)
        raise RuntimeError(f"contest PDF artifact metadata is wrong: {detail!r}")

    return verification_id


def assert_contest_pdf(
    client: httpx.Client,
    connection: sqlite3.Connection,
    *,
    problem: str,
    job_id: str,
    job: dict[str, object],
    expected_head: str,
) -> tuple[str, str]:
    summary = job.get("summary")
    if (
        job.get("status") != "ok"
        or job.get("running") is not False
        or job.get("job_type") != "build"
        or not isinstance(summary, dict)
    ):
        raise RuntimeError(f"contest PDF job failed: {job!r}")
    if (
        summary.get("language") != "english"
        or summary.get("requested_outputs") != ["statement_pdf"]
        or summary.get("successful_outputs") != ["statement_pdf"]
    ):
        raise RuntimeError(f"contest PDF summary is inconsistent: {summary!r}")
    outputs = summary.get("outputs")
    pdf_summary = outputs.get("statement_pdf") if isinstance(outputs, dict) else None
    if not isinstance(pdf_summary, dict):
        raise RuntimeError(f"contest PDF output summary is missing: {outputs!r}")
    results = pdf_summary.get("results")
    totals = pdf_summary.get("totals")
    if (
        pdf_summary.get("job_type") != "pdf"
        or pdf_summary.get("language") != "english"
        or totals != {"total": 1, "success": 1, "failed": 0}
        or not isinstance(results, list)
        or len(results) != 1
        or not isinstance(results[0], dict)
    ):
        raise RuntimeError(f"contest PDF output details are wrong: {pdf_summary!r}")
    result = results[0]
    source_folder = str(result.get("source_folder") or "")
    if (
        result.get("status") != "success"
        or result.get("problem_slug") != problem
        or result.get("source_commit") != expected_head
        or not source_folder
    ):
        raise RuntimeError(f"contest PDF problem result is wrong: {result!r}")
    artifact_id = str(pdf_summary.get("artifact_id") or "")
    if not artifact_id or pdf_summary.get("pdf_file") != "contest-pdf/statements.pdf":
        raise RuntimeError(f"contest PDF artifact identity is missing: {pdf_summary!r}")

    download = client.get(
        f"/contests/{CONTEST}/packages/artifacts/{artifact_id}"
    )
    if (
        download.status_code != 200
        or len(download.content) < 100
        or not download.content.startswith(b"%PDF-")
    ):
        raise RuntimeError(
            f"contest PDF download is invalid: status={download.status_code} "
            f"size={len(download.content)}"
        )

    job_root = _contest_job_root(job_id)
    summary_path = job_root / "summary.json"
    persisted_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if persisted_summary != summary:
        raise RuntimeError("contest job status and persisted summary differ")
    statement_root = _resolved_child(
        job_root,
        job_root
        / "contest-pdf-src"
        / "problems"
        / source_folder
        / "statements"
        / "english",
    )
    expected_samples = {
        "sample.001.in": b"7\n",
        "sample.001.ans": b"49\n",
    }
    for filename, expected in expected_samples.items():
        path = statement_root / filename
        if path.read_bytes() != expected:
            raise RuntimeError(
                f"contest PDF compiled an incorrect sample payload: {filename}"
            )
    problem_tex = (statement_root / "problem.tex").read_text(encoding="utf-8")
    if (
        r"\exmpfile" not in problem_tex
        or "sample.001.in" not in problem_tex
        or "sample.001.ans" not in problem_tex
    ):
        raise RuntimeError("contest PDF problem.tex omitted the verified sample")
    compile_log = (job_root / "logs" / "contest-pdf.log").read_text(
        encoding="utf-8",
        errors="replace",
    )
    if (
        "== xelatex pass 1 ==" not in compile_log
        or "== xelatex pass 2 ==" not in compile_log
        or compile_log.count("returncode: 0") < 2
    ):
        raise RuntimeError("contest PDF did not complete both xelatex passes")
    generated_pdf = job_root / "contest-pdf" / "statements.pdf"
    if generated_pdf.read_bytes() != download.content:
        raise RuntimeError("contest PDF download differs from the compiled artifact")

    materialization_verification_id = _assert_contest_database(
        connection,
        job_id=job_id,
        artifact_id=artifact_id,
        expected_head=expected_head,
        pdf_payload=download.content,
    )
    return artifact_id, materialization_verification_id
