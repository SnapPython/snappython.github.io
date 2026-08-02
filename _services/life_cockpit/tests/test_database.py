from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "cockpit.db")
        self.database.initialize()
        self.user = "student"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_day_round_trip_and_independent_records(self) -> None:
        self.database.update_priority(
            self.user, "2026-08-02", 0, "完成实验", False
        )
        self.database.update_notes(
            self.user, "2026-08-02", "会议结论"
        )
        self.database.update_week(
            self.user, "2026-W31", "验证模型", "完成消融实验", 40
        )
        task, _ = self.database.upsert_task(
            self.user,
            "task-1",
            "2026-08-02",
            "research",
            "整理数据",
            False,
        )
        block, _ = self.database.upsert_block(
            self.user,
            "block-1",
            "2026-08-02",
            "09:00",
            "10:30",
            "research",
            "跑实验",
            False,
        )

        day = self.database.get_day(
            self.user, "2026-08-02", "2026-W31"
        )
        self.assertEqual(day["priorities"][0]["text"], "完成实验")
        self.assertEqual(day["notes"]["text"], "会议结论")
        self.assertEqual(day["week"]["progress"], 40)
        self.assertEqual(day["tasks"], [task])
        self.assertEqual(day["blocks"][0]["id"], block["id"])
        self.assertGreaterEqual(day["revision"], 5)

    def test_old_local_time_blocks_are_migrated(self) -> None:
        state = {
            "days": {
                "2026-08-03": {
                    "priorities": [
                        {"text": "读论文", "done": True},
                    ],
                    "tasks": [],
                    "blocks": [
                        {
                            "id": "old-block",
                            "time": "14:00",
                            "category": "course",
                            "text": "组会",
                            "done": False,
                        }
                    ],
                    "notes": "",
                }
            },
            "weeks": {},
        }
        self.database.import_local_state(self.user, state)
        day = self.database.get_day(
            self.user, "2026-08-03", "2026-W32"
        )
        self.assertEqual(day["blocks"][0]["start"], "14:00")
        self.assertEqual(day["blocks"][0]["end"], "15:00")
        self.assertEqual(day["priorities"][0]["text"], "读论文")

    def test_google_event_updates_without_duplicates(self) -> None:
        changed, _ = self.database.upsert_google_event(
            user=self.user,
            block_id="gcal:event-1",
            day="2026-08-04",
            start="08:00",
            end="09:00",
            category="course",
            title="课程",
            done=False,
            google_event_id="event-1",
            google_etag="v1",
            google_updated_at="2026-08-04T00:00:00Z",
            source="google",
        )
        self.assertTrue(changed)
        changed, _ = self.database.upsert_google_event(
            user=self.user,
            block_id="different-local-id",
            day="2026-08-04",
            start="08:30",
            end="09:30",
            category="course",
            title="课程（调整）",
            done=False,
            google_event_id="event-1",
            google_etag="v2",
            google_updated_at="2026-08-04T00:10:00Z",
            source="google",
        )
        self.assertTrue(changed)
        day = self.database.get_day(
            self.user, "2026-08-04", "2026-W32"
        )
        self.assertEqual(len(day["blocks"]), 1)
        self.assertEqual(day["blocks"][0]["text"], "课程（调整）")

    def test_backup_restore_replaces_existing_planner_data(self) -> None:
        self.database.upsert_task(
            self.user,
            "old-task",
            "2026-08-05",
            "research",
            "旧任务",
            False,
        )
        replacement = {
            "days": {
                "2026-08-06": {
                    "priorities": [],
                    "tasks": [
                        {
                            "id": "new-task",
                            "category": "course",
                            "text": "新任务",
                            "done": False,
                        }
                    ],
                    "blocks": [],
                    "notes": "",
                }
            },
            "weeks": {},
        }

        self.database.replace_local_state(self.user, replacement)

        old_day = self.database.get_day(
            self.user, "2026-08-05", "2026-W32"
        )
        new_day = self.database.get_day(
            self.user, "2026-08-06", "2026-W32"
        )
        self.assertEqual(old_day["tasks"], [])
        self.assertEqual(new_day["tasks"][0]["id"], "new-task")

    def test_google_disconnect_removes_external_blocks(self) -> None:
        self.database.set_google_token(
            self.user, "access", "refresh", 9999999999, "scope"
        )
        self.database.upsert_block(
            self.user,
            "cockpit-block",
            "2026-08-07",
            "09:00",
            "09:30",
            "research",
            "本地时间块",
            False,
        )
        self.database.mark_block_synced(
            self.user,
            "cockpit-block",
            "event-cockpit",
            "etag-1",
            "2026-08-07T00:00:00Z",
        )
        self.database.upsert_google_event(
            user=self.user,
            block_id="gcal:event-google",
            day="2026-08-07",
            start="10:00",
            end="10:30",
            category="course",
            title="Google 时间块",
            done=False,
            google_event_id="event-google",
            google_etag="etag-2",
            google_updated_at="2026-08-07T00:10:00Z",
            source="google",
        )

        self.database.delete_google_token(self.user)

        day = self.database.get_day(
            self.user, "2026-08-07", "2026-W32"
        )
        self.assertEqual(len(day["blocks"]), 1)
        self.assertEqual(day["blocks"][0]["id"], "cockpit-block")
        self.assertIsNone(day["blocks"][0]["googleEventId"])
        self.assertEqual(day["blocks"][0]["syncState"], "pending_upsert")
        self.assertIsNone(self.database.get_google_token(self.user))


if __name__ == "__main__":
    unittest.main()
