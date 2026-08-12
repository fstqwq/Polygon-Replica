from pathlib import Path


def maintained_testlib_header(*, repo_root: Path) -> Path:
    header = (Path(repo_root).resolve() / "third_party" / "upstream" / "testlib" / "testlib.h").resolve()
    if (not header.exists()) or (not header.is_file()) or header.is_symlink():
        raise RuntimeError(f"missing maintained testlib header: {header}")
    return header


def workspace_testlib_header(workspace: Path) -> Path | None:
    header = (Path(workspace).resolve() / "third_party" / "testlib" / "testlib.h").resolve()
    if (not header.exists()) or (not header.is_file()) or header.is_symlink():
        return None
    return header
