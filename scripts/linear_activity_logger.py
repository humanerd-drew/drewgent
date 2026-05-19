"""
Linear Activity Logger — Drewgent Kanban Integration

Monitors Discord sessions for work patterns, creates kanban cards in
drewgent_tasks.db, and optionally creates Linear issues.

Run as cron job every 5 minutes. State persists across runs to avoid
duplicate card creation (tracked via session/message IDs).

Usage:
    python3 linear_activity_logger.py

Environment:
    LINEAR_API_KEY    — Linear API key (optional; kanban card created even if missing)
    DREWGENT_HOME     — Override Drewgent home path
"""

import tempfile

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add source to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from drewgent_constants import get_drewgent_home

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# =============================================================================
# Paths
# =============================================================================

DREW_GENT_HOME = get_drewgent_home()
SESSIONS_DIR = DREW_GENT_HOME / "P2-hippocampus" / "sessions"
STATE_FILE = DREW_GENT_HOME / "state" / "linear_activity_logger.json"

# =============================================================================
# Work pattern detection (Korean)
# =============================================================================

WORK_PATTERNS = [
    re.compile(r"진행해\s*줘", re.IGNORECASE),
    re.compile(r"해\s*줘", re.IGNORECASE),
    re.compile(r"실행해\s*줘", re.IGNORECASE),
    re.compile(r"끝났어", re.IGNORECASE),
    re.compile(r"완료\s*했어", re.IGNORECASE),
    re.compile(r"끝내자", re.IGNORECASE),
    re.compile(r"시작해\s*줘", re.IGNORECASE),
    re.compile(r"해\s*봅시다", re.IGNORECASE),
    re.compile(r"elm\s*do\s*it", re.IGNORECASE),
]

SKIP_PATTERNS = [
    re.compile(r"^확인해\s*봐", re.IGNORECASE),
    re.compile(r"^질문\s*확인", re.IGNORECASE),
    re.compile(r"^이거\s*확인", re.IGNORECASE),
]


def detect_work_pattern(content: str) -> bool:
    """Return True if content contains a work trigger pattern."""
    if not content or len(content.strip()) < 4:
        return False
    # Skip confirmation/question patterns first
    for skip in SKIP_PATTERNS:
        if skip.match(content.strip()):
            return False
    for pattern in WORK_PATTERNS:
        if pattern.search(content):
            return True
    return False


# =============================================================================
# State management
# =============================================================================

def load_state() -> dict:
    """Load processed message IDs state."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "processed_messages": [],  # list of "session_id:message_index"
        "last_run_at": None,
        "created_cards": 0,
    }


def save_state(state: dict) -> None:
    """Persist state atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = None, None
    f = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(STATE_FILE.parent), suffix=".tmp", prefix=".activity_logger_"
        )
        f = os.fdopen(fd, "w", encoding="utf-8")
        fd = None  # fd is now owned by f
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        f = None
        os.replace(tmp, STATE_FILE)
    except BaseException:
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        elif fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


import tempfile


def mark_processed(state: dict, session_id: str, message_idx: int) -> None:
    """Mark a message as processed (avoid duplicate)."""
    key = f"{session_id}:{message_idx}"
    if key not in state["processed_messages"]:
        state["processed_messages"].append(key)
        # Keep only last 5000 to avoid unbounded growth
        if len(state["processed_messages"]) > 5000:
            state["processed_messages"] = state["processed_messages"][-5000:]


def was_processed(state: dict, session_id: str, message_idx: int) -> bool:
    """Return True if this message was already processed."""
    return f"{session_id}:{message_idx}" in state["processed_messages"]


# =============================================================================
# Session reading
# =============================================================================

def get_recent_sessions(hours: int = 1) -> list[Path]:
    """Return .json session files modified within the last `hours`."""
    cutoff = datetime.now() - timedelta(hours=hours)
    sessions = []
    if SESSIONS_DIR.exists():
        for p in SESSIONS_DIR.iterdir():
            if p.suffix in (".json", ".jsonl") and p.stat().st_mtime > cutoff.timestamp():
                sessions.append(p)
    return sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)


def load_session_messages(session_path: Path) -> list[dict]:
    """Load messages from a session file. Returns list of {role, content, timestamp}."""
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to load session %s: %s", session_path.name, e)
        return []

    # Handle both .json (structured) and .jsonl (line-by-line) formats
    messages = []
    if isinstance(data, dict) and "messages" in data:
        # .json format: {"messages": [...], "session_id": "..."}
        messages = data.get("messages", [])
    elif isinstance(data, list):
        # .jsonl format: list of message dicts
        messages = data
    return messages


# =============================================================================
# Content extraction
# =============================================================================

def extract_content(message: dict) -> str:
    """Extract text content from a message dict (handles [humanerd] prefix)."""
    content = message.get("content", "")
    if not content:
        return ""
    # Strip [humanerd] prefix if present
    content = re.sub(r"^\[humanerd\]\s*", "", content.strip())
    return content


def build_title(content: str, messages: list[dict], idx: int) -> str:
    """Build a title from the message content. Use previous assistant message as context."""
    title = content[:80].strip()
    if len(content) > 80:
        title += "..."
    # If previous message was from assistant, prepend context
    if idx > 0:
        prev = messages[idx - 1]
        if prev.get("role") == "assistant":
            prev_content = extract_content(prev)
            if prev_content and len(prev_content) > 10:
                # Truncate context to 50 chars
                ctx = prev_content[:50].strip()
                if ctx:
                    title = f"[{ctx}] {title}"
    return title


# =============================================================================
# Linear API (optional)
# =============================================================================

LINEAR_API_ENDPOINT = "https://api.linear.app/graphql"


def get_linear_api_key() -> str | None:
    key = os.getenv("LINEAR_API_KEY", "")
    return key if key else None


def create_linear_issue(team_id: str, title: str, body: str, parent_ids: list[str] | None = None) -> str | None:
    """Create a Linear issue. Returns issue identifier or None on failure."""
    api_key = get_linear_api_key()
    if not api_key:
        return None

    import requests

    mutation = """
    mutation CreateIssue($teamId: String!, $title: String!, $body: String, $parentIds: [String!]) {
      issueCreate(input: {
        teamId: $teamId,
        title: $title,
        body: $body,
        inverseBlockedByRelations: $parentIds
      }) {
        success
        issue {
          identifier
          title
        }
      }
    }
    """
    variables = {"teamId": team_id, "title": title, "body": body}
    if parent_ids:
        variables["parentIds"] = parent_ids

    try:
        resp = requests.post(
            LINEAR_API_ENDPOINT,
            json={"query": mutation, "variables": variables},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            logger.warning("Linear API error: %s", data["errors"])
            return None
        issue = data.get("data", {}).get("issueCreate", {}).get("issue")
        if issue:
            return issue.get("identifier")
    except Exception as e:
        logger.warning("Failed to create Linear issue: %s", e)
    return None


# =============================================================================
# Kanban card creation
# =============================================================================

def create_kanban_card(
    title: str,
    body: str,
    source: str,
    trigger_source: str = "activity_logger",
    idempotency_key: str | None = None,
    board: str = "default",
    linear_team_id: str | None = None,
    linear_parent_ids: list[str] | None = None,
) -> str | None:
    """
    Create a kanban card via drewgent_kanban_db.task_create().
    Returns task_id or None on failure.
    """
    try:
        from tools.drewgent_kanban_db import task_create

        result = task_create(
            title=title,
            body=body,
            assignee="drewgent",
            status="ready",  # no parent deps for activity logger tasks
            priority=2,  # medium
            created_by="activity_logger",
            trigger_source=trigger_source,
            idempotency_key=idempotency_key,
            board=board,
        )

        if result.get("ok"):
            task_id = result.get("task_id")
            logger.info("Created kanban card: %s — %s", task_id, title[:60])

            # Optionally create Linear issue
            if linear_team_id:
                linear_id = create_linear_issue(
                    linear_team_id, title, body, linear_parent_ids
                )
                if linear_id:
                    logger.info("Created Linear issue: %s for card %s", linear_id, task_id)

            return task_id
        else:
            logger.warning("task_create failed: %s", result.get("error"))
    except Exception as e:
        logger.warning("Failed to create kanban card: %s", e)

    return None


# =============================================================================
# Main
# =============================================================================

def run() -> dict:
    """
    Run one activity logging tick. Returns summary dict.
    """
    state = load_state()
    state["last_run_at"] = datetime.now().isoformat()

    # Find recent sessions (last 1 hour to catch recent work)
    sessions = get_recent_sessions(hours=1)
    logger.info("Activity logger: scanning %d recent sessions", len(sessions))

    new_cards = 0
    processed_count = 0

    linear_team_id = os.getenv("LINEAR_TEAM_ID", "") or None

    for session_path in sessions:
        session_id = session_path.stem
        messages = load_session_messages(session_path)

        for idx, message in enumerate(messages):
            if message.get("role") != "user":
                continue

            content = extract_content(message)
            if not content:
                continue

            if not detect_work_pattern(content):
                continue

            if was_processed(state, session_id, idx):
                continue

            # Build title with context from previous assistant message
            title = build_title(content, messages, idx)
            body = content

            # Idempotency key: unique per session+message
            idempotency_key = f"activity:{session_id}:{idx}"

            task_id = create_kanban_card(
                title=title,
                body=body,
                source=session_path.name,
                trigger_source="activity_logger",
                idempotency_key=idempotency_key,
                board="default",
                linear_team_id=linear_team_id,
            )

            if task_id:
                new_cards += 1

            mark_processed(state, session_id, idx)
            processed_count += 1

    state["created_cards"] = state.get("created_cards", 0) + new_cards
    save_state(state)

    result = {
        "ok": True,
        "sessions_scanned": len(sessions),
        "messages_processed": processed_count,
        "cards_created": new_cards,
        "total_cards": state["created_cards"],
    }

    logger.info(
        "Activity logger tick complete: %d cards created (total: %d)",
        new_cards,
        state["created_cards"],
    )

    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))