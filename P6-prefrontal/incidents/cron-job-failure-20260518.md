# Incident Report — Cron Job Failure Analysis
## 2026-05-18

**Date**: 2026-05-18 02:20 KST
**Severity**: Medium
**Status**: Resolved

## Incident

Cron job들이 제대로 작동하지 않음 (Trend Harvester disabled, SEO over-execution).

## Root Cause

1. **Trend Harvester**: `enabled=false`로 수동 비활성화됨 (아마 조작 실수)
2. **SEO Article Harvester**: gateway 재시작 시 stale run detection의 fast-forward grace(2시간)가 일부 스케줄 미스 케이스를 catch-up 처리
3. KST croniter 해석은 실제로 올바름 (버그 아님)

## Fix Applied

1. Trend Harvester 재활성화: `enabled=true`, `state=scheduled`, `next_run=2026-05-18T06:00:00+09:00`
2. SEO next_run_at 확인: `2026-05-18T06:00:00+09:00` (정상)
3. KST croniter는 KST-aware datetime 입력 시 올바르게 동작 → 별도 코딩 수정 불필요

## Current Job State (2026-05-18 02:20 KST)

| Job | enabled | state | next_run_at | last_run_at |
|-----|---------|-------|-------------|-------------|
| SEO Article Harvester | true | scheduled | 2026-05-18T06:00:00+09:00 | 2026-05-18T01:48:21+09:00 |
| Trend Harvester | true | scheduled | 2026-05-18T06:00:00+09:00 | 2026-05-18T01:59:21+09:00 |

## Schedule

- `0 */6 * * *` → KST 06:00, 12:00, 18:00, 00:00

## Related

- [[P6-prefrontal/plans/growth-2026]] — growth plan reference
- [[P4-cortex/growth/INTEGRATION_PROTOCOL]] — integration protocol