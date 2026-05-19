"""
Linear Kanban Tools

Provides tools for managing Linear issues with kanban-style task orchestration.
All operations use the Linear GraphQL API via requests (no official SDK needed).

State file: ~/.drewgent/state/kanban_dispatcher.json
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from drewgent_constants import get_drewgent_home

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

LINEAR_API_ENDPOINT = "https://api.linear.app/graphql"
KANBAN_STATE_FILE = get_drewgent_home() / "state" / "kanban_dispatcher.json"
KANBAN_STATE_VERSION = 1


def _get_api_key() -> str:
    key = os.getenv("LINEAR_API_KEY", "")
    if not key:
        raise PermissionError("LINEAR_API_KEY environment variable is not set")
    return key


def _linear_request(query: str, variables: Optional[dict] = None) -> dict:
    """Make a request to the Linear GraphQL API. Returns parsed JSON."""
    import requests

    api_key = _get_api_key()
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(
        LINEAR_API_ENDPOINT, json=payload, headers=headers, timeout=30
    )
    response.raise_for_status()
    data = response.json()

    if data.get("errors"):
        raise RuntimeError(f"Linear API error: {data['errors']}")

    return data.get("data", {})


# =============================================================================
# State File Operations
# =============================================================================


def _ensure_state_dir():
    """Create the state directory if it doesn't exist."""
    KANBAN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def read_kanban_state() -> dict:
    """Read the kanban dispatcher state file. Returns empty dict if missing."""
    _ensure_state_dir()
    if not KANBAN_STATE_FILE.exists():
        return {"version": KANBAN_STATE_VERSION, "active_tasks": {}, "parent_child_map": {}, "orphan_children": {}}
    try:
        with open(KANBAN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to read kanban state file: %s. Returning empty state.", e)
        return {"version": KANBAN_STATE_VERSION, "active_tasks": {}, "parent_child_map": {}, "orphan_children": {}}


def write_kanban_state(state: dict) -> None:
    """Atomically write the kanban dispatcher state file."""
    _ensure_state_dir()
    fd, tmp_path = tempfile.mkstemp(dir=str(KANBAN_STATE_FILE.parent), suffix=".tmp", prefix=".kanban_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, KANBAN_STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# =============================================================================
# Tool Handlers
# =============================================================================

def _tool_error(msg: str) -> str:
    """Return a structured error string for tool failures."""
    return json.dumps({"ok": False, "error": msg})


# --- kanban_linear_create ---
KANBAN_CREATE_SCHEMA = {
    "name": "kanban_linear_create",
    "description": "Create a Linear issue. Optionally set parent relationships via blocks/inverseBlockedBy.",
    "input_schema": {
        "type": "object",
        "properties": {
            "team_id": {"type": "string", "description": "Linear team UUID (e.g., 'a1b2c3d4-...')"},
            "title": {"type": "string", "description": "Issue title"},
            "body": {"type": "string", "description": "Issue description (Markdown supported)", "default": ""},
            "assignee_id": {"type": "string", "description": "Assignee user UUID (omit for unassigned)", "default": ""},
            "parent_ids": {"type": "array", "items": {"type": "string"}, "description": "Linear issue UUIDs that this issue is blocked by (parent tasks)", "default": []},
            "priority": {"type": "integer", "description": "Priority: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low", "default": 0},
            "label_ids": {"type": "array", "items": {"type": "string"}, "description": "Label UUIDs to apply", "default": []},
            "project_id": {"type": "string", "description": "Project UUID to add issue to", "default": ""},
            "state_id": {"type": "string", "description": "Initial workflow state UUID (omit to use team default)", "default": ""},
        },
        "required": ["team_id", "title"],
    },
}


def handle_kanban_linear_create(args: dict) -> str:
    """Create a Linear issue and optionally register it in the kanban state."""
    try:
        team_id = args.get("team_id", "")
        title = args.get("title", "")
        body = args.get("body", "")
        assignee_id = args.get("assignee_id") or None
        parent_ids = args.get("parent_ids", [])
        priority = args.get("priority", 0)
        label_ids = args.get("label_ids", []) or []
        project_id = args.get("project_id") or None
        state_id = args.get("state_id") or None

        if not team_id or not title:
            return _tool_error("team_id and title are required")

        create_input = {
            "teamId": team_id,
            "title": title,
        }
        if body:
            create_input["description"] = body
        if assignee_id:
            create_input["assigneeId"] = assignee_id
        if parent_ids:
            create_input["blockedByIssueIds"] = parent_ids
        if priority:
            create_input["priority"] = priority
        if label_ids:
            create_input["labelIds"] = label_ids
        if project_id:
            create_input["projectId"] = project_id
        if state_id:
            create_input["stateId"] = state_id

        query = """
        mutation CreateIssue($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue {
                    id
                    identifier
                    title
                    url
                }
            }
        }
        """
        data = _linear_request(query, {"input": create_input})
        result = data.get("issueCreate", {})
        if not result.get("success"):
            return _tool_error(f"Issue creation failed: {result}")

        issue = result["issue"]

        # Register in kanban state if parent-child relationships exist
        if parent_ids:
            state = read_kanban_state()
            for parent_id in parent_ids:
                if parent_id not in state["parent_child_map"]:
                    state["parent_child_map"][parent_id] = []
                if issue["id"] not in state["parent_child_map"][parent_id]:
                    state["parent_child_map"][parent_id].append(issue["id"])
            write_kanban_state(state)

        return json.dumps({
            "ok": True,
            "task_id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "url": issue["url"],
        })

    except PermissionError as e:
        return _tool_error(str(e))
    except Exception as e:
        logger.exception("kanban_linear_create failed")
        return _tool_error(str(e))


# --- kanban_linear_update_status ---
KANBAN_UPDATE_STATUS_SCHEMA = {
    "name": "kanban_linear_update_status",
    "description": "Update a Linear issue's workflow state (e.g., move to completed, started, canceled, etc.).",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Linear issue UUID or identifier (e.g., 'ENG-123')"},
            "status": {"type": "string", "description": "Target state type: 'triage', 'backlog', 'unstarted', 'started', 'completed', 'canceled'"},
        },
        "required": ["task_id", "status"],
    },
}


def _resolve_state_id(team_id: str, status: str) -> str:
    """Resolve a state type to a state UUID for the given team."""
    type_map = {
        "triage": "triage",
        "backlog": "backlog",
        "unstarted": "unstarted",
        "started": "started",
        "completed": "completed",
        "canceled": "canceled",
    }
    linear_type = type_map.get(status)
    if not linear_type:
        raise ValueError(f"Unknown status '{status}'. Valid: {list(type_map.keys())}")

    query = """
    query GetState($teamId: ID!) {
        workflowStates(filter: { team: { id: { eq: $teamId } } }) {
            nodes { id name type }
        }
    }
    """
    data = _linear_request(query, {"teamId": team_id})
    states = data.get("workflowStates", {}).get("nodes", [])
    for s in states:
        if s["type"] == linear_type:
            return s["id"]

    raise RuntimeError(f"No state of type '{linear_type}' found for team {team_id}")


def _get_issue_team_id(task_id: str) -> str:
    """Fetch the team ID for a given issue."""
    query = """
    query GetIssueTeam($id: String!) {
        issue(id: $id) {
            team { id }
        }
    }
    """
    data = _linear_request(query, {"id": task_id})
    team = data.get("issue", {}).get("team")
    if not team:
        raise RuntimeError(f"Could not find team for issue {task_id}")
    return team["id"]


def handle_kanban_linear_update_status(args: dict) -> str:
    """Update the workflow state of a Linear issue."""
    try:
        task_id = args.get("task_id", "")
        status = args.get("status", "")

        if not task_id or not status:
            return _tool_error("task_id and status are required")

        # Get team ID for the issue
        team_id = _get_issue_team_id(task_id)
        state_id = _resolve_state_id(team_id, status)

        query = """
        mutation UpdateIssueStatus($id: String!, $stateId: String!) {
            issueUpdate(id: $id, input: { stateId: $stateId }) {
                success
                issue {
                    id
                    identifier
                    state { id name type }
                }
            }
        }
        """
        data = _linear_request(query, {"id": task_id, "stateId": state_id})
        result = data.get("issueUpdate", {})
        if not result.get("success"):
            return _tool_error(f"Status update failed: {result}")

        issue = result["issue"]
        return json.dumps({
            "ok": True,
            "task_id": issue["id"],
            "identifier": issue["identifier"],
            "new_status": issue["state"]["type"],
            "state_name": issue["state"]["name"],
        })

    except Exception as e:
        logger.exception("kanban_linear_update_status failed")
        return _tool_error(str(e))


# --- kanban_linear_list ---
KANBAN_LIST_SCHEMA = {
    "name": "kanban_linear_list",
    "description": "List Linear issues with optional filters (assignee, state, team, labels).",
    "input_schema": {
        "type": "object",
        "properties": {
            "assignee_id": {"type": "string", "description": "Filter by assignee UUID"},
            "status": {"type": "string", "description": "Filter by state type: 'triage', 'backlog', 'unstarted', 'started', 'completed', 'canceled'"},
            "team_id": {"type": "string", "description": "Filter by team UUID"},
            "label_ids": {"type": "array", "items": {"type": "string"}, "description": "Filter by label UUIDs (AND)"},
            "limit": {"type": "integer", "description": "Max results to return", "default": 50},
        },
    },
}


def handle_kanban_linear_list(args: dict) -> str:
    """List Linear issues with optional filters."""
    try:
        assignee_id = args.get("assignee_id") or None
        status = args.get("status") or None
        team_id = args.get("team_id") or None
        label_ids = args.get("label_ids") or []
        limit = min(args.get("limit", 50), 250)

        # Build filter object fields and variables dict
        # team.id / assignee.id / label.id expect ID type, not String
        filter_fields = []
        variables: dict = {"limit": limit}
        if assignee_id:
            filter_fields.append("assignee: { id: { eq: $assigneeId } }")
            variables["assigneeId"] = assignee_id
        if status:
            filter_fields.append("state: { type: { in: [$statusType] } }")
            variables["statusType"] = status
        if team_id:
            filter_fields.append("team: { id: { eq: $teamId } }")
            variables["teamId"] = team_id
        if label_ids:
            filter_fields.append("labels: { every: { id: { in: $labelIds } } }")
            variables["labelIds"] = label_ids

        filter_str = ", ".join(filter_fields) if filter_fields else ""
        filter_clause = ("filter: {" + filter_str + "}") if filter_str else ""

        # Build variable declarations — use ID type for entity references, String for scalars
        var_parts = ["$limit: Int!"]
        for k in variables:
            if k == "limit":
                continue
            graphql_type = "String" if k == "statusType" else "ID"
            var_parts.append(f"${k}: {graphql_type}!")
        var_decl = ", ".join(var_parts)

        query = f"""
query ListIssues({var_decl}) {{
    issues(
        {filter_clause}
        first: $limit
    ) {{
        nodes {{
            id
            identifier
            title
            description
            priority
            url
            state {{ id name type }}
            assignee {{ id name email }}
            team {{ id name key }}
            labels {{
                nodes {{ id name color }}
            }}
        }}
        pageInfo {{ hasNextPage endCursor }}
    }}
}}
"""
        data = _linear_request(query, variables)
        issues = data.get("issues", {}).get("nodes", [])

        return json.dumps({
            "ok": True,
            "count": len(issues),
            "issues": [
                {
                    "id": i["id"],
                    "identifier": i["identifier"],
                    "title": i["title"],
                    "description": i.get("description", ""),
                    "priority": i.get("priority", 0),
                    "url": i["url"],
                    "state": i["state"]["type"] if i.get("state") else None,
                    "state_name": i["state"]["name"] if i.get("state") else None,
                    "assignee": i["assignee"]["name"] if i.get("assignee") else None,
                    "team": i["team"]["key"] if i.get("team") else None,
                    "labels": [l["name"] for l in (i.get("labels", {}).get("nodes") or [])],
                }
                for i in issues
            ],
        })

    except Exception as e:
        logger.exception("kanban_linear_list failed")
        return _tool_error(str(e))


# --- kanban_linear_get ---
KANBAN_GET_SCHEMA = {
    "name": "kanban_linear_get",
    "description": "Get full details of a Linear issue by ID or identifier.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Linear issue UUID or identifier (e.g., 'ENG-123')"},
            "include_dependencies": {"type": "boolean", "description": "Include blocked-by and blocks relationships", "default": False},
        },
        "required": ["task_id"],
    },
}


def handle_kanban_linear_get(args: dict) -> str:
    """Get a single Linear issue's full details."""
    try:
        task_id = args.get("task_id", "")
        include_deps = args.get("include_dependencies", False)

        if not task_id:
            return _tool_error("task_id is required")

        deps_fields = ""
        if include_deps:
            deps_fields = """
            blockedBy { nodes { id identifier title state { type } } }
            blocks { nodes { id identifier title state { type } } }
            """

        query = f"""
        query GetIssue($id: String!) {{
            issue(id: $id) {{
                id
                identifier
                title
                description
                priority
                url
                createdAt
                updatedAt
                dueDate
                state {{ id name type }}
                assignee {{ id name email }}
                team {{ id name key }}
                labels {{ nodes {{ id name color }} }}
                project {{ id name }}
                {deps_fields}
                comments(orderBy: createdAt) {{
                    nodes {{ id body createdAt user {{ name }} }}
                }}
            }}
        }}
        """
        data = _linear_request(query, {"id": task_id})
        issue = data.get("issue")
        if not issue:
            return _tool_error(f"Issue not found: {task_id}")

        result = {
            "ok": True,
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "description": issue.get("description", ""),
            "priority": issue.get("priority", 0),
            "url": issue["url"],
            "created_at": issue.get("createdAt"),
            "updated_at": issue.get("updatedAt"),
            "due_date": issue.get("dueDate"),
            "state": issue["state"]["type"] if issue.get("state") else None,
            "state_name": issue["state"]["name"] if issue.get("state") else None,
            "assignee": issue["assignee"]["name"] if issue.get("assignee") else None,
            "team": issue["team"]["key"] if issue.get("team") else None,
            "labels": [l["name"] for l in (issue.get("labels", {}).get("nodes") or [])],
            "project": issue.get("project", {}).get("name") if issue.get("project") else None,
        }

        if include_deps:
            result["blocked_by"] = [
                {"id": b["id"], "identifier": b["identifier"], "title": b["title"], "state": b["state"]["type"] if b.get("state") else None}
                for b in (issue.get("blockedBy", {}).get("nodes") or [])
            ]
            result["blocks"] = [
                {"id": b["id"], "identifier": b["identifier"], "title": b["title"], "state": b["state"]["type"] if b.get("state") else None}
                for b in (issue.get("blocks", {}).get("nodes") or [])
            ]

        return json.dumps(result)

    except Exception as e:
        logger.exception("kanban_linear_get failed")
        return _tool_error(str(e))


# --- kanban_linear_add_comment ---
KANBAN_ADD_COMMENT_SCHEMA = {
    "name": "kanban_linear_add_comment",
    "description": "Add a comment to a Linear issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Linear issue UUID or identifier (e.g., 'ENG-123')"},
            "body": {"type": "string", "description": "Comment body (Markdown supported)"},
        },
        "required": ["task_id", "body"],
    },
}


def handle_kanban_linear_add_comment(args: dict) -> str:
    """Add a comment to a Linear issue."""
    try:
        task_id = args.get("task_id", "")
        body = args.get("body", "")

        if not task_id or not body:
            return _tool_error("task_id and body are required")

        query = """
        mutation AddComment($issueId: String!, $body: String!) {
            commentCreate(input: { issueId: $issueId, body: $body }) {
                success
                comment {
                    id
                    body
                    createdAt
                }
            }
        }
        """
        data = _linear_request(query, {"issueId": task_id, "body": body})
        result = data.get("commentCreate", {})
        if not result.get("success"):
            return _tool_error(f"Comment creation failed: {result}")

        comment = result["comment"]
        return json.dumps({
            "ok": True,
            "comment_id": comment["id"],
            "created_at": comment["createdAt"],
        })

    except Exception as e:
        logger.exception("kanban_linear_add_comment failed")
        return _tool_error(str(e))


# --- kanban_linear_get_dependencies ---
KANBAN_GET_DEPS_SCHEMA = {
    "name": "kanban_linear_get_dependencies",
    "description": "Get blocked-by (parent) and blocks (child) relationships for a Linear issue.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Linear issue UUID or identifier (e.g., 'ENG-123')"},
        },
        "required": ["task_id"],
    },
}


def handle_kanban_linear_get_dependencies(args: dict) -> str:
    """Get the dependency relationships for a Linear issue."""
    try:
        task_id = args.get("task_id", "")
        if not task_id:
            return _tool_error("task_id is required")

        query = """
        query GetDeps($id: String!) {
            issue(id: $id) {
                id
                identifier
                title
            }
        }
        """
        data = _linear_request(query, {"id": task_id})
        issue = data.get("issue")
        if not issue:
            return _tool_error(f"Issue not found: {task_id}")

        blocked_by_ids = []
        blocked_by = []
        blocks = []

        return json.dumps({
            "ok": True,
            "task_id": issue["id"],
            "identifier": issue["identifier"],
            "blocked_by_ids": blocked_by_ids,
            "blocks_ids": [],
        })

    except Exception as e:
        logger.exception("kanban_linear_get_dependencies failed")
        return _tool_error(str(e))


# =============================================================================
# Registry
# =============================================================================

from tools.registry import registry

registry.register(
    name="kanban_linear_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=lambda args, **kw: handle_kanban_linear_create(args),
    check_fn=lambda: bool(os.getenv("LINEAR_API_KEY")),
    requires_env=["LINEAR_API_KEY"],
    description="Create a Linear issue with optional parent-child blocking relationships",
    emoji="📋",
)

registry.register(
    name="kanban_linear_update_status",
    toolset="kanban",
    schema=KANBAN_UPDATE_STATUS_SCHEMA,
    handler=lambda args, **kw: handle_kanban_linear_update_status(args),
    check_fn=lambda: bool(os.getenv("LINEAR_API_KEY")),
    requires_env=["LINEAR_API_KEY"],
    description="Update a Linear issue's workflow state (triage, backlog, unstarted, started, completed, canceled)",
    emoji="🔄",
)

registry.register(
    name="kanban_linear_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=lambda args, **kw: handle_kanban_linear_list(args),
    check_fn=lambda: bool(os.getenv("LINEAR_API_KEY")),
    requires_env=["LINEAR_API_KEY"],
    description="List Linear issues with optional filters (assignee, state, team, labels)",
    emoji="📑",
)

registry.register(
    name="kanban_linear_get",
    toolset="kanban",
    schema=KANBAN_GET_SCHEMA,
    handler=lambda args, **kw: handle_kanban_linear_get(args),
    check_fn=lambda: bool(os.getenv("LINEAR_API_KEY")),
    requires_env=["LINEAR_API_KEY"],
    description="Get full details of a Linear issue including dependencies",
    emoji="🔍",
)

registry.register(
    name="kanban_linear_add_comment",
    toolset="kanban",
    schema=KANBAN_ADD_COMMENT_SCHEMA,
    handler=lambda args, **kw: handle_kanban_linear_add_comment(args),
    check_fn=lambda: bool(os.getenv("LINEAR_API_KEY")),
    requires_env=["LINEAR_API_KEY"],
    description="Add a comment to a Linear issue",
    emoji="💬",
)

registry.register(
    name="kanban_linear_get_dependencies",
    toolset="kanban",
    schema=KANBAN_GET_DEPS_SCHEMA,
    handler=lambda args, **kw: handle_kanban_linear_get_dependencies(args),
    check_fn=lambda: bool(os.getenv("LINEAR_API_KEY")),
    requires_env=["LINEAR_API_KEY"],
    description="Get blocked-by (parent) and blocks (child) relationships for a Linear issue",
    emoji="🔗",
)
