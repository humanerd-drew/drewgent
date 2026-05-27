"""CLI entry point for the drewgent-agent ACP adapter.

Loads environment variables from ``~/.drewgent/.env``, configures logging
to write to stderr (so stdout is reserved for ACP JSON-RPC transport),
and starts the ACP agent server.

Usage::

    python -m acp_adapter.entry
    # or
    drewgent acp
    # or
    drewgent-acp
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from drewgent_constants import get_drewgent_home


def _setup_logging() -> None:
    """Route all logging to stderr so stdout stays clean for ACP stdio."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _load_env() -> None:
    """Load .env from DREW_HOME (default ``~/.drewgent``)."""
    from drewgent_cli.env_loader import load_drewgent_dotenv

    drewgent_home = get_drewgent_home()
    loaded = load_drewgent_dotenv(drewgent_home=drewgent_home)
    if loaded:
        for env_file in loaded:
            logging.getLogger(__name__).info("Loaded env from %s", env_file)
    else:
        logging.getLogger(__name__).info(
            "No .env found at %s, using system env", drewgent_home / ".env"
        )


def _try_kanban_worker_mode() -> bool:
    """Run a kanban task directly without the full ACP/LLM server.

    When KANBAN_WORKER_MODE=1 and KANBAN_TASK_ID is set, the worker enters
    targeted execution mode instead of starting the full ACP server loop.

    Task body is expected to be one of:
      - JSON: {"action": "run_script", "script": "...", "args": {...}}
      - JSON: {"action": "update_file", "path": "...", "content": "..."}
      - JSON: {"action": "http_request", "method": "...", "url": "...", ...}
      - Plain text: passed through to the LLM-free execution handler

    Returns True if worker mode was entered (this function exits the process).
    Returns False if KANBAN_WORKER_MODE is not set.
    """
    import json as _json
    import logging as _logging
    from datetime import datetime as _dt

    if os.environ.get("KANBAN_WORKER_MODE") != "1":
        return False

    task_id = os.environ.get("KANBAN_TASK_ID")
    if not task_id:
        print("KANBAN_WORKER_MODE=1 but KANBAN_TASK_ID not set", file=sys.stderr)
        sys.exit(1)

    logger = _logging.getLogger("kanban.worker")
    logger.info("Entering KANBAN_WORKER_MODE for task %s", task_id)

    # Add project root to path
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    from tools.drewgent_kanban_db import task_get, task_complete, task_heartbeat

    task = task_get(task_id)
    if not task:
        print(f"Task {task_id} not found", file=sys.stderr)
        sys.exit(1)

    # Send initial heartbeat
    task_heartbeat(task_id, note=f"Worker started at {_dt.now().isoformat()}")

    body = task.get("body") or ""
    result_msg = ""
    summary_msg = ""

    try:
        # Try to parse task body as structured JSON
        parsed = _json.loads(body)

        if isinstance(parsed, dict):
            action = parsed.get("action", "")

            if action == "run_script":
                import subprocess as _subprocess
                script = parsed.get("script", "")
                args = parsed.get("args", [])
                timeout = parsed.get("timeout", 300)
                cwd = parsed.get("cwd")
                logger.info("Running script: %s args=%s", script, args)
                proc = _subprocess.run(
                    ["python3", "-c", script] + list(args),
                    capture_output=True, text=True, timeout=timeout, cwd=cwd,
                )
                result_msg = f"stdout: {proc.stdout}\nstderr: {proc.stderr}\nreturncode: {proc.returncode}"
                summary_msg = f"Script exit {proc.returncode}"

            elif action == "update_file":
                file_path = parsed.get("path", "")
                content = parsed.get("content", "")
                mode = parsed.get("mode", "w")
                if not file_path:
                    raise ValueError("update_file requires 'path'")
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                Path(file_path).write_text(content)
                result_msg = f"File written: {file_path} ({len(content)} bytes)"
                summary_msg = f"Updated {file_path}"

            elif action == "http_request":
                import urllib.request as _urllib_request
                import urllib.parse as _urllib_parse
                method = parsed.get("method", "GET").upper()
                url = parsed.get("url", "")
                headers = parsed.get("headers", {})
                data = parsed.get("data")
                timeout = parsed.get("timeout", 30)

                if not url:
                    raise ValueError("http_request requires 'url'")

                req = _urllib_request.Request(url, data=(
                    _urllib_parse.urlencode(data).encode() if data else None
                ), method=method)
                for k, v in headers.items():
                    req.add_header(k, v)

                with _urllib_request.urlopen(req, timeout=timeout) as resp:
                    result_msg = f"status: {resp.status}\nbody: {resp.read().decode(errors='replace')}"
                    summary_msg = f"HTTP {method} {resp.status}"

            else:
                # Unknown action — treat whole body as result
                result_msg = f"Unknown action '{action}' in task body: {parsed}"
                summary_msg = f"Worker mode: unknown action '{action}'"

        elif isinstance(parsed, list):
            # List of actions — execute in order
            results = []
            for i, step in enumerate(parsed):
                step_action = step.get("action", "") if isinstance(step, dict) else str(step)
                results.append(f"step {i}: {step_action}")
            result_msg = "\n".join(results)
            summary_msg = f"Executed {len(parsed)} steps"

    except (_json.JSONDecodeError, TypeError):
        # Non-JSON body — treat as plain text result
        result_msg = body
        summary_msg = f"Task body ({len(body)} chars)"

    # Final heartbeat + complete
    task_heartbeat(task_id, note=f"Worker finishing at {_dt.now().isoformat()}")
    task_complete(task_id, result=result_msg, summary=summary_msg)
    logger.info("Task %s completed: %s", task_id, summary_msg)
    print(f"[KANBAN_WORKER_MODE] Task {task_id} completed: {summary_msg}")
    sys.exit(0)
    return True  # never reached


def main() -> None:
    """Entry point: load env, configure logging, run the ACP agent.

    If KANBAN_WORKER_MODE=1 and KANBAN_TASK_ID is set, enters targeted
    execution mode instead of the full ACP server loop.
    """
    _setup_logging()
    _load_env()

    logger = logging.getLogger(__name__)

    # KANBAN_WORKER_MODE: targeted execution without full ACP/LLM server
    if _try_kanban_worker_mode():
        return  # _try_kanban_worker_mode calls sys.exit() on success

    logger.info("Starting drewgent-agent ACP adapter")

    # Ensure the project root is on sys.path so ``from run_agent import AIAgent`` works
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    import acp
    from .server import HermesACPAgent

    agent = HermesACPAgent()
    try:
        asyncio.run(acp.run_agent(agent, use_unstable_protocol=True))
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    except Exception:
        logger.exception("ACP agent crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
