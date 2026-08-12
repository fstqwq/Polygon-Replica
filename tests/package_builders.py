import io
import zipfile


def polygon_problem_package(
    *,
    short_name: str = "synthetic-problem",
    title: str = "Synthetic Problem",
    windows_test_data: bool = False,
) -> bytes:
    newline = "\r\n" if windows_test_data else "\n"
    problem_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="{short_name}">
  <names>
    <name language="english" value="{title}"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("problem.xml", problem_xml)
        package.writestr("tests/01", f"1 2{newline}")
        package.writestr("tests/01.a", f"3{newline}")
        package.writestr("statement-sections/english/name.tex", f"{title}\n")
        package.writestr("statement-sections/english/legend.tex", "Synthetic legend.\n")
        package.writestr("statement-sections/english/input.tex", "Synthetic input.\n")
        package.writestr("statement-sections/english/output.tex", "Synthetic output.\n")
    return payload.getvalue()


def polygon_contest_package(problem_count: int = 4) -> bytes:
    problem_rows = []
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for offset in range(problem_count):
            index = chr(ord("A") + offset)
            folder = f"problem-{index.lower()}"
            problem_rows.append(
                f'<problem index="{index}" short-name="{folder}" '
                f'url="https://example.invalid/{folder}"/>'
            )
            problem_payload = polygon_problem_package(
                short_name=folder,
                title=f"Synthetic Problem {index}",
                windows_test_data=True,
            )
            with zipfile.ZipFile(io.BytesIO(problem_payload), "r") as problem_zip:
                for info in problem_zip.infolist():
                    package.writestr(
                        f"problems/{folder}/{info.filename}",
                        problem_zip.read(info),
                    )
        package.writestr(
            "contest.xml",
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<contest>\n"
            "  <names><name language=\"english\" value=\"Synthetic Contest\"/></names>\n"
            "  <problems>\n    "
            + "\n    ".join(problem_rows)
            + "\n  </problems>\n"
            "</contest>\n",
        )
        package.writestr(
            "statements/english/statements.tex",
            "\\contest{Synthetic Contest}{Test City}{August 3, 2026}\n",
        )
        package.writestr("statements/english/olymp.sty", "% synthetic style\n")
    return payload.getvalue()
