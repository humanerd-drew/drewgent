---
title: Agents
type: guide
space: concept
tags: [concept]
created: 2026-05-20
updated: 2026-05-20
links:
  - "[[P5-ego/SELF_MODEL]]"
---


# Drewgent Agent - Development Guide

Instructions for AI coding assistants and developers working on the drewgent-agent codebase.

## Development Environment

```bash
source venv/bin/activate  # ALWAYS activate before running Python
```

## Project Structure

```
drewgent-agent/
├── run_agent.py          # AIAgent class — core conversation loop
├── model_tools.py        # Tool orchestration, _discover_tools(), handle_function_call()
├── toolsets.py           # Toolset definitions, _HERMES_CORE_TOOLS list
├── cli.py                # DrewgentCLI class — interactive CLI orchestrator
├── drewgent_state.py       # SessionDB — SQLite session store (FTS5 search)
├── agent/                # Agent internals
│   ├── prompt_builder.py     # System prompt assembly
│   ├── context_compressor.py # Auto context compression
│   ├── prompt_caching.py     # Anthropic prompt caching
│   ├── auxiliary_client.py   # Auxiliary LLM client (vision, summarization)
│   ├── model_metadata.py     # Model context lengths, token estimation
│   ├── models_dev.py         # models.dev registry integration (provider-aware context)
│   ├── display.py            # KawaiiSpinner, tool preview formatting
│   ├── skill_commands.py     # Skill slash commands (shared CLI/gateway)
│   └── trajectory.py         # Trajectory saving helpers
├── drewgent_cli/           # CLI subcommands and setup
│   ├── main.py           # Entry point — all `drewgent`` subcommands
│   ├── config.py         # DEFAULT_CONFIG, OPTIONAL_ENV_VARS, migration
│   ├── commands.py       # Slash command definitions + SlashCommandCompleter
│   ├── callbacks.py      # Terminal callbacks (clarify, sudo, approval)
│   ├── setup.py          # Interactive setup wizard
│   ├── skin_engine.py    # Skin/theme engine — CLI visual customization
│   ├── skills_config.py  # `drewgent skills` — enable/disable skills per platform
│   ├── tools_config.py   # `drewgent` tools` — enable/disable tools per platform
│   ├── skills_hub.py     # `/skills` slash command (search, browse, install)
│   ├── models.py         # Model catalog, provider model lists
│   ├── model_switch.py   # Shared /model switch pipeline (CLI + gateway)
│   └── auth.py           # Provider credential resolution
├── tools/                # Tool implementations (one file per tool)
│   ├── registry.py       # Central tool registry (schemas, handlers, dispatch)
│   ├── approval.py       # Dangerous command detection
│   ├── terminal_tool.py  # Terminal orchestration
│   ├── process_registry.py # Background process management
│   ├── file_tools.py     # File read/write/search/patch
│   ├── web_tools.py      # Web search/extract (Parallel + Firecrawl)
│   ├── browser_tool.py   # Browserbase browser automation
│   ├── code_execution_tool.py # execute_code sandbox
│   ├── delegate_tool.py  # Subagent delegation
│   ├── mcp_tool.py       # MCP client (~1050 lines)
│   └── environments/     # Terminal backends (local, docker, ssh, modal, daytona, singularity)
├── gateway/              # Messaging platform gateway
│   ├── run.py            # Main loop, slash commands, message dispatch
│   ├── session.py        # SessionStore — conversation persistence
│   └── platforms/        # Adapters: telegram, discord, slack, whatsapp, homeassistant, signal
├── acp_adapter/          # ACP server (VS Code / Zed / JetBrains integration)
├── cron/                 # Scheduler (jobs.py, scheduler.py)
├── environments/         # RL training environments (Atropos)
├── tests/                # Pytest suite (~3000 tests)
└── batch_runner.py       # Parallel batch processing
```

**User config:** `~/.drewgent/config.yaml` (settings), `~/.drewgent/.env` (API keys)

## File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

---

## AIAgent Class (run_agent.py)

```python
class AIAgent:
    def __init__(self,
        model: str = "anthropic/claude-opus-4.6",
        max_iterations: int = 90,
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str = None,           # "cli", "telegram", etc.
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        # ... plus provider, api_mode, callbacks, routing params
    ): ...

    def chat(self, message: str) -> str:
        """Simple interface — returns final response string."""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """Full interface — returns dict with final_response + messages."""
```

### Agent Loop

The core loop is inside `run_conversation()` — entirely synchronous:

```python
while api_call_count < self.max_iterations and self.iteration_budget.remaining > 0:
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

Messages follow OpenAI format: `{"role": "system/user/assistant/tool", ...}`. Reasoning content is stored in `assistant_msg["reasoning"]`.

---

## CLI Architecture (cli.py)

- **Rich** for banner/panels, **prompt_toolkit** for input with autocomplete
- **KawaiiSpinner** (`agent/display.py`) — animated faces during API calls, `┊` activity feed for tool results
- `load_cli_config()` in cli.py merges hardcoded defaults + user config YAML
- **Skin engine** (`drewgent_cli/skin_engine.py`) — data-driven CLI theming; initialized from `display.skin` config key at startup; skins customize banner colors, spinner faces/verbs/wings, tool prefix, response box, branding text
- `process_command()` is a method on `DrewgentCLI` — dispatches on canonical command name resolved via `resolve_command()` from the central registry
- Skill slash commands: `agent/skill_commands.py` scans `~/.drewgent/skills/`, injects as **user message** (not system prompt) to preserve prompt caching

### Slash Command Registry (`drewgent_cli/commands.py`)

All slash commands are defined in a central `COMMAND_REGISTRY` list of `CommandDef` objects. Every downstream consumer derives from this registry automatically:

- **CLI** — `process_command()` resolves aliases via `resolve_command()`, dispatches on canonical name
- **Gateway** — `GATEWAY_KNOWN_COMMANDS` frozenset for hook emission, `resolve_command()` for dispatch
- **Gateway help** — `gateway_help_lines()` generates `/help` output
- **Telegram** — `telegram_bot_commands()` generates the BotCommand menu
- **Slack** — `slack_subcommand_map()` generates `/hermes` subcommand routing
- **Autocomplete** — `COMMANDS` flat dict feeds `SlashCommandCompleter`
- **CLI help** — `COMMANDS_BY_CATEGORY` dict feeds `show_help()`

### Adding a Slash Command

1. Add a `CommandDef` entry to `COMMAND_REGISTRY` in `drewgent_cli/commands.py`:
```python
CommandDef("mycommand", "Description of what it does", "Session",
           aliases=("mc",), args_hint="[arg]"),
```
2. Add handler in `DrewgentCLI.process_command()` in `cli.py`:
```python
elif canonical == "mycommand":
    self._handle_mycommand(cmd_original)
```
3. If the command is available in the gateway, add a handler in `gateway/run.py`:
```python
if canonical == "mycommand":
    return await self._handle_mycommand(event)
```
4. For persistent settings, use `save_config_value()` in `cli.py`

**CommandDef fields:**
- `name` — canonical name without slash (e.g. `"background"`)
- `description` — human-readable description
- `category` — one of `"Session"`, `"Configuration"`, `"Tools & Skills"`, `"Info"`, `"Exit"`
- `aliases` — tuple of alternative names (e.g. `("bg",)`)
- `args_hint` — argument placeholder shown in help (e.g. `"<prompt>"`, `"[name]"`)
- `cli_only` — only available in the interactive CLI
- `gateway_only` — only available in messaging platforms
- `gateway_config_gate` — config dotpath (e.g. `"display.tool_progress_command"`); when set on a `cli_only` command, the command becomes available in the gateway if the config value is truthy. `GATEWAY_KNOWN_COMMANDS` always includes config-gated commands so the gateway can dispatch them; help/menus only show them when the gate is open.

**Adding an alias** requires only adding it to the `aliases` tuple on the existing `CommandDef`. No other file changes needed — dispatch, help text, Telegram menu, Slack mapping, and autocomplete all update automatically.

---

## Adding New Tools

Requires changes in **3 files**:

**1. Create `tools/your_tool.py`:**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. Add import** in `model_tools.py` `_discover_tools()` list.

**3. Add to `toolsets.py`** — either `_HERMES_CORE_TOOLS` (all platforms) or a new toolset.

The registry handles schema collection, dispatch, availability checking, and error wrapping. All handlers MUST return a JSON string.

**Path references in tool schemas**: If the schema description mentions file paths (e.g. default output directories), use `display_drewgent_home()` to make them profile-aware. The schema is generated at import time, which is after `_apply_profile_override()` sets `HERMES_HOME`.

**State files**: If a tool stores persistent state (caches, logs, checkpoints), use `get_drewgent_home()` for the base directory — never `Path.home() / ".hermes"`. This ensures each profile gets its own state.

**Brain tools example** (`tools/brain_tool.py`):
brain_tool registers two tools — `brain_query` and `brain_record` — giving the agent
active bidirectional access to its wiki-based knowledge base. Unlike most tools which
perform an action and return a result, brain tools query/record structured knowledge
in the Obsidian wiki at `~/.drewgent/memories/`. See `tools/brain_tool.py` for the
implementation pattern.

**Brain maintenance** (`agent/auto_learn.py`):
The `WikiMaintenance` class provides autonomous wiki health operations:
- `retire_stale_entries()` — decision-matrix retirement (180d hard, 90d cold, 120d low-engagement)
- `deduplicate_wiki()` — removes duplicate daily log entries (normalized comparison)
- `detect_knowledge_gaps()` — identifies tracked topics without wiki coverage
- `run_autonomous_maintenance()` — runs all three with a single call

`AutoLearner.run_maintenance()` is called automatically at `shutdown_memory_provider()`
(session end) and also from the gateway cron ticker (every ~1 hour when gateway is running),
keeping the wiki healthy without requiring user intervention.

Access tracking: `query_wiki()` records which entries are returned via `_touch_result_ids()`,
updating `last_accessed` + `access_count` in the vector store. `Insight.should_retire()`
uses access frequency alongside file age for smarter retirement decisions.

Knowledge gap system: `detect_knowledge_gaps()` finds missing topics.
`get_growth_suggestions()` + `fill_gap()` let the agent proactively explore and fill gaps.
`query_wiki()` falls back to gap suggestions when no direct match is found.

**Agent-level tools** (todo, memory): intercepted by `run_agent.py` before
`handle_function_call()` — these are internal agent mechanisms, not external tools.
See `todo_tool.py` for the pattern. Brain tools are NOT agent-level tools; they
are regular registry tools like any other.

---

## Adding Configuration

### config.yaml options:
1. Add to `DEFAULT_CONFIG` in `drewgent_cli/config.py`
2. Bump `_config_version` (currently 5) to trigger migration for existing users

### .env variables:
1. Add to `OPTIONAL_ENV_VARS` in `drewgent_cli/config.py` with metadata:
```python
"NEW_API_KEY": {
    "description": "What it's for",
    "prompt": "Display name",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

### Config loaders (two separate systems):

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` |
| `load_config()` | `drewgent` tools`, `drewgent setup` | `drewgent_cli/config.py` |
| Direct YAML load | Gateway | `gateway/run.py` |

---

## Skin/Theme System

The skin engine (`drewgent_cli/skin_engine.py`) provides data-driven CLI visual customization. Skins are **pure data** — no code changes needed to add a new skin.

### Architecture

```
drewgent_cli/skin_engine.py    # SkinConfig dataclass, built-in skins, YAML loader
~/.drewgent/skins/*.yaml       # User-installed custom skins (drop-in)
```

- `init_skin_from_config()` — called at CLI startup, reads `display.skin` from config
- `get_active_skin()` — returns cached `SkinConfig` for the current skin
- `set_active_skin(name)` — switches skin at runtime (used by `/skin` command)
- `load_skin(name)` — loads from user skins first, then built-ins, then falls back to default
- Missing skin values inherit from the `default` skin automatically

### What skins customize

| Element | Skin Key | Used By |
|---------|----------|---------|
| Banner panel border | `colors.banner_border` | `banner.py` |
| Banner panel title | `colors.banner_title` | `banner.py` |
| Banner section headers | `colors.banner_accent` | `banner.py` |
| Banner dim text | `colors.banner_dim` | `banner.py` |
| Banner body text | `colors.banner_text` | `banner.py` |
| Response box border | `colors.response_border` | `cli.py` |
| Spinner faces (waiting) | `spinner.waiting_faces` | `display.py` |
| Spinner faces (thinking) | `spinner.thinking_faces` | `display.py` |
| Spinner verbs | `spinner.thinking_verbs` | `display.py` |
| Spinner wings (optional) | `spinner.wings` | `display.py` |
| Tool output prefix | `tool_prefix` | `display.py` |
| Per-tool emojis | `tool_emojis` | `display.py` → `get_tool_emoji()` |
| Agent name | `branding.agent_name` | `banner.py`, `cli.py` |
| Welcome message | `branding.welcome` | `cli.py` |
| Response box label | `branding.response_label` | `cli.py` |
| Prompt symbol | `branding.prompt_symbol` | `cli.py` |

### Built-in skins

- `default` — Classic Drewgent gold/kawaii (the current look)
- `ares` — Crimson/bronze war-god theme with custom spinner wings
- `mono` — Clean grayscale monochrome
- `slate` — Cool blue developer-focused theme

### Adding a built-in skin

Add to `_BUILTIN_SKINS` dict in `drewgent_cli/skin_engine.py`:

```python
"mytheme": {
    "name": "mytheme",
    "description": "Short description",
    "colors": { ... },
    "spinner": { ... },
    "branding": { ... },
    "tool_prefix": "┊",
},
```

### User skins (YAML)

Users create `~/.drewgent/skins/<name>.yaml`:

```yaml
name: cyberpunk
description: Neon-soaked terminal theme

colors:
  banner_border: "#FF00FF"
  banner_title: "#00FFFF"
  banner_accent: "#FF1493"

spinner:
  thinking_verbs: ["jacking in", "decrypting", "uploading"]
  wings:
    - ["⟨⚡", "⚡⟩"]

branding:
  agent_name: "Cyber Agent"
  response_label: " ⚡ Cyber "

tool_prefix: "▏"
```

Activate with `/skin cyberpunk` or `display.skin: cyberpunk` in config.yaml.

---

## Important Policies
### Prompt Caching Must Not Break

Drewgent-Agent ensures caching remains valid throughout a conversation. **Do NOT implement changes that would:**
- Alter past context mid-conversation
- Change toolsets mid-conversation
- Reload memories or rebuild system prompts mid-conversation

Cache-breaking forces dramatically higher costs. The ONLY time we alter context is during context compression.

### Working Directory Behavior
- **CLI**: Uses current directory (`.` → `os.getcwd()`)
- **Messaging**: Uses `MESSAGING_CWD` env var (default: home directory)

### Background Process Notifications (Gateway)

When `terminal(background=true, check_interval=...)` is used, the gateway runs a watcher that
pushes status updates to the user's chat. Control verbosity with `display.background_process_notifications`
in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS` env var):

- `all` — running-output updates + final message (default)
- `result` — only the final completion message
- `error` — only the final message when exit code != 0
- `off` — no watcher messages at all

---

## Profiles: Multi-Instance Support

Drewgent supports **profiles** — multiple fully isolated instances, each with its own
`HERMES_HOME` directory (config, API keys, memory, sessions, skills, gateway, etc.).

The core mechanism: `_apply_profile_override()` in `drewgent_cli/main.py` sets
`HERMES_HOME` before any module imports. All 119+ references to `get_drewgent_home()`
automatically scope to the active profile.

### Rules for profile-safe code

1. **Use `get_drewgent_home()` for all HERMES_HOME paths.** Import from `drewgent_constants`.
   NEVER hardcode `~/.drewgent` or `Path.home() / ".hermes"` in code that reads/writes state.
   ```python
   # GOOD
   from drewgent_constants import get_drewgent_home
   config_path = get_drewgent_home() / "config.yaml"

   # BAD — breaks profiles
   config_path = Path.home() / ".hermes" / "config.yaml"
   ```

2. **Use `display_drewgent_home()` for user-facing messages.** Import from `drewgent_constants`.
   This returns `~/.drewgent` for default or `~/.drewgent/profiles/<name>` for profiles.
   ```python
   # GOOD
   from drewgent_constants import display_drewgent_home
   print(f"Config saved to {display_drewgent_home()}/config.yaml")

   # BAD — shows wrong path for profiles
   print("Config saved to ~/.drewgent/config.yaml")
   ```

3. **Module-level constants are fine** — they cache `get_drewgent_home()` at import time,
   which is AFTER `_apply_profile_override()` sets the env var. Just use `get_drewgent_home()`,
   not `Path.home() / ".hermes"`.

4. **Tests that mock `Path.home()` must also set `HERMES_HOME`** — since code now uses
   `get_drewgent_home()` (reads env var), not `Path.home() / ".hermes"`:
   ```python
   with patch.object(Path, "home", return_value=tmp_path), \
        patch.dict(os.environ, {"HERMES_HOME": str(tmp_path / ".hermes")}):
       ...
   ```

5. **Gateway platform adapters should use token locks** — if the adapter connects with
   a unique credential (bot token, API key), call `acquire_scoped_lock()` from
   `gateway.status` in the `connect()`/`start()` method and `release_scoped_lock()` in
   `disconnect()`/`stop()`. This prevents two profiles from using the same credential.
   See `gateway/platforms/telegram.py` for the canonical pattern.

6. **Profile operations are HOME-anchored, not HERMES_HOME-anchored** — `_get_profiles_root()`
   returns `Path.home() / ".hermes" / "profiles"`, NOT `get_drewgent_home() / "profiles"`.
This is intentional — it lets `drewgent` -p coder profile list` see all profiles regardless
of which one is active.

---

## Brain Signal System

Self-awareness architecture for tool/skill integration. The agent tracks its own state during integration workflows and receives proactive hints about missing steps.

### Architecture (3 Layers)

```
user_prompt → SignalEmitter → event_bus → SignalProcessor
                                          ↓
                                    IntegrationWorkflow
                                          ↓
                                    ArchitectureModel
                                          ↓
                                  AwarenessReporter → hint injection
```

| Layer | File | Role |
|-------|------|------|
| 감각계 | `agent/brain_signals.py` (351 lines) | SignalEmitter — detects patterns, emits events |
| 판별 레이어 | `agent/signal_processor.py` (650 lines) | IntegrationWorkflow tracking + correlation mapping |
| 행동 레이어 | `agent/awareness_reporter.py` (295 lines) | Progress hint generation + guidance |
| Event bus | `agent/event_bus.py` | Pub/sub singleton connecting all layers |

### Signal Types

```
user.prompt                  — user message received
tool.start                   — tool call started
tool.complete                — tool call finished
agent.modifying              — file written/patched
tool.integration.start       — tool integration intent detected
tool.integration.detected    — tool file modification detected
skill.integration.start      — skill integration intent detected
skill.integration.detected  — skill file modification detected
brain.awareness.*            — awareness layer signals (emitted by processor)
brain.report.hint            — hint delivered to agent
session.end                  — session ending
```

### Integration Workflow (Tool Example)

When user asks to add a tool:

1. **SignalEmitter.user_prompt()** — detects intent, emits `tool.integration.start`
2. **SignalProcessor._on_integration_start()** — creates `IntegrationWorkflow` with workflow_id
3. **AwarenessReporter._on_integration_started()** — delivers initial guidance hint
4. **Agent modifies `tools/new_tool.py`** — `agent_modifying` event → processor tracks file
5. **SignalProcessor._on_agent_modifying()** — calls `arch_model.detect_tool_integration_progress()`
6. **AwarenessReporter._on_integration_progress()** — emits "다음: model_tools.py" hint
7. Hint is **injected into user message** at API call time (ephemeral, not persisted)
8. Agent modifies `model_tools.py` → progress hint updates to "다음: toolsets.py"
9. Agent modifies `toolsets.py` → `is_complete=True` → completion event
10. **Workflow moves to history** — completion celebration emitted

### Persistence

Active workflows are saved to `sessionDB` (`integration_workflows` table, v8 schema) on `shutdown_memory_provider()` and restored on agent init. Enables mid-session interruption recovery.

```python
# Save: shutdown_memory_provider() → persist_active_workflows(session_db, session_id)
# Restore: __init__() → get_signal_processor().restore_workflows(session_db, session_id)
```

### Hint Injection

In `run_agent.py` main loop (per-turn API call preparation):
- At `current_turn_user_idx`, checks `get_signal_processor().get_active_workflows()`
- For each active workflow, calls `ArchitectureModel.detect_*_integration_progress()`
- Appends `next_hint` to user message content as ephemeral injection (never persisted)

### run_agent.py Call Sites

| Location | Signal | Trigger |
|----------|--------|---------|
| `__init__` (~line 1196) | `tool_start("tool_registry_loaded")` | agent init |
| `__init__` (~line 1203) | `restore_workflows()` | agent init |
| `run_conversation` (~line 8242) | `user_prompt()` | user message received |
| sequential tool path | `tool_start`, `tool_complete` | each tool call |
| sequential tool path | `agent_modifying` | after each file-modifying tool result |
| `shutdown_memory_provider` (~line 3021) | `persist_active_workflows()` | session end |
| `shutdown_memory_provider` (~line 3031) | `session_end()` | session end |

### ArchitectureModel Reference

**Tool integration** — 3 files must be modified:
```python
TOOL_INTEGRATION_FILES = ["tools/", "model_tools.py", "toolsets.py"]
```

**Skill integration** — 2 steps:
```python
SKILL_INTEGRATION_FILES = ["skills/", "agent/skill_commands.py"]
```

## Known Pitfalls

### Python 3.14: DO NOT use `json.loads/dumps` after local assignment in the same function

Python 3.14's compiler has a bug where referencing `json` (the module) in an `except` clause causes Python to treat `json` as a local variable throughout the entire function scope. If `json.loads()` or `json.dumps()` is called before the `except` clause, an `UnboundLocalError: cannot access local variable 'json' where it is not associated with a value` is raised.

**Affected pattern (buggy in Python 3.14):**
```python
def run_conversation(self, ...):
    ...
    try:
        json.loads(args)          # Causes UnboundLocalError in Python 3.14
    except json.JSONDecodeError:  # "json" here triggers the scope bug
        ...
```

**Correct patterns - choose one:**

1. **Use `__import__()` to bypass the scope resolution:**
   ```python
   try:
       __import__('json').loads(args)
   except Exception as e:
       if type(e).__name__ == "JSONDecodeError":
           ...
   ```

2. **Move json operations before any `except json.*` clauses, or use a wrapper function:**
   ```python
   def _json_loads(s):
       return __import__('json').loads(s)
   
   def run_conversation(self, ...):
       try:
           _json_loads(args)   # Also works
       except Exception as e:
           ...
   ```

**Why this happens:** Python 3.14's compiler performs scope analysis before execution. Seeing `json.X` in an `except` clause marks `json` as a local name. When `json.loads()` appears before the `except` clause (in execution order), the local hasn't been assigned yet → UnboundLocalError. This affects **any large function** (`run_conversation` is ~3500 lines), regardless of where the `json` reference appears in source order.

**Current known occurrences** that were fixed:
- `run_agent.py:10934` — `json.loads(args)` in `run_conversation` tool-call validation loop
- `run_agent.py:10924` — `json.dumps(args)` in same loop
- `run_agent.py:10637` — `json.dumps(raw)` in same function

Other files (`tools/*.py`, `agent/*.py`) are smaller and don't trigger this bug because their `except json.*` clauses are in narrow scopes where `json.loads/dumps` calls are either all before or all after the `except`.

### DO NOT hardcode `~/.drewgent` paths
Use `get_drewgent_home()` from `drewgent_constants` for code paths. Use `display_drewgent_home()`
for user-facing print/log messages. Hardcoding `~/.drewgent` breaks profiles — each profile
has its own `HERMES_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

### DO NOT use `simple_term_menu` for interactive menus
Rendering bugs in tmux/iTerm2 — ghosting on scroll. Use `curses` (stdlib) instead. See `drewgent_cli/tools_config.py` for the pattern.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

### Tests must not write to `~/.drewgent/`
The `_isolate_drewgent_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.drewgent/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_drewgent_home()` resolve within the temp dir.
Use the pattern from `tests/drewgent_cli/test_profiles.py`:
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

---

## Model Configuration & Smart Routing

Drewgent uses a two-tier model strategy to balance cost and capability.

### Current Setup (config.yaml)

```yaml
# Primary: strong reasoning model for complex coding tasks
model:
  provider: minimax
  model: minimax-m3

# Fallback: when primary is unavailable (rate-limit, overload)
fallback_model:
  provider: opencode-go
  model: deepseek-v4-flash

# Smart routing: cheap model for simple turns (auto-detected)
smart_model_routing:
  enabled: true
  max_simple_chars: 160
  max_simple_words: 28
  cheap_model:
    provider: opencode-go
    model: deepseek-v4-flash

# Auxiliary tasks (compression, search, MCP, vision, etc.)
# All routed to cheap model for efficiency
auxiliary:
  vision/provider: opencode-go
  web_extract/provider: opencode-go
  compression/provider: opencode-go
  session_search/provider: opencode-go
  mcp/provider: opencode-go
  flush_memories/provider: opencode-go
```

### Routing Logic

| Condition | Route | Cost |
|-----------|-------|------|
| Short question (≤160 chars, ≤28 words, no code/URL/keywords) | `opencode-go/deepseek-v4-flash` | $0.14/$0.28 Mtok |
| Complex task (code blocks, URLs, keywords like "debug/implement/refactor") | `minimax/minimax-m3` | ~$0.60/$2.40 Mtok |
| Primary unavailable | `opencode-go/deepseek-v4-flash` (fallback) | $0.14/$0.28 Mtok |
| Compression, search, MCP, vision, flush_memories | `opencode-go/deepseek-v4-flash` | $0.14/$0.28 Mtok |

### Smart Routing Detection

The `choose_cheap_model_route()` function in `agent/smart_model_routing.py` conservatively routes to cheap model only when ALL conditions are met:
- Message ≤ `max_simple_chars` (160)
- Word count ≤ `max_simple_words` (28)
- No newlines beyond 1
- No code backticks (`` ` `` or `` ``` ``)
- No URLs
- No complex keywords (debug, implement, refactor, test, docker, etc.)

### Credential Requirements

- `MINIMAX_API_KEY` — for primary model (MiniMax M3)
- `OPENCODE_GO_API_KEY` — for fallback, smart routing, and all auxiliary tasks (OpenCode Go subscription)

Both stored in `~/.drewgent/.env` (chmod 600).

---

## Testing

```bash
source venv/bin/activate
python -m pytest tests/ -q          # Full suite (~3000 tests, ~3 min)
python -m pytest tests/test_model_tools.py -q   # Toolset resolution
python -m pytest tests/test_cli_init.py -q       # CLI config loading
python -m pytest tests/gateway/ -q               # Gateway tests
python -m pytest tests/tools/ -q                 # Tool-level tests
```

Always run the full suite before pushing changes.

---

## Cron & AIAgent 상태 (2026-06-05)

### 배경
`run_agent.py`와 `agent/` 디렉토리에 upstream refactoring 코드가 **반만 적용**된 상태였음.
- `run_agent.py`에 추출된 모듈(`conversation_loop.py`, `api_retry_loop.py` 등)을 참조하는 코드가 추가됨
- 하지만 해당 모듈 파일들은 working tree에 없음 (upstream branch 미병합 상태)
- 이로 인해 `api_start_time`, `retry_count` 등 15+ 변수가 undefined 상태가 되어 AIAgent 기동 불가

### 해결 (2026-06-05)
- `git checkout HEAD -- run_agent.py agent/`로 HEAD (working pre-refactoring) 상태로 복원
- AIAgent 정상 작동 확인 (`deepseek-v4-pro`로 OKAY 응답)
- `cron/scheduler.py`의 cron model config + api_mode 보정 코드는 유지
- `gateway/run.py`의 `GatewayRunner` stub 메서드는 유지

### 크론 작업
- SEO/Trend Harvester: `script_only` 모드 유지 (직접 스크립트 실행, AIAgent 불필요)
- 향후 AIAgent 기반 크론 잡 추가 시 정상 작동 가능

### 게이트웨이 주의사항
- `gateway/run.py` `GatewayRunner`에 `start()`/`stop()`/`wait_for_shutdown()` 메서드 추가됨
- 변경사항 적용을 위해 게이트웨이 재시작 필요:
  ```bash
  drewgent gateway stop && drewgent gateway run --replace
  ```

---

## Cron Jobs 전체 현황 (2026-06-05)

### script_only (AIAgent 불필요, 직접 스크립트 실행) — 8개

| 작업 | 주기 | 스크립트 |
|------|------|---------|
| SEO Article Harvester | 6시간 | `scripts/cron_seo_harvester.py` |
| Trend Harvester | 6시간 | `scripts/cron_trend_harvester.py` |
| kanban-dispatcher | 1분 | `scripts/dispatch_once_default.py` |
| kanban-dispatcher-content | 1분 | `scripts/dispatch_once_content.py` |
| kanban-dispatcher-integrations | 1분 | `scripts/dispatch_once_integrations.py` |
| cron-output-cleanup | 매일 04:00 | `scripts/cron_output_cleanup.py` |
| brain-signal-report | 매일 09:00 | `scripts/cron_brain_signal_report.py` |
| kanban-maintenance | 매주 일 03:00 | `scripts/kanban_maintenance.py` |

### LLM 필요 — 2개

| 작업 | 주기 | 비고 |
|------|------|------|
| content-pipeline | 3시간 | AIAgent로 콘텐츠 선별·kanban 태스크 생성 |
| site-spec-audit-weekly | 매주 일 04:00 | AIAgent + MCP 사이트 감사 |

### 전환 배경
- ALL jobs were going through AIAgent → LLM 예산 + API rate limit 소진
- kanban-dispatcher 3개는 **매 1분** 실행 → 하루 12,960회 API 호출이 script_only로 0회로 감소
- `run_agent.py` HEAD 복원으로 AIAgent는 정상 작동 확인됨
- content-pipeline, site-spec-audit만 AIAgent 유지 (tool calling 필요)

### Log Noise Fixes (2026-06-05)
- `source/_agent/orchestrator/orchestrate_tool.py`: P4-cortex orchestrator `warning` → `info`
- `agent/brain_monitor.py`: 죽은 `from gateway.run import GatewayState` 제거 → 뇌신호 리포트 Discord 전달 활성화
- `agent/signal_processor.py`: QA gate `contract`/`micro` phase는 `info`, "BLOCKED" 문구 제거. `full` phase만 `warning` 유지

### Kanban Dispatcher 개선 (Phase 1-4, 2026-06-05)
- **Phase 1**: 적응형 MAX_CLAIM (큐 깊이 3/5/10), 실패 태스크 후순위, 소진 태스크 보고
- **Phase 2**: Backpressure (MAX_CLAIM ≤ ready_count // 2)
- **Phase 3**: Heartbeat watchdog (5분 무응답 SIGTERM → reclaim)
- **Phase 4**: Worker affinity (`skills` 기반 cooldown, `worker_affinity.json` 통계)

### Known Issues
- **content-pipeline tool format**: DeepSeek API가 `tools[0].function: missing field 'name'` 오류 반환.
  OpenAI SDK로 직접 요청 시 정상, drewgent 에이전트 SDK 선택 로직 문제. `cron/scheduler.py` api_mode 보정 코드 추가 (00:18). 03:00 실행 검증 필요.
- **upstream refactoring**: `run_agent.py` refactoring 커밋이 main에 미병합 상태. `git checkout HEAD -- run_agent.py agent/`로 pre-refactoring 유지 중.

