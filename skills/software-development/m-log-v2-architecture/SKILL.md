---
name: m-log-v2-architecture
title: m-log-v2 Architecture
description: "Domain-based project structure for m-log-v2 — Svelte 5 MPA frontend, Hono backend on CF Workers, CSS variables design system"
trigger: "m-log-v2 전면 구조 개편 — Vanilla JS SPA → Svelte 5 MPA + Hono 백엔드"
provenance:
  session: "2026-06-13 m-log-v2 restructuring"
  decision: "SPA 대신 MPA 선택"
created: 2026-06-14
updated: 2026-06-14
---

# M-LOG v2 Architecture

## 디렉토리
- `src/api/` — Hono 라우트 + 미들웨어
- `src/saju/` — 사주 엔진
- `src/user/` — 계정/인증/기록
- `src/report/` — 리포트 생성
- `src/payment/` — 결제 검증
- `src/db/` — D1 쿼리
- `src/config/` — 설정/상수
- `src/utils/` — 공통 유틸
- `src/ui/` — Svelte 5 MPA 프론트엔드
- `public/app/` — 원본 SPA 백업

## 주요 명령어
- `npm run dev` = wrangler dev (localhost:8787)
- `npm run build:ui` = Vite build + public/ 디렉토리 생성
- `npm run deploy` = Cloudflare 배포

## MPA URL 구조
- `/input/`, `/dashboard/`, `/payment/`, `/compare/`
- `/report/{desire,desire-deep,ai,comprehensive,dating}/`
- 디렉토리/index.html 방식
- worker.ts에서 .html → 디렉토리 URL 301 리디렉트

## 디자인 시스템 (필수)
- CSS variables ONLY (`var(--xxx)`) — #hex 금지
- data-theme="dark" 필수
- AppShell 공유 컴포넌트 사용
- 원본 CSS: public/app/shared/theme/variables.css + public/app/css/index.css

## 현재 상태 (2026-06-14)
- Hono + 도메인 구조 완료
- Quintax/Just5/데드코드 제거
- Secrets 5개 등록
- Svelte 5 MPA 9페이지 전환 완료
- AppShell, 리포트 포맷팅, 히스토리 저장 구현
- wrangler dev에서 ASSETS .html 307 이슈 있음 (프로덕션 정상)
