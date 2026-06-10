---
name: cron-script-fastpath
description: "Add a script field to jobs.json + scheduler.py branch to bypass LLM for simple shell cron jobs (cost optimization). Reusable pattern for any 'Run: python3 xxx.py' style cron."
---

# Cron Script Fast-Path — LLM Bypass for Simple Shell Jobs

Drewgent cron jobs run their `prompt` through `AIAgent` (LLM) by default.
For jobs whose prompt is just a shell command ("Run: python3 xxx.py"),
this is wasteful — LLM parses the prompt, runs the script, and forwards
stdout to delivery. Each run costs a full LLM round-trip.

This skill documents a **surgical patch** that adds a `script:` field
fast-path: when a job has `script: "path"`, scheduler runs the script
directly via `subprocess.run()` and skips the LLM entirely.

---

## 1. When to Use

Apply when a cron job's prompt is **structurally a single shell command**:
- "Run: python3 ~/.drewgent/scripts/foo.py"
- "Run: bash ~/.drewgent/scripts/bar.sh"
- Any job where the prompt's only value-add over `subprocess.run` is
  "Report: ..." or "If silent, [SILENT]" — both of which the script
  already produces in stdout.

**Do NOT use** for jobs that need:
- LLM reasoning (SEO article scoring, Trend analysis, site-audit synthesis)
- Multi-step coordination with side tools (n8n, MCP, etc.)
- Reading multiple files + writing a human-friendly report

If unsure, leave the job on the LLM path. Cost-saving at the cost of
quality is not the goal.

---

## 2. Patch Surface (3 files)

### File 1: `cron/scheduler.py` — add branch + helper

**Branch** — at the top of `run_job()`'s `try:` block (before
`AIAgent` setup, after session init):

```python
try:
    # Script-based fast path: LLM 거치지 않고 직접 subprocess 실행
    # (cron-output-cleanup, kanban-maintenance 등 단순 shell prompt job)
    # 결정론적 shell 실행 + stdout 그대로 delivery → LLM 0회.
    script_path = (job.get("script") or "").strip()
    if script_path:
        return _run_script_subprocess(
            job, script_path, origin, _cron_session_id, _session_db
        )

    # Inject origin context so the agent's send_message tool knows the chat.
    # ... existing LLM setup ...
```

**Helper** — at module level, after `run_job()` definition, before
`def tick()`. Returns the same tuple shape so `tick()` delivery logic
reuses without changes:

```python
def _run_script_subprocess(
    job: dict,
    script_path: str,
    origin: Optional[dict],
    cron_session_id: str,
    session_db,
) -> tuple[bool, str, str, Optional[str]]:
    """Execute a script-based cron job via direct subprocess (no LLM).

    For jobs whose prompt is just a shell command (cron-output-cleanup,
    kanban-maintenance, etc.), this path bypasses the AIAgent round-trip
    entirely and runs the script directly. Returns the same tuple shape
    as run_job() so tick() delivery logic stays uniform.
    """
    job_id = job["id"]
    job_name = job.get("name", job_id)
    expanded_script = os.path.expanduser(script_path)
    logger.info(
        "Script-based job '%s' (ID: %s, script: %s)",
        job_name, job_id, expanded_script,
    )

    if not os.path.isfile(expanded_script):
        error_msg = f"Script not found: {expanded_script}"
        logger.error(error_msg)
        return False, "", "", error_msg

    try:
        env = {**os.environ, "DREW_HOME": str(_drewgent_home)}
        result = subprocess.run(
            [sys.executable, expanded_script],
            capture_output=True, text=True, timeout=300,
            env=env, cwd=str(_drewgent_home),
        )
        output = result.stdout.strip()
        error_output = result.stderr.strip()

        if result.returncode != 0:
            error_msg = f"Script exit {result.returncode}: {error_output[:500]}"
            logger.error("Script job '%s' failed: %s", job_name, error_msg)
            return False, output, "", error_msg

        logger.info(
            "Script job '%s' completed (exit=0, output=%d chars)",
            job_name, len(output),
        )
        return True, output, output, None
    except subprocess.TimeoutExpired:
        return False, "", "", f"Script timeout (300s): {expanded_script}"
    except Exception as e:
        return False, "", "", f"{type(e).__name__}: {e}"
```

### File 2: `cron/jobs.json` — flip `script: null` to path

For each target job, change:
```json
"script": null,
```
to:
```json
"script": "~/.drewgent/scripts/<name>.py",
```

Keep the existing `prompt` field untouched — it's now unused but stays
as a human-readable breadcrumb of what the job does.

### File 3: (optional) the script itself

If the cron job's previous behavior was "LLM runs shell + reports
output", make sure the underlying script already produces the
required output format. For kanban-maintenance specifically, a new
`scripts/kanban_maintenance.py` was written from the
`kanban-maintenance-guide.md` recipe (cleanup + HTML refresh).

---

## 3. Tick() Reuse — Why It Just Works

`tick()` in scheduler.py (line 800+) iterates `due_jobs`, calls
`run_job()`, and runs the result through a uniform pipeline:

```python
success, output, final_response, error = run_job(job)

is_silent = (
    success
    and bool(final_response)
    and SILENT_MARKER in final_response.strip().upper()
)

if not is_silent:
    output_file = save_job_output(job["id"], output)

deliver_content = final_response if success else f"⚠️ Cron job ... failed: ..."
if deliver_content and not (success and is_silent):
    _deliver_result(job, deliver_content, ...)
```

Because `_run_script_subprocess` returns the same
`(success, output, final_response, error)` tuple, all of the above
logic — `[SILENT]` detection, output file save, delivery to Discord
or local — **just works** without any change in `tick()`.

The script's stdout becomes `final_response` directly. If stdout
contains `[SILENT]`, delivery is skipped (line 833-835). If script
exits non-zero, `error` is set and a "⚠️ failed" delivery goes out
(line 831).

---

## 4. Dry-Run Verification (no LLM, no cron tick)

After patching, verify with a direct call to `run_job()` from
`execute_code`:

```python
import sys, os, json
sys.path.insert(0, '/Users/drew/.drewgent/source/drewgent-agent')
os.chdir('/Users/drew/.drewgent/source/drewgent-agent')

from cron.scheduler import run_job

with open('/Users/drew/.drewgent/cron/jobs.json') as f:
    jobs = json.load(f)['jobs']

# Pick one of the patched jobs
job = next(j for j in jobs if j.get('script'))
print(f"script: {job['script']!r}")

success, output, final, error = run_job(job)
print(f"success={success}")
print(f"final[:300]={final[:300]!r}")
print(f"error={error!r}")
```

`success=True` + a non-empty `final` + `error=None` = LLM was bypassed,
script ran, delivery path will fire normally. No tokens consumed.

---

## 5. Cost Impact (per enabled job)

| Metric | Before | After |
|---|---|---|
| LLM calls per run | 1 (full AIAgent round-trip) | 0 |
| LLM model | main (e.g. MiniMax-M3, 1M context) | n/a |
| Subprocess cost | small (one shell + script) | same |
| Delivery | identical | identical |

For jobs running on a cadence (daily, weekly), the saving accumulates.
A 1M-context M3 call is expensive — replacing it with `subprocess.run`
of a 50-line Python script is essentially free.

---

## 6. Verification Checklist (pre-commit / pre-deploy)

- [ ] `python3 -m py_compile` on `cron/scheduler.py` and the new script
- [ ] `python3 -c "import json; json.load(open('cron/jobs.json'))"` valid
- [ ] Dry-run via `execute_code` shows `success=True, error=None`
- [ ] Other cron jobs (5 unaffected: SEO, Trend, 3× kanban-dispatcher,
      site-spec-audit) still hit the LLM path — verify by inspecting
      that their `script:` field is still `null` in jobs.json
- [ ] On next live tick of the patched job, check `cron-runner.log`
      for "Script-based job ... completed" entry (no "Running job" +
      no AIAgent mention)

---

## 7. Common Pitfalls

1. **Don't change `tick()`**. The whole point of the same-tuple
   contract is no caller changes. If you find yourself editing
   `tick()`, the helper's return shape is wrong.
2. **`os.path.expanduser`** for `~` in script path — jobs.json
   stores `~` but `subprocess.run` won't expand it.
3. **Don't move env injection into the helper**. The `finally` in
   `run_job()` already cleans `DREW_SESSION_PLATFORM/CHAT_ID/etc.`
   for both branches because the helper is called inside `try:`.
4. **Timeout 300s** is hardcoded. If a script needs longer, increase
   it (but prefer making the script faster — cron has its own
   `DREW_CRON_TIMEOUT` for LLM path).
5. **The script is invoked with `sys.executable`**, not the user's
   python. If the script depends on the drewgent venv (most do),
   ensure `shebang` or interpreter path is correct, OR rely on the
   env `VIRTUAL_ENV` being set in `_drewgent_home` invocation.
6. **"Done ✅" claims in review docs can be stale.** KANBAN-REVIEW-
   20260520.md had "Worker mode: KANBAN_WORKER_MODE implemented ✅"
   but the actual code path runs AIAgent. Always grep the actual
   code path before trusting review docs.

---

## 8. Related

- `cron/scheduler.py:run_job` — branch insertion point (line ~493)
- `cron/scheduler.py:tick` — caller, no changes needed
- `~/.drewgent/P4-cortex/growth/kanban-maintenance-guide.md` — recipe
  used to write `scripts/kanban_maintenance.py`
- `~/.drewgent/P6-prefrontal/incidents/cron-jobs-stalled-20260601` —
  related to cron-runner lifecycle, separate concern
- `skills/software-development/yaml-config-patch-drewgent` — sister
  skill for `~/.drewgent/config.yaml` + `P5-ego/config/config.yaml`
  dual-patch pattern

---

*Created 2026-06-03 after Round 2 (H1) of background LLM cost optimization:
2 jobs migrated, dry-run LLM=0 confirmed, full-QA pending next live tick.*
