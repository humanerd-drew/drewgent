---
name: hermes-model-routing
title: Hermes Model Routing — Multi-Model Configuration
description: Configure Hermes' 4-level model routing (main/delegation/auxiliary/provider_routing) to distribute different models across agents, subagents, and auxiliary tasks. Covers cost models (subscription vs per-call), provider resolution, role-based model selection, and configuration patterns.
domain: software-development
space: growth
type: skill
tags: [model-routing, provider-routing, delegation, auxiliary, llm-cost, hermes-config]
created: 2026-06-13
updated: 2026-06-14
links:
  - "[[P3-sensors/gateway/drewgent-architecture-dataflow]]"
  - "[[software-development/cost-optimization-background-llm]]"
  - "[[P0-brainstem/brain/rules]]"
---

# Hermes Model Routing — Multi-Model Configuration

Configure Hermes to use different models for different roles instead of
a single model doing everything.

## When to use

Trigger words from user: "model routing", "multi-model", "subagent model",
"different models for different tasks", "auxiliary model", "특정 작업은
다른 모델로", "라우팅", "모델 분배".

## Routing Architecture Overview

Hermes supports **4 levels of model routing**:

```
┌─────────────────────────────────────────────────────┐
│  1. MAIN MODEL (model.default + model.provider)      │
│     → 인터랙티브 세션, 사용자가 직접 보는 응답        │
├─────────────────────────────────────────────────────┤
│  2. SUBAGENT (delegation.model + delegation.provider) │
│     → delegate_task tool로 spawn된 child agents       │
├─────────────────────────────────────────────────────┤
│  3. AUXILIARY (auxiliary.*.provider + model)          │
│     → vision, web_extract, session_search, compression│
├─────────────────────────────────────────────────────┤
│  4. PROVIDER ROUTING (provider_routing.*)             │
│     → OpenRouter 전용: sort/order/allow/ignore        │
└─────────────────────────────────────────────────────┘
```

Each level can have its own (model, provider) pair. When a level's
provider/model is unset (None/empty), it inherits from the parent:
main → delegation → auxiliary (in that order).

## Cost Models

Two fundamental cost models determine routing strategy:

| Model | Cost Type | Strategy |
|-------|-----------|----------|
| **Subscription** (OpenCode Go $10/mo) | Fixed monthly cost, $0 marginal | **Maximize usage** — every unused call is wasted |
| **Per-call** (MiniMax Token Plan, OpenRouter PAYG) | Per-token/per-request billing | **Minimize unnecessary usage** — use only when justified |

When both are available, the optimal strategy:
- Route ALL possible traffic through the subscription provider
- Use per-call provider only as fallback or for capabilities the subscription can't cover

## Model Selection Dimensions

When choosing which model goes where, evaluate along 4 axes:

### 1. Reasoning Quality
```
High ─────────────────────────────────────→ Low
qwen3.7-max   deepseek-v4-pro   kimi-k2.6   deepseek-v4-flash
kimi-k2.7-code  qwen3.7-plus    glm-5.1     kimi-k2.5
```
- **Use heavy models** for: architecture decisions, complex debugging, code review, multi-step reasoning
- **Use light models** for: simple Q&A, file summarization, text formatting, grep result analysis

### 2. Speed / Latency
```
Fast ──────────────────────────────────────→ Slow
deepseek-v4-flash   kimi-k2.5   qwen3.7-plus   qwen3.7-max
kimi-k2.6           deepseek-v4-pro  kimi-k2.7-code  minimax-m3
```
- **Fast models → interactive main agent**: user waiting time is #1 UX metric
- **Slow/heavy models → subagent/background**: quality over speed when user isn't waiting

### 3. Context Window
| Window | Models |
|--------|--------|
| **1M** | minimax-m3 (via OpenCode Go), MiniMax-M3 (via direct minimax provider) |
| 128K-1M | deepseek-v4-flash/pro, qwen3.7-max, kimi-k2.6 |
| Standard | kimi-k2.5, glm-5.1, mimo series |

### 4. Special Capabilities
| Capability | Models |
|------------|--------|
| **Vision/multimodal** | mimo-v2.5-pro (via OpenCode Go), mimo-v2-omni |
| **Korean-optimized** | (none specific — deepseek/qwen/glm all handle Korean well) |
| **Chinese-native** | glm-5.1, qwen3.7-max |

## Configuration Patterns

### Default (single model — current Drewgent routing)

```yaml
model:
  default: "opencode-go/deepseek-v4-flash"
  provider: "opencode-go"
```
→ Everything uses deepseek-v4-flash via OpenCode Go ($10/mo subscription, $0 marginal cost).
No model routing needed — single model for all tasks.

### 4-Level Routing (recommended)

```yaml
model:
  default: "opencode-go/deepseek-v4-flash"       # Interactive: fast
  provider: "opencode-go"                         # Subscription: $0 marginal cost

delegation:
  model: "deepseek-v4-pro"                        # Subagent: quality over speed
  provider: "opencode-go"

auxiliary:
  vision:
    provider: "opencode-go"
    model: "mimo-v2.5-pro"                        # Vision: multimodal required
  web_extract:
    provider: "opencode-go"
    model: "deepseek-v4-flash"                    # Web summary: fast is enough
  session_search:
    provider: "opencode-go"
    model: "deepseek-v4-flash"                    # Search summary: fast is enough
```

### OpenRouter Provider Routing (when using OpenRouter)

```yaml
model:
  provider: "openrouter"

provider_routing:
  order: ["opencode-go", "minimax", "anthropic"]   # Priority order
  sort: "throughput"                                # Or: price, latency
```

## Provider Auto-Resolution Chain

When `provider: "auto"`, Hermes resolves in this priority:

1. **Active OAuth** in auth store (nous, openai-codex, etc.)
2. **OPENROUTER_API_KEY** or **OPENAI_API_KEY** env var → "openrouter"
3. **PROVIDER_REGISTRY iteration** — first API-key provider with usable env var:
   `openai-api → gemini → zai → kimi-coding → minimax → deepseek → opencode-zen → opencode-go → ...`
4. **AWS Bedrock** credential chain
5. **Error**: "No inference provider configured"

⚠️ Dict order matters — `minimax` comes before `opencode-go`. If both MINIMAX_API_KEY
and OPENCODE_GO_API_KEY are set, auto picks **minimax** (first match).

## Procedure

### Phase 1 — Inventory available models

```bash
# All models for a provider (static + models.dev dynamic merge)
python3 -c "
from hermes_cli.models import provider_model_ids
for m in provider_model_ids('opencode-go'):
    print(f'  {m}')
"
```

### Phase 2 — Map tasks to tiers

| Task | Recommended Tier |
|------|-----------------|
| Interactive chat (user waiting) | Fast model (deepseek-v4-flash) |
| Complex code generation | Heavy model (qwen3.7-max, deepseek-v4-pro) |
| Subagent parallel tasks | Quality model (deepseek-v4-pro via delegation) |
| Subagent simple tasks | Fast model (deepseek-v4-flash — override per delegate_task) |
| Vision/image analysis | Multimodal (mimo-v2.5-pro via auxiliary.vision) |
| Web page summarization | Light model (deepseek-v4-flash via auxiliary.web_extract) |
| Session search summarization | Light model (deepseek-v4-flash via auxiliary.session_search) |
| Context compression | Light model (configured in auxiliary or compression section) |

### Phase 3 — Apply config

Patch `~/.hermes/config.yaml` (or active profile's config) at the three levels:
- `model.default` + `model.provider`
- `delegation.model` + `delegation.provider`
- `auxiliary.*.provider` + `auxiliary.*.model`

### Phase 4 — Verify

```bash
# Check effective config
hermes doctor

# Check model picker
hermes model

# Verify by starting a session and checking header
# "Model: opencode-go/deepseek-v4-flash" in welcome banner
```

## Pitfalls

### P1: OpenCode Go ≠ OpenRouter

OpenCode Go (`https://opencode.ai/zen/go/v1`) is a **standalone provider**
in Hermes, NOT an OpenRouter provider. It has its own API key
(`OPENCODE_GO_API_KEY`) and its own model catalog. Do not confuse with
OpenRouter's `opencode-go/` model prefix which routes through OpenRouter's
infrastructure — Hermes' native `opencode-go` provider goes directly.

### P2: Provider auto-resolution ≠ what you expect

`provider: "auto"` iterates PROVIDER_REGISTRY in dict order, which means
`minimax` is checked before `opencode-go`. If both keys are present, auto
silently picks the first match. Explicitly set `provider: "opencode-go"`
to guarantee which provider is used.

### P3: Two config.yaml files

Drewgent has `~/.hermes/config.yaml` (primary) and
`~/.drewgent/P5-ego/config/config.yaml` (legacy copy). Both must be
patched in sync if the routing config is duplicated there. Check both
before claiming "done".

### P4: Subagent model field vs provider field

When setting `delegation.model` without `delegation.provider`, the model
name is resolved against the parent's provider. If parent is opencode-go
and you set `model: "MiniMax-M3"`, Hermes will try to find "MiniMax-M3"
in opencode-go's model list (which has `minimax-m3` lowercase — may or
may not match). Always set BOTH `provider` and `model` for clarity.

### P6: Session restore can override configured provider via stored metadata

When sessions are long-lived (CLI sessions spanning multiple turns),
`_restore_primary_runtime` is called at the start of each new turn.
This restores the **original** provider/model from session metadata,
NOT the currently configured provider.

```python
# run_agent.py ~line 6068
self.provider = rt["provider"]   # stored metadata, not current config
```

This means:

- If a session was created when the default was `openrouter`, its metadata
  stores `openrouter`. Even after you change config to `opencode-go`,
  that session continues using `openrouter` on every turn.
- If OpenRouter has no key or the model doesn't exist on OpenRouter, you
  get silent HTTP 400 errors on every turn of the affected session.

**Symptoms**: Dashboard shows recurring `HTTP 400: ...not a valid model ID`
errors for sessions that started hours or days ago. New sessions work fine.

**Debugging**: Check the agent log for `restore_primary` lines to see which
sessions are using a different provider:

```bash
grep 'restore_primary.*provider=' agent.log
# → provider=openrouter ... model=opencode-go/deepseek-v4-flash
```

**Fix options** (pick one):

1. **Remove stale API key**: If the stale provider is not needed (e.g.
   OpenRouter when you only use OpenCode Go), remove its API key from
   `.env`. Session restore will fail → fallback → current provider.
2. **Restart the gateway**: `hermes gateway restart` — fresh sessions
   start with the current config. Old sessions won't be restored.
3. **Validate model name per provider**: If you need both providers, each
   session type must use a model name valid for its provider. The model
   `opencode-go/deepseek-v4-flash` is valid on OpenCode Go but NOT on
   OpenRouter (which needs `deepseek/deepseek-v4-flash` or similar).

### P7: Configured but never-called models are not a routing bug

If `delegation.model: deepseek-v4-pro` and `auxiliary.vision.model:
mimo-v2.5-pro` are configured but show zero calls after weeks of use:

- **deepseek-v4-pro** is only used when `delegate_task()` is called.
  Subagent delegation requires an EXPLICIT user request or an
  orchestrator profile that decomposes tasks. If no task naturally
  triggers delegation, the subagent model stays idle.
- **mimo-v2.5-pro** is only used for vision/image analysis. No image
  processing requests → no calls.

This is NOT a routing bug — the configuration is correct. The models
are idle because their trigger conditions haven't been met. The dashboard
Models card will show them automatically when they start being used.

To verify a model name is valid (not silently failing):
```bash
# Test the model with the provider
curl -s https://opencode.ai/zen/go/v1/models \
  -H "Authorization: Bearer $OPENCODE_GO_API_KEY" | \
  python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin).get('data',[])]"
```

### P5: Subscription model ≠ no reason to split models

With OpenCode Go's $10/mo flat subscription, marginal cost per call is $0.
This means **cost optimization** (saving tokens) is no longer a reason to
route. However, model splitting still matters for:

- **Latency**: Interactive agents need fast models (deepseek-v4-flash).
  Heavy models (qwen3.7-max) slow down every turn.
- **Capability**: Vision tasks need multimodal models (mimo-v2.5-pro).
  1M context tasks need minimax-m3.
- **Subagent quality**: Background tasks can afford slower models for
  higher quality output (deepseek-v4-pro for review).

The previous cost-optimization era skills (`cost-optimization-background-llm`)
were written for per-call billing (MiniMax Token Plan). Under subscription,
the routing framework shifts from "which model saves money" to
"which model matches the task's latency/capability requirements."

## References

- `references/opencode-go.md` — OpenCode Go provider details and model list
- `references/routing-change-sweep.md` — Procedure for cleaning up old model references when routing changes
- `references/provider-fallback-debugging.md` — Real session trace: debugging HTTP 400 errors from session metadata override
- `references/minimax.md` — MiniMax pricing model and Token Plan structure
