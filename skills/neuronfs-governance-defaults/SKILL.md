---

title: Skill
type: document
space: concept
tags: [concept]
created: 2026-05-20
updated: 2026-05-20
links: []
links:
  - "[[P4-cortex/knowledge/NEURONFS_RULES]]"
---




# NeuronFS Governance Defaults

This skill provides default governance rules demonstrating NeuronFS patterns. These rules establish baseline AI behavior constraints using the vorq harness and 禁 (禁) micro-opcodes.

## Core Philosophy

The filesystem IS the constraint engine. Rules are encoded as folder structures that force AI behavior:

- **`禁`** (禁) = NEVER_DO — 1-char Chinese = 8-char meaning
- **`必`** (必) = MUST_DO — forced lookup before action  
- **`命`** (命) = MANDATE — absolute requirement

## Default Rules

### P0-brainstem: Critical Never-Do Rules

#### 禁secrets_in_code
```
禁secrets_in_code 必vorq
FORBIDDEN: Hardcoded secrets, API keys, tokens, passwords
REASON: Security breach risk — secrets must be environment variables
SCOPE: Any .py, .js, .ts, .go, .java, .rs, .env file
EXAMPLE VIOLATION:
    api_key = "sk-abc123..."  # BAD
CORRECT APPROACH:
    import os
    api_key = os.getenv("API_KEY")  # GOOD
VERIFICATION: Run "grep -r 'sk-\|password\|token\|secret' ." before commit
```

#### 禁rm_rf_root
```
禁rm_rf_root 必vorq
FORBIDDEN: rm -rf on root directory or system paths
REASON: Catastrophic data loss risk
GUARD: Must confirm with user before ANY rm -rf
SCOPE: /, /home, /usr, /etc, /var, ~, $HOME, ./*
EXCEPTION: Only in explicitly scoped temp directories with user confirmation
```

#### 禁console_log_production
```
禁console_log_production 必vorq
FORBIDDEN: console.log, print(), System.out.println in production code
REASON: Pollutes logs, leaks debugging info in production
REPLACEMENT: Use structured logging (winston, pino, log4j, logging module)
CORRECT:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("User action completed", extra={"user_id": user_id})
```

### P1-limbic: Values and Style Constraints

#### 命secure_defaults
```
命secure_defaults
MANDATE: Security by default in all code
RULES:
- Input validation on all user-supplied data
- Parameterized queries for database access
- HTTPS/TLS for all external communications
- Principle of least privilege
- Fail securely (default deny)
```

#### 禁comments_griefing
```
禁comments_griefing
FORBIDDEN: Negative, condescending, or dismissive comments in code
REASON: Code is written by humans — maintain dignity
EXAMPLE:
    # This is stupid and wrong    # TODO: Fix this mess later
CORRECT:
    # TODO: Refactor to handle edge case X
    # Note: Current implementation assumes Y based on Z
```

### P2-hippocampus: Memory Patterns

#### 禁forget_context
```
禁forget_context
FORBIDDEN: Forgetting user-provided context or preferences
REASON: User should not repeat themselves
ENFORCEMENT:
- Always use session_search before asking for clarification
- Save user preferences with memory tool
- Check memory for relevant past interactions
```

### P3-sensors: Tool Routing

#### 命verify_before_edit
```
命verify_before_edit
MANDATE: Verify file state before editing
RULES:
- Read file content before suggesting edits
- Confirm changes make sense in context
- Don't assume — check actual content
- Use tools to verify, not assumptions
```

#### 禁blind_write
```
禁blind_write
FORBIDDEN: Writing code without reading existing file first
REASON: Blind writes can corrupt or misalign code
REQUIRED:
1. Read existing file with file read tool
2. Understand current structure
3. Make targeted edits
4. Verify edit was applied correctly
```

## Using the vorq Harness

The `vorq` (value-or-lookup) pattern forces AI to look up unknown governance tokens:

```
禁console_log 必vorq
```

When the AI encounters an unknown `禁*` token:
1. Search for the corresponding folder in the brain
2. Load the rule definition
3. Execute the rule's conditions BEFORE taking the forbidden action
4. If no guard folder exists, the action is FORBIDDEN by default

## Creating Custom Rules

Place `.neuron` files in your brain structure:

```
~/.drewgent/brain/myproject/
└── P0-brainstem/
    └── 禁/                    # Forbidden patterns
        ├── secrets_in_code/
        │   └── rule.neuron
        └── rm_rf_root/
            └── rule.neuron
```

## Integration with Brain

This skill demonstrates rules that should be placed in your active brain's `P0-brainstem/禁/` directory. To create a production brain:

```bash
/brain init myproject
/brain activate myproject
# Then add .neuron files to the layers
```

## Verification Commands

After implementing these rules, verify compliance:

```bash
# Check for hardcoded secrets
grep -rE 'sk-|password|token|secret|api_key' --include="*.py" --include="*.js" .

# Check for console.log in production files
grep -r 'console\.log' src/ | grep -v '\.test\.js' | grep -v 'debug'

# Verify environment variable usage
grep -r 'os\.getenv\|process\.env' .
```

## Rule Enforcement

Rules are enforced through:

1. **Brain loaded into system prompt** — AI reads rules at session start
2. **vorq lookup** — Unknown tokens trigger rule lookup
3. **Bomb kill switch** — `/brain bomb <path>` disables rules
4. **Neuron firing** — `/brain fire <path>` strengthens frequently-needed rules
