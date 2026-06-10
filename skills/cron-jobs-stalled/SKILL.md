---
name: cron-jobs-stalled
description: Diagnose and recover cron jobs in jobs.json that stopped running (next_run_at=null, no recent output). Distinguishes scheduler bug vs script quality issue. Applies 60-second recovery patch.
category: devops
---

# Cron Jobs Stalled — Diagnostic & Recovery

When user reports "X cron not running" or "X job stopped" or jobs.json shows enabled jobs with no recent execution, this skill diagnoses and recovers.

**Triggers:**
- "X cron stopped", "X job not running for N days"
- `~/.drewgent/cron/jobs.json` enabled jobs with `last_run_at` > 1 day old
- All enabled jobs may have `next_run_at: null`
- `~/.drewgent/cron/output/{job_id}/` has no recent files
- Output files exist but contain junk (empty body, trivial title)

---

## Step 1: Read jobs.json directly (filesystem truth)

```bash
python3 -c "import json; d=json.load(open('$HOME/.drewgent/cron/jobs.json')); [print(f'{j.get(\"name\",\"?\"):30} enabled={j.get(\"enabled\")} last={str(j.get(\"last_run_at\",\"\"))[:19]} next={str(j.get(\"next_run_at\",\"\"))[:19] or \"NULL\"} status={j.get(\"last_status\",\"\")}') for j in d.get('jobs',[])]"

ls -lt ~/.drewgent/cron/output/*/ 2>/dev/null | head -30
```

Don't trust `last_status=ok` — it's a stale marker. `next_run_at` is the source of truth for whether the scheduler will run the job.

**Output dir name = job_id (12-char hex), NOT job name.** `cron/output/{job_id}/` directory names are auto-generated from `jobs.json` `id` field. To find the dir for a job, read its `id` from jobs.json, don't guess from the name. (e.g. `kanban-dispatcher-integrations` has id `cb909be06e0e` → dir is `cron/output/cb909be06e0e/`, not `cron/output/integrations-board-dispatcher/`.)

---

## Step 2: Classify the failure

Three distinct patterns. Diagnose carefully — fix is different for each.

### Pattern A: `next_run_at: null` for recurring jobs (scheduler bug)

**Symptoms:**
- All (or most) enabled jobs have `next_run_at: null`
- `last_run_at` 1-2 days old
- Output dir has no recent files

**Root cause:** `cron/jobs.py` `get_due_jobs()` only calls `_recoverable_oneshot_run_at()` for null `next_run_at`. Recurring jobs (`schedule.kind in {'cron', 'interval'}`) get dropped silently.

**Documented incidents:**
- `P6-prefrontal/incidents/cron-jobs-stalled-20260601.md` (5 enabled jobs, ~36h dormant)
- `P6-prefrontal/incidents/cron-job-failure-20260518.md` (related — double-run fix, qa_evidence_dir KeyError)

**Fix (immediate, 60s recovery):**
```python
from cron.jobs import load_jobs, save_jobs, _drewgent_now
from datetime import timedelta

now = _drewgent_now()
jobs = load_jobs()
patched = 0
for j in jobs:
    if (j.get('enabled')
        and j.get('next_run_at') is None
        and j.get('schedule', {}).get('kind') in ('cron', 'interval')):
        j['next_run_at'] = (now - timedelta(seconds=5)).isoformat()
        patched += 1
save_jobs(jobs)
print(f"patched: {patched} jobs")
```

**Fix (permanent, in `cron/jobs.py` `get_due_jobs()`):** Add branch for recurring jobs that recomputes `next_run_at` from `schedule` when null. Applied 2026-06-01, takes effect on gateway restart.

### Pattern B: Scheduler runs but script produces junk

**Symptoms:**
- `last_run_at` updates every 6h (cron ticks normally)
- Output dir has recent files but content is trivial
- Articles saved with empty body or trivial title (e.g. "안녕하세요")

**Root cause:** Bad RSS feed added (e.g. letspl.me 밋업 feed → "안녕하세요" event intros), or extraction script broke.

**Diagnostic:**
```bash
# Find junk articles
ls -lt ~/.drewgent/cron/output/{job_id}/ | head -10
# Check content of recent files
for f in $(ls -t ~/.drewgent/cron/output/{job_id}/2026-*.md | head -5); do
  echo "=== $f ==="
  head -20 "$f"
done
```

**Fix:** 
1. Audit the RSS_FEEDS / source list — remove obviously off-topic sources
2. Add content min-length guards in main() (e.g. `MIN_TITLE_LENGTH=10`, `MIN_BODY_LENGTH=200`)
3. Add explicit `⏭️ SKIP (reason): url` log line
4. Add skip counter to final summary

See `skills/seo-article-harvester/scripts/harvester.py` (line 33-36 constants, line 514-552 main loop) for the canonical fix pattern.

### Pattern C: launchd isn't running cron_runner at all

**Symptoms:**
- jobs.json state looks fine
- `last_run_at` is stale (1+ days)
- `launchctl list | grep drewgent` shows no `ai.drewgent.cron-runner` plist

**Use skill:** `launchd-process-health-check` (separate skill)

---

### Pattern D: False alarm (system healthy, user perceives stall)

**Symptoms:**
- User reports "X job stopped" or "X not running for days" but `next_run_at` is valid (future-dated)
- `cron-runner.log` has recent "dispatchers run" lines (within last 1-2 min)
- `cron/output/{job_id}/` has recent files (within cycle window)
- Only `launchctl list` shows PID=- or output dir for one job looks empty

**Root cause — three sub-patterns, all look like stalls but aren't:**

**D1. Long-cycle job with valid next_run_at.** A 6h-cycle job (e.g. SEO/Trend harvester with `0 */6 * * *`) had its last run 1.5 days ago because the schedule is `0 */6 * * *` and next_run falls in 6h intervals. User sees "1.5 days since last run" and assumes stopped. **Verify by reading `next_run_at`** — if it's a valid future timestamp, the scheduler is correct.

**D2. launchctl tracking failure.** `launchctl list | grep cron-runner` shows PID=- but cron-runner is actually running. **launchd cannot track detached processes** — even when cron-runner is alive and producing output every minute, launchctl reports PID=-. Don't conclude "stopped" from this alone. (Verified 2026-06-01: cron output 🟢 4 dirs + log 1-min tick + 3 dispatchers run, despite `launchctl list PID=-`.)

**D3. Output dir name mismatch.** Looking for `cron/output/integrations-board-dispatcher/` is empty, so you conclude the job is missing. Actually the job is registered as `kanban-dispatcher-integrations` (id `cb909be06e0e`) and outputs to `cron/output/cb909be06e0e/` — and that dir has plenty of recent files. **Always map job_name → job_id from jobs.json before checking output dirs.**

**Diagnostic — 2 hard evidence signals required (NOT launchctl list alone):**

```bash
# Signal 1: cron-runner.log "dispatchers run" timestamp
tail -5 ~/.drewgent/P6-prefrontal/logs/cron-runner.log
# If timestamp is 5min+ stale → really stopped (Pattern C)
# If timestamp is recent → system is alive

# Signal 2: latest cron output file mtime (across ALL jobs)
find ~/.drewgent/cron/output/*/ -name "*.md" -mmin -10
# If 1+ file modified in last 10min → at least one job is running
# If nothing in last 10min → really stopped (or all boards are empty queues)
```

**Soft evidence (NOT sufficient alone):**
- `launchctl list | grep ai.drewgent.cron-runner` PID=- → launchd may not track detached processes
- `last_status=ok` on all jobs → stale marker, not proof of recent run
- `last_run_at` older than 1 day on a 6h-cycle job → within cycle window, not stalled
- One specific output dir is empty → could be name mismatch (D3) or that specific job not running

**Verification fix (when in doubt — 90s):**
```python
# Force-due one specific job to verify scheduler is alive
import json
from datetime import datetime, timedelta
from pathlib import Path

JOBS = "/Users/drew/.drewgent/cron/jobs.json"
OUT = Path("/Users/drew/.drewgent/cron/output")

data = json.load(open(JOBS))
target = next(j for j in data['jobs'] if j['name'] == 'X Article Harvester')
target['next_run_at'] = (datetime.now() - timedelta(seconds=5)).isoformat()
json.dump(data, open(JOBS, 'w'), ensure_ascii=False, indent=2)

# Wait 75-90s for at least one tick
import time; time.sleep(75)

# Check the job's output dir (using job_id, NOT name)
d = OUT / target['id']
files = sorted(d.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
if files and (datetime.now() - datetime.fromtimestamp(files[0].stat().st_mtime)).total_seconds() < 90:
    print(f"✅ spawn confirmed: {files[0].name}")
else:
    print(f"⚠ no spawn — investigate further")
```

**Fix:** None needed — the system is healthy. Communicate to user:
- "The 6h cycle (SEO/Trend) scheduled next run at 6/2 00:00. Last run 1.5 days ago is within cycle window."
- "If you want immediate verification, patched `next_run_at` to (now-5s) and confirmed spawn within 75-90s."

---

## Step 3: Apply recovery

| Pattern | Recovery |
|---------|----------|
| A (next_run_at=null) | jobs.json patch above + wait 60s for tick |
| B (junk output) | Script-level fix: feed list + content guard |
| C (launchd) | `launchd-process-health-check` skill |
| D (false alarm) | No fix needed — communicate to user. Optionally force-due one job to verify (90s patch + wait). |

For Pattern A, after patch:
```bash
# Wait 60s for cron tick
sleep 60

# Verify: jobs.json next_run_at should be non-null and future-dated
python3 -c "import json; d=json.load(open('$HOME/.drewgent/cron/jobs.json')); [print(f'{j.get(\"name\",\"?\"):30} next={str(j.get(\"next_run_at\",\"\"))[:19] or \"NULL\"}') for j in d.get('jobs',[]) if j.get('enabled')]"

# Verify: output dir has new file from this tick
ls -lt ~/.drewgent/cron/output/*/ 2>/dev/null | head -5
```

---

## Step 4: Verification (3-Phase QA Gate)

Use the 3-phase QA gate pattern for any code change. Evidence goes in `~/.drewgent/P2-hippocampus/qa-evidence/{task_id}/`.

**Contract** (write before coding):
- acceptance criteria as bullet list
- task_id is uuid4 string
- phase: "contract"

**Micro** (write after each step):
- per-step verification with `verified: true/false`
- accumulated across turns (not single-step overwrites)

**Full** (write at end):
- `all_criteria_met: true/false` boolean
- per-criterion evidence array
- `out_of_scope: [...]` for items deliberately excluded

See `禁task_qa_gate.neuron` for full HP-3 specification. `skills/qa/qa-cycle/` provides the workflow template.

---

## Pitfalls

- **Don't trust `last_status=ok`** — stale marker, not proof of recent execution.
- **Don't trust `launchctl list PID=-` alone** — launchd cannot track detached processes. cron-runner can be alive and producing output every minute while `launchctl list` shows PID=-. Use `cron-runner.log` timestamp + cron output dir mtime as the 2 hard evidence signals (Pattern D).
- **Don't trust `cron/output/{job_name}/`** — dir name = job_id (12-char hex), not job name. Read `id` from jobs.json first.
- **Don't just re-enable the job** — if `next_run_at` is null, `enabled=false→true` won't trigger a tick.
- **Don't just patch jobs.json** — without restart, the in-memory cron loop may overwrite your patch on next save. Permanent fix is in `cron/jobs.py`.
- **Don't add a NEW entry to jobs.json and expect immediate execution** — the in-memory cron loop loaded jobs.json at process start. New entries added after that are invisible to the loop until process restart (or file-watcher-based reload). The `get_due_jobs()` recovery branch in `cron/jobs.py` will set `next_run_at` for null entries on next load_jobs(), but if the process never re-reads jobs.json, the entry is dormant forever. Verified 2026-06-01: kanban-maintenance added at 18:38, 19:30 patch (now-5s) didn't trigger spawn because in-memory state is from 18:08.
- **Don't confuse cron-runner scope with jobs.json scheduler** — `ai.drewgent.cron-runner` plist runs 3 board dispatchers (default/content/integrations) only, processing kanban board tasks. jobs.json `schedule.kind in {cron, interval}` entries (SEO/Trend/kanban-maintenance/cron-output-cleanup) are processed by a SEPARATE process (likely gateway internal scheduler). Two different in-memory states, two different reload triggers.
- **Don't mix A with B** — scheduler stuck vs script producing junk need different fixes. User often says "stopped" but means "producing bad output."
- **Don't apply Pattern A fix to `kind: 'once'` jobs** — only recurring jobs (`kind in {cron, interval}`).
- **Don't fire the Pattern A fix without first checking launchd is alive** — if cron_runner isn't running, jobs.json patch does nothing.
- **Don't over-engineer a fix for a 0-risk residual** — when in-memory state is stale on a new entry, the practical impact is "job doesn't run until process restart or 6/7 cycle." If the job is non-critical (e.g. weekly cleanup) and the user is doing a checkup, recommend (H4) terminate + log follow-up. The 5-min on-call may not be worth the gateway restart.

---

## Related

- `kanban-dispatcher-stalled` — parallel skill for kanban dispatcher
- `launchd-process-health-check` — for launchd-level issues
- `drewgent-runtime-checkup` — broader system checkup
- `P6-prefrontal/incidents/cron-jobs-stalled-20260601.md` — incident report (5 enabled jobs, ~36h)
- `P6-prefrontal/incidents/cron-job-failure-20260518.md` — related incident (double-run)
- `P6-prefrontal/incidents/cron-runner-launchd-detached-20260601.md` — 5/30 incident false alarm analysis (PID=- soft evidence, plist StartInterval=60 already configured)
- `cron/jobs.py` line 667-705 — `get_due_jobs()` recurring job recovery branch
- `禁task_qa_gate.neuron` — 3-phase QA gate specification
