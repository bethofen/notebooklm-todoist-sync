#!/usr/bin/env python3
"""
Spaced Repetition Scheduler for Google NotebookLM & Todoist
------------------------------------------------------------
Runs via GitHub Actions to maintain spaced repetition intervals for NotebookLM
flashcards/notes using Todoist as the task interface and schedule.json as state.
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# --- Configuration & Constants ---
TODOIST_API_BASE = "https://api.todoist.com/api/v1"
SCHEDULE_FILE = Path(__file__).resolve().parent / "schedule.json"
BANGKOK_TZ = timezone(timedelta(hours=7))

# Spaced Repetition Escalation (in days)
INTERVALS = [1, 3, 7, 16, 35, 75, 180]
INTERVAL_MULTIPLIER = 2.2

TASK_DESCRIPTION_TEMPLATE = "เปิด NotebookLM ทบทวน Flashcard เสร็จแล้วกดติ๊กถูกเพื่อเริ่มนับรอบต่อไป"

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("notebooklm_sync")


class TodoistAPIError(Exception):
    """Custom exception for Todoist API errors."""
    pass


class TodoistClient:
    """Lightweight client for Todoist Unified API v1."""

    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token.strip()}",
            "Content-Type": "application/json",
        })
        retries = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "DELETE"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get_active_tasks(self) -> List[Dict[str, Any]]:
        """Fetch all currently active (uncompleted) tasks."""
        try:
            resp = self.session.get(f"{TODOIST_API_BASE}/tasks", timeout=15)
            if resp.status_code == 401:
                raise TodoistAPIError("Authentication failed: Invalid TODOIST_API_TOKEN.")
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch active tasks: {e}")
            raise TodoistAPIError(f"Error fetching active tasks: {e}") from e

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch a specific task by ID.
        Returns task dict if active (200), or None if completed/deleted (404).
        """
        try:
            resp = self.session.get(f"{TODOIST_API_BASE}/tasks/{task_id}", timeout=15)
            if resp.status_code == 404:
                return None
            if resp.status_code == 401:
                raise TodoistAPIError("Authentication failed: Invalid TODOIST_API_TOKEN.")
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch task {task_id}: {e}")
            raise TodoistAPIError(f"Error fetching task {task_id}: {e}") from e

    def create_task(self, content: str, description: str, due_string: str = "today") -> Dict[str, Any]:
        """Create a new task in Todoist."""
        payload = {
            "content": content,
            "description": description,
            "due_string": due_string,
        }
        try:
            resp = self.session.post(f"{TODOIST_API_BASE}/tasks", json=payload, timeout=15)
            if resp.status_code == 401:
                raise TodoistAPIError("Authentication failed: Invalid TODOIST_API_TOKEN.")
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"Failed to create task '{content}': {e}")
            raise TodoistAPIError(f"Error creating task: {e}") from e

    def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID. Returns True if successfully deleted or already absent."""
        try:
            resp = self.session.delete(f"{TODOIST_API_BASE}/tasks/{task_id}", timeout=15)
            if resp.status_code in (204, 404):
                return True
            if resp.status_code == 401:
                raise TodoistAPIError("Authentication failed: Invalid TODOIST_API_TOKEN.")
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to delete task {task_id}: {e}")
            return False


def get_bangkok_today() -> date:
    """Return the current date in Asia/Bangkok timezone (UTC+7)."""
    return datetime.now(BANGKOK_TZ).date()


def calculate_next_interval(reps: int, current_interval: int) -> int:
    """
    Calculate interval based on reps count:
    - Predefined list: [1, 3, 7, 16, 35, 75, 180]
    - If reps exceeds predefined list, multiply previous interval by 2.2x
    """
    if reps < len(INTERVALS):
        return INTERVALS[reps]
    return max(1, round(current_interval * INTERVAL_MULTIPLIER))


def parse_add_command(content: str, description: str = "") -> Optional[Tuple[str, str]]:
    """
    Parse an 'add: <title> <url>' command from Todoist task content and description.
    Supports:
      - 'add: <title> <url>'
      - 'add: [<title>](<url>)' (Markdown link)
      - 'add: <title>' with URL provided in task description
      - 'add: <url>' (fallback default title)
    Returns:
      Tuple[title, url] if valid command, or None otherwise.
    """
    if not content:
        return None

    # Check for prefix 'add:' (case-insensitive)
    match = re.match(r"^\s*add\s*:\s*(.*)$", content, re.IGNORECASE | re.DOTALL)
    if not match:
        return None

    rest = match.group(1).strip()
    title = ""
    url = ""

    # Case 1: Markdown link in content: [Title](URL)
    md_match = re.search(r"\[([^\]]+)\]\s*\((https?://[^\s)]+)\)", rest)
    if md_match:
        title = md_match.group(1).strip()
        url = md_match.group(2).strip()
        return title, url

    # Case 2: Raw URL in content
    url_match = re.search(r"(https?://\S+)", rest)
    if url_match:
        url = url_match.group(1).strip().rstrip(")>]")
        title = rest.replace(url_match.group(1), "").strip()
        title = re.sub(r"^[\s\"'\[(]+|[\s\"'\])]+$", "", title).strip()
        if not title:
            title = "NotebookLM Notebook"
        return title, url

    # Case 3: URL placed in description
    if description:
        desc_url_match = re.search(r"(https?://\S+)", description)
        if desc_url_match:
            url = desc_url_match.group(1).strip().rstrip(")>]")
            title = re.sub(r"^[\s\"'\[(]+|[\s\"'\])]+$", "", rest).strip()
            if not title:
                title = "NotebookLM Notebook"
            return title, url

    logger.warning(f"Found 'add:' task without valid URL: content='{content}', desc='{description}'")
    return None


def normalize_url(url: str) -> str:
    """Normalize URL by stripping trailing slash for exact deduplication."""
    return url.strip().rstrip("/")


def load_schedule(filepath: Path) -> List[Dict[str, Any]]:
    """Load schedule.json, returning an empty list if file doesn't exist."""
    if not filepath.exists():
        logger.info(f"{filepath} not found, initializing empty schedule.")
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                logger.error(f"Invalid format in {filepath}: expected list, got {type(data).__name__}")
                return []
            return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Error loading {filepath}: {e}")
        return []


def save_schedule(filepath: Path, schedule: List[Dict[str, Any]], dry_run: bool = False) -> None:
    """Atomically save schedule.json to avoid corruption and race conditions."""
    if dry_run:
        logger.info("[Dry Run] Would save schedule.json:")
        logger.info(json.dumps(schedule, indent=2, ensure_ascii=False))
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = filepath.parent
    try:
        with tempfile.NamedTemporaryFile("w", dir=temp_dir, delete=False, encoding="utf-8") as tf:
            json.dump(schedule, tf, indent=2, ensure_ascii=False)
            tf.write("\n")
            temp_path = Path(tf.name)
        temp_path.replace(filepath)
        logger.info(f"Successfully saved updated schedule to {filepath}")
    except OSError as e:
        logger.error(f"Failed to atomically write schedule to {filepath}: {e}")
        raise


def sync(api_token: str, dry_run: bool = False) -> None:
    """Main synchronization and spaced repetition logic."""
    client = TodoistClient(api_token)
    today = get_bangkok_today()
    today_str = today.strftime("%Y-%m-%d")
    logger.info(f"Starting sync. Current Bangkok date (UTC+7): {today_str}")

    schedule = load_schedule(SCHEDULE_FILE)
    schedule_changed = False

    # Track normalized URLs for deduplication
    existing_urls = {normalize_url(item["url"]): item for item in schedule if "url" in item}

    # Step 1: Fetch active tasks from Todoist
    logger.info("Fetching active tasks from Todoist...")
    active_tasks = client.get_active_tasks()
    active_task_ids = {str(task["id"]) for task in active_tasks}
    logger.info(f"Found {len(active_tasks)} active tasks in Todoist.")

    # Step 2: Process "add:" tasks
    for task in active_tasks:
        task_id = str(task["id"])
        content = task.get("content", "")
        description = task.get("description", "")

        parsed = parse_add_command(content, description)
        if not parsed:
            continue

        title, url = parsed
        norm_url = normalize_url(url)

        if norm_url in existing_urls:
            logger.info(f"Notebook with URL '{url}' is already in schedule. Deleting duplicate 'add:' task.")
            if not dry_run:
                client.delete_task(task_id)
            continue

        new_entry: Dict[str, Any] = {
            "title": title,
            "url": url,
            "reps": 0,
            "interval": 1,
            "next_review": today_str,
            "active_task_id": None,
            "last_reviewed": None,
            "created_at": today_str,
        }
        schedule.append(new_entry)
        existing_urls[norm_url] = new_entry
        schedule_changed = True
        logger.info(f"Added new notebook: '{title}' -> {url}")

        if not dry_run:
            logger.info(f"Cleaning up command task ID {task_id} from Todoist...")
            client.delete_task(task_id)

    # Step 3: Check completion status of existing items with active tasks
    for item in schedule:
        active_id = item.get("active_task_id")
        if not active_id:
            continue

        active_id_str = str(active_id)

        # Rule: If task is still in active tasks list, user hasn't completed it yet.
        # DO NOT advance date, DO NOT create duplicates!
        if active_id_str in active_task_ids:
            logger.info(f"Item '{item['title']}' (Task ID: {active_id_str}) is still active. Waiting for completion.")
            continue

        # If not found in active tasks, confirm via single GET request (handles edge cases like pagination)
        task_detail = client.get_task(active_id_str)
        if task_detail is not None:
            logger.info(f"Item '{item['title']}' (Task ID: {active_id_str}) verified active. Waiting for completion.")
            continue

        # Task returned 404 -> User checked off/completed the task!
        logger.info(f"Task ID {active_id_str} for '{item['title']}' was COMPLETED!")
        item["reps"] += 1
        new_interval = calculate_next_interval(item["reps"], item.get("interval", 1))
        item["interval"] = new_interval
        item["last_reviewed"] = today_str

        # Calculate next review date starting from today (actual completion date)
        next_date = today + timedelta(days=new_interval)
        item["next_review"] = next_date.strftime("%Y-%m-%d")
        item["active_task_id"] = None
        schedule_changed = True

        logger.info(
            f"Escalated '{item['title']}': reps={item['reps']}, "
            f"interval={new_interval}d, next_review={item['next_review']}"
        )

    # Step 4: Create review tasks for due items (no active task and next_review <= today)
    for item in schedule:
        if item.get("active_task_id") is not None:
            continue

        next_review_str = item.get("next_review")
        if not next_review_str:
            continue

        try:
            next_review_date = datetime.strptime(next_review_str, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"Invalid date format for '{item['title']}': {next_review_str}")
            continue

        if next_review_date <= today:
            rep_display = item["reps"] + 1
            content = f"📚 ทบทวน: [{item['title']}]({item['url']}) (รอบที่ {rep_display})"
            description = TASK_DESCRIPTION_TEMPLATE

            logger.info(f"Item '{item['title']}' is due (Round {rep_display}). Creating Todoist task...")
            if dry_run:
                logger.info(f"[Dry Run] Would create task: {content}")
                item["active_task_id"] = "dry-run-task-id"
                schedule_changed = True
            else:
                new_task = client.create_task(content=content, description=description, due_string="today")
                item["active_task_id"] = str(new_task["id"])
                schedule_changed = True
                logger.info(f"Created task ID {item['active_task_id']} for '{item['title']}'")

    # Step 5: Save schedule.json if changed
    if schedule_changed:
        save_schedule(SCHEDULE_FILE, schedule, dry_run=dry_run)
    else:
        logger.info("No schedule changes in this run.")


def main():
    parser = argparse.ArgumentParser(description="NotebookLM & Todoist Spaced Repetition Sync")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution without modifying Todoist or state")
    args = parser.parse_args()

    api_token = os.environ.get("TODOIST_API_TOKEN")
    if not api_token and not args.dry_run:
        logger.error("Environment variable 'TODOIST_API_TOKEN' is required but not set.")
        sys.exit(1)

    try:
        sync(api_token or "dry-run-token", dry_run=args.dry_run)
    except TodoistAPIError as e:
        logger.error(f"Todoist API Error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unexpected error during sync: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
