from __future__ import annotations

import unittest

from app.service.judgehost.public_status import project_public_status


def _host(
    name: str,
    *,
    online: bool = True,
    enabled: bool = True,
    compiler: str = "command=/usr/bin/g++\ng++ 14.2.0",
) -> dict[str, object]:
    return {
        "hostname": name,
        "peer_addr": "203.0.113.10",
        "enabled": enabled,
        "online": online,
        "age_sec": 75,
        "last_seen_at": "2026-08-10T01:02:03+00:00",
        "last_task_id": "secret-task",
        "last_run_id": "secret-run",
        "active_leases": 1 if online else 0,
        "judged_case_count": 12,
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


class PublicJudgehostStatusTests(unittest.TestCase):
    def test_projection_exposes_only_anonymous_host_fields(self) -> None:
        raw = {
            "enabled": True,
            "hosts_online": 1,
            "hosts_total": 1,
            "hosts": [_host("private-hostname")],
            "queue": {"queued": 2, "leased": 1, "completed": 99, "failed": 7},
        }
        projected = project_public_status(
            raw,
            [
                {
                    "language_id": "cpp",
                    "command": "/opt/toolchains/g++",
                    "arguments": ["-O2", "/private/include"],
                }
            ],
        )
        rendered = repr(projected)
        self.assertEqual(projected["summary"], "1 online")
        self.assertEqual(projected["active"], 1)
        self.assertEqual(projected["hosts"][0]["label"], "Judgehost 1")
        self.assertNotIn("private-hostname", rendered)
        self.assertNotIn("203.0.113.10", rendered)
        self.assertNotIn("secret-task", rendered)
        self.assertNotIn("secret-run", rendered)
        self.assertNotIn("/usr/bin/g++", rendered)
        self.assertNotIn("/opt/toolchains", rendered)
        self.assertNotIn("/private/include", rendered)
        self.assertNotIn("completed", projected)
        self.assertNotIn("failed", projected)

    def test_footer_summary_states(self) -> None:
        cases = (
            ({"enabled": False, "hosts_online": 0, "hosts_total": 0}, ("disabled", "muted")),
            ({"enabled": True, "hosts_online": 0, "hosts_total": 2}, ("offline", "danger")),
            ({"enabled": True, "hosts_online": 1, "hosts_total": 2}, ("1/2 online", "warn")),
            ({"enabled": True, "hosts_online": 2, "hosts_total": 2}, ("2 online", "ok")),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                projected = project_public_status({**raw, "hosts": [], "queue": {}}, [])
                self.assertEqual((projected["summary"], projected["tone"]), expected)

    def test_online_toolchain_mismatch_marks_reported_versions(self) -> None:
        raw = {
            "enabled": True,
            "hosts_online": 2,
            "hosts_total": 3,
            "hosts": [
                _host("one", compiler="command=/usr/bin/g++\ng++ 14.2.0"),
                _host("two", compiler="command=/custom/g++\ng++ 13.3.0"),
                _host("offline", online=False, compiler="command=/old/g++\ng++ 9.5.0"),
            ],
            "queue": {},
        }
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
        raw = {
            "enabled": True,
            "hosts_online": 2,
            "hosts_total": 2,
            "hosts": [_host("one"), {**_host("two"), "toolchains": []}],
            "queue": {},
        }
        projected = project_public_status(raw, [])
        self.assertFalse(projected["toolchain_mismatch"])
        self.assertEqual(len(projected["toolchains"]), 1)
        self.assertTrue(projected["toolchains"][0]["agrees"])
        self.assertEqual(projected["toolchains"][0]["versions"][0]["host_count"], 1)

    def test_partial_reports_merge_into_one_agreed_summary(self) -> None:
        hosts = [_host(f"full-{index}") for index in range(2)]
        for host in hosts:
            toolchains = host["toolchains"]
            self.assertIsInstance(toolchains, list)
            toolchains.append(
                {
                    "language_id": "py",
                    "compiler": "command=/usr/bin/python3\nPython 3.9.16",
                    "runner": "",
                }
            )
        hosts.extend(_host(f"cpp-{index}") for index in range(3))
        hosts.extend({**_host(f"missing-{index}"), "toolchains": []} for index in range(3))

        projected = project_public_status(
            {
                "enabled": True,
                "hosts_online": 8,
                "hosts_total": 8,
                "hosts": hosts,
                "queue": {},
            },
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
