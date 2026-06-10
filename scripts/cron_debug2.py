import sys
sys.path.insert(0, '/Users/drew/.drewgent/source/drewgent-agent')

from cron.jobs import get_due_jobs, load_jobs, _ensure_aware
from drewgent_time import now as drewgent_now
from datetime import datetime
import copy

print("=== Cron Debug ===")
print(f"drewgent_now: {drewgent_now()}")
print()

jobs = load_jobs()
for j in jobs:
    print(f"Job: {j['name']}")
    print(f"  enabled={j['enabled']}, state={j['state']}")
    print(f"  next_run_at (raw): {j['next_run_at']}")
    print(f"  last_run_at: {j['last_run_at']}")
    if j['next_run_at']:
        nrt = _ensure_aware(datetime.fromisoformat(j['next_run_at']))
        print(f"  next_run_at (aware): {nrt} (tz={nrt.tzinfo})")
        diff = (drewgent_now() - nrt).total_seconds()
        print(f"  now - next_run: {diff:.0f}s ({diff/3600:.1f}h)")
    print()

print("=== get_due_jobs() result ===")
due = get_due_jobs()
print(f"Due jobs: {len(due)}")
for job in due:
    print(f"  - {job['name']} (next={job['next_run_at']})")
print()

# Simulate what happens with _compute_grace_seconds
from cron.jobs import _compute_grace_seconds
for j in jobs:
    if j['enabled'] and j['next_run_at']:
        grace = _compute_grace_seconds(j['schedule'])
        print(f"{j['name']}: grace={grace}s ({grace/3600:.1f}h)")