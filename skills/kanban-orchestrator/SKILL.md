---
title: Skill
type: document
space: concept
tags: [concept]
created: 2026-05-20
updated: 2026-05-20
links:
  - "[[P3-sensors/skills/SKILL-INDEX]]"
  - "[[P4-cortex/growth/drewgent-kanban-implementation-plan]]"
  - "[[references/protocol.md]]"
  - "[[skills/autonomous-ai-agents/drewgent-agent]]"
  - "[[skills/kanban-dashboard]]"
  - "[[skills/kanban-worker]]"
---





# Kanban Orchestrator Skill

Decompose a complex goal into ordered subtasks, link them as a dependency chain, spawn workers for each subtask, and monitor upstream results before spawning downstream tasks.

## When to Use This Skill

Use this when:
- User gives a complex goal that needs to be broken into steps
- You need to spawn multiple sub-agents that depend on each other's output
- You want to track progress across a multi-step workflow
- You need to prevent sub-agents from working on out-of-order tasks

## Core Workflow

```
1. Analyze goal → identify subtasks
2. Create tasks (parent_task_ids for dependencies)
3. Link tasks (task_link) for dynamic reordering
4. Spawn workers for ready tasks
5. Monitor completion → promote children
6. Aggregate results → final output
```

## Step 1: Analyze and Decompose

Break the goal into smallest meaningful units. Rules:
- Each task should be completable by a single worker without needing other tasks' output mid-execution
- Dependencies should reflect data flow (downstream needs upstream's result)
- Do NOT create micro-tasks (don't break at "write function X" level — break at "implement module Y" level)

Example decomposition:
```
Goal: "Build user authentication system"
Tasks:
  1. Design auth schema (no deps) → ready
  2. Implement user model (deps: 1) → todo
  3. Implement auth endpoints (deps: 2) → todo
  4. Write tests (deps: 3) → todo
```

## Step 2: Create Tasks with Dependencies

Use `kanban_create` with `parent_task_ids`:

```python
# Create parent task (no dependencies)
parent = kanban_create(
    title="Implement user authentication",
    body="Build complete auth system: schema, model, endpoints, tests",
    trigger_source="orchestrator",
)

# Create child task with dependency
child = kanban_create(
    title="Design auth schema",
    body="DB schema for users, sessions, refresh tokens",
    parent_task_ids=[parent["task_id"]],  # blocks until parent done
    trigger_source="orchestrator",
)
```

**Status rules** (auto-assigned by `kanban_create`):
- No parents → `ready`
- All parents done → `ready`
- Any parent not done → `todo`

## Step 3: Link Tasks (Dynamic Reordering)

For dynamic dependency chains that aren't known at creation time:

```python
# Link after both tasks exist
task_link(parent_id="t_abc123", child_id="t_def456")

# Effect: if parent not done, child demoted to 'todo'
# Effect: when parent completes, child promoted to 'ready'
```

**Note**: `task_link` also checks parent status. If parent is already done, child stays `ready`. If parent is not done, child is demoted to `todo`.

## Step 4: Spawn Workers for Ready Tasks

```python
# List all ready tasks
ready_tasks = kanban_list(status="ready")
# → [{"task_id": "t_xxx", "title": "...", ...}, ...]

# Claim and spawn worker for each
for task in ready_tasks:
    worker_pid = spawn_worker(task["task_id"])
    print(f"Spawned worker {worker_pid} for {task['task_id']}")
```

**Spawn pattern** (from `kanban_tools.py`):
```python
spawn_worker(task_id, board="default", max_runtime_seconds=3600)
# → returns {"ok": True, "worker_pid": 12345, "task_id": "t_xxx"}
```

## Step 5: Monitor and Promote

The `kanban_complete` function automatically promotes children when a parent completes. You don't need to manually recheck.

Monitor via task events:
```python
# Get task history
task_events = task_get_events(task_id)
# → [{"kind": "created", "created_at": "..."}, {"kind": "promoted", ...}]
```

## Step 6: Aggregate Results

After all tasks complete, collect results:

```python
# Get all completed children
completed = kanban_list(status="completed")
# Filter by parent or integration_workflow_id

# Aggregate into final response
results = [task["result"] for task in completed if task["integration_workflow_id"] == wf_id]
```

## Hallucination Prevention

When a worker creates subtasks during execution, it should pass `created_cards` to `kanban_complete`:

```python
kanban_complete(
    task_id="t_parent123",
    result="Module implemented with 3 sub-tasks created",
    summary="Auth module complete",
    created_cards=["t_child1", "t_child2", "t_child3"],  # IDs verified against DB
)
```

If any `created_cards` ID is fake, completion is blocked with `completion_blocked_hallucination`.

## Prose Reference Detection

Workers may mention task IDs in natural language ("see t_abc123"). These are flagged as `unresolved_refs` but do NOT block completion.

```python
kanban_complete(
    task_id="t_xyz",
    result="Done. Passed to t_def456 for next step",
)
# → {unresolved_refs: ["t_def456"]} — flagged but allowed
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `KANBAN_BOARD` | Board name (default: 'default') |
| `ORCHESTRATOR_WF_ID` | Integration workflow ID for grouping tasks |

## Reference

See [[references/protocol.md]] for the full API schema and examples.

## Related Skills

- [[skills/kanban-worker]] — Worker-side task execution
- [[skills/kanban-dashboard]] — Board visualization
- [[skills/autonomous-ai-agents/drewgent-agent]] — How to spawn Drewgent sub-agents

## Related

- [[P4-cortex/growth/drewgent-kanban-implementation-plan]]
- [[P3-sensors/skills/SKILL-INDEX]]