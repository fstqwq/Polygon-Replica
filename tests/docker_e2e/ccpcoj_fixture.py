"""Generate a fresh synthetic native archive for the CCPCOJ consumer test."""

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from app.main import runtime
from tests.common import E2ETestBase
from tests.db_helpers import db_fetch_one
from tests.package_support import publish_problem, verification_builder

INTERACTOR = r"""#include "testlib.h"
#include <iostream>

int main(int argc, char **argv) {
    registerInteraction(argc, argv);
    int n = inf.readInt();
    for (int i = 0; i < 2; ++i) {
        std::cout << n + i << std::endl;
        if (ouf.readInt() != 3 * (n + i)) quitf(_wa, "incorrect triple");
    }
    quitf(_ok, "both triples correct");
}
"""
SOLUTION = r"""#include <iostream>
int main() {
    int n;
    while (std::cin >> n) std::cout << 3 * n << std::endl;
}
"""


class SyntheticExport(E2ETestBase):
    def build_archive(self, destination: Path) -> None:
        workspace, problem_id, _ = publish_problem(
            self._workspace_path(),
            self.problem,
            self.user,
            mode="interactive",
        )
        (workspace / "interactors/interactor.cpp").write_text(
            INTERACTOR, encoding="utf-8"
        )
        (workspace / "solutions/accepted.cpp").write_text(SOLUTION, encoding="utf-8")
        config_path = workspace / "config/problem.json"
        config = json.loads(config_path.read_text())
        config.update(time_limit_ms=1375, memory_limit_mb=256)
        config_path.write_text(json.dumps(config), encoding="utf-8")
        events = [
            {"source": speaker, "content": text}
            for speaker, text in (
                ("interactor", "1\n"),
                ("solution", "3\n"),
                ("interactor", "2\n"),
                ("solution", "6\n"),
            )
        ]
        test = {
            "id": "001",
            "kind": "manual",
            "sample": True,
            "sample_json": {
                "presentation": "interaction",
                "passes": [{"number": 1, "events": events}],
            },
        }
        (workspace / "tests/spec.json").write_text(
            json.dumps({"tests": [test]}), encoding="utf-8"
        )
        assets = workspace / "statement-assets"
        assets.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as temp:
            pages = []
            for color in ("red", "blue"):
                page_file = Path(temp) / f"{color}.pdf"
                pages.append(str(page_file))
                svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="{color}"/></svg>'
                subprocess.run(
                    ["rsvg-convert", "-f", "pdf", "-o", str(page_file)],
                    input=svg.encode(),
                    check=True,
                )
            subprocess.run(["pdfunite", *pages, str(assets / "square.pdf")], check=True)
        sections = workspace / "statement-sections"
        if not (sections / "chinese").is_dir():
            shutil.copytree(sections / "english", sections / "chinese")
        for section in sorted((workspace / "statement-sections").iterdir()):
            if section.is_dir():
                (section / "name.tex").write_text(
                    "Synthetic Triple Exchange", encoding="utf-8"
                )
                page = 2 if section.name == "chinese" else 1
                (section / "legend.tex").write_text(
                    rf"Triple each integer. \includegraphics[page={page},width=2cm]{{square.pdf}}",
                    encoding="utf-8",
                )
        files = workspace / "attachments"
        files.mkdir(exist_ok=True)
        (files / "example.txt").write_text("synthetic download\n", encoding="utf-8")
        runtime.git_service.commit(
            workspace, "Synthetic import fixture", self.user, "fixture@example.invalid"
        )
        runtime.git_service.push(workspace, "main")
        packages = runtime.problem_package_service
        native = packages.ensure_native_package(
            packages.published_revision(problem_id),
            verification_builder(
                problem_id, mode="interactive", answer_bytes=b"synthetic judge log\n"
            ),
        )
        _, path, _ = runtime.export_service.create_export(
            self.problem, "ccpcoj", native_package_id=native["id"]
        )
        with zipfile.ZipFile(path) as archive:
            (record,) = json.loads(archive.read("problemlist.json"))
            doc = json.loads(record["sample_input"])
            self.assertEqual(record["sample_output"], record["sample_input"])
            self.assertEqual((doc["layout"], doc["view"]), ("interaction", "time"))
            self.assertEqual(
                doc["data"][0]["turns"],
                [
                    {
                        "role": "in" if event["source"] == "interactor" else "out",
                        "text": event["content"],
                    }
                    for event in events
                ],
            )
            self.assertEqual(
                archive.read("TEST_00001/0001.out"), b"synthetic judge log\n"
            )
            images = set()
            for locale in record["problem_locales"]:
                image = locale["description"].split("/")[-1].split("}")[0]
                png = archive.read("ATTACH_00001/" + image)
                self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
                images.add(png)
            self.assertEqual(len(images), 2)
        batch_slug = f"{self.user}/synthetic-batch"
        batch_workspace = self._seed_workspace(batch_slug, self.user)
        (batch_workspace / "statement-sections/english/name.tex").write_text(
            "Synthetic Batch", encoding="utf-8"
        )
        _, batch_id, _ = publish_problem(batch_workspace, batch_slug, self.user)
        config_path = batch_workspace / "config/build.json"
        build = json.loads(config_path.read_text(encoding="utf-8"))
        build.pop("checker_source", None)
        config_path.write_text(json.dumps(build), encoding="utf-8")
        solution_root = batch_workspace / "solutions"
        (solution_root / "accepted.cpp").write_text(
            "#include <iostream>\nint main() { int x; std::cin >> x; std::cout << 2*x << std::endl; }\n",
            encoding="utf-8",
        )
        (solution_root / "wrong.cpp").write_text(
            "#include <iostream>\nint main() { std::cout << 9 << std::endl; }\n",
            encoding="utf-8",
        )
        (solution_root / "wrong.cpp.desc").write_text(
            "expected: wrong_answer\n", encoding="utf-8"
        )
        runtime.git_service.commit(
            batch_workspace,
            "Synthetic batch jury programs",
            self.user,
            "fixture@example.invalid",
        )
        runtime.git_service.push(batch_workspace, "main")
        batch = packages.ensure_native_package(
            packages.published_revision(batch_id),
            verification_builder(
                batch_id,
                solution_verdicts={"solutions/wrong.cpp": ("wrong_answer", "WA")},
            ),
        )
        runtime.export_service.create_export(
            batch_slug, "ccpcoj", native_package_id=batch["id"]
        )
        owner = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        contest = runtime.contest_service
        contest_id = contest.create_contest_with_owner(
            slug="synthetic-export", title="Synthetic export", owner_user_id=owner["id"]
        )
        pair_slug = f"{self.user}/synthetic-interactive-pair"
        pair_workspace, pair_id, _ = publish_problem(
            self._seed_workspace(pair_slug, self.user), pair_slug, self.user, mode="interactive"
        )
        (pair_workspace / "statement-sections/english/name.tex").write_text("Interactive Pair")
        runtime.git_service.commit(pair_workspace, "Name pair fixture", self.user, "fixture@example.invalid")
        runtime.git_service.push(pair_workspace, "main")
        pair_native = packages.ensure_native_package(
            packages.published_revision(pair_id), verification_builder(pair_id, mode="interactive")
        )
        runtime.export_service.create_export(pair_slug, "ccpcoj", native_package_id=pair_native["id"])
        for idx, pid in (("A", batch_id), ("B", problem_id), ("C", pair_id)):
            contest.add_problem(contest_id, idx, pid, owner["id"])
        bundles = runtime.contest_package_service
        snapshot = bundles.freeze_download(
            contest_id=contest_id,
            contest_slug="synthetic-export",
            package_format="ccpcoj",
        )
        children = {}
        for item in snapshot.items:
            child = runtime.export_service.cached_external_package(
                problem_id=item.problem_id,
                native_package_id=item.native_package_id,
                package_format="ccpcoj",
            )
            assert child is not None
            children[item.problem_id] = child
        # The importer accepts the statement booklet alongside its native files.
        bundle = bundles.build_download(
            snapshot,
            external_packages=children,
            statement_pdfs={"english": assets / "square.pdf"},
        )
        try:
            shutil.copyfile(bundle.path, destination)
        finally:
            bundle.close()


def main(destination: Path) -> None:
    fixture = SyntheticExport()
    fixture.setUp()
    try:
        fixture.build_archive(destination)
    finally:
        fixture.tearDown()
        fixture.doCleanups()


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
