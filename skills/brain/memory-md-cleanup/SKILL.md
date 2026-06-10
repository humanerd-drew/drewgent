---
name: memory-md-cleanup
description: Clean up Drewgent's persistent memory file (MEMORY.md) when it nears the 8K char cap. Identify resolved/one-time entries, preserve operational facts, verify after.
---

# MEMORY.md Cleanup

`~/.drewgent/P2-hippocampus/memories/MEMORY.md` hits 8K char cap when auto-accumulated + user entries pile up. Manual cleanup needed — auto-cleanup is NOT implemented (`growth-2026.md` "분기별 메모리 정리 자동화" is TODO).

## Trigger

- System prompt shows `[98% — 7,901/8,000 chars]` or similar near-cap status
- User says "메모리 정리" / "MEMORY.md 정리"

## Steps

### 1. Read current state
```python
content = open('~/.drewgent/P2-hippocampus/memories/MEMORY.md').read()
print(f'chars: {len(content)}, cap usage: {len(content)/8000*100:.1f}%, entries (§): {content.count(chr(167))}')
```

### 2. Check for concurrent writes
`MEMORY.md.lock` mtime 5분+ stale이 아니면 wait. 그 외 진행.

### 3. Classify entries
**Cut (resolved/one-time):**
- "follow-up" / "patched" / "fixed" / "완료" / "✅" 단어 등장
- 다른 entry가 미참조하는 historical event
- system prompt active docs (SELF_MODEL, KANBAN_INDEX, architecture-dataflow)에서 미참조

**Keep (operational/active):**
- system prompt active docs에서 참조되는 facts
- port / path / plist label / version / token / CF account 같은 operational numbers
- 다음 session에 적용될 trigger pattern (self-critique framing, cron infra, mock patterns)
- ongoing incident 핵심 findings (resolve되기 전)

### 4. Present options via mcp_clarify
H1: aggressive — cut all candidates (0 risk, max headroom)
H2: conservative — cut 1~2 largest만
H3: increase cap (8000→12000, config edit, 매 session inject 비용 증가)

User timeout → best judgement = **H1**. 0 risk, 가장 많은 buffer, follow-up으로 추가 trim 가능.

### 5. Write new file with mcp_write_file
- Line 1: `[YYYY-MM-DD cleanup: H{N}, removed {N} entries (~{chars} saved). Active entries preserved.]`
- Following: original format — entry line + `§` on its own line as separator

### 6. Fix stale cross-references
제거된 entry를 참조하는 warning이 다른 entry에 남아있으면 patch로 정리. 예: "⚠️ bot.py의 M2.7 호출은 6/1 follow-up patch로 M3 통일됨" — 제거된 follow-up entry를 가리키던 warning을 resolved 상태로 update.

### 7. Verify
```python
content = open('~/.drewgent/P2-hippocampus/memories/MEMORY.md').read()
assert len(content) < 8000, f'still over cap: {len(content)}'
print(f'OK: {len(content)} chars ({len(content)/8000*100:.1f}% of 8K cap)')
```

## Pitfalls

- **Operational facts 보존** — port, path, plist label, version, CF account, token reference. 다음 작업에 직접 영향. cut 대상 아님.
- **Cross-reference 무결성** — 한 entry의 warning이 제거될 다른 entry를 참조하면, 그 entry cut 후 warning이 misleading. step 6으로 patch.
- **Cleanup note는 첫 줄** — 미래 agent가 "이게 뭐지?" 안 하도록.
- **§ separator 형식 유지** — original format (entry + blank line with §) 보존. 다른 separator 쓰면 injection pattern 깨질 수 있음.
- **.lock file 동시성** — mtime 5분+ stale 아니면 wait. system이 write 중일 수 있음.
- **H1 추천 시 근거** — 0 risk, manual cleanup만 가능 (auto 안 됨), buffer 확보. cap 늘리기는 비용 증가 + 또 차면 반복.

## Verification

After cleanup:
- `len(content) < 8000` (under cap) — required
- `len(content) < 6400` (80% — buffer for next accumulation) — recommended
- Active 9~10 entries 보존 (system prompt에서 보이는 핵심 facts)
- Cleanup note 첫 줄
- Cross-reference warning 정리됨

## Related

- `~/.drewgent/P2-hippocampus/memories/SCHEMA.md` — memory schema
- `growth-2026.md` "분기별 메모리 정리 자동화" — auto-cleanup TODO (현재 manual only)
- `cron-jobs-stalled` — pattern parallel (stalled state recovery via read+identify+fix)
