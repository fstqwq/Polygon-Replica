import asyncio
import hashlib
import io
import json
import xml.etree.ElementTree as ET

import yaml
from fastapi import HTTPException
import shutil
import stat
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.impl.contest.package import _prepare_external_packages, contest_packages_download
from app.impl.contest.overview import contest_overview_page
from app.impl.runtime.dependency import bind_application
from app.main import app, runtime
from app.service.contest.package import (
    ContestPackageService,
    ContestPackageSnapshot,
)
from app.service.export.service import CachedExternalPackage
from app.service.problem_package.store import MaterializationRow
from tests.common import E2ETestBase
from tests.db_helpers import db_execute, db_fetch_all, db_fetch_one
from tests.package_builders import PdfSandbox
from tests.ui_support import _request
from jinja2.filters import do_filesizeformat
from tests.package_support import (
    blocked_export_queue,
    publish_problem,
    verification_builder,
)


class TestContestPackageDownload(E2ETestBase):
    seed_primary_workspace = False

    def setUp(self) -> None:
        super().setUp()
        self.user = "alice"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.contest = runtime.contest_service
        self.packages = runtime.problem_package_service
        self.registry = runtime.export_service.package_adapters
        self.native_packages = [
            self._publish_package("alice/alpha", ("english", "chinese")),
            self._publish_package("alice/beta", ("english", "chinese", "german")),
        ]
        self.actor_id = int(db_fetch_one("SELECT id FROM users WHERE username='alice'")["id"])
        self.contest_id = self.contest.create_contest_with_owner(
            slug="example-contest", title="Package fixture", owner_user_id=self.actor_id,
        )
        self.problem_ids = [package["problem_id"] for package in self.native_packages]
        for idx, package in zip(("A", "B"), self.native_packages, strict=True):
            self.contest.add_problem(self.contest_id, idx, package["problem_id"], self.actor_id)
        self.service = ContestPackageService(
            self.contest, self.registry, self.packages,
            problem_zip_max_expanded_bytes=4 * 1024 * 1024,
        )

    def _publish_package(self, problem_slug: str, languages: tuple[str, ...]) -> MaterializationRow:
        workspace = self._seed_workspace(problem_slug, self.user)
        english = workspace / "statement-sections/english"
        for language in languages:
            if language != "english":
                destination = workspace / "statement-sections" / language
                shutil.copytree(english, destination, dirs_exist_ok=True)
        for directory in (workspace / "statement-sections").iterdir():
            if directory.is_dir() and directory.name not in languages:
                shutil.rmtree(directory)
        _workspace, problem_id, _commit = publish_problem(workspace, problem_slug, self.user)
        revision = self.packages.published_revision(problem_id)
        return self.packages.ensure_native_package(revision, verification_builder(problem_id))

    def _snapshot(self, package_format: str = "domjudge") -> ContestPackageSnapshot:
        return self.service.freeze_download(
            contest_id=self.contest_id,
            contest_slug="example-contest",
            package_format=package_format,
        )

    def _external_packages(
        self,
        snapshot: ContestPackageSnapshot,
    ) -> dict[int, CachedExternalPackage]:
        result: dict[int, CachedExternalPackage] = {}
        for item in snapshot.items:
            path = self.root / f"external-{item.problem_id}.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "problem.yaml",
                    f"format: {snapshot.package_format}\nname: {item.problem_slug}\n",
                )
                archive.writestr("tests/1", f"input-{item.problem_id}\n")
                if snapshot.package_format == "domjudge":
                    archive.writestr(
                        "domjudge-problem.ini",
                        f"externalid = {item.problem_slug}\n"
                        "short-name = standalone\n"
                        "color = #123456\n",
                    )
            result[item.problem_id] = CachedExternalPackage(
                export_id=f"e-{item.problem_id}",
                native_package_id=item.native_package_id,
                package_format=snapshot.package_format,
                filename=path.name,
                path=path,
            )
        return result

    def _statement_pdfs(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for language in ("english", "chinese"):
            path = self.root / f"{language}.pdf"
            path.write_bytes(f"%PDF-{language}\n".encode())
            result[language] = path
        return result

    def _publish_interaction(self, passes: int) -> MaterializationRow:
        slug = "alice/game"
        work = self._seed_workspace(slug, self.user)
        ids = ("101", "105", "119")
        shutil.copytree(work / "statement-sections/english", work / "statement-sections/chinese",
                        dirs_exist_ok=True)
        _, problem_id, _ = publish_problem(work, slug, self.user, mode="interactive", test_ids=ids)
        problem_config = work / "config/problem.json"
        config = json.loads(problem_config.read_text())
        config["pass_limit"] = passes
        problem_config.write_text(json.dumps(config))
        plays = [
            [("interactor", "START"), ("solution", "A\n4"), ("interactor", "END")],
            [("interactor", "GO"), ("solution", "B"), ("interactor", "2"),
             ("solution", "2"), ("interactor", "0")],
        ]
        rows = [{"id": name, "kind": "manual"} for name in ids]
        for row, play in zip((rows[0], rows[2]), plays, strict=True):
            events = [{"source": who, "content": msg} for who, msg in play]
            row.update(sample=True, sample_json={
                "presentation": "interaction",
                "passes": [{"events": events} for _ in range(passes)],
            })
        (work / "tests/spec.json").write_text(json.dumps({"tests": rows}))
        runtime.git_service.commit(work, "Add structured contest examples", self.user, "alice@localhost")
        runtime.git_service.push(work, "main")
        package = self.packages.ensure_native_package(
            self.packages.published_revision(problem_id),
            verification_builder(
                problem_id, test_ids=ids, sample_ids=frozenset(("101", "119")),
                inputs_by_id={name: (name + " judge\n").encode() for name in ids},
                answer_bytes=b"[00:01] judge-log\n" if passes == 1 else None,
                mode="interactive", pass_limit=passes,
            ),
        )
        self.contest.add_problem(self.contest_id, "E", problem_id, self.actor_id)
        return package

    def _check_archive(self, package: zipfile.ZipFile, fmt: str, passes: int) -> None:
        answer = b"[00:01] judge-log\n" if passes == 1 else b""
        if fmt == "polygon-linux":
            root = ET.fromstring(package.read("problem.xml"))
            self.assertTrue(root.attrib["url"].endswith("/alice/game"))
            source = root.find("assets/interactor/source")
            copy = root.find("assets/interactor/copy")
            assert source is not None and copy is not None
            self.assertEqual(package.read(source.get("path")), package.read(copy.get("path")))
            self.assertEqual(root.find("judging").attrib["run-count"], str(passes))
            expected = [
                {"input": "START\n\n\nEND\n", "output": "\nA\n4\n\n"},
                {"input": "GO\n\n2\n\n0\n", "output": "\nB\n\n2\n\n"},
            ]
            for lang in ("chinese", "english"):
                prefix = f"statements/{lang}/"
                self.assertEqual(json.loads(package.read(prefix + "problem-properties.json"))["sampleTests"], expected)
                self.assertTrue(package.read(f"statements/.pdf/{lang}/problem.pdf").startswith(b"%PDF"))
                tex = package.read(prefix + "examples.tex").decode()
                for n, actors in enumerate(("isi", "isisi"), 1):
                    contents = ("START", "A\n4", "END") if n == 1 else ("GO", "B", "2", "2", "0")
                    for phase in range(1, passes + 1):
                        cursor = 0
                        for step, (actor, content) in enumerate(zip(actors, contents, strict=True), 1):
                            role = "interactor" if actor == "i" else "solution"
                            path = f"examples/sample-{n}/pass-{phase}/event-{step}.txt"
                            token = f"{{{role}}}{{{path}}}"
                            cursor = tex.index(token, cursor) + len(token)
                            self.assertEqual(package.read(prefix + path).decode(), content)
                for directory in (prefix, f"statement-sections/{lang}/"):
                    for ordinal, pair in zip(("01", "03"), expected, strict=True):
                        self.assertEqual(package.read(directory + "example." + ordinal).decode(), pair["input"])
                        self.assertEqual(package.read(directory + "example." + ordinal + ".a").decode(), pair["output"])
            for n, name in enumerate(("101", "105", "119"), 1):
                self.assertEqual(package.read(f"tests/{n:02}"), (name + " judge\n").encode())
                self.assertEqual(package.read(f"tests/{n:02}.a"), answer)
        elif fmt == "qoj":
            conf = package.read("problem.conf").decode().splitlines()
            for line in ("interaction_mode on", "n_tests 3", "n_ex_tests 2", "n_sample_tests 2"):
                self.assertIn(line, conf)
            self.assertEqual("polygon_runtwice_interactive on" in conf, passes == 2)
            self.assertIn("interactor.cpp", package.namelist())
            for stem, name in (("1", "101"), ("2", "105"), ("3", "119"), ("ex_1", "101"), ("ex_2", "119")):
                self.assertEqual(package.read(stem + ".in"), (name + " judge\n").encode())
                self.assertEqual(package.read(stem + ".ans"), answer)
        else:
            metadata = yaml.safe_load(package.read("problem.yaml"))
            if fmt == "domjudge":
                self.assertEqual(metadata["validation"], "custom interactive")
                self.assertEqual(metadata["problem_format_version"], "legacy")
                self.assertTrue(package.read("problem_statement/problem.pdf").startswith(b"%PDF"))
            else:
                self.assertEqual(fmt, "icpc-2025-09")
                self.assertEqual(metadata["problem_format_version"], "2025-09")
                self.assertIn("interactive", metadata["type"])
                self.assertEqual("multi-pass" in metadata["type"], passes == 2)
                for code in ("en", "zh"):
                    self.assertTrue(package.read(f"statement/problem.{code}.pdf").startswith(b"%PDF"))
            self.assertIn("output_validator/build", package.namelist())
            for name in ("101", "105", "119"):
                self.assertEqual(package.read(f"data/secret/{name}.in"), (name + " judge\n").encode())
                self.assertEqual(package.read(f"data/secret/{name}.ans"), answer)

    def _republish_plain_samples(self) -> None:
        work = runtime.storage_layout.workspace(self.user, "alice/alpha")
        ids = ("001", "002", "003")
        rows = [
            {"id": "001", "kind": "manual", "sample": True},
            {"id": "002", "kind": "manual", "sample": True, "sample_input": "manual input", "sample_output": "manual output"},
            {"id": "003", "kind": "manual", "sample": True, "sample_json": {
                "presentation": "pair", "passes": [{"input": "no output", "output": ""}],
            }},
        ]
        for name in ids:
            (work / f"tests/manual/{name}.in").write_text(name)
        (work / "tests/spec.json").write_text(json.dumps({"tests": rows}))
        runtime.git_service.commit(work, "Exercise plain sample presentations", self.user, "alice@localhost")
        runtime.git_service.push(work, "main")
        pid = self.problem_ids[0]
        self.native_packages[0] = self.packages.ensure_native_package(
            self.packages.published_revision(pid), verification_builder(
                pid, test_ids=ids, inputs_by_id={name: name.encode() for name in ids}, answer_bytes=b"judge answer",
            ),
        )

    def _check_download_matrix(self, passes: int) -> None:
        self._republish_plain_samples()
        native = self._publish_interaction(passes)
        supported = {"polygon-linux", "qoj", "domjudge", "icpc-2025-09"}
        self.assertEqual(set(self.registry.formats), supported | {"nowcoder"})
        with patch.object(runtime.tex_compile_service, "sandbox", PdfSandbox()):
            for fmt in sorted(supported):
                with self.subTest(fmt=fmt, passes=passes):
                    _, path, _ = runtime.export_service.create_export("alice/game", fmt, native_package_id=native["id"])
                    with zipfile.ZipFile(path) as single:
                        self._check_archive(single, fmt, passes)
                    response = contest_packages_download("example-contest", self.user, fmt)
                    try:
                        with zipfile.ZipFile(response.path) as outer:
                            ordinary = outer.read("packages/A-alice-alpha.zip")
                            if fmt == "polygon-linux":
                                with zipfile.ZipFile(io.BytesIO(ordinary)) as plain:
                                    props = json.loads(plain.read("statements/english/problem-properties.json"))
                                    self.assertEqual(props["sampleTests"], [
                                        {"input": "001", "output": "judge answer"},
                                        {"input": "manual input", "output": "manual output"},
                                        {"input": "no output", "output": ""},
                                    ])
                            with zipfile.ZipFile(io.BytesIO(outer.read("packages/E-alice-game.zip"))) as inner:
                                self._check_archive(inner, fmt, passes)
                    finally:
                        asyncio.run(response.background())
            with self.assertRaisesRegex(ValueError, "single-pass pass-fail"):
                runtime.export_service.create_export("alice/game", "nowcoder", native_package_id=native["id"])
            with self.assertRaises(HTTPException) as error:
                contest_packages_download("example-contest", self.user, "nowcoder")
            self.assertIn("E alice/game [nowcoder]", str(error.exception.detail))
            self.assertIsNone(runtime.export_service.cached_external_package(
                problem_id=native["problem_id"], native_package_id=native["id"], package_format="nowcoder",
            ))
            self.assertIsNotNone(runtime.export_service.cached_external_package(
                problem_id=self.problem_ids[0], native_package_id=self.native_packages[0]["id"], package_format="nowcoder",
            ))

    def test_single_pass_interactive_archive_matrix(self) -> None:
        self._check_download_matrix(1)

    def test_two_pass_interactive_archive_matrix(self) -> None:
        self._check_download_matrix(2)

    def test_polygon_old_archives_rebuild_and_missing_display_resource_retries(self) -> None:
        package = self.native_packages[0]
        pid, mid = package["problem_id"], package["id"]
        native_path = runtime.storage_layout.resolve_artifact(package["archive_rel_path"])
        with zipfile.ZipFile(native_path) as archive:
            original = {name: archive.read(name) for name in archive.namelist()}
        manifest_path = "test-data/manifest.json"
        manifest = json.loads(original[manifest_path])

        def replace_native(members: dict[str, bytes]) -> None:
            with zipfile.ZipFile(native_path, "w") as archive:
                for name, content in members.items():
                    archive.writestr(name, content)
            data = native_path.read_bytes()
            db_execute(
                "UPDATE problem_package_materializations SET archive_sha256=?,archive_size_bytes=? WHERE id=?",
                [hashlib.sha256(data).hexdigest(), len(data), mid],
            )

        export = runtime.export_service
        with patch.object(runtime.tex_compile_service, "sandbox", PdfSandbox()):
            export_id, output, _ = export.create_export("alice/alpha", "polygon-linux", native_package_id=mid)
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("statement-sections/english/example.01.a", "[00:01] stale judge log")
            data = output.read_bytes()
            db_execute("UPDATE exports SET id=?,sha256=?,size_bytes=? WHERE id=?",
                       ["e-old", hashlib.sha256(data).hexdigest(), len(data), export_id])
            self.assertIsNone(export.cached_external_package(
                problem_id=pid, native_package_id=mid, package_format="polygon-linux",
            ))
            old_manifest = dict(manifest)
            del old_manifest["statement_samples_version"], old_manifest["statement_samples"]
            replace_native({**original, manifest_path: json.dumps(old_manifest).encode()})
            with self.assertRaisesRegex(ValueError, "Rebuild the Native Package"):
                export.create_export("alice/alpha", "polygon-linux", native_package_id=mid)
            with self.assertRaises(HTTPException) as error:
                contest_packages_download("example-contest", self.user, "polygon-linux")
            self.assertIn("A alice/alpha [polygon-linux]", str(error.exception.detail))
            self.assertIn("Rebuild the Native Package", str(error.exception.detail))

            repaired = self.packages.ensure_native_package(
                self.packages.published_revision(pid), verification_builder(pid),
            )
            self.assertEqual(repaired["id"], mid)
            bad = dict(original)
            sample_path = manifest["statement_samples"]["001"]["input"]
            del bad["statement-build/english/" + sample_path]
            replace_native(bad)
            with self.assertRaisesRegex(ValueError, "001 .*english.*" + sample_path):
                export.create_export("alice/alpha", "polygon-linux", native_package_id=mid)
            self.assertIsNone(export.cached_external_package(
                problem_id=pid, native_package_id=mid, package_format="polygon-linux",
            ))
            replace_native(original)
            response = contest_packages_download("example-contest", self.user, "polygon-linux")
            try:
                with zipfile.ZipFile(response.path) as outer:
                    with zipfile.ZipFile(io.BytesIO(outer.read("packages/A-alice-alpha.zip"))) as inner:
                        self.assertEqual(inner.read("statement-sections/english/example.01.a"), b"display output\n")
            finally:
                asyncio.run(response.background())
            first = export.create_export("alice/alpha", "polygon-linux", native_package_id=mid)
            self.assertNotEqual(first[0], export_id)
            self.assertEqual(first, export.create_export("alice/alpha", "polygon-linux", native_package_id=mid))

    def test_freeze_requires_ready_packages_and_intersects_languages(self) -> None:
        overview = contest_overview_page(
            _request("/contests/example-contest/overview"), "example-contest", self.user,
        )
        for package in self.native_packages:
            self.assertIn(
                do_filesizeformat(package["archive_size_bytes"], binary=True),
                overview.body.decode(),
            )
        snapshot = self._snapshot()

        self.assertEqual(snapshot.source_generation, self.contest.contest_context("example-contest")["source_generation"])
        self.assertEqual(snapshot.statement_languages, ("english", "chinese"))
        self.assertEqual(
            [item.native_package_id for item in snapshot.items],
            [package["id"] for package in self.native_packages],
        )

        self.packages.store.invalidate_materialization(self.native_packages[1]["id"], "fixture unavailable")
        with self.assertRaisesRegex(ValueError, "Packages are not ready: alice/beta"):
            self._snapshot()

    def test_validate_snapshot_rejects_contest_changes(self) -> None:
        snapshot = self._snapshot()
        self.contest.remove_problem(self.contest_id, self.problem_ids[1])

        with self.assertRaisesRegex(ValueError, "retry download"):
            self.service.validate_snapshot(snapshot)

    def test_freeze_requires_one_common_statement_language(self) -> None:
        self._publish_package("alice/beta", ("german",))

        with self.assertRaisesRegex(ValueError, "no common statement language"):
            self._snapshot()

    def test_download_assembles_cached_packages_and_common_statements(self) -> None:
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                external_packages = self._external_packages(snapshot)
                cached_payloads = {
                    problem_id: cached.path.read_bytes()
                    for problem_id, cached in external_packages.items()
                }
                download = self.service.build_download(
                    snapshot,
                    external_packages=external_packages,
                    statement_pdfs=self._statement_pdfs(),
                )
                self.assertEqual(
                    download.filename,
                    f"example-contest-{package_format}-packages.zip",
                )
                with zipfile.ZipFile(download.path) as archive:
                    self.assertEqual(
                        archive.namelist(),
                        [
                            "statements.en.pdf",
                            "statements.zh.pdf",
                            "packages/A-alice-alpha.zip",
                            "packages/B-alice-beta.zip",
                        ],
                    )
                    self.assertEqual(archive.read("statements.en.pdf"), b"%PDF-english\n")
                    for item in snapshot.items:
                        token = item.problem_slug.replace("/", "-")
                        payload = archive.read(f"packages/{item.idx}-{token}.zip")
                        if package_format != "domjudge":
                            self.assertEqual(
                                payload,
                                cached_payloads[item.problem_id],
                            )
                        with zipfile.ZipFile(io.BytesIO(payload)) as package:
                            self.assertEqual(
                                package.read("tests/1"),
                                f"input-{item.problem_id}\n".encode(),
                            )
                            self.assertEqual(
                                package.read("problem.yaml"),
                                f"format: {package_format}\nname: {item.problem_slug}\n".encode(),
                            )
                            if package_format == "domjudge":
                                color = ("#e6194b", "#4363d8")[item.ordinal - 1]
                                self.assertEqual(
                                    package.read("domjudge-problem.ini").decode(),
                                    f"externalid = {item.problem_slug}\n"
                                    f"short-name = {item.idx}\n"
                                    f"color = {color}\n",
                                )
                for problem_id, cached in external_packages.items():
                    self.assertEqual(cached.path.read_bytes(), cached_payloads[problem_id])
                download.close()
                self.assertFalse(download.cleanup_root.exists())

    def test_download_rejects_incomplete_inputs(self) -> None:
        snapshot = self._snapshot()
        external_packages = self._external_packages(snapshot)
        external_packages.pop(self.problem_ids[1])
        with self.assertRaisesRegex(ValueError, "external package set is incomplete"):
            self.service.build_download(
                snapshot,
                external_packages=external_packages,
                statement_pdfs=self._statement_pdfs(),
            )

    def test_download_rejects_an_invalid_cached_external_archive(self) -> None:
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                external_packages = self._external_packages(snapshot)
                external_packages[self.problem_ids[0]].path.write_bytes(b"not a zip")

                with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
                    self.service.build_download(
                        snapshot,
                        external_packages=external_packages,
                        statement_pdfs=self._statement_pdfs(),
                    )

    def test_download_rejects_corrupt_members_and_unsafe_paths(self) -> None:
        for package_format in self.registry.formats:
            for invalid_kind in ("crc", "path", "conflict"):
                with self.subTest(package_format=package_format, invalid_kind=invalid_kind):
                    snapshot = self._snapshot(package_format)
                    external_packages = self._external_packages(snapshot)
                    path = external_packages[self.problem_ids[0]].path
                    if invalid_kind == "crc":
                        path.write_bytes(
                            path.read_bytes().replace(f"input-{self.problem_ids[0]}\n".encode(), f"INPUT-{self.problem_ids[0]}\n".encode(), 1)
                        )
                    else:
                        with zipfile.ZipFile(path, "w") as archive:
                            if invalid_kind == "path":
                                archive.writestr("../outside", b"payload")
                            else:
                                archive.writestr("tests", b"payload")
                                archive.writestr("tests/1", b"payload")
                    with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
                        self.service.build_download(
                            snapshot,
                            external_packages=external_packages,
                            statement_pdfs=self._statement_pdfs(),
                        )

    def test_unchanged_packages_validate_directory_entries(self) -> None:
        for package_format in self.registry.formats:
            if package_format == "domjudge":
                continue
            for invalid_kind in ("symlink", "crc", "expanded"):
                with self.subTest(package_format=package_format, invalid_kind=invalid_kind):
                    snapshot = self._snapshot(package_format)
                    external_packages = self._external_packages(snapshot)
                    path = external_packages[self.problem_ids[0]].path
                    directory = zipfile.ZipInfo("payload/")
                    directory.external_attr = (
                        stat.S_IFLNK if invalid_kind == "symlink" else stat.S_IFDIR
                    ) << 16
                    content = (
                        b"x" * (4 * 1024 * 1024 + 1)
                        if invalid_kind == "expanded"
                        else b"directory payload"
                    )
                    with zipfile.ZipFile(path, "w") as archive:
                        archive.writestr(directory, content)
                    if invalid_kind == "crc":
                        path.write_bytes(
                            path.read_bytes().replace(content, b"Directory payload", 1)
                        )
                    with self.assertRaisesRegex(ValueError, "cached external package is invalid"):
                        self.service.build_download(
                            snapshot,
                            external_packages=external_packages,
                            statement_pdfs=self._statement_pdfs(),
                        )

    def test_download_reports_the_expanded_limit_for_a_cached_archive(self) -> None:
        for package_format in self.registry.formats:
            with self.subTest(package_format=package_format):
                snapshot = self._snapshot(package_format)
                external_packages = self._external_packages(snapshot)
                with zipfile.ZipFile(external_packages[self.problem_ids[0]].path, "w") as archive:
                    archive.writestr("data/secret/020.ans", b"x" * (4 * 1024 * 1024 + 1))

                with self.assertRaises(ValueError) as raised:
                    self.service.build_download(
                        snapshot,
                        external_packages=external_packages,
                        statement_pdfs=self._statement_pdfs(),
                    )

                filename = (
                    f"external-{self.problem_ids[0]}.zip"
                    if package_format == "domjudge"
                    else "A-alice-alpha.zip"
                )
                self.assertEqual(
                    str(raised.exception),
                    f"cached external package is invalid: {filename}: "
                    "expanded zip payload is too large at data/secret/020.ans; "
                    "increase PROBLEM_ZIP_MAX_EXPANDED_BYTES (currently 4194304 bytes)",
                )

    def test_freeze_rejects_unregistered_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported package format: custom"):
            self._snapshot("custom")

    def test_prepare_submits_missing_exports_together_and_reuses_completed_cache(self) -> None:
        snapshot = self._snapshot()
        results: list[dict[int, CachedExternalPackage]] = []
        errors: list[BaseException] = []

        def prepare() -> None:
            try:
                with bind_application(app):
                    results.append(_prepare_external_packages(snapshot, actor_user_id=self.actor_id))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(runtime.tex_compile_service, "sandbox", PdfSandbox(),), blocked_export_queue() as release:
            thread = threading.Thread(target=prepare, daemon=True)
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                queued = db_fetch_one("SELECT COUNT(*) AS n FROM export_jobs WHERE status='queued'")
                if int(queued["n"]) == 2:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(int(queued["n"]), 2)
            self.assertTrue(thread.is_alive())
            release.set()
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(set(results[0]), set(self.problem_ids))
        reused = _prepare_external_packages(snapshot, actor_user_id=self.actor_id)
        self.assertEqual(reused, results[0])
        rows = db_fetch_all("SELECT status FROM export_jobs")
        self.assertEqual([row["status"] for row in rows], ["succeeded", "succeeded"])
        for cached in reused.values():
            with zipfile.ZipFile(cached.path) as archive:
                self.assertIn("problem.yaml", archive.namelist())

    def test_prepare_reports_problem_identity_format_and_worker_error(self) -> None:
        snapshot = self._snapshot()
        package = self.native_packages[0]
        runtime.storage_layout.resolve_artifact(package["archive_rel_path"]).write_bytes(b"damaged archive")
        with patch.object(runtime.tex_compile_service, "sandbox", PdfSandbox(),):
            with self.assertRaises(ValueError) as raised:
                _prepare_external_packages(snapshot, actor_user_id=self.actor_id)
        self.assertIn("A alice/alpha [domjudge]:", str(raised.exception))
        failures = db_fetch_all("SELECT error FROM export_jobs WHERE problem_id=? AND status='failed'", [self.problem_ids[0]])
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0]["error"])
        self.assertIsNone(runtime.export_service.cached_external_package(
            problem_id=self.problem_ids[0], native_package_id=package["id"], package_format="domjudge",
        ))

if __name__ == "__main__":
    unittest.main()
