from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Iterator, Protocol

from app.config import build_config_values
from app.service.importing.archive import ArchivePolicy, ArchiveView
from app.service.problem.runtime_config import (
    ProblemConfigLimits,
    problem_config_limits,
)


class ProblemImporter(Protocol):
    def import_package(
        self,
        workspace: Path,
        package_name: str,
        package: ArchiveView,
        *,
        normalize_test_data_newlines: bool = False,
        text_limit_bytes: int,
        statement_sample_max_bytes: int,
        problem_config_limits: ProblemConfigLimits,
    ) -> dict[str, object]: ...


@contextmanager
def archive_view_from_bytes(
    payload: bytes,
    *,
    max_entries: int = 4096,
    max_expanded_bytes: int = 256 * 1024 * 1024,
    max_metadata_bytes: int = 4 * 1024 * 1024,
) -> Iterator[ArchiveView]:
    """Expose test archive bytes through the production file-backed boundary."""

    with tempfile.TemporaryDirectory(prefix="polygon-archive-test-") as raw_root:
        path = Path(raw_root) / "package.zip"
        path.write_bytes(payload)
        with ArchiveView(
            path,
            ArchivePolicy(
                max_entries=max_entries,
                max_expanded_bytes=max_expanded_bytes,
                max_metadata_bytes=max_metadata_bytes,
            ),
        ) as archive:
            yield archive


def import_problem_package(
    service: ProblemImporter,
    workspace: Path,
    package_name: str,
    payload: bytes,
    normalize_test_data_newlines: bool = False,
    *,
    max_expanded_bytes: int = 256 * 1024 * 1024,
    text_limit_bytes: int = 256 * 1024,
    statement_sample_max_bytes: int = 32 * 1024,
) -> dict[str, object]:
    """Invoke one problem importer through its canonical ArchiveView input."""

    with archive_view_from_bytes(
        payload,
        max_expanded_bytes=max_expanded_bytes,
    ) as archive:
        return service.import_package(
            workspace,
            package_name,
            archive,
            normalize_test_data_newlines=normalize_test_data_newlines,
            text_limit_bytes=text_limit_bytes,
            statement_sample_max_bytes=statement_sample_max_bytes,
            problem_config_limits=problem_config_limits(build_config_values()),
        )
