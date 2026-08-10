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

    def test_online_toolchain_mismatch_warns_and_groups_profiles(self) -> None:
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
        self.assertEqual(projected["toolchain_warning"], "Online judgehosts report different toolchains.")
        self.assertEqual(len(projected["toolchain_profiles"]), 2)
        self.assertEqual(projected["hosts"][2]["toolchain_profile"], "")
        self.assertNotIn("g++ 9.5.0", repr(projected))

    def test_missing_online_reports_warn_without_false_mismatch(self) -> None:
        raw = {
            "enabled": True,
            "hosts_online": 2,
            "hosts_total": 2,
            "hosts": [_host("one"), {**_host("two"), "toolchains": []}],
            "queue": {},
        }
        projected = project_public_status(raw, [])
        self.assertEqual(projected["toolchain_warning"], "Toolchain reports are incomplete.")


if __name__ == "__main__":
    unittest.main()
