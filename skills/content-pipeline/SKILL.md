---
title: Content Pipeline
name: content-pipeline
description: Drewgent content pipeline - editorial topic selection, draft writing, Korean humanization, review-ready publishing
type: document
space: concept
tags: [concept]
created: 2026-05-22
updated: 2026-06-02
links:
  - "[[P1-limbic/persona/writing-style-guide]]"
  - "[[P2-hippocampus/kanban/KANBAN_INDEX]]"
  - "[[P4-cortex/growth/INTEGRATION_PROTOCOL]]"
  - "[[skills/content-writer]]"
  - "[[skills/kanban-worker]]"
---


# Content Pipeline Skill

Aggregator pattern: 이 skill은 content를 수집하지 않음. 
이미 실행 중인 cron job들의 output을 읽고, topic을 선별해서 kanban task로 만든다.

Draft 작성은 `content-writer` skill이 별도로 담당한다.

## Editorial North Star

humanerd.kr은 자동 뉴스 블로그가 아니라 **Drew가 AI, 도구, 코드, 글쓰기, 시스템 설계를 통해 어떻게 사고하고 만드는지를 보여주는 개인 작업실**이다.

공개 후보가 되는 글은 반드시 아래 등식을 만족해야 한다:

```
public-worthy content = 기록 + 해석 + 재사용 가능한 통찰
```

단순한 수집 결과, 출시 소식, 활동 보고, 링크 요약은 공개 draft로 만들지 않는다. 그런 항목은 `raw` 또는 `archive`에 남겨도 되지만 content board task로 승격하지 않는다.

## Editorial State Machine

```
raw source
  ↓ editorial_screen
candidate
  ↓ enough Drew-angle + reader value
draft              → status: draft      → Quartz EXCLUDE
  ↓ human review
published          → status: published  → Quartz INCLUDE
  ↓ rework needed
in_review          → status: in_review  → Quartz EXCLUDE
  ↓ no longer useful
archived           → status: archived   → Quartz EXCLUDE
```

`content-pipeline`은 `candidate → draft`까지만 자동화한다. `published` 전환은 humanerd가 직접 검토한 뒤에만 한다.

## Trigger

Cron job: 3시간마다 (`0 */3 * * *` KST)
- KST 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00, 00:00

---

## Phase 1: Source Tally (Aggregator Mode)

직접 수집하지 않음. 기존 cron output 3개를 읽음.

### 1. Trend Harvester → analyzed/keep

```
위치: ~/.drewgent/P4-cortex/growth/trend-harvester/analyzed/keep/
형식: JSON (item{name,description,url,source}, total_score, decision)
필터: decision == "keep", scored_at 최근 48시간
선별: 상위 5개 → topic 후보
```

```bash
# 실행
ls -t ~/.drewgent/P4-cortex/growth/trend-harvester/analyzed/keep/*.json | head -10
# → 상위 10개 JSON 파일 경로
```

JSON 파싱:
```python
import json, pathlib
keep_dir = pathlib.Path("~/.drewgent/P4-cortex/growth/trend-harvester/analyzed/keep")
files = sorted(keep_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
for f in files:
    d = json.load(open(f))
    item = d["item"]
    print(f"- {item['name']}: score={d['total_score']:.1f}, source={item['source']}")
```

### 2. SEO Harvester → report.json

```
위치: ~/.drewgent/P2-hippocampus/knowledge/seo-articles/report.json
형식: JSON (articles[{title,url,keyword,score}])
선별: score ≥ 0.7, keyword 명확한 것 상위 3개
```

```bash
cat ~/.drewgent/P2-hippocampus/knowledge/seo-articles/report.json | python3 -c "
import json, sys
d = json.load(sys.stdin)
articles = d.get('articles', [])
scored = [(a, a.get('score', 0)) for a in articles if a.get('score', 0) >= 0.7]
for a, s in sorted(scored, key=lambda x: -x[1])[:5]:
    print(f'- {a[\"title\"]} | keyword={a.get(\"keyword\",\"?\")} | score={s:.2f}')
"
```

### 3. Activity Logger → kanban tasks (last 24h)

```
위치: drewgent_tasks.db (board=default, trigger_source=activity_logger)
쿼리: SELECT title, body FROM tasks
      WHERE trigger_source = 'activity_logger'
      AND status = 'completed'
      AND created_at > datetime('now', '-24 hours')
선별: conversation insight 관련 것 (implement/build/create 등 포함) 상위 3개
```

```python
import sqlite3, pathlib
db = pathlib.Path.home() / ".drewgent/P2-hippocampus/kanban/state/drewgent_tasks.db"
conn = sqlite3.connect(str(db))
rows = conn.execute("""
    SELECT title, body FROM tasks
    WHERE trigger_source = 'activity_logger'
    AND status = 'completed'
    AND created_at > datetime('now', '-24 hours')
    ORDER BY created_at DESC
    LIMIT 10
""").fetchall()
for title, body in rows:
    print(f"- [{title}] {body[:100] if body else ''}")
conn.close()
```

---

## Phase 2: Topic Selection

세 source에서 합산 최대 **3개 topic** 선별. 기본값은 **0개**다. 충분히 좋은 후보가 없으면 `[SILENT]`가 올바른 결과다.

| 출처 | Topic 유형 | Prefix |
|------|-----------|--------|
| Trend Harvester | `[draft-trend]` | 기술 동향 |
| SEO Harvester | `[draft-seo]` | 검색 최적화 |
| Activity Logger | `[draft-conversation]` | 작업 insight |

### Editorial Gate

각 후보는 0~2점으로 평가한다. 총점 **7점 이상**만 content board task로 만든다.

| 기준 | 0점 | 1점 | 2점 |
|---|---|---|---|
| Drew-angle | Drew의 작업/판단과 무관 | 약하게 연결됨 | Drew의 작업 방식, 도구, 포트폴리오와 직접 연결 |
| Insight | 요약/소식 수준 | 관찰은 있으나 일반화 약함 | 독자가 재사용할 수 있는 판단 기준이 있음 |
| Evidence | 출처/맥락 불충분 | 기본 출처 있음 | 출처 + 실제 작업 맥락 또는 artifact 있음 |
| Portfolio value | 지나가는 메모 | blog note로는 가능 | portfolio/insight 축에 남겨도 가치 있음 |
| Specificity | 일반론, 키워드 나열 | 어느 정도 구체적 | 구체적 사례, 프로젝트, 도구, 실패/결정 포함 |

**자동 reject 규칙:**
- 단순 제품/도구 출시 요약
- Drew가 직접 써본 흔적이나 판단이 없는 외부 트렌드
- SEO keyword만 좋고 관점이 없는 주제
- 이미 최근 30일 내 같은 주장으로 다룬 주제
- "AI가 대단하다/위험하다" 수준의 일반론

**source별 우선순위:**
1. `[draft-conversation]`: Drew의 작업에서 나온 insight면 우선.
2. `[draft-trend]`: Drewgent, humanerd-site, agent tooling, creative coding, publishing system과 연결될 때만.
3. `[draft-seo]`: 검색 유입보다 사이트 정체성에 맞는 evergreen 주제일 때만.

**최대 3개.** 품질 > 수량.

---

## Phase 3: Kanban Task Creation

선별한 topic마다 content board에 task 생성:

```python
kanban_create(
    title=f"[draft-{type}] {topic_title}",
    body=f"""## Topic
{topic_description}

## Editorial Decision
- score: {score}/10
- publish_intent: blog | insight | portfolio | archive
- Drew-angle: {why_this_matters_to_drew}
- reusable_insight: {one_sentence_reader_value}
- reject_if_missing: if this cannot show Drew's judgment, archive instead of drafting

## Content Source
- source: {source_name}
- collected_at: {YYYY-MM-DD HH:MM}

## 글쓰기 방향
- 톤: writing-style-guide.md hybrid approach 따르기
  - 톤: 휴머너드 말투 (긴 플로우, Bold 강조, "당신" 직접호칭, 1인칭 "저"/"나")
  - SEO: 기존 방식 유지 (aliases, 해시태그, SEO 키워드 섹션)
- 제목: 질문 또는 provocative statement
- 도입: Bold 훅 한 문장 → 10~20문장 플로우

## Frontmatter
```
title: {title}
type: document
space: concept
tags: [blog, {category}]
aliases: ['/blog/{slug}']
created: {YYYY-MM-DD}
status: draft
links:
  - "[[P1-limbic/persona/writing-style-guide]]"
```

## 작성 시 반드시 확인할 것
1. forbidden.patterns grep → 0건
2. Bold 섹션 강조 2~4개 존재
3. "당신" 직접호칭 + 1인칭 "저"/"나" 포함
4. 본문 날짜 (X월 X일) 없음
5. SEO 키워드 3~7개, 해시태그 8~14개
6. aliases in frontmatter
7. 기록 + 해석 + 재사용 가능한 통찰이 모두 있음
""",
    board="content",
    trigger_source="content_pipeline",
    priority=1,
    idempotency_key=f"{YYYY-MM-DD}-{slug}",
)
```

**Title prefix 규칙:**
- 트렌드 기반: `[draft-trend]`
- SEO 키워드 기반: `[draft-seo]`
- 대화/경험 기반: `[draft-conversation]`

**Draft 파일 위치:** `memories/insights/YYYY-MM-{slug}.md`
- 예: `memories/insights/2026-05-gemini-cli-shutdown.md`
- Obsidian에서 직접 확인 가능: `~/.drewgent/P2-hippocampus/memories/insights/`
- humanerd-site의 `/blog/{slug}` 또는 `/blog/YYYY/{slug}` 경로로 Quartz에 의해 공개됨. Raw monthly log는 공개하지 않음.

---

## Phase 4: Draft Writing (Drewgent Worker)

kanban-dispatcher가 ready task를 worker에 배분.

### 4-1. Task Claim
```python
kanban_claim(task_id, ttl_seconds=1800)
```

### 4-2. Research (if needed)
Topic 관련 정보가 충분하지 않으면 web search로 보강. 출처: URL, source-date 명시.

### 4-3. Draft 작성
`memories/insights/`에 Markdown 파일 생성.

**writing-style-guide.md 적용 (hybrid approach):**
- **톤**: 휴머너드 말투 (긴 플로우, Bold 강조, "당신" 직접호칭, 1인칭 "저"/"나")
- **SEO**: 기존 방식 유지 (aliases, 해시태그, SEO 키워드 섹션, frontmatter)

** ReefWatch 스타일 글 구조 (기술 심화 글용):**

 ReefWatch 아티클 (dev.to/siiddhantt/building-reefwatch)의 7가지 핵심 포인트를 적용한 템플릿:

1. **문제 프레이밍** — 공감되는 상황부터 시작. Bad: "이 글은 ~에 대해 알아봅니다." Good: 구체적 경험/상황 → 문제 발견
2. **강한 주장 (Bold)** — 기존 방식의 한계를 Bold로. 예: "But that is not triage. That is a polished to-do list."
3. **디자인 제약 (emphasis)** — 핵심 원칙을 emphasis로 강조. 예: "The design constraint from the start was simple: no evidence, no answer."
4. **One-sentence 요약 (blockquote)** — 한 문장으로 전체 정의. 블록쿼트로 시각적으로突出
5. **이미지 배치 — "설명 전에 보여주기"** — 개념 설명 직전에 시각 자료. Obsidian: `![[image.png|너비]]`
6. **"What This Guide Builds" 테이블** — 글 도입부에 outcomes를 테이블로 제시. 독자가 첫 스캔에서 판단
7. **Build Path / 구조 테이블** — 순서를 시각화. 다단계 컴포넌트 설명에 효과적

** ReefWatch 확장 템플릿:**

```
# {제목: 질문 또는 provocative statement}

{커버 이미지: ![]() — 16:9 landscape, 글 전체를 대표하는 시각적}

**{문제 프레이밍 — Bold 훅}**
{구체적 상황 묘사. 독자가 공감할 수 있는 경험/관찰}

**{강한 주장 — Bold 한 문장}**
{기존 방식의 한계 또는 문제의 본질}

{분석 플로우: 10~20문장. 경험 → 관찰 → 분석}

**{중간 핵심 강조 — Bold}**
{구체적 해결책 또는 인사이트}

{이어지는 플로우}

> {One-sentence 정의 — 블록쿼트로 한 문장 요약}

## What This Guide Builds

| 당신은 이것을 할 수 있게 된다 |
|---|
| {outcome 1} |
| {outcome 2} |
| {outcome 3} |

{구현/설계 섹션}

{이미지: ![]() — 플로우/아키텍처 다이어그램, 설명 전에 배치}

{이어지는 설명}

**{마무리 강조 — Bold 한 문장}**
{결론 또는 행동 유도}

---

**이런 분들께 추천**
- {타겟 독자}

**SEO 키워드**: {키워드 3~7개}
**#해시태그**: #{태그1} #{태그2} ...
```

** ReefWatch 스타일 글쓰기 순서:**
1. 문제 상황 묘사 (Opening) — Bold 훅
2. 기존 방식의 한계 (Bold 주장)
3. 해결 원칙 (emphasis)
4. 구체적 구현/해결책
5. 구조/순서 정리 (표 또는 트리)
6. 마무리 행동 유도

**기술 심화 글이 아닌 경우 (트렌드/단편):** 확장 템플릿 대신 기본 구조(문제 프레이밍 → Bold 주장 → Bold 마무리)만 사용. "What This Guide Builds" 테이블은 생략 가능.

---

기술 심화 글의 경우, Drewgent의 내장 이미지 생성 도구로 커버 이미지나 다이어그램을 만들 수 있음.

Drewgent는 FAL.ai FLUX 2 Pro 모델 + Clarity Upscaler (2x 자동 업스케일)를 사용함.

**사용 방법:**
```python
# image_generate tool을 호출 (Drewgent toolset에 이미 등록됨)
# 파라미터: prompt, aspect_ratio, num_inference_steps, guidance_scale, num_images
# aspect_ratio: "landscape" (16:9), "square" (1:1), "portrait" (9:16)
```

**용도별 생성 가이드:**

| 용도 | 비율 | 프롬프트 스타일 |
|------|------|---------------|
| 커버 이미지 | `landscape` (16:9) | 글 제목을 시각적으로 표현, 미니멀한 컴포지션 |
| 아키텍처 다이어그램 | `landscape` (16:9) | 시스템 구조를 플로우차트로, 다이어그램 스타일 |
| 비교 이미지 | `square` (1:1) | Before/After, A vs B 등의 시각화 |
| UI 모의 | `landscape` (16:9) | 깔끔한 앱/대시보드 스크린샷 스타일 |

**이미지 프롬프트 작성 원칙:**
1. **영문으로 작성** — FLUX 2 Pro는 영문에 최적화
2. **스타일 지정** 포함 — "minimalist diagram", "dark mode UI screenshot", "clean architecture diagram"
3. **글의 색상에 맞추기** — humanerd.kr 색상 참고: `--humanerd-accent: #7b5f3d` (amber), `--humanerd-paper: #fbfaf7` (warm off-white)
4. **최대 50단어** — 장황한 프롬프트보다 간결한 설명이 결과가 좋음

**예시 이미지 프롬프트:**
```
"AI agent investigating production incident across multiple tools — clean dark mode dashboard with timeline, evidence cards, and correlation graph. Modern minimal UI, amber accents on dark background."
```

**생성 후 Obsidian에 저장 + 배치:**
```bash
# FAL.ai 결과 URL을 Obsidian assets에 다운로드
# 저장 위치: memories/insights/images/{slug}-{n}.png

# Obsidian embed (설명 전에 배치 — ReefWatch 규칙)
![[images/{slug}-{n}.png|너비]]

# 너비 조절: |300, |400, |600 등 (픽셀)
# 예: ![[images/reefwatch-flow-1.png|600]]
```

**이미지 배치 원칙 — "설명 전에 보여주기":**
1. 섹션 내에서 **핵심 개념을 먼저 설명하면** 독자가 그림을 보면서 내용을 자연스럽게 이해함
2. 다이어그램 → 설명 순서: "![[flow-diagram.png]]" → "위 그림은 X의 구조를 보여준다"
3. 스크린샷 → 설명 순서: "![[workspace-screenshot.png]]" → "실제 워크스페이스는 이렇다"
4. 비교 이미지 → 설명 순서: "![[before-after.png]]" → "바꾸기 전/후 차이다"

**기술 심화 글의 이미지 전략:**
- **1개 필수** — 커버 이미지 또는 주요 다이어그램 (글 도입부 근처)
- **1~2개 선택** — 아키텍처 플로우, 비교 시각화, UI 모의 등
- **너비 조절** — Obsidian embed의 `|300` `|400` `|600`으로 본문 폭에 맞춤
- **너무 많지 않기** — 3개 이상이면 집중력 분산, 1~2개가 이상적

**예시 — 트렌드 글:**
```
# AI 에이전트, 증거 없이 답하지 마라

**문제 프레이밍**
당신이 AI 에이전트를 구축한다고 치자. 모니터링은 PagerDuty에서 하고, 에러는 Sentry에 있고, 배포는 GitHub Actions에서 됐다. 문제는 여러 곳에 흩어져 있는데, 일반 챗봇은 "Sentry에서 에러를 확인해보세요"라고만 말할 수 있다.

**강한 주장**
하지만 그것은 triage가 아니다. 그건 정돈된 할 일 목록일 뿐이다.

**디자인 제약**
원칙은 단순했다: 증거 없이는 답하지 않는다.

**해결책**
이 원칙을 지키기 위해 Coral을 데이터 플레인으로 사용하면...
```

### 4-4. Language Polish — DeepSeek 한글 윤문

AI 티 제거 + 한글 교정을 DeepSeek로 윤문. Phase 4-3 draft 완성 후 실행.

**Vault에서 API 키 조회:**
```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.drewgent"))
from modules.secrets_vault import SecretsVault
vault = SecretsVault()
api_key = vault.resolve("vault_9fa1b5bb")  # DEEPSEEK_API_KEY
```

**실행:**
```bash
DEEPSEEK_API_KEY="<vault에서 조회한 키>" \
python3 ~/.drewgent/P4-cortex/scripts/humanize_korean.py \
    ~/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md \
    ~/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}_polished.md
```

**스크립트:** `~/.drewgent/P4-cortex/scripts/humanize_korean.py`
- DeepSeek API (deepseek-chat) 호출
- AI 패턴 제거: ~에 대해, ~라고 생각한다, 입니다/습니다 ending 과잉, "//" 스타일
- Markdown formatting 보존
- 원본 손상 없음 (별도 파일로 출력)
- temperature=0.3 (자연스러운 variation, 사실 왜곡 방지)

**실행 후:**
1. `_polished.md` 파일이 생성됨 → 원본과 교체 (rename)
2. 이후 forbidden.patterns grep 실행

**Vault ref:**
| 서비스 | ref | 확인 |
|--------|-----|------|
| DeepSeek API | `vault_9fa1b5bb` | `vault.list("api_key")`로 확인 |

Vault에 키 등록 (최초 1회):
```python
from modules.secrets_vault import vault
vault.register("DEEPSEEK_API_KEY", os.getenv("DEEPSEEK_API_KEY", "sk-your-key-here"), category="api_key")
```

### 4-5. Language Polish 체크리스트 (DeepSeek 윤문 후)

DeepSeek 윤문을 사용할 경우 이 단계는 **skip** — DeepSeek가 처리함.

DeepSeek를 사용하지 않는 경우에만 실행:
```python
# 1. 불필요한 한문/한자/일어 → 한글 변환
# 한자: "倫理" → "윤리", "機能" → "기능", "存在" → "존재"
# 한문: "不可以" → "불가", "，要注意" → "주요"
# 일어: "ceras" → 없음 (외래어として以外), "ニーズ" → "니즈"

# 2. 비문检查 — 읽기 어려운 문장 reformulate
# 3. 오탈자 자동 수정
# 4. writing-style-guide forbidden.patterns 재확인
```

**Polish 체크리스트 (DeepSeek 윤문 후):**
```
[ ] humanize_korean.py 실행 완료
[ ] _polished.md → 원본 파일로 rename
[ ] forbidden.patterns grep → 0건
```

**원문 예시 (수정 전):**
> 이 功能은 您的 жизнь에 매우 重要합니다不可以

**수정 후 (DeepSeek):**
> 이 기능은 당신의 삶에 매우 중요합니다

---

### 4-5. DeepSeek Korean Humanization

|MiniMax-M3의 한글 출력质量问题 + 일반적인 AI 글특성(형식적, 비문장적)을 해결하기 위해, DeepSeek를使った 윤문 단계를 실행:

```python
# deepseek_humanize.py 실행
import subprocess, sys
result = subprocess.run([
    sys.executable,
    "~/.drewgent/scripts/deepseek_humanize.py",
    "--input", draft_file,
    "--output", f"{draft_file}.tmp"
], capture_output=True, text=True)
if result.returncode == 0:
    # 윤문 결과를 원본에 적용
    import shutil
    shutil.move(f"{draft_file}.tmp", draft_file)
    print("DeepSeek humanization: DONE")
else:
    print(f"DeepSeek humanization: FAILED — {result.stderr[:200]}")
```

**실행 전 조건:**
- Draft 파일이 존재하고 500바이트 이상
- DeepSeek API key가 vault에 등록됨 (`vault_948a246f`)

**DeepSeek Humanization 체크리스트 (실행 후 출력):**
```
DeepSeek Humanization: DONE
  Input chars: N
  Output chars: N
  Changes: N sentences modified
  Hanyang detection: N hanja/haneo converted
PASS
```

**실패 시:** 원본 draft 파일 그대로 유지 (작업 실패가 아님). Phase 5로 진행.

### 4-6. Kanban Complete
```python
draft_file = f"/Users/drew/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md"
kanban_complete(
    task_id,
    result=f"Draft file: {draft_file}",
    summary=f"Draft created: {title}",
    metadata={"draft_path": f"P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md"}
)
```

**result에는 반드시 절대경로 포함** — Phase 5 Periodic Delivery의 SQLite regex가 `~` 또는 `/Users/drew/`로 시작하는 경로를 추출함.
Phase 5 regex: `r'memories/insights/(20\d{2}-\d{2}-[^.]+\.md)'` 또는 `r'/Users/drew/.drewgent/P2-hippocampus/memories/insights/(20\d{2}-\d{2}-[^.]+\.md)'`

---

## Phase 5: Humanerd Review

### Review Steps (humanerd)
1. Obsidian에서 `memories/insights/` 확인
2. draft 파일 열기
3. Editorial Gate 재확인:
   - Drew-angle이 실제로 드러나는가?
   - 독자가 가져갈 판단 기준이 있는가?
   - 단순 트렌드 요약이 아니라 해석이 있는가?
   - 6개월 뒤에도 portfolio/insight 기록으로 의미가 있는가?
4. 오탈자·내용 수정
4. frontmatter 확인:
   - 통과: `status: draft` → `status: published`
   - 보류: `status: draft` → `status: in_review`
   - 폐기: `status: draft` → `status: archived`
   - `publish_date: {YYYY-MM-DD}` 추가
5. Quartz rebuild (자동 또는 수동)

---

## Quality Gate (writer용)

Draft 작성 완료 시 확인:
1. **Language Polish** (4-4 단계) 먼저 수행 — 한자/한문/일어→한글 변환, 비문 수정
2. **forbidden.patterns** grep → 0건
3. **Bold** 섹션 강조 2~4개 존재
4. **"당신"** 직접호칭 + 1인칭 "저"/"나" 포함
5. **본문 날짜** 없음 (X월 X일 금지)
6. **출처 없는 주장** 없음 (모르면 "확인 필요" 표기)
7. **SEO 키워드** 3~7개, **해시태그** 8~14개
8. aliases in frontmatter (`/blog/{slug}`)
9. **Editorial Gate**: Drew-angle + reusable insight + specificity 통과

**Polish 체크리스트 출력 예시:**
```
Language Polish: DONE
  한자→한글: 3건 변환
  한문 표현: 1건 변환
  일어 외래어: 0건
  비문 수정: 2건
Forbidden: 0건
Bold sections: 3
Direct address ("당신"): YES
1인칭 ("저"/"나"): YES
Dates in body: 0
SEO keywords: 5
해시태그: 11
aliases: /blog/gemini-cli-shutdown
PASS
```

```
## Content Pipeline — YYYY-MM-DD HH:MM KST

Topics selected: N

| # | Source | Topic | Task ID | Draft File |
|---|--------|-------|---------|------------|
| 1 | trend | {title} | {id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md |
| 2 | seo | {title} | {id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md |
| 3 | conversation | {title} | {id} | — (in progress) |

Worker 배분: kanban-dispatcher-content가 5분마다 ready task를 worker에 배분
Review at: Obsidian → P2-hippocampus → memories → insights
```

### Phase 3 완료 Delivery (Phase 3 후 즉시 출력)

```
## Content Pipeline — YYYY-MM-DD HH:MM KST

Topics selected: N (task created)

| # | Source | Topic | Task ID | Draft File |
|---|--------|-------|---------|------------|
| 1 | trend | {title} | {id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md |
| 2 | seo | {title} | {id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{YYYY-MM}-{slug}.md |
| 3 | conversation | {title} | {id} | — (in progress) |

Worker 배분: kanban-dispatcher-content가 5분마다 ready task를 worker에 배분
Review at: Obsidian → P2-hippocampus → memories → insights
```

Topics selected=0이면 `[SILENT]`.

---

### Phase 3 Periodic Delivery (매 cron tick마다, 완료된 task 확인)

content-pipeline cron job이 매 실행마다 content board의 **completed task**를 조회해 draft 파일 위치를 보고:

```python
import sqlite3, os, re, pathlib, glob

DB = os.path.expanduser("~/.drewgent/P2-hippocampus/kanban/state/drewgent_tasks.db")
INSIGHTS = os.path.expanduser("~/.drewgent/P2-hippocampus/memories/insights/")
conn = sqlite3.connect(DB)
rows = conn.execute("""
    SELECT id, title, body, completed_at
    FROM tasks
    WHERE trigger_source = 'content_pipeline'
      AND board = 'content'
      AND status = 'completed'
      AND completed_at > datetime('now', '-72 hours')
    ORDER BY completed_at DESC
""").fetchall()
conn.close()

drafts = []
for task_id, title, body, completed_at in rows:
    draft_path = "— (path not recorded)"
    if body:
        # body: "Draft file: ~/.drewgent/P2-hippocampus/memories/insights/2026-05-{slug}.md"
        m = re.search(r'P2-hippocampus/memories/insights/(\d{4}-\d{2}-[^/]+\.md)', body)
        if m:
            draft_path = os.path.join(INSIGHTS, m.group(1))
            if not os.path.exists(draft_path):
                date_prefix = m.group(1)[:7]
                matches = glob.glob(os.path.join(INSIGHTS, f"{date_prefix}-*.md"))
                draft_path = max(matches, key=os.path.getmtime) if matches else f"~/.drewgent/P2-hippocampus/memories/insights/{m.group(1)}"
    drafts.append((title, task_id, draft_path))
```

**완료된 task가 있으면这份 delivery 출력:**

```
## Content Pipeline Update — YYYY-MM-DD HH:MM KST

Draft files ready for review:

| # | Topic | Task ID | Draft File |
|---|-------|---------|------------|
| 1 | {title} | {id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{filename}.md |
| 2 | {title} | {id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{filename}.md |

Review at: Obsidian → P2-hippocampus → memories → insights
```

완료된 task가 없으면 (completed_at이 72시간 내 없으면) `[SILENT]`.

```
## Content Pipeline — YYYY-MM-DD HH:MM KST

Topics selected: N

| # | Source | Topic | Task ID | Draft File |
|---|--------|-------|---------|------------|
| 1 | trend | {title} | {task_id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{filename}.md |
| 2 | seo | {title} | {task_id} | /Users/drew/.drewgent/P2-hippocampus/memories/insights/{filename}.md |
| 3 | conversation | {title} | {task_id} | — (in progress) |

Review at: Obsidian → P2-hippocampus → memories → insights
```

Draft File 列은 task body에서 `P2-hippocampus/memories/insights/` 경로를 추출 — 있으면 절대경로로 표시, 없으면 "— (in progress)" 표시.

---

## Phase 6: Publishing (Quartz Auto-Deploy — No Git)

### Publishing Flow

Obsidian에서 `status: published`로 바꾸면 → fswatch가 vault 변경 감지 → quartz build → wrangler pages deploy → humanerd.kr 게시 (약 10초 내외)

```
humanerd (Obsidian에서 draft 편집)
  → frontmatter: status: published, publish_date: YYYY-MM-DD
  → fswatch가 vault 파일 변경 감지 (LaunchAgent 자동 실행)
    → 5초 debounce 후 quartz build
      → wrangler pages deploy public/ --project-name=humanerd-site
        → Cloudflare Pages에 directory deploy (git 불필요)
          → humanerd.kr 게시
```

**wrangler pages deploy는 git 없이 directory를 직접 Cloudflare Pages에 올립니다.**
git repo 아니어도 동작, git commit 불필요.

### Infrastructure

| Component | Details |
|-----------|---------|
| fswatch LaunchAgent | `com.drewgent.quartz-fswatch` (PID 5247 ✅) |
| fswatch script | `~/.drewgent/P4-cortex/scripts/quartz-fswatch.sh` |
| Watched dirs | `memories/insights`, `P4-cortex/growth`, `P4-cortex/knowledge`, `humanerd-site/content` |
| Debounce | 5초 (변경 후 5초内有 추가 변경이면 다시 5초 대기) |
| Build | `cd humanerd-site && npx quartz build --concurrency=4` |
| Deploy | `wrangler pages deploy public/ --project-name=humanerd-site` (git 불필요) |
| Deploy LaunchAgent | `com.drewgent.quartz-deploy` (runs on-demand via wrapper script) |
| CF Account ID | `dc0199b6b6c27bc9bb2f3201d47cb643` |
| CF Project | `humanerd-site` |
| Site URL | `https://humanerd.kr` |

### LaunchAgent States

```
fswatch:
  5247  running  com.drewgent.quartz-fswatch   ← vault 변경 감지
  63582 running  com.drewgent.quartz-deploy    ← (KeepAlive, 필요시 실행)

humanerd.kr 실시간 게시 상태:
  vault 파일 변경 → fswatch 감지 → 5초 debounce → quartz build → wrangler deploy → ~3초 후 게시
```

### Managing LaunchAgents

```bash
# 상태 확인
launchctl list | grep quartz

# fswatch 재시작
launchctl unload ~/Library/LaunchAgents/com.drewgent.quartz-fswatch.plist
launchctl load -w ~/Library/LaunchAgents/com.drewgent.quartz-fswatch.plist

# 로그 확인
tail -f ~/Library/Logs/quartz-fswatch.log
tail -f ~/Library/Logs/quartz-deploy.log
```

### Publishing Checklist (Obsidian에서)

```
[ ] 오탈자·내용 수정
[ ] frontmatter: status: published (draft → published)
[ ] frontmatter: publish_date: YYYY-MM-DD 추가
[ ] aliases: ['/blog/YYYY/slug'] 또는 ['/blog/slug'] 설정 (SEO-friendly URL)
[ ] fswatch가 자동 감지 → humanerd.kr 게시 (~10초)
[ ] humanerd.kr에서 게시 확인
```

### Manual Trigger (LaunchAgent 없이)

```bash
# 수동 빌드 + 배포 (fswatch 통하지 않고)
bash ~/.drewgent/P4-cortex/scripts/quartz-deploy.sh

# 또는 wrangler 직접
cd ~/.drewgent/humanerd-site
npx quartz build
wrangler pages deploy public/ --project-name=humanerd-site
```

### Configuration Files

```
~/.drewgent/humanerd-site/
├── wrangler.toml              ← project_name = "humanerd-site"
└── .wrangler.jsonv2          ← account_id, CF Pages project 설정

~/.drewgent/P4-cortex/scripts/
├── quartz-deploy.sh          ← build + wrangler deploy 스크립트
└── quartz-fswatch.sh         ← fswatch 파일 변경 감지 → debounce → deploy
```

---

## Quality Gate (writer용)

Draft 작성 완료 시 확인:
1. **Language Polish** (4-4 단계) 먼저 수행 — 한자/한문/일어→한글 변환, 비문 수정
2. **forbidden.patterns** grep → 0건
3. **Bold** 섹션 강조 2~4개 존재
4. **"당신"** 직접호칭 + 1인칭 "저"/"나" 포함
5. **본문 날짜** 없음 (X월 X일 금지)
6. **출처 없는 주장** 없음 (모르면 "확인 필요" 표기)
7. **SEO 키워드** 3~7개, **해시태그** 8~14개
8. aliases in frontmatter (`/blog/{slug}`)

**Polish 체크리스트 출력 예시:**
```
Language Polish: DONE
  한자→한글: 3건 변환
  한문 표현: 1건 변환
  일어 외래어: 0건
  비문 수정: 2건
Forbidden: 0건
Bold sections: 3
Direct address ("당신"): YES
1인칭 ("저"/"나"): YES
Dates in body: 0
SEO keywords: 5
해시태그: 11
aliases: /blog/gemini-cli-shutdown
PASS
```

---

## Obsidian Publishing Workflow

### Step-by-Step

1. **Draft 작성** (Phase 4) — memories/insights/에draft 파일 생성
2. **Obsidian에서 편집** — 오탈자·内容の견·글쓰기 교정
3. **Publication 준비** — frontmatter에서:
   ```
   status: published
   publish_date: 2026-05-27
   ```
4. **fswatch가 자동 감지** — vault 파일 변경 → quartz build → Cloudflare Pages 배포
5. **humanerd.kr에서 확인** — 1~2분 내 게시

### Alias 규칙 (SEO-Friendly URL)

| Frontmatter | Site URL |
|------------|----------|
| `aliases: ['/blog/2026/agent-workflow']` | humanerd.kr/blog/2026/agent-workflow |
| `aliases: ['/projects/kanban-review']` | humanerd.kr/projects/kanban-review |
| `aliases: ['/lab/dream-system']` | humanerd.kr/lab/dream-system |

Slug 생성 규칙:
- Timestamp 제거: `KANBAN-REVIEW-20260520` → `kanban-review`
- 소문자 + kebab: `Open Crab Ontology` → `open-crab-ontology`
- 의미 없는 단어 제거: `The`, `A`, `An`

---

## Related

- [[P1-limbic/persona/writing-style-guide]] — Writing tone & rules
- [[P2-hippocampus/kanban/KANBAN_INDEX]] — Kanban board integration
- [[skills/kanban-worker]] — Worker execution model
- [[skills/content-writer]] — Draft writing skill (별도)
