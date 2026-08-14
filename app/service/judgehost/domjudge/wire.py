from collections.abc import Iterable

from app.service.judgehost.configuration import JudgehostSettings
from app.service.judgehost.domjudge.codec import (
    config_payload,
    decode_text,
    languages_payload,
)
from app.service.judgehost.host.model import JudgehostHostRow


class DomjudgeWireProjector:
    """Project canonical application values onto the DOMjudge wire shapes."""

    @staticmethod
    def configuration(settings: JudgehostSettings) -> dict[str, object]:
        return config_payload(settings.values)

    @staticmethod
    def languages() -> list[dict[str, object]]:
        return languages_payload()

    @staticmethod
    def hosts(rows: Iterable[JudgehostHostRow]) -> list[dict[str, object]]:
        return _hosts_payload(rows)


def _hosts_payload(
    hosts: Iterable[JudgehostHostRow],
) -> list[dict[str, object]]:
    rows = sorted(
        (dict(row) for row in hosts),
        key=lambda item: (
            decode_text(raw=item.get("last_seen_at")),
            decode_text(raw=item.get("hostname")),
        ),
        reverse=True,
    )
    out: list[dict[str, object]] = []
    for row in rows:
        token = decode_text(raw=row.get("hostname"))
        if token:
            out.append(
                {
                    "hostname": token,
                    "enabled": bool(row.get("enabled", True)),
                    "polltime": decode_text(raw=row.get("last_seen_at")),
                }
            )
    return out
