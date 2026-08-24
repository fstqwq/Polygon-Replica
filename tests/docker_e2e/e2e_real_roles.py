"""Role-aware page smoke checks and concurrent workspace collaboration E2E."""

import os
import sqlite3
import subprocess
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx


ALICE = "alice"
BOB = "bob"
READER = "reader"
OUTSIDER = "outsider"

ClientFactory = Callable[[], httpx.Client]
RegisterUser = Callable[[str, str], httpx.Client]
PostRedirect = Callable[
    [httpx.Client, str, dict[str, str]],
    httpx.Response,
]


class _MergeChoiceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.workspace_choices: set[str] = set()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        name = attributes.get("name") or ""
        if (
            attributes.get("type") == "radio"
            and name.startswith("choice_")
            and attributes.get("value") == "workspace"
        ):
            self.workspace_choices.add(name)


def _connect() -> sqlite3.Connection:
    database = Path(os.environ["POLYGON_REPLICA_E2E_DB"]).resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {completed.stderr or completed.stdout}"
        )
    return completed.stdout.strip()


def _git_bytes(*args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _resolved_child(root: Path, child: Path) -> Path:
    resolved_root = root.resolve()
    resolved_child = child.resolve()
    if resolved_child == resolved_root or resolved_root not in resolved_child.parents:
        raise RuntimeError(f"path escaped expected E2E root: {resolved_child}")
    return resolved_child


def _workspace_path(problem: str, username: str) -> Path:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT w.path
            FROM workspaces w
            JOIN problems p ON p.id=w.problem_id
            JOIN users u ON u.id=w.user_id
            WHERE p.slug=? AND u.username=?
            """,
            [problem, username],
        ).fetchone()
    if row is None:
        raise RuntimeError(f"workspace was not provisioned for {username!r}")
    root = Path(os.environ["POLYGON_REPLICA_E2E_WORKSPACE_ROOT"])
    return _resolved_child(root, Path(str(row["path"])))


def _bare_repo(problem: str) -> Path:
    with _connect() as connection:
        row = connection.execute(
            "SELECT repo_name FROM problems WHERE slug=?",
            [problem],
        ).fetchone()
    if row is None:
        raise RuntimeError(f"problem is missing: {problem}")
    root = Path(os.environ["POLYGON_REPLICA_E2E_BARE_ROOT"])
    return _resolved_child(root, root / str(row["repo_name"]))


def _published_head(bare: Path) -> str:
    return _git("--git-dir", str(bare), "rev-parse", "refs/heads/main")


def _published_file(bare: Path, head: str, path: str) -> bytes:
    return _git_bytes("--git-dir", str(bare), "show", f"{head}:{path}")


def _assert_get(
    client: httpx.Client,
    path: str,
    expected_status: int,
    *,
    role: str,
) -> httpx.Response:
    response = client.get(path)
    if response.status_code >= 500 or response.status_code != expected_status:
        raise RuntimeError(
            f"{role} GET {path} returned {response.status_code}, "
            f"expected {expected_status}: {response.text[:500]}"
        )
    return response


def _problem_pages(problem: str, verification_id: str) -> tuple[str, ...]:
    base = f"/problems/{problem}"
    return (
        f"{base}/statement",
        f"{base}/preview",
        f"{base}/generators",
        f"{base}/checker",
        f"{base}/checker/view-standard",
        f"{base}/validator",
        f"{base}/interactor",
        f"{base}/solutions",
        f"{base}/solutions/editor?path=solutions/main.cpp",
        f"{base}/tests",
        f"{base}/files?path=config/problem.json",
        f"{base}/workspace",
        f"{base}/history",
        f"{base}/run",
        f"{base}/run/new",
        f"{base}/run/details?verification_id={verification_id}",
        (
            f"{base}/run/details/test-fragment?"
            f"verification_id={verification_id}&test=001.in"
        ),
        f"{base}/export",
        f"{base}/access",
    )


def _contest_pages(contest: str) -> tuple[str, ...]:
    base = f"/contests/{contest}"
    return (
        f"{base}/overview",
        f"{base}/problems",
        f"{base}/properties",
        f"{base}/access",
    )


def _grant_roles(
    admin: httpx.Client,
    *,
    sessions: dict[str, httpx.Client],
    post_redirect: PostRedirect,
    problem: str,
    contest: str,
) -> None:
    for username, role in ((ALICE, "write"), (BOB, "write"), (READER, "read")):
        post_redirect(
            admin,
            f"/problems/{problem}/access/grant",
            {"target_user": username, "role": role},
        )
    post_redirect(
        admin,
        f"/contests/{contest}/access/grant",
        {"target_user": ALICE, "role": "write"},
    )
    for username, role in ((BOB, "write"), (READER, "read")):
        post_redirect(
            sessions[ALICE],
            f"/contests/{contest}/access/grant",
            {"target_user": username, "role": role},
        )


def _assert_persisted_roles(problem: str, contest: str) -> None:
    with _connect() as connection:
        problem_roles = {
            str(row["username"]): str(row["role"])
            for row in connection.execute(
                """
                SELECT u.username,a.role
                FROM repo_acl a
                JOIN users u ON u.id=a.user_id
                JOIN problems p ON p.id=a.problem_id
                WHERE p.slug=?
                """,
                [problem],
            ).fetchall()
        }
        contest_roles = {
            str(row["username"]): str(row["role"])
            for row in connection.execute(
                """
                SELECT u.username,m.role
                FROM contest_members m
                JOIN users u ON u.id=m.user_id
                JOIN contests c ON c.id=m.contest_id
                WHERE c.slug=?
                """,
                [contest],
            ).fetchall()
        }
    expected_problem = {ALICE: "write", BOB: "write", READER: "read"}
    expected_contest = {ALICE: "write", BOB: "write", READER: "read"}
    if any(
        problem_roles.get(user) != role
        for user, role in expected_problem.items()
    ):
        raise RuntimeError(f"problem roles were not persisted: {problem_roles!r}")
    if any(
        contest_roles.get(user) != role
        for user, role in expected_contest.items()
    ):
        raise RuntimeError(f"contest roles were not persisted: {contest_roles!r}")
    if OUTSIDER in problem_roles or OUTSIDER in contest_roles:
        raise RuntimeError("outsider unexpectedly received direct access")


def _assert_contest_writer_adds_problem(
    *,
    admin: httpx.Client,
    sessions: dict[str, httpx.Client],
    post_redirect: PostRedirect,
    contest: str,
    addable_problem: str,
) -> None:
    post_redirect(
        admin,
        f"/problems/{addable_problem}/access/grant",
        {"target_user": ALICE, "role": "write"},
    )
    with _connect() as connection:
        existing = connection.execute(
            """
            SELECT 1
            FROM contest_problems cp
            JOIN contests c ON c.id=cp.contest_id
            JOIN problems p ON p.id=cp.problem_id
            WHERE c.slug=? AND p.slug=?
            """,
            [contest, addable_problem],
        ).fetchone()
    if existing is not None:
        raise RuntimeError("ordinary-user Contest add fixture was already present")

    response = post_redirect(
        sessions[ALICE],
        f"/contests/{contest}/problems/add",
        {"problem_slugs": addable_problem, "q": ""},
    )
    location = response.headers.get("location", "")
    if not (
        location.startswith(f"/contests/{contest}/access?focus_problem_id=")
        and location.endswith("#problem-access-matrix")
    ):
        raise RuntimeError(
            "ordinary Contest writer add redirected unexpectedly: "
            f"{response.headers!r}"
        )

    with _connect() as connection:
        added = connection.execute(
            """
            SELECT added_by.username AS added_by,direct.role AS direct_role,
                   membership.role AS contest_role
            FROM contest_problems cp
            JOIN contests c ON c.id=cp.contest_id
            JOIN problems p ON p.id=cp.problem_id
            JOIN users added_by ON added_by.id=cp.added_by_user_id
            JOIN users alice ON alice.username=?
            JOIN repo_acl direct
              ON direct.problem_id=p.id AND direct.user_id=alice.id
            JOIN contest_members membership
              ON membership.contest_id=c.id AND membership.user_id=alice.id
            WHERE c.slug=? AND p.slug=?
            """,
            [ALICE, contest, addable_problem],
        ).fetchone()
    if (
        added is None
        or str(added["added_by"]) != ALICE
        or str(added["direct_role"]) != "write"
        or str(added["contest_role"]) != "write"
    ):
        detail = None if added is None else dict(added)
        raise RuntimeError(
            f"ordinary Contest writer add was not persisted correctly: {detail!r}"
        )

    with _connect() as connection:
        access_ids = connection.execute(
            """
            SELECT p.id AS problem_id,
                   (SELECT id FROM users WHERE username=?) AS bob_id,
                   (SELECT id FROM users WHERE username=?) AS reader_id
            FROM problems p
            WHERE p.slug=?
            """,
            [BOB, READER, addable_problem],
        ).fetchone()
    if access_ids is None:
        raise RuntimeError("added Contest Problem access ids are unavailable")
    problem_id = int(access_ids["problem_id"])
    bob_id = int(access_ids["bob_id"])
    reader_id = int(access_ids["reader_id"])
    scoped_path = f"/problems/{addable_problem}/statement?contest={contest}"
    _assert_get(sessions[BOB], scoped_path, 403, role=BOB)
    _assert_get(sessions[READER], scoped_path, 403, role=READER)
    post_redirect(
        sessions[ALICE],
        f"/contests/{contest}/access/problems/save",
        {
            f"original_role.{problem_id}.{bob_id}": "none",
            f"role.{problem_id}.{bob_id}": "write",
            f"original_role.{problem_id}.{reader_id}": "none",
            f"role.{problem_id}.{reader_id}": "read",
        },
    )

    for role in (ALICE, BOB, READER):
        _assert_get(sessions[role], scoped_path, 200, role=role)
    _assert_get(sessions[OUTSIDER], scoped_path, 403, role=OUTSIDER)
    awarded_path = "attachments/matrix-awarded-write.txt"
    post_redirect(
        sessions[BOB],
        f"/problems/{addable_problem}/files/save?contest={contest}",
        {
            "path": awarded_path,
            "content": "direct write granted by Contest access matrix\n",
            "dir": "attachments",
        },
    )
    if not (_workspace_path(addable_problem, BOB) / awarded_path).is_file():
        raise RuntimeError("matrix-awarded writer could not edit the added Problem")


def _walk_pages(
    *,
    anonymous: httpx.Client,
    sessions: dict[str, httpx.Client],
    problem: str,
    contest: str,
    verification_id: str,
) -> None:
    root_pages = ("/problems", "/contests", "/settings", "/agent/sessions", "/sudo")
    problem_pages = _problem_pages(problem, verification_id)
    contest_pages = _contest_pages(contest)
    admin_pages = (
        "/admin",
        "/admin/judgehosts",
        "/admin/users",
        "/admin/mail",
        "/admin/config",
    )

    _assert_get(anonymous, "/login", 200, role="anonymous")
    _assert_get(anonymous, "/register", 200, role="anonymous")
    _assert_get(anonymous, "/register/verify", 200, role="anonymous")
    _assert_get(anonymous, "/setup", 303, role="anonymous")
    for path in (*root_pages, *problem_pages, *contest_pages):
        _assert_get(anonymous, path, 303, role="anonymous")
    for path in admin_pages:
        _assert_get(anonymous, path, 303, role="anonymous")

    for role, client in sessions.items():
        _assert_get(client, "/", 303, role=role)
        for path in ("/login", "/register", "/setup"):
            _assert_get(client, path, 303, role=role)
        _assert_get(client, "/register/verify", 200, role=role)
        for path in root_pages:
            _assert_get(client, path, 200, role=role)

        object_status = 403 if role == OUTSIDER else 200
        for path in (*problem_pages, *contest_pages):
            _assert_get(client, path, object_status, role=role)

        admin_status = 200 if role == "owner-admin" else 403
        for path in admin_pages[:-1]:
            _assert_get(client, path, admin_status, role=role)
        config = _assert_get(
            client,
            admin_pages[-1],
            302 if role == "owner-admin" else 403,
            role=role,
        )
        if role == "owner-admin":
            location = config.headers.get("location", "")
            if not location.startswith("/admin/config/"):
                raise RuntimeError(f"admin config omitted category redirect: {location!r}")
            _assert_get(client, location, 200, role=role)


def _assert_write_boundaries(
    *,
    sessions: dict[str, httpx.Client],
    post_redirect: PostRedirect,
    problem: str,
    contest: str,
) -> None:
    denied_path = "attachments/reader-must-not-write.txt"
    denied_requests = (
        (
            READER,
            f"/problems/{problem}/files/save",
            {"path": denied_path, "content": "reader", "dir": "attachments"},
        ),
        (
            OUTSIDER,
            f"/problems/{problem}/files/save",
            {"path": denied_path, "content": "outsider", "dir": "attachments"},
        ),
        (
            READER,
            f"/problems/{problem}/access/grant",
            {"target_user": OUTSIDER, "role": "read"},
        ),
        (
            READER,
            f"/contests/{contest}/properties/save",
            {"title": "unauthorized", "location": "", "date_text": ""},
        ),
        (
            READER,
            f"/contests/{contest}/access/revoke",
            {"target_user": BOB},
        ),
    )
    for role, path, data in denied_requests:
        client = sessions[role]
        response = client.post(
            path,
            data=data,
            headers={"Origin": str(client.base_url).rstrip("/")},
        )
        if response.status_code != 403:
            raise RuntimeError(
                f"{role} POST {path} returned {response.status_code}, expected 403: "
                f"{response.text[:500]}"
            )
    reader_workspace = _workspace_path(problem, READER)
    if (reader_workspace / denied_path).exists():
        raise RuntimeError("read-only user changed the problem workspace")
    post_redirect(
        sessions[ALICE],
        f"/problems/{problem}/access/grant",
        {"target_user": OUTSIDER, "role": "read"},
    )
    _assert_get(
        sessions[OUTSIDER],
        f"/problems/{problem}/statement",
        200,
        role=OUTSIDER,
    )
    post_redirect(
        sessions[ALICE],
        f"/problems/{problem}/access/revoke",
        {"target_user": OUTSIDER},
    )
    _assert_get(
        sessions[OUTSIDER],
        f"/problems/{problem}/statement",
        403,
        role=OUTSIDER,
    )
    _assert_persisted_roles(problem, contest)


def _assert_contest_scoped_problem_links(
    *,
    sessions: dict[str, httpx.Client],
    problem: str,
    contest: str,
) -> None:
    scoped_path = f"/problems/{problem}/statement?contest={contest}"
    for role in (ALICE, BOB, READER):
        _assert_get(sessions[role], scoped_path, 200, role=role)
    _assert_get(sessions[OUTSIDER], scoped_path, 403, role=OUTSIDER)


def _assert_reader_exits_contest(
    *,
    sessions: dict[str, httpx.Client],
    post_redirect: PostRedirect,
    problem: str,
    contest: str,
) -> None:
    response = post_redirect(
        sessions[READER],
        f"/contests/{contest}/access/revoke",
        {"target_user": READER},
    )
    if response.headers.get("location") != "/contests":
        raise RuntimeError(
            f"Contest reader exit redirected unexpectedly: {response.headers!r}"
        )

    with _connect() as connection:
        membership = connection.execute(
            """
            SELECT 1
            FROM contest_members membership
            JOIN contests contest ON contest.id=membership.contest_id
            JOIN users user ON user.id=membership.user_id
            WHERE contest.slug=? AND user.username=?
            """,
            [contest, READER],
        ).fetchone()
        direct_role = connection.execute(
            """
            SELECT acl.role
            FROM repo_acl acl
            JOIN problems problem ON problem.id=acl.problem_id
            JOIN users user ON user.id=acl.user_id
            WHERE problem.slug=? AND user.username=?
            """,
            [problem, READER],
        ).fetchone()
    if membership is not None:
        raise RuntimeError("Contest reader exit did not remove the membership")
    if direct_role is None or str(direct_role["role"]) != "read":
        raise RuntimeError(
            f"Contest reader exit changed direct Problem access: {direct_role!r}"
        )

    _assert_get(
        sessions[READER],
        f"/contests/{contest}/overview",
        403,
        role=READER,
    )
    _assert_get(
        sessions[READER],
        f"/problems/{problem}/statement",
        200,
        role=READER,
    )


def _assert_workspace_base(problem: str, username: str, expected_head: str) -> Path:
    workspace = _workspace_path(problem, username)
    head = _git("-C", str(workspace), "rev-parse", "HEAD")
    if head != expected_head:
        raise RuntimeError(
            f"{username} workspace started from {head}, expected {expected_head}"
        )
    if _git("-C", str(workspace), "status", "--porcelain"):
        raise RuntimeError(f"{username} workspace was not initially clean")
    return workspace


def _run_workspace_conflict(
    *,
    alice: httpx.Client,
    bob: httpx.Client,
    post_redirect: PostRedirect,
    problem: str,
    initial_head: str,
) -> str:
    bare = _bare_repo(problem)
    alice_workspace = _assert_workspace_base(problem, ALICE, initial_head)
    bob_workspace = _assert_workspace_base(problem, BOB, initial_head)
    conflict_path = "attachments/concurrent-edit.txt"
    alice_content = "Alice published this line.\n"
    bob_content = "Bob kept this line after resolving the conflict.\n"

    post_redirect(
        alice,
        f"/problems/{problem}/files/save",
        {"path": conflict_path, "content": alice_content, "dir": "attachments"},
    )
    post_redirect(
        bob,
        f"/problems/{problem}/files/save",
        {"path": conflict_path, "content": bob_content, "dir": "attachments"},
    )
    if (alice_workspace / conflict_path).read_text(encoding="utf-8") != alice_content:
        raise RuntimeError("Alice's workspace edit was not saved")
    if (bob_workspace / conflict_path).read_text(encoding="utf-8") != bob_content:
        raise RuntimeError("Bob's workspace edit was not saved")

    post_redirect(
        alice,
        f"/problems/{problem}/revision/commit",
        {"message": "Alice concurrent edit"},
    )
    alice_head = _published_head(bare)
    if alice_head == initial_head:
        raise RuntimeError("Alice's commit did not advance the published revision")
    if _git("-C", str(alice_workspace), "rev-parse", "HEAD") != alice_head:
        raise RuntimeError("Alice's workspace did not retain her published head")
    if _published_file(bare, alice_head, conflict_path) != alice_content.encode("utf-8"):
        raise RuntimeError("Alice's published content is incorrect")

    post_redirect(
        bob,
        f"/problems/{problem}/revision/commit",
        {"message": "Bob stale commit must fail"},
    )
    if _published_head(bare) != alice_head:
        raise RuntimeError("Bob's stale commit unexpectedly changed published main")
    if _git("-C", str(bob_workspace), "rev-parse", "HEAD") != initial_head:
        raise RuntimeError("Bob's rejected commit changed his workspace base")
    if not _git("-C", str(bob_workspace), "status", "--porcelain"):
        raise RuntimeError("Bob's rejected commit lost his local edit")

    merge_start = post_redirect(
        bob,
        f"/problems/{problem}/merge/start",
        {},
    )
    merge_path = merge_start.headers.get("location", "")
    parsed = urlparse(merge_path)
    expected_prefix = f"/problems/{problem}/merge/"
    if not parsed.path.startswith(expected_prefix):
        raise RuntimeError(f"Bob did not receive a merge preview: {merge_path!r}")
    preview_id = parsed.path[len(expected_prefix) :]
    if not preview_id or "/" in preview_id:
        raise RuntimeError(f"merge preview identity is invalid: {preview_id!r}")

    review = _assert_get(bob, f"{parsed.path}?mode=manual", 200, role=BOB)
    parser = _MergeChoiceParser()
    parser.feed(review.text)
    if not parser.workspace_choices:
        raise RuntimeError("Bob's conflict review offered no workspace resolution")
    choices = {name: "workspace" for name in sorted(parser.workspace_choices)}
    post_redirect(
        bob,
        f"{parsed.path}/apply",
        {"mode": "manual", **choices},
    )

    if _git("-C", str(bob_workspace), "rev-parse", "HEAD") != alice_head:
        raise RuntimeError("Bob's resolved workspace did not advance to Alice's head")
    if (bob_workspace / conflict_path).read_text(encoding="utf-8") != bob_content:
        raise RuntimeError("Bob's manual conflict resolution did not keep his content")
    if not _git("-C", str(bob_workspace), "status", "--porcelain"):
        raise RuntimeError("Bob's resolved content was not left for review and commit")

    post_redirect(
        bob,
        f"/problems/{problem}/revision/commit",
        {"message": "Bob resolved concurrent edit"},
    )
    bob_head = _published_head(bare)
    if bob_head == alice_head:
        raise RuntimeError("Bob's resolved commit did not advance published main")
    parents = _git(
        "--git-dir",
        str(bare),
        "rev-list",
        "--parents",
        "-n",
        "1",
        bob_head,
    ).split()
    if parents != [bob_head, alice_head]:
        raise RuntimeError(f"Bob's commit is not a linear child of Alice's: {parents!r}")
    if _published_file(bare, bob_head, conflict_path) != bob_content.encode("utf-8"):
        raise RuntimeError("Bob's resolved content was not published")
    if _git("-C", str(bob_workspace), "status", "--porcelain"):
        raise RuntimeError("Bob's workspace remained dirty after the resolved commit")
    if _git("-C", str(alice_workspace), "rev-parse", "HEAD") != alice_head:
        raise RuntimeError("Bob's work unexpectedly changed Alice's workspace")
    if (alice_workspace / conflict_path).read_text(encoding="utf-8") != alice_content:
        raise RuntimeError("Bob's work unexpectedly changed Alice's file")
    return bob_head


def exercise_role_pages_and_collaboration(
    *,
    admin: httpx.Client,
    client_factory: ClientFactory,
    register_user: RegisterUser,
    post_redirect: PostRedirect,
    problem: str,
    addable_problem: str,
    contest: str,
    verification_id: str,
    initial_head: str,
) -> str:
    passwords = {
        ALICE: "Alice-E2E-Password-9",
        BOB: "Bob-E2E-Password-9",
        READER: "Reader-E2E-Password-9",
        OUTSIDER: "Outsider-E2E-Password-9",
    }
    sessions: dict[str, httpx.Client] = {}
    anonymous = client_factory()
    try:
        for username, password in passwords.items():
            sessions[username] = register_user(username, password)
        _grant_roles(
            admin,
            sessions=sessions,
            post_redirect=post_redirect,
            problem=problem,
            contest=contest,
        )
        _assert_persisted_roles(problem, contest)
        _assert_contest_writer_adds_problem(
            admin=admin,
            sessions=sessions,
            post_redirect=post_redirect,
            contest=contest,
            addable_problem=addable_problem,
        )
        _walk_pages(
            anonymous=anonymous,
            sessions={"owner-admin": admin, **sessions},
            problem=problem,
            contest=contest,
            verification_id=verification_id,
        )
        _assert_write_boundaries(
            sessions=sessions,
            post_redirect=post_redirect,
            problem=problem,
            contest=contest,
        )
        _assert_contest_scoped_problem_links(
            sessions=sessions,
            problem=problem,
            contest=contest,
        )
        _assert_reader_exits_contest(
            sessions=sessions,
            post_redirect=post_redirect,
            problem=problem,
            contest=contest,
        )
        final_head = _run_workspace_conflict(
            alice=sessions[ALICE],
            bob=sessions[BOB],
            post_redirect=post_redirect,
            problem=problem,
            initial_head=initial_head,
        )
    finally:
        anonymous.close()
        for client in sessions.values():
            client.close()
    print(
        "e2e-real role page walk and Alice/Bob conflict resolution completed "
        f"head={final_head}"
    )
    return final_head
