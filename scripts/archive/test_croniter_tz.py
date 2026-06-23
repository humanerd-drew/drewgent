import sys
sys.path.insert(0, '/Users/drew/.drewgent/source/drewgent-agent')

# Check how croniter is imported in jobs.py: from croniter import croniter
from croniter import croniter as CroniterClass
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
now_kst = datetime(2026, 5, 18, 2, 0, 0, tzinfo=KST)

print(f"CroniterClass type: {CroniterClass}")

# With KST-aware datetime (no tz param)
cron = CroniterClass('0 */6 * * *', now_kst)
next_time = cron.get_next(datetime)
print(f'KST-aware, no tz param: {next_time} (tzinfo={getattr(next_time, "tzinfo", None)})')

# Try with tz='Asia/Seoul' kwarg
try:
    cron2 = CroniterClass('0 */6 * * *', now_kst, tz='Asia/Seoul')
    next_time2 = cron2.get_next(datetime)
    print(f'tz=Asia/Seoul kwarg: {next_time2} (tzinfo={getattr(next_time2, "tzinfo", None)})')
except TypeError as e:
    print(f'tz kwarg FAILED: {e}')

# What croniter 2.x signature looks like
import inspect
sig = inspect.signature(CroniterClass.__init__)
print(f'CroniterClass.__init__ signature: {sig}')