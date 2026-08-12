import re


def problem_slug_file_token(problem_slug: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", problem_slug.strip()).strip("-")
    return token or "problem"


def problem_source_folder(
    entry: dict[str, object],
    source_folder_map: dict[int, str],
) -> str:
    problem_id = int(entry["problem_id"])
    mapped = source_folder_map.get(problem_id, "").strip()
    if mapped:
        return mapped
    idx_token = re.sub(
        r"[^A-Za-z0-9._-]+",
        "-",
        str(entry.get("idx") or entry.get("label") or "").strip().lower(),
    ).strip("-")
    slug_token = problem_slug_file_token(str(entry["problem_slug"]))
    return f"{idx_token}-{slug_token}" if idx_token else slug_token
