"""Fail-closed verifier for the official DOMjudge source behind the mock."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from domjudge_contract import (
    APPROVAL_FILENAME,
    SOURCE_REQUIREMENTS,
    UPSTREAM_PEELED_COMMIT,
    UPSTREAM_REPOSITORY,
    UPSTREAM_TAG,
    state_dir,
    verified_behaviors,
)


def _run_git(arguments: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    approval_path = state_dir() / APPROVAL_FILENAME
    approval_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="domjudge-contract-") as raw_temp:
        checkout = Path(raw_temp) / "domjudge"
        _run_git(
            [
                "-c",
                "advice.detachedHead=false",
                "clone",
                "--depth",
                "1",
                "--branch",
                UPSTREAM_TAG,
                "--single-branch",
                UPSTREAM_REPOSITORY,
                str(checkout),
            ]
        )
        actual_commit = _run_git(["rev-parse", "HEAD"], cwd=checkout)
        peeled_tag_commit = _run_git(
            ["rev-parse", f"refs/tags/{UPSTREAM_TAG}^{{}}"],
            cwd=checkout,
        )
        if actual_commit != UPSTREAM_PEELED_COMMIT or peeled_tag_commit != actual_commit:
            raise RuntimeError(
                "DOMjudge checkout or peeled tag resolved to an unexpected commit: "
                f"expected {UPSTREAM_PEELED_COMMIT}, HEAD={actual_commit}, "
                f"peeled_tag={peeled_tag_commit}"
            )

        sources = {
            relative: (checkout / relative).read_text(encoding="utf-8")
            for relative in SOURCE_REQUIREMENTS
        }
        missing = {
            f"{relative.as_posix()}:{behavior}": [
                literal for literal in literals if literal not in sources[relative]
            ]
            for relative, requirements in SOURCE_REQUIREMENTS.items()
            for behavior, literals in requirements.items()
        }
        missing = {
            behavior: literals
            for behavior, literals in missing.items()
            if literals
        }
        if missing:
            details = "; ".join(
                f"{behavior}: {literals!r}" for behavior, literals in sorted(missing.items())
            )
            raise RuntimeError(f"official DOMjudge source does not satisfy mock contract: {details}")

        source_digests = {
            relative.as_posix(): hashlib.sha256(
                (checkout / relative).read_bytes()
            ).hexdigest()
            for relative in SOURCE_REQUIREMENTS
        }
        _atomic_json(
            approval_path,
            {
                "approved": True,
                "repository": UPSTREAM_REPOSITORY,
                "tag": UPSTREAM_TAG,
                "commit": actual_commit,
                "source_sha256s": source_digests,
                "verified_behaviors": verified_behaviors(),
            },
        )
        print(
            "approved DOMjudge Judgehost wire contract "
            f"tag={UPSTREAM_TAG} commit={actual_commit} "
            f"source_sha256s={source_digests}"
        )


if __name__ == "__main__":
    main()
