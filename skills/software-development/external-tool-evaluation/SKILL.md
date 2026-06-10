---
name: external-tool-evaluation
title: External Tool Evaluation
description: Evaluate a third-party GitHub repo or external tool for Drewgent integration. Use raw markdown curl + GitHub API to bypass UI, identify algorithm taxonomy, map to Drewgent hot spots, score 3-4 integration modes, recommend POC-first.
domain: software-development
space: outcome
type: workflow
tags: [external-tool, evaluation, github, integration, drewgent]
created: 2026-06-02
updated: 2026-06-02
links:
  - "[[P4-cortex/growth/INTEGRATION_PROTOCOL]]"
  - "[[P3-sensors/skills/SKILL-INDEX]]"
  - "[[P4-cortex/knowledge/garry-tan-unified-architecture-drewgent-review]]"
  - "[[skills/llm-model-migration]]"
  - "[[skills/gateway-module-extraction]]"
---

# External Tool Evaluation — Drewgent Integration Workflow

## When to use

User shares a GitHub repo link OR asks "what about X for Drewgent" / "should we use X" / "evaluate X for integration." Trigger keywords: "토큰 똑똑하게", "이 도구 쓸 수 있을까", "X 통합 검토", "headroom / RTK / lean-ctx / kompress / X 평가해줘".

## Why a skill is needed

GitHub UI via `mcp_browser_navigate` returns ~700-element accessibility tree with 95% chrome. Raw markdown via curl gives the actual content in 1 call. Repo root `docs/` is often a Next.js site, not source — `docs/spec/` or `crates/` is where architecture lives. Without this method, the agent spends 5+ browser tool calls extracting what `curl | head` returns in 1.

## Workflow (7 steps)

### 1. Raw markdown fetch (bypass GitHub UI)
```bash
curl -sL https://raw.githubusercontent.com/{owner}/{repo}/main/README.md | head -200
# 4.3k stars + last-commit 3h ago = maturity check satisfied
```

### 2. Repo health snapshot
```bash
curl -sL https://api.github.com/repos/{owner}/{repo}
# Check: stars, last_push_at (NOT updated_at), default_branch, license
```

### 3. Find actual architecture/spec files
```bash
# root docs/ is often a Next.js site — list subdirs
curl -sL https://api.github.com/repos/{owner}/{repo}/contents/docs
# For Rust+Python repos: look in crates/, headroom/transforms/, sdk/
# For docs-heavy repos: docs/spec/, docs/content/spec/
```
Pitfall: `docs/README.md` is often the website's README (Vercel deploy info), not the source README.

### 4. Identify the algorithm/feature taxonomy
Read 2-3 spec files or core source files. Extract:
- What it does (1-line)
- Algorithm list (each with 1-line description)
- Integration modes (proxy / library / wrap / MCP / middleware)
- Benchmarks (savings %, accuracy preserved)
- Reversibility (can original be retrieved?)

### 5. Map to Drewgent hot spots
For each algorithm/feature, list Drewgent files that would benefit:
- Tool output hot spots: `tools/mcp_tool.py`, `tools/browser_tool.py`, `tools/file_tools.py`, `tools/terminal_tool.py`
- LLM call site: `run_agent.py:_interruptible_api_call()` (line ~5244)
- Existing compression: `agent/context_compressor.py` (conversation-level summary)
- Prompt caching: `agent/prompt_caching.py` (prefix alignment conflicts)

### 6. Score integration modes against Drewgent
Always evaluate 3-4 options with the same axes:
- Code change size
- Latency overhead
- Conflict with our gateway proxy structure
- Reversibility (can we roll back?)

Common modes for Drewgent:
- A. **Proxy** (zero code, runs alongside gateway) — usually conflicts with our existing gateway
- B. **SDK/Library in `_interruptible_api_call`** — surgical, best for compression libs
- C. **Wrap CLI** (`headroom wrap claude`) — conflicts with our `drewgent` wrapper
- D. **MCP tool** (`mcp install`) — modular, agent-discoverable, low risk

### 7. Recommend with POC-first framing
Per memory: "옵션 (H1~H4) + '내 추천' + 'over-engineering 위험' 또는 '0 risk vs 작업 시간' 가성비 분석."

Default recommendation: POC first (30 min) before integration. Verify with dry-run or dry-mode audit before wiring into critical path.

## Output template (terminal-friendly, no markdown headers per SOUL)

```
[Tool] 한 줄 요약
[maturity] stars, last commit
[algorithms] 4-6 bullets
[benchmarks] % savings on real workloads + accuracy
[integration modes] 4-way score table
[hot spot mapping] our files × their algorithms
[risks] 3-4 bullets
[POC] 1-step 30min experiment
[options] H1 POC / H2 surgical / H3 full integration — with "내 추천" framing
```

## Pitfalls

- **GitHub UI snapshot is mostly chrome** — `browser_navigate` returns nav menus, header, sidebar, footer with ref IDs. The actual README content gets truncated at 8000 chars. Use raw curl.
- **`docs/` is often a Next.js site** — `docs/package.json`, `docs/next.config.mjs`, `docs/components/` are signs. Look for `docs/spec/`, `docs/content/`, or skip straight to `crates/`, `src/`, `headroom/`, etc.
- **Stars and recency lie** — 4.3k stars + commit 3h ago can still be a side project. Check contributor count and whether the most active branch is the default.
- **Don't propose proxy mode by default** — Drewgent's gateway is already a proxy. Adding another proxy layer creates debugging hell. Library/SDK in our existing call site is almost always better.
- **Benchmarks are on the tool's own workloads** — 92% savings on "code search" may not translate to Drewgent's pattern. Always map to our actual hot spots.
- **Compression + caching conflict** — if the tool reorders prefixes or rewrites system prompt, prompt_caching.py's KV cache hit rate can drop to 0. Verify CacheAligner / prefix-stability behavior before integration.

## Verification

- [ ] Raw README fetched (not browser snapshot)
- [ ] Repo health checked (stars + last_push_at)
- [ ] 2-3 architecture/spec files read for actual algorithm details
- [ ] Drewgent hot spots identified with file paths
- [ ] 3-4 integration modes scored against Drewgent's gateway constraint
- [ ] POC option offered first (30 min, 0 risk)
- [ ] Risks section includes prompt-caching conflict check

## Related

- [[P4-cortex/growth/INTEGRATION_PROTOCOL]] — when evaluation leads to "yes, integrate," follow this protocol
- [[skills/llm-model-migration]] — sibling skill for LLM provider updates
- [[skills/gateway-module-extraction]] — sibling skill for gateway refactoring
- [[P4-cortex/knowledge/garry-tan-unified-architecture-drewgent-review]] — "fat skills, thin harness" lens for evaluating if the tool's complexity fits
