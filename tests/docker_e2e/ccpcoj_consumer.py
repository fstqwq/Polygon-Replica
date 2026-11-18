"""Run inside the pinned upstream CCPCOJ Web image, against its own MySQL schema."""

import json
import os
from html.parser import HTMLParser
import tempfile
import zipfile
from pathlib import Path

import pymysql
from backtask.task_queue.problem_import_executor import execute_problem_import


class StatementImages(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "img":
            self.sources.append(dict(attrs)["src"] or "")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory, pymysql.connect(
        host=os.environ["DB_HOSTNAME"],
        user=os.environ["DB_USERNAME"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_DATABASE"],
        charset="utf8mb4",
    ) as connection:
        root = Path(directory)
        tests, attachments, extracted = (
            root / name for name in ("tests", "attachments", "package")
        )
        tests.mkdir()
        attachments.mkdir()
        with zipfile.ZipFile("/fixture/problem.zip") as archive:
            archive.extractall(extracted)
        records = json.loads((extracted / "problemlist.json").read_bytes())
        assert [row["problem_new_id"] for row in records] == [1, 2, 3]
        execute_problem_import(
            str(extracted),
            testdata_dir=str(tests),
            public_attach_root=str(attachments),
            oj_status="cpc",
            now_course_id=None,
            external_conn=connection,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT problem_id,sample_input,sample_output FROM problem WHERE title=%s",
                ("Synthetic Batch",),
            )
            ((batch_id, batch_input, batch_output),) = cursor.fetchall()
            assert json.loads(batch_input)["data"] == ["display input\n"]
            assert json.loads(batch_output)["data"] == ["display output\n"]
            cursor.execute(
                "SELECT spj,sample_input,sample_output FROM problem WHERE title=%s", ("Interactive Pair",)
            )
            ((pair_mode, pair_input, pair_output),) = cursor.fetchall()
            assert int(pair_mode) == 2
            assert json.loads(pair_input) == {"data_type": "json", "data": ["display input\n"]}
            assert json.loads(pair_output) == {"data_type": "json", "data": ["display output\n"]}
            cursor.execute(
                "SELECT problem_id,spj,time_limit,memory_limit,attach,sample_input,sample_output "
                "FROM problem WHERE title=%s",
                ("Synthetic Triple Exchange",),
            )
            (row,) = cursor.fetchall()
            pid, mode, seconds, memory, attach, sample_in, sample_out = row
            assert (int(mode), float(seconds), int(memory)) == (2, 1.375, 256)
            assert sample_in == sample_out
            doc = json.loads(sample_in)
            assert (doc["layout"], doc["view"]) == ("interaction", "time")
            assert doc["data"] == [
                {
                    "turns": [
                        {"role": "in", "text": "1\n"},
                        {"role": "out", "text": "3\n"},
                        {"role": "in", "text": "2\n"},
                        {"role": "out", "text": "6\n"},
                    ]
                }
            ]
            cursor.execute(
                "SELECT locale_key,locale_label FROM problem_locale WHERE problem_id=%s", (pid,)
            )
            assert dict(cursor.fetchall()) == {"chinese": "中文", "english": "English"}
            cursor.execute(
                "SELECT description FROM problem_locale_html WHERE problem_id=%s",
                (pid,),
            )
            for (html,) in cursor:
                images = StatementImages()
                images.feed(html)
                (image,) = images.sources
                assert image.startswith(
                    f"/upload/problem_attach/{attach}/"
                ) and image.endswith(".png")
                assert (attachments / attach / Path(image).name).is_file()
        with zipfile.ZipFile("/fixture/problem.zip") as archive:
            for name in ("0001.in", "0001.out", "tpj.cc"):
                assert (tests / str(pid) / name).read_bytes() == archive.read(
                    "TEST_00002/" + name
                )
            for name in ("0001.in", "0001.out", "tpj.cc"):
                assert (tests / str(batch_id) / name).read_bytes() == archive.read(
                    "TEST_00001/" + name
                )
        for image in ("1-square.png", "2-square.png"):
            assert (
                (attachments / attach / image)
                .read_bytes()
                .startswith(b"\x89PNG\r\n\x1a\n")
            )
        with zipfile.ZipFile(attachments / attach / "player-files.zip") as download:
            assert download.read("example.txt") == b"synthetic download\n"


if __name__ == "__main__":
    main()
