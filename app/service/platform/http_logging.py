"""HTTP logging policy for high-frequency service endpoints."""

import logging


_JUDGEHOST_FETCH_WORK_PATH = "/api/v4/judgehosts/fetch-work"


class UvicornAccessFilter(logging.Filter):
    """Suppress successful idle-poll access records while retaining failures."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client_addr, method, path, _http_version, status_code = args
        return not (
            method == "POST"
            and path == _JUDGEHOST_FETCH_WORK_PATH
            and status_code == 200
        )


def install_uvicorn_access_filter() -> None:
    """Install the process-wide access filter exactly once."""

    access_logger = logging.getLogger("uvicorn.access")
    if any(isinstance(item, UvicornAccessFilter) for item in access_logger.filters):
        return
    access_logger.addFilter(UvicornAccessFilter())
