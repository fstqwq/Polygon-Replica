"""Import three DOMjudge projections and execute their jury submissions on 9.0.0."""

import argparse
import json
import os
import shutil
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import httpx

import e2e_real as product


CONTEST_ID = "projection-e2e"
DOMSERVER_TIMEOUT_SEC = 240.0
JUDGEHOST_TIMEOUT_SEC = 180.0
IMPORT_TIMEOUT_SEC = 120.0
JURY_TIMEOUT_SEC = 360.0


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _statement_files(title: str) -> dict[str, str]:
    templates = Path(os.environ["POLYGON_REPLICA_E2E_SKILLS_ROOT"]) / "polygon-init/templates"
    return {
        "statement/statements.ftl": (templates / "statements.ftl").read_text(
            encoding="utf-8"
        ),
        "statement/problem.tex": (templates / "problem.tex").read_text(
            encoding="utf-8"
        ),
        "statement/olymp.sty": (templates / "olymp.sty").read_text(
            encoding="utf-8"
        ),
        "statement-sections/english/name.tex": title + "\n",
        "statement-sections/english/legend.tex": "A real DOMjudge package test.\n",
        "statement-sections/english/input.tex": "One integer.\n",
        "statement-sections/english/output.tex": "One integer.\n",
        "statement-sections/english/notes.tex": "\n",
    }


VALIDATOR = r'''#include "testlib.h"
int main(int argc, char **argv) {
    registerValidation(argc, argv);
    inf.readInt();
    inf.readEoln();
    inf.readEof();
}
'''

CHECKER = r'''#include "testlib.h"
int main(int argc, char **argv) {
    registerTestlibCmd(argc, argv);
    long long expected = ans.readLong();
    long long actual = ouf.readLong();
    if (expected == actual) quitf(_ok, "ok");
    quitf(_wa, "expected %lld, found %lld", expected, actual);
}
'''

MULTIPASS_CHECKER = r'''#include "testlib.h"
#include <fstream>
int main(int argc, char **argv) {
    registerTestlibCmd(argc, argv);
    int stage = inf.readInt();
    int actual = ouf.readInt();
    if (stage == 1 && actual == 1) {
        std::ofstream next(std::string(argv[3]) + "/nextpass.in");
        next << "2\n";
        next.close();
        quitf(_ok, "continue to pass two");
    }
    if (stage == 2 && actual == 2) quitf(_ok, "two passes completed");
    quitf(_wa, "unexpected answer %d on stage %d", actual, stage);
}
'''

INTERACTOR = r'''#include "testlib.h"
int main(int argc, char **argv) {
    registerInteraction(argc, argv);
    int value = inf.readInt();
    std::cout << value << std::endl;
    int reply;
    if (!(std::cin >> reply)) quitf(_wa, "no reply");
    if (reply == value * 2) quitf(_ok, "correct reply");
    quitf(_wa, "wrong reply");
}
'''


def _pass_fail_files() -> dict[str, str]:
    files = _statement_files("Projection pass-fail")
    files.update(
        {
            "config/problem.json": _json_text(
                {
                    "memory_limit_mb": 256,
                    "mode": "pass-fail",
                    "pass_limit": 1,
                    "time_limit_ms": 300,
                }
            ),
            "config/build.json": _json_text(
                {
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "generator_sources": [],
                    "validator_source": "validators/validator.cpp",
                }
            ),
            "tests/spec.json": _json_text(
                {"tests": [{"id": "001", "kind": "manual"}]}
            ),
            "tests/manual/001.in": "7\n",
            "checkers/checker.cpp": CHECKER,
            "validators/validator.cpp": VALIDATOR,
            "solutions/accepted.cpp": (
                "#include <iostream>\nint main(){long long x;std::cin>>x;"
                "std::cout<<x*2<<'\\n';}\n"
            ),
            "solutions/wrong.cpp": "#include <iostream>\nint main(){std::cout<<0<<'\\n';}\n",
            "solutions/wrong.cpp.desc": "expected: wrong_answer\n",
            "solutions/timeout.cpp": "int main(){for(;;){} }\n",
            "solutions/timeout.cpp.desc": "expected: time_limit_exceeded\n",
            "solutions/runtime.cpp": "int main(){return 1;}\n",
            "solutions/runtime.cpp.desc": "expected: run_time_error\n",
            "solutions/tle-or-correct.cpp": (
                "#include <iostream>\nint main(){long long x;std::cin>>x;"
                "std::cout<<x*2<<'\\n';}\n"
            ),
            "solutions/tle-or-correct.cpp.desc": "expected: tle_or_correct\n",
            "solutions/tle-or-re.cpp": "int main(){return 1;}\n",
            "solutions/tle-or-re.cpp.desc": "expected: tle_or_re\n",
            "solutions/rejected.cpp": "this is not C++\n",
            "solutions/rejected.cpp.desc": "expected: rejected\n",
        }
    )
    return files


def _interactive_files() -> dict[str, str]:
    files = _statement_files("Projection interactive")
    files.update(
        {
            "config/problem.json": _json_text(
                {
                    "memory_limit_mb": 256,
                    "mode": "interactive",
                    "pass_limit": 1,
                    "time_limit_ms": 1000,
                }
            ),
            "config/build.json": _json_text(
                {
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "generator_sources": [],
                    "interactor_source": "interactors/interactor.cpp",
                    "validator_source": "validators/validator.cpp",
                }
            ),
            "tests/spec.json": _json_text(
                {"tests": [{"id": "001", "kind": "manual"}]}
            ),
            "tests/manual/001.in": "7\n",
            "interactors/interactor.cpp": INTERACTOR,
            "validators/validator.cpp": VALIDATOR,
            "solutions/accepted.cpp": (
                "#include <iostream>\nint main(){long long x;std::cin>>x;"
                "std::cout<<x*2<<std::endl;}\n"
            ),
        }
    )
    return files


def _multipass_files() -> dict[str, str]:
    files = _statement_files("Projection multi-pass")
    files.update(
        {
            "config/problem.json": _json_text(
                {
                    "memory_limit_mb": 256,
                    "mode": "pass-fail",
                    "pass_limit": 2,
                    "time_limit_ms": 1000,
                }
            ),
            "config/build.json": _json_text(
                {
                    "accepted_solution_source": "solutions/accepted.cpp",
                    "checker_source": "checkers/checker.cpp",
                    "generator_sources": [],
                    "validator_source": "validators/validator.cpp",
                }
            ),
            "tests/spec.json": _json_text(
                {"tests": [{"id": "001", "kind": "manual"}]}
            ),
            "tests/manual/001.in": "1\n",
            "checkers/checker.cpp": MULTIPASS_CHECKER,
            "validators/validator.cpp": VALIDATOR,
            "solutions/accepted.cpp": (
                "#include <iostream>\nint main(){int stage;std::cin>>stage;"
                "std::cout<<stage<<'\\n';}\n"
            ),
        }
    )
    return files


FIXTURES = {
    "e2e/pass-fail": _pass_fail_files,
    "e2e/interactive": _interactive_files,
    "e2e/multi-pass": _multipass_files,
}


def _domserver() -> httpx.Client:
    return httpx.Client(
        base_url=os.environ["POLYGON_REPLICA_E2E_DOMSERVER_ORIGIN"],
        auth=("admin", os.environ["POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD"]),
        follow_redirects=False,
        timeout=30.0,
    )


def _api(client: httpx.Client, method: str, path: str, **kwargs: object) -> httpx.Response:
    response = client.request(method, "/api/v4" + path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(
            f"DOMjudge {method} {path} returned {response.status_code}: "
            f"{response.text[:1000]}"
        )
    return response


def _wait_domserver() -> None:
    deadline = time.monotonic() + DOMSERVER_TIMEOUT_SEC
    last = ""
    while time.monotonic() < deadline:
        try:
            with _domserver() as client:
                response = client.get("/api/v4/info")
            if response.status_code in {200, 401}:
                return
            last = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(1.0)
    raise RuntimeError(f"DOMserver was not ready within 240 seconds: {last}")


def _setup_product() -> None:
    product.AGENT_ROOT.mkdir(parents=True, exist_ok=True)
    product.AGENT_TEMP.mkdir(parents=True, exist_ok=True)
    with product._client() as client:
        product._setup(client)
        product._post(
            client,
            "/admin/judgehosts/runtime",
            {
                "judgehost_enable": "1",
                "judgehost_api_username": "judgehost",
                "judgehost_api_token": os.environ[
                    "POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN"
                ],
            },
        )
        initialized = product._agent_cli(
            "init",
            "--register-url",
            product._agent_register_url(client),
            "--agent-name",
            "DOMserver 9.0.0 E2E",
            "--desktop-id",
            "domserver-900-ci",
            "--init-ts",
            "2026-08-14T00:00:00Z",
        )
        if initialized.get("user") != product.USERNAME:
            raise RuntimeError(f"Agent initialized for wrong user: {initialized!r}")
        session_id = str(initialized.get("agent_session_id") or "")
        if not session_id:
            raise RuntimeError(f"Agent init omitted session identity: {initialized!r}")
        product._agent_set_general_scope(client, session_id, "commit")


def _setup_domserver() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    contest = {
        "duration": "5:00:00",
        "formal_name": "Polygon Replica projection E2E",
        "id": CONTEST_ID,
        "start_time": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    account = [
        {
            "externalid": "projection-jury",
            "name": "Projection Jury",
            "password": os.environ["POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD"],
            "type": "admin",
            "username": "admin",
        }
    ]
    with _domserver() as client:
        response = _api(
            client,
            "POST",
            "/contests",
            files={"json": ("contest.json", json.dumps(contest), "application/json")},
        )
        if response.json() != CONTEST_ID:
            raise RuntimeError(f"DOMjudge created an unexpected contest: {response.text}")
        _api(
            client,
            "POST",
            "/users/accounts",
            files={"json": ("accounts.json", json.dumps(account), "application/json")},
        )
        current = _api(client, "GET", f"/contests/{CONTEST_ID}/account").json()
        if current.get("team_id") != "projection-jury":
            raise RuntimeError(f"DOMjudge admin was not bound to the jury team: {current!r}")


def prepare() -> None:
    _wait_domserver()
    _setup_product()
    _setup_domserver()
    print("DOMserver 9.0.0 and Polygon Replica are prepared")


def _set_agent_problem(problem: str) -> Path:
    product.PROBLEM = problem
    product.AGENT_REPO = product.AGENT_ROOT / problem
    return product.AGENT_REPO


def _approve_problem(client: httpx.Client, problem: str) -> Path:
    repo = _set_agent_problem(problem)
    created = product._agent_cli("create", "--problem", problem)
    if created.get("problem") != problem:
        raise RuntimeError(f"Agent created the wrong problem: {created!r}")
    access = product._agent_cli("connect", "--problem", problem)
    request_id = str(access.get("request_id") or "")
    approve_url = str(access.get("approve_url") or "")
    product._agent_approve(client, approve_url, scope="commit")
    approved = product._agent_cli(
        "poll",
        "--request-id",
        request_id,
        "--wait",
        "--interval-sec",
        "0.1",
        "--timeout-sec",
        "10",
    )
    if approved.get("status") != "approved":
        raise RuntimeError(f"Agent access was not approved: {approved!r}")
    cloned = product._agent_cli("clone", "--problem", problem, "--target-dir", str(repo))
    if cloned.get("created_repo") is not True:
        raise RuntimeError(f"Agent clone failed: {cloned!r}")
    return repo


def _replace_authored_files(repo: Path, files: dict[str, str]) -> None:
    for relative in (
        "attachments",
        "checkers",
        "config",
        "generators",
        "interactors",
        "solutions",
        "statement",
        "statement-assets",
        "statement-sections",
        "tests",
        "validators",
    ):
        shutil.rmtree(repo / relative, ignore_errors=True)
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _export_fixture(client: httpx.Client, problem: str, files: dict[str, str]) -> Path:
    repo = _approve_problem(client, problem)
    _replace_authored_files(repo, files)
    pushed = product._agent_cli("push", "--problem", problem, "--target-dir", str(repo))
    if pushed.get("applied") is not True:
        raise RuntimeError(f"Agent push failed: {pushed!r}")
    committed = product._agent_cli(
        "commit",
        "--problem",
        problem,
        "--message",
        "publish DOMserver 9.0.0 fixture",
    )
    if committed.get("status") != "ok":
        raise RuntimeError(f"Agent commit failed: {committed!r}")
    _job_id, archive = product._agent_export("domjudge")
    output_root = Path(os.environ["POLYGON_REPLICA_E2E_OUTPUT_ROOT"])
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"{problem.rsplit('/', 1)[-1]}.zip"
    shutil.copy2(archive, destination)
    return destination


def _expected_mismatch_messages(archive: Path) -> set[str]:
    messages: set[str] = set()
    with zipfile.ZipFile(archive) as package:
        for name in package.namelist():
            if not name.startswith("submissions/mixed/") or name.endswith("/"):
                continue
            source = package.read(name).decode("utf-8")
            first_line = source.splitlines()[0]
            marker = "@EXPECTED_RESULTS@:"
            results = first_line.upper().split(marker, 1)[1].strip()
            messages.add(
                f"Annotated result '{results.replace(',', ', ')}' "
                f"does not match directory for {name}"
            )
    return messages


def _import_problem(client: httpx.Client, archive: Path) -> str:
    started = time.monotonic()
    with archive.open("rb") as source:
        response = _api(
            client,
            "POST",
            f"/contests/{CONTEST_ID}/problems",
            files={"zip": (archive.name, source, "application/zip")},
            timeout=IMPORT_TIMEOUT_SEC,
        )
    if time.monotonic() - started > IMPORT_TIMEOUT_SEC:
        raise RuntimeError(f"DOMjudge import exceeded 120 seconds: {archive.name}")
    payload = response.json()
    danger = set(payload.get("messages", {}).get("danger", []))
    expected_danger = _expected_mismatch_messages(archive)
    if danger != expected_danger:
        raise RuntimeError(
            f"DOMjudge import danger messages differ for {archive.name}: "
            f"actual={sorted(danger)!r} expected={sorted(expected_danger)!r}"
        )
    if archive.stem == "pass-fail" and len(danger) != 3:
        raise RuntimeError(f"DOMjudge did not report exactly three mixed warnings: {payload!r}")
    problem_id = str(payload.get("problem_id") or "")
    if not problem_id:
        raise RuntimeError(f"DOMjudge import omitted problem ID: {payload!r}")
    return problem_id


def _wait_judgehosts(product_client: httpx.Client, domserver: httpx.Client) -> None:
    product._wait_for_real_judgehost(product_client, timeout_sec=JUDGEHOST_TIMEOUT_SEC)
    deadline = time.monotonic() + JUDGEHOST_TIMEOUT_SEC
    last: object = None
    while time.monotonic() < deadline:
        last = _api(domserver, "GET", "/judgehosts").json()
        if isinstance(last, list) and last:
            return
        time.sleep(1.0)
    raise RuntimeError(f"DOMserver Judgehost did not register within 180 seconds: {last!r}")


def _judgement_result(row: dict[str, object]) -> str:
    return str(row.get("judgement_type_id") or row.get("result") or "").upper()


def _wait_jury_results(client: httpx.Client, expected_count: int) -> list[dict[str, object]]:
    deadline = time.monotonic() + JURY_TIMEOUT_SEC
    last_submissions: object = None
    last_judgements: object = None
    while time.monotonic() < deadline:
        last_submissions = _api(
            client, "GET", f"/contests/{CONTEST_ID}/submissions"
        ).json()
        last_judgements = _api(
            client, "GET", f"/contests/{CONTEST_ID}/judgements"
        ).json()
        if isinstance(last_submissions, list) and isinstance(last_judgements, list):
            complete = [
                cast(dict[str, object], row)
                for row in last_judgements
                if isinstance(row, dict) and _judgement_result(row)
            ]
            if len(last_submissions) == expected_count and len(complete) >= expected_count:
                return complete
        time.sleep(1.0)
    raise RuntimeError(
        "jury submissions were not judged within 360 seconds: "
        f"submissions={last_submissions!r} judgements={last_judgements!r}"
    )


def _login_and_verify(client: httpx.Client) -> str:
    page = client.get("/login")
    page.raise_for_status()
    parser = product._HiddenInputParser()
    parser.feed(page.text)
    response = client.post(
        "/login",
        data={
            "_csrf_token": product._required_field(parser.values, "_csrf_token"),
            "_password": os.environ["POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD"],
            "_username": "admin",
        },
    )
    if response.status_code not in {302, 303}:
        raise RuntimeError(f"DOMjudge login failed: {response.status_code} {response.text[:500]}")
    contests = _api(
        client,
        "GET",
        "/contests",
        params={"onlyActive": "false", "strict": "false"},
    ).json()
    matching = [row for row in contests if row.get("id") == CONTEST_ID]
    if len(matching) != 1:
        raise RuntimeError(f"DOMjudge contest was not listed exactly once: {contests!r}")
    internal_id = matching[0].get("cid")
    if internal_id is None:
        raise RuntimeError(f"DOMjudge non-strict contest omitted internal cid: {matching[0]!r}")
    changed = client.get(
        f"/jury/change-contest/{internal_id}",
        headers={"Referer": str(client.base_url) + "jury"},
    )
    if changed.status_code not in {302, 303}:
        raise RuntimeError(f"DOMjudge contest selection failed: {changed.status_code}")
    verified = client.post("/jury/judging-verifier", data={"verify_multiple": "1"})
    verified.raise_for_status()
    lower = verified.text.lower()
    if "0 unexpected results" not in lower or "0 without magic string" not in lower:
        raise RuntimeError("DOMjudge Judging verifier did not accept every jury result")
    return verified.text


def run() -> None:
    with product._client() as product_client, _domserver() as domserver:
        product._login(product_client)
        _wait_judgehosts(product_client, domserver)
        archives: list[Path] = []
        for problem, factory in FIXTURES.items():
            files = factory()
            archive = _export_fixture(product_client, problem, files)
            archives.append(archive)
            _import_problem(domserver, archive)
        judgements = _wait_jury_results(domserver, expected_count=9)
        results = Counter(_judgement_result(row) for row in judgements)
        expected = Counter({"AC": 4, "CE": 1, "RTE": 2, "TLE": 1, "WA": 1})
        if results != expected:
            raise RuntimeError(f"DOMjudge jury results differ: {results!r} != {expected!r}")
        first_verifier_page = _login_and_verify(domserver)
        second_verifier_page = domserver.get("/jury/judging-verifier")
        second_verifier_page.raise_for_status()
        if "9 verified earlier" not in second_verifier_page.text.lower():
            raise RuntimeError(
                "DOMjudge Judging verifier did not persist all nine decisions: "
                f"{first_verifier_page[:500]!r}"
            )
    print(
        "DOMserver 9.0.0 imported and judged pass-fail, interactive-only, "
        f"and multi-pass-only packages: {[path.name for path in archives]!r}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("wait-domserver", "prepare", "run"))
    args = parser.parse_args()
    if args.phase == "wait-domserver":
        _wait_domserver()
    elif args.phase == "prepare":
        prepare()
    else:
        run()


if __name__ == "__main__":
    main()
