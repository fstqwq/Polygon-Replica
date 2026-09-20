import unittest

from app.service.judgehost.host.public_status import project_public_status
from app.service.judgehost.host.model import JudgehostStatus, JudgehostStatusRow


def _host(
    name: str,
    *,
    online: bool = True,
    enabled: bool = True,
    compiler: str = "command=/usr/bin/g++\ng++ 14.2.0",
) -> JudgehostStatusRow:
    return {
        "hostname": name,
        "peer_addr": "203.0.113.10",
        "enabled": enabled,
        "online": online,
        "age_sec": 75,
        "last_seen_at": "2026-08-10T01:02:03+00:00",
        "first_seen_at": "2026-08-10T01:02:03+00:00",
        "active_leases": 1 if online else 0,
        "judged_case_count": 12,
        "last_judging_at": None,
        "last_judging": None,
        "recent_avg_per_case_sec": 0.125,
        "toolchains": [
            {
                "language_id": "cpp",
                "compiler": compiler,
                "runner": "",
                "observed_at": "2026-08-10T01:02:03+00:00",
                "judgetask_id": 42,
            }
        ],
    }


def _status(hosts: list[JudgehostStatusRow], *, queued: int = 0) -> JudgehostStatus:
    return {
        "enabled": True,
        "auth_configured": True,
        "hosts_online": sum(host["online"] and host["enabled"] for host in hosts),
        "hosts_total": len(hosts),
        "hosts": hosts,
        "queue": {"queued": queued, "leased": 0, "completed": 0, "failed": 0},
    }


class PublicJudgehostStatusTests(unittest.TestCase):
    def test_projection_exposes_only_anonymous_host_fields(self) -> None:
        raw = _status([_host("private-hostname")], queued=2)
        raw["queue"] = {"queued": 2, "leased": 1, "completed": 99, "failed": 7}
        projected = project_public_status(
            raw,
            [
                {
                    "language_id": "c",
                    "command": "/usr/bin/gcc",
                    "arguments": ["-std=gnu11"],
                },
                {
                    "language_id": "cpp",
                    "command": "/opt/toolchains/g++",
                    "arguments": ["-O2", "/private/include"],
                }
            ],
        )
        rendered = repr(projected)
        self.assertEqual(projected["summary"], "1 online (1 busy)")
        self.assertEqual(projected["busy_hosts"], 1)
        self.assertEqual(projected["hosts"][0]["label"], "Judgehost 1")
        self.assertEqual(projected["hosts"][0]["activity"], "busy")
        self.assertNotIn("private-hostname", rendered)
        self.assertNotIn("203.0.113.10", rendered)
        self.assertNotIn("/usr/bin/g++", rendered)
        self.assertNotIn("/opt/toolchains", rendered)
        self.assertNotIn("/private/include", rendered)
        self.assertNotIn("completed", projected)
        self.assertNotIn("failed", projected)
        self.assertEqual(
            [spec["language_id"] for spec in projected["compile_specs"]],
            ["cpp"],
        )

    def test_footer_summary_states(self) -> None:
        cases = (
            (False, 0, 0, ("disabled", "muted")),
            (True, 0, 2, ("offline", "danger")),
            (True, 1, 2, ("1/2 online (0 busy)", "warn")),
            (True, 2, 2, ("2 online (0 busy)", "ok")),
        )
        for enabled, online, total, expected in cases:
            with self.subTest(enabled=enabled, online=online, total=total):
                raw = _status([])
                raw["enabled"] = enabled
                raw["hosts_online"] = online
                raw["hosts_total"] = total
                projected = project_public_status(raw, [])
                self.assertEqual((projected["summary"], projected["tone"]), expected)

    def test_disabled_host_uses_public_offline_idle_vocabulary(self) -> None:
        projected = project_public_status(
            _status([_host("disabled", enabled=False)]),
            [],
        )

        self.assertEqual(projected["hosts"][0]["state"], "offline")
        self.assertEqual(projected["hosts"][0]["activity"], "idle")
        self.assertEqual(projected["busy_hosts"], 0)

    def test_online_toolchain_mismatch_marks_reported_versions(self) -> None:
        raw = _status(
            [
                _host("one", compiler="command=/usr/bin/g++\ng++ 14.2.0"),
                _host("two", compiler="command=/custom/g++\ng++ 13.3.0"),
                _host("offline", online=False, compiler="command=/old/g++\ng++ 9.5.0"),
            ],
        )
        projected = project_public_status(raw, [])
        self.assertTrue(projected["toolchain_mismatch"])
        self.assertEqual(len(projected["toolchains"]), 1)
        toolchain = projected["toolchains"][0]
        self.assertEqual(toolchain["language_label"], "C++")
        self.assertFalse(toolchain["agrees"])
        self.assertEqual(len(toolchain["versions"]), 2)
        self.assertNotIn("toolchain_profile", projected["hosts"][0])
        self.assertNotIn("g++ 9.5.0", repr(projected))

    def test_missing_online_reports_do_not_create_mismatch(self) -> None:
        missing = _host("two")
        missing["toolchains"] = []
        raw = _status([_host("one"), missing])
        projected = project_public_status(raw, [])
        self.assertFalse(projected["toolchain_mismatch"])
        self.assertEqual(len(projected["toolchains"]), 1)
        self.assertTrue(projected["toolchains"][0]["agrees"])
        self.assertEqual(projected["toolchains"][0]["versions"][0]["host_count"], 1)

    def test_partial_reports_merge_into_one_agreed_summary(self) -> None:
        hosts = [_host(f"full-{index}") for index in range(2)]
        for host in hosts:
            toolchains = host["toolchains"]
            toolchains.append(
                {
                    "language_id": "py",
                    "compiler": "command=/usr/bin/python3\nPython 3.9.16",
                    "runner": "",
                    "observed_at": "2026-08-10T01:02:03+00:00",
                    "judgetask_id": 42,
                }
            )
        hosts.extend(_host(f"cpp-{index}") for index in range(3))
        for index in range(3):
            missing = _host(f"missing-{index}")
            missing["toolchains"] = []
            hosts.append(missing)

        projected = project_public_status(
            _status(hosts),
            [],
        )

        self.assertFalse(projected["toolchain_mismatch"])
        self.assertEqual(
            [toolchain["language_label"] for toolchain in projected["toolchains"]],
            ["C++", "Python"],
        )
        self.assertEqual(
            [toolchain["versions"][0]["host_count"] for toolchain in projected["toolchains"]],
            [5, 2],
        )
        self.assertTrue(all(toolchain["agrees"] for toolchain in projected["toolchains"]))


if __name__ == "__main__":
    unittest.main()
