from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
os.environ["LC_DATABASE_PATH"] = str(Path(TEST_DIR.name) / "api.db")
os.environ["LC_DATA_DIR"] = TEST_DIR.name
os.environ["LC_PROXY_SECRET"] = "test-proxy-secret"
os.environ["LC_ALLOWED_USERS"] = "student"
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GOOGLE_CLIENT_SECRET"] = ""
os.environ["LC_GOOGLE_TOKEN_KEY"] = ""

from fastapi.testclient import TestClient

from app import app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.headers = {
            "Remote-User": "student",
            "X-LC-Proxy-Secret": "test-proxy-secret",
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        TEST_DIR.cleanup()

    def test_proxy_and_sso_headers_are_required(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)
        response = self.client.get(
            "/api/status",
            headers={
                "Remote-User": "student",
                "X-LC-Proxy-Secret": "wrong",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_day_records_sync_through_api(self) -> None:
        priority = self.client.put(
            "/api/days/2026-08-05/priorities/0",
            headers=self.headers,
            json={"text": "写论文", "done": False},
        )
        self.assertEqual(priority.status_code, 200)
        task = self.client.post(
            "/api/days/2026-08-05/tasks",
            headers=self.headers,
            json={
                "id": "task-api",
                "category": "research",
                "text": "整理参考文献",
                "done": False,
            },
        )
        self.assertEqual(task.status_code, 200)
        block = self.client.post(
            "/api/days/2026-08-05/blocks",
            headers=self.headers,
            json={
                "id": "block-api",
                "start": "09:00",
                "end": "09:30",
                "category": "research",
                "text": "阅读论文",
                "done": False,
            },
        )
        self.assertEqual(block.status_code, 200)

        day = self.client.get(
            "/api/days/2026-08-05", headers=self.headers
        )
        self.assertEqual(day.status_code, 200)
        payload = day.json()
        self.assertEqual(payload["priorities"][0]["text"], "写论文")
        self.assertEqual(payload["tasks"][0]["id"], "task-api")
        self.assertEqual(payload["blocks"][0]["start"], "09:00")
        self.assertFalse(payload["google"]["configured"])

    def test_invalid_block_range_is_rejected(self) -> None:
        response = self.client.post(
            "/api/days/2026-08-05/blocks",
            headers=self.headers,
            json={
                "start": "10:00",
                "end": "09:30",
                "category": "life",
                "text": "无效时间",
                "done": False,
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_backup_restore_replaces_server_state(self) -> None:
        response = self.client.post(
            "/api/replace-state",
            headers=self.headers,
            json={
                "state": {
                    "days": {
                        "2026-08-08": {
                            "priorities": [],
                            "tasks": [
                                {
                                    "id": "restored-task",
                                    "category": "life",
                                    "text": "恢复后的任务",
                                    "done": False,
                                }
                            ],
                            "blocks": [],
                            "notes": "",
                        }
                    },
                    "weeks": {},
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        previous = self.client.get(
            "/api/days/2026-08-05", headers=self.headers
        ).json()
        restored = self.client.get(
            "/api/days/2026-08-08", headers=self.headers
        ).json()
        self.assertEqual(previous["tasks"], [])
        self.assertEqual(restored["tasks"][0]["id"], "restored-task")


if __name__ == "__main__":
    unittest.main()
