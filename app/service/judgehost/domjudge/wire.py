from collections.abc import Iterable

from app.service.judgehost.configuration import JudgehostSettings
from app.service.judgehost.domjudge.codec import (
    config_payload,
    languages_payload,
)
from app.service.judgehost.domjudge.wire_model import (
    DomjudgeConfiguration,
    DomjudgeHost,
    DomjudgeLanguage,
)
from app.service.judgehost.host.model import (
    JudgehostHostRow,
    judgehost_name_sort_key,
)


class DomjudgeWireProjector:
    """Project canonical application values onto the DOMjudge wire shapes."""

    @staticmethod
    def configuration(settings: JudgehostSettings) -> DomjudgeConfiguration:
        return config_payload(settings.values)

    @staticmethod
    def languages() -> list[DomjudgeLanguage]:
        return languages_payload()

    @staticmethod
    def hosts(rows: Iterable[JudgehostHostRow]) -> list[DomjudgeHost]:
        return _hosts_payload(rows)


def _hosts_payload(
    hosts: Iterable[JudgehostHostRow],
) -> list[DomjudgeHost]:
    rows = sorted(
        hosts,
        key=lambda item: judgehost_name_sort_key(item["hostname"]),
    )
    out: list[DomjudgeHost] = []
    for row in rows:
        token = row["hostname"]
        if token:
            out.append(
                {
                    "hostname": token,
                    "enabled": row["enabled"],
                    "polltime": row["last_seen_at"],
                }
            )
    return out
