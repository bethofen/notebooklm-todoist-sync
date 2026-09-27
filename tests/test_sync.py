import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from sync_todoist import (
    INTERVALS,
    TodoistClient,
    calculate_next_interval,
    get_bangkok_today,
    load_schedule,
    normalize_url,
    parse_add_command,
    save_schedule,
    sync,
)


class TestSpacedRepetitionLogic(unittest.TestCase):

    @patch("requests.Session")
    def test_get_active_tasks_handles_paginated_results(self, mock_session_cls):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        # Mock page 1 with next_cursor, page 2 without cursor
        mock_resp1 = MagicMock()
        mock_resp1.status_code = 200
        mock_resp1.json.return_value = {
            "results": [{"id": "task-1", "content": "Task One"}],
            "next_cursor": "cursor-abc",
        }

        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.json.return_value = {
            "results": [{"id": "task-2", "content": "Task Two"}],
            "next_cursor": None,
        }

        mock_session.get.side_effect = [mock_resp1, mock_resp2]

        client = TodoistClient("dummy-token")
        client.session = mock_session
        tasks = client.get_active_tasks()

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["id"], "task-1")
        self.assertEqual(tasks[1]["id"], "task-2")

    def test_calculate_next_interval(self):
        # Test predefined intervals [1, 3, 7, 16, 35, 75, 180]
        expected_intervals = [1, 3, 7, 16, 35, 75, 180]
        for rep, expected in enumerate(expected_intervals):
            self.assertEqual(calculate_next_interval(rep, 1), expected)

        # Test escalation beyond predefined list (reps >= 7)
        # reps = 7, current_interval = 180 -> 180 * 2.2 = 396
        self.assertEqual(calculate_next_interval(7, 180), 396)
        # reps = 8, current_interval = 396 -> 396 * 2.2 = 871.2 -> 871
        self.assertEqual(calculate_next_interval(8, 396), 871)

    def test_parse_add_command(self):
        # Plain title and URL
        res = parse_add_command("add: วิชาคณิตศาสตร์ https://notebooklm.google.com/notebook/12345")
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "วิชาคณิตศาสตร์")
        self.assertEqual(res[1], "https://notebooklm.google.com/notebook/12345")

        # Markdown link
        res = parse_add_command("add: [Machine Learning Specialization](https://notebooklm.google.com/notebook/ml-999)")
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "Machine Learning Specialization")
        self.assertEqual(res[1], "https://notebooklm.google.com/notebook/ml-999")

        # URL in description
        res = parse_add_command("add: Cloud Architecture", description="https://notebooklm.google.com/notebook/cloud")
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "Cloud Architecture")
        self.assertEqual(res[1], "https://notebooklm.google.com/notebook/cloud")

        # Fallback title if only URL
        res = parse_add_command("add: https://notebooklm.google.com/notebook/anon")
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "NotebookLM Notebook")
        self.assertEqual(res[1], "https://notebooklm.google.com/notebook/anon")

        # Case insensitive 'ADD:' or 'Add:'
        res = parse_add_command("ADD: System Design https://notebooklm.google.com/notebook/sys")
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "System Design")

        # Non-add task
        self.assertIsNone(parse_add_command("Buy groceries tomorrow"))
        self.assertIsNone(parse_add_command("address: 123 Main St"))

    def test_normalize_url(self):
        self.assertEqual(
            normalize_url("https://notebooklm.google.com/notebook/123/"),
            "https://notebooklm.google.com/notebook/123",
        )
        self.assertEqual(
            normalize_url("https://notebooklm.google.com/notebook/123"),
            "https://notebooklm.google.com/notebook/123",
        )

    def test_bangkok_today(self):
        today = get_bangkok_today()
        self.assertIsInstance(today, date)

    @patch("sync_todoist.TodoistClient")
    @patch("sync_todoist.save_schedule")
    @patch("sync_todoist.load_schedule")
    def test_full_sync_flow(self, mock_load, mock_save, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Scenario 1: User adds a notebook via Todoist
        mock_client.get_active_tasks.return_value = [
            {
                "id": "cmd-101",
                "content": "add: Quantum Physics https://notebooklm.google.com/notebook/qp-01",
                "description": "",
            }
        ]
        mock_client.create_task.return_value = {"id": "review-task-001"}
        mock_load.return_value = []

        sync("fake-token")

        # Should delete the add: command task
        mock_client.delete_task.assert_called_with("cmd-101")
        # Should create the round 1 review task
        mock_client.create_task.assert_called_once()
        args, kwargs = mock_client.create_task.call_args
        self.assertIn("Quantum Physics", kwargs["content"])
        self.assertIn("รอบที่ 1", kwargs["content"])
        self.assertEqual(kwargs["due_string"], "today")

        # Should save schedule with active_task_id = 'review-task-001'
        mock_save.assert_called_once()
        saved_schedule = mock_save.call_args[0][1]
        self.assertEqual(len(saved_schedule), 1)
        item = saved_schedule[0]
        self.assertEqual(item["title"], "Quantum Physics")
        self.assertEqual(item["reps"], 0)
        self.assertEqual(item["active_task_id"], "review-task-001")

    @patch("sync_todoist.TodoistClient")
    @patch("sync_todoist.save_schedule")
    @patch("sync_todoist.load_schedule")
    def test_active_task_not_duplicated_or_advanced(self, mock_load, mock_save, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Item has an active task ID in Todoist
        mock_client.get_active_tasks.return_value = [{"id": "task-existing-1"}]
        mock_load.return_value = [
            {
                "title": "Existing Topic",
                "url": "https://notebooklm.google.com/notebook/ex1",
                "reps": 1,
                "interval": 3,
                "next_review": "2026-09-25",
                "active_task_id": "task-existing-1",
            }
        ]

        sync("fake-token")

        # Must NOT create new task, must NOT modify schedule
        mock_client.create_task.assert_not_called()
        mock_save.assert_not_called()

    @patch("sync_todoist.TodoistClient")
    @patch("sync_todoist.save_schedule")
    @patch("sync_todoist.load_schedule")
    def test_completed_task_escalates_interval(self, mock_load, mock_save, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Active tasks is empty, get_task returns None (404 - completed)
        mock_client.get_active_tasks.return_value = []
        mock_client.get_task.return_value = None

        mock_load.return_value = [
            {
                "title": "Completed Topic",
                "url": "https://notebooklm.google.com/notebook/done1",
                "reps": 0,
                "interval": 1,
                "next_review": "2026-09-27",
                "active_task_id": "completed-task-99",
            }
        ]

        sync("fake-token")

        # Reps should advance to 1, next interval is 3 (INTERVALS[1])
        mock_save.assert_called_once()
        saved = mock_save.call_args[0][1][0]
        self.assertEqual(saved["reps"], 1)
        self.assertEqual(saved["interval"], 3)
        self.assertIsNone(saved["active_task_id"])
        expected_next = (get_bangkok_today() + timedelta(days=3)).strftime("%Y-%m-%d")
        self.assertEqual(saved["next_review"], expected_next)


if __name__ == "__main__":
    unittest.main()

