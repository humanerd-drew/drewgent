---
title: SEO Article Harvester
type: skill
space: outcome
tags: [outcome, seo, rss, crawling]
created: 2026-05-20
updated: 2026-06-05
links:
  - "[[P3-sensors/skills/SKILL-INDEX]]"
  - "[[skills/seo-article-harvester/references/crawling-tools]]"
  - "[[P2-hippocampus/knowledge/seo-articles]]"
---

# SEO Article Harvester

RSS 피드를 모니터링하여 SEO 관련 기사를 자동 수집·크롤링·저장하는 스킬.

## 피드 목록 (2026-06-05 갱신, 28개)

### SEO / 마케팅 (10개) — 모든 글 통과
- `ahrefs.com/blog/feed`
- `searchengineland.com/feed`
- `semrush.com/blog/feed`
- `yoast.com/feed/`
- `seopress.org/feed`
- `ascentkorea.com/feed`
- `growthmk.com/feed`
- `moz.com/blog/feed`
- `searchenginejournal.com/feed`
- `copyblogger.com/feed`

### 테크 / 개발 (11개) — 제목 기반 키워드 필터 적용
- `blog.google/technology/ai/rss`
- `developers.googleblog.com/feeds/posts/default` ← 2026-06-05 대체 URL 발견
- `blog.chromium.org/feeds/posts/default` ← 2026-06-05 대체 URL 발견
- `techcrunch.com/feed`
- `theverge.com/rss/index.xml`
- `wired.com/feed/rss`
- `zdnet.com/rss.xml`
- `feeds.arstechnica.com/arstechnica/index`
- `css-tricks.com/feed`
- `smashingmagazine.com/feed`
- `aws.amazon.com/blogs/aws/feed`
- `github.blog/feed`

### 글로벌 / 커뮤니티 (7개) — 제목 기반 키워드 필터 적용
- `openschema.co.jp/feed`
- `hnrss.org/frontpage`
- `dev.to/feed`
- `news.ycombinator.com/rss`
- `techmeme.com/feed.xml`

### 제거된 피드 (죽었거나 봇 차단)
- rankpill.com — 404
- ranktracker.com — 404
- conductor.com — XML 깨짐
- seoforgooglenews.com — SSL 인증서 만료
- link-assistant.com — 404
- polemicdigital.com — 404
- seo.tbwakorea.com — Cloudflare 차단 (403)
- twinword.com — 500, twinword.co.kr — XML 깨짐
- nngroup.com — 404
- perplexity.ai — RSS 미제공, 403
- growth-memo.com — XML 깨짐
- seoforjournalism.com — 423 Locked

## 주제 필터 (2026-06-05 추가)

SEO 전용 도메인의 모든 글은 통과. 일반 테크/커뮤니티 피드는 제목에 SEO 키워드가 포함된 경우만 수집.

### SEO 키워드 목록
```
seo, search engine, google search/algorithm/update, ranking, keyword,
backlink, link building, serp, organic traffic, ppc, google ads,
content marketing, search marketing, webmaster, crawl, index, sitemap,
core web vital, structured data, schema markup, rich snippet,
featured snippet, ai overview, search generative, google analytics,
search console, local seo, ecommerce seo, technical seo, on-page,
off-page, domain authority, page authority, domain rating,
content strategy, editorial, publisher, google news, search traffic,
click-through, conversion rate, aibo, google discover, google labs
```

## 크론

- 스케줄: `0 */6 * * *` (6시간마다)
- 실행: `~/.drewgent/scripts/cron_seo_harvester.py` (script_only, AIAgent 미사용)
- 전달: Discord 채널
- 마지막 갱신: 2026-06-05 (죽은 피드 교체 + 주제 필터 추가 + script_only 전환)

## 관련 파일
- `scripts/harvester.py` — RSS 수집 + 크롤링 + 저장
- `scripts/label_heritage.py` — Heritage 태깅
- `scripts/cron_seo_harvester.py` — 크론 래퍼 (harvester + label_heritage 순차 실행)
- `P2-hippocampus/knowledge/seo-articles/` — 수집된 기사 저장소
