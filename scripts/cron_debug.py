import sys
sys.path.insert(0, '/Users/drew/.drewgent/source/drewgent-agent')

from drewgent_time import now as drewgent_now
from cron.jobs import _ensure_aware
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

now = drewgent_now()
print('drewgent_now():', now)
print('Type:', type(now))
print('tzinfo:', now.tzinfo)

# Test _ensure_aware
test_dt = datetime(2026, 5, 18, 2, 0, 0)
aware = _ensure_aware(test_dt)
print()
print('_ensure_aware(naive 2026-05-18 02:00:00):', aware)
print('tzinfo:', aware.tzinfo)

# Test comparison
print()
print('now <= now:', now <= now)
print('type check:', type(now) == type(aware))
print('isoformat:', now.isoformat())

# Test the _ensure_aware function directly
from cron.jobs import _ensure_aware
print()
print('Direct call _ensure_aware(now):', _ensure_aware(now))