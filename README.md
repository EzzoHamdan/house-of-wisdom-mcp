# House of Wisdom MCP

> The medieval Bayt al-Hikma succeeded because it was **diverse**. Scholars, translators, and thinkers from many traditions worked side by side, each bringing a distinct lens to the same questions. This MCP does the same for AI: consult multiple model families in parallel; each investigates independently and returns its own complete perspective. There is no synthesizer — *you* (the orchestrator) weigh the perspectives yourself, exactly as the House of Wisdom's readers weighed many sources.

**Multi-AI consultation, not synthesis.** An MCP (Model Context Protocol) server that consults multiple AI models simultaneously — OpenAI, Anthropic via OpenRouter, DeepSeek, local Ollama models, and any OpenAI-compatible API. Get more reliable, comprehensive answers by combining insights from diverse model families, each grounded in its own investigation.

> **Why the name:** the original [House of Wisdom](https://en.wikipedia.org/wiki/House_of_Wisdom) (9th-century Baghdad) was a multi-tradition, multi-discipline institution where the best answers emerged from many perspectives, not from a single authority. That is the entire premise of this tool.

---

## The three modes (SCRIBE / TRANSLATOR / SCHOLAR)

The House of Wisdom framing maps to three cognitive depths, named for medieval roles. Pick a mode based on how complex and open the question is.

### SCRIBE — `mode: "scribe"`
One-shot from given context, **no tool calls**, ~10s. The fast path.

The medieval scribe copied and answered from the manuscript in front of him — he didn't go looking for more. Use SCRIBE when:
- You have **already pre-fed the relevant file/code contents** into `context`, OR
- The question is a **judgment call** that doesn't need codebase verification.

Equivalent to the old `agentic: false`. Three parallel inferences, no tools. Fits a 3-concurrent-model cloud plan (e.g. Ollama Pro).

### TRANSLATOR — `mode: "translator"` (default)
Bounded, **scope-caged** tool loop. Each consultant investigates within `scope_hint` and a tight tool budget (~12 calls), then answers. ~30-60s.

The medieval translator worked carefully through specific source material — he stayed with the text he was given. Use TRANSLATOR when:
- The answer must be **grounded in specific known files** (you know which files matter), AND
- You want each consultant to **verify its claims** against those files before answering.

Equivalent to the old `agentic: true`. The `scope_hint` is a cage — consultants are told not to wander outside it.

### SCHOLAR — `mode: "scholar"`
**Liberated free inquiry.** Generous tool budget (~64 calls), `scope_hint` treated as a starting point not a cage, consultants may follow relevant threads elsewhere in the workspace. Slowest; bounded by `parallel_timeout`.

The medieval scholar was a free inquirer — he followed leads across the library, aiming for thoroughness. Use SCHOLAR when:
- The question is **genuinely open** — you don't know in advance which files matter, AND
- You want consultants to **explore broadly** and follow relevant threads.

The `scope_hint` is a starting point, not a cage. Consultants may read elsewhere in the workspace if it materially advances the answer.

### Decision axis

| Mode | What you get | When to use it |
|---|---|---|
| SCRIBE | Diversity on a settled question (3 minds, no tools) | Pre-fed context or judgment calls |
| TRANSLATOR | Diversity + grounding in known material | Hard problems with known files |
| SCHOLAR | Diversity + free inquiry for open questions | Genuinely open investigations |

**Do not fire the council reflexively on every prompt** — only when a different model family seeing the problem would actually change the outcome. For puzzles that just need your own focused reasoning, use your IDE's built-in thinking tool (e.g. `sequentialthinking` if you have it installed) — one mind, instant, free.

---

## The two tools

### `ai_council_list_models`
Lists the configured consultants and whether each is enabled. Call this **before** `ai_council` if you want to:
- Show the user their current roster
- Let the user pick a subset
- Check what's available

Returns: `[{name, model_id, provider, enabled}]` + `max_models` + `enabled_count`.

This tool does **not** ping endpoints or verify API keys — it only reflects the server's loaded config.

### `ai_council`
Fires the council. Returns a list of independent perspectives (no synthesizer), each tagged with the mode it ran in.

**Arguments:**
| Arg | Type | Required | Description |
|---|---|---|---|
| `context` | string | yes | Background info. For SCRIBE mode, paste file contents here (up to 200k chars). For TRANSLATOR/SCHOLAR, brief context is enough — consultants read files themselves. |
| `question` | string | yes | The question to answer. |
| `mode` | string | no | `"scribe"` / `"translator"` / `"scholar"`. Takes precedence over the deprecated `agentic` arg. Defaults to `translator` if `synthesizer_tools.enabled` is true, else `scribe`. |
| `workspace_root` | string | no | Sandbox root for TRANSLATOR/SCHOLAR modes. Absolute path. Only used when mode is not scribe. |
| `scope_hint` | string | no | TRANSLATOR/SCHOLAR only. Natural-language scope, e.g. `"Start with A.md and B.md"`. In TRANSLATOR mode this is a cage; in SCHOLAR mode it's a starting point. |
| `models` | array of strings | no | Subset of consultant names to fire, e.g. `["GLM", "Opus"]`. Unknown names ignored; if none match, call fails. Omit to use all enabled models. |
| `agentic` | boolean | no | **DEPRECATED** — use `mode` instead. `false`→SCRIBE, `true`→TRANSLATOR. Kept for backward compat. |

**Return shape:**
```json
{
  "status": "success",
  "data": {
    "perspectives": [
      {"label": "GLM", "model_name": "GLM", "code_name": "Alpha", "analysis": "...", "status": "ok", "mode": "translator"},
      {"label": "Kimi", "model_name": "Kimi", "code_name": "Beta", "analysis": "...", "status": "ok", "mode": "translator"}
    ],
    "consensus": {"models_queried": 2, "models_succeeded": 2, "models_failed": 0}
  }
}
```

---

## How to install the MCP

### Prerequisites
- **Python 3.10+**
- **`uv`/`uvx` installed** — [installation guide](https://docs.astral.sh/uv/getting-started/installation). Verify with `uvx --version`.
- **For local models:** [Ollama](https://ollama.com) running on `localhost:11434`. Pull the models you want to use (`ollama pull glm-5.2:cloud` etc.).
- **For paid API models:** the relevant API keys (OpenAI, OpenRouter, DeepSeek, etc.).

### Step 1 — create your config file

Copy the example config from this repo and edit it:

```bash
curl -O https://raw.githubusercontent.com/EzzoHamdan/house-of-wisdom-mcp/master/config.example.yaml
# edit it: set your models, API keys, and preferred defaults
```

Or write one from scratch — see `config.example.yaml` in this repo for a fully commented template. The only field you strictly need is `models`.

### Step 2 — register the MCP server with your IDE

The registration syntax differs per IDE, but the server command is the same everywhere. Pick your client:

#### Kilo Code
`~/.config/kilo/kilo.json` (or project `kilo.json`):
```json
{
  "mcp": {
    "ai-council": {
      "type": "local",
      "command": ["uvx", "--from", "git+https://github.com/EzzoHamdan/house-of-wisdom-mcp@master", "ai-council", "--config", "/path/to/your/config.yaml"],
      "enabled": true,
      "timeout": 240000
    }
  }
}
```

#### Claude Desktop
`~/.claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):
```json
{
  "mcpServers": {
    "ai-council": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/EzzoHamdan/house-of-wisdom-mcp@master", "ai-council", "--config", "/path/to/your/config.yaml"]
    }
  }
}
```

#### Claude Code (CLI)
```bash
claude mcp add ai-council uvx --from git+https://github.com/EzzoHamdan/house-of-wisdom-mcp@master ai-council --config /path/to/your/config.yaml
```

#### Cursor
`.cursor/mcp.json` in your project, or Settings → MCP:
```json
{
  "mcpServers": {
    "ai-council": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/EzzoHamdan/house-of-wisdom-mcp@master", "ai-council", "--config", "/path/to/your/config.yaml"]
    }
  }
}
```

#### Codex (OpenAI CLI)
`~/.codex/config.toml`:
```toml
[mcp_servers.ai-council]
command = "uvx"
args = ["--from", "git+https://github.com/EzzoHamdan/house-of-wisdom-mcp@master", "ai-council", "--config", "/path/to/your/config.yaml"]
```

#### Any other MCP-capable client
If your client supports stdio MCP servers, it can run this one. The server side is identical; only the registration syntax changes. Check your client's docs for "MCP" support.

### Step 3 — restart your IDE / MCP client

The server loads your config at startup. After editing the config or upgrading the server, restart the client to pick up changes.

### Step 4 — verify

Ask your AI assistant to call `ai_council_list_models`. It should return your configured roster. Then fire a test council call in SCRIBE mode (fast, no tools) to confirm the models respond.

---

## Adding, enabling, and disabling consultants

All consultants live in your config file. The server loads them at startup; flip `enabled: true/false` and restart to apply.

### Local Ollama models (free)

```yaml
models:
  - name: "GLM"                         # human-readable label shown in perspectives
    provider: "custom"
    model_id: "glm-5.2:cloud"           # the Ollama tag (run `ollama list`)
    base_url: "http://localhost:11434/v1"
    api_key: "ollama"                    # any non-empty string works for local Ollama
    enabled: true
```

To disable one temporarily: set `enabled: false`. To remove it: delete the block.

### Paid OpenAI models

```yaml
# Add your key at the top of the file (or via env var AI_COUNCIL_OPENAI_API_KEY)
openai_api_key: "sk-..."

models:
  - name: "GPT-5.6-Terra"
    provider: "openai"
    model_id: "gpt-5.6-terra"
    enabled: true
```

### Paid Anthropic models via OpenRouter

OpenRouter is one API key for many models — see [openrouter.ai/models](https://openrouter.ai/models). This is the easiest way to add Claude, Gemini, and many others with a single key.

```yaml
# Add your key at the top of the file (or via env var AI_COUNCIL_OPENROUTER_API_KEY)
openrouter_api_key: "sk-or-..."

models:
  - name: "Claude Opus"
    provider: "openrouter"
    model_id: "anthropic/claude-opus-4"
    enabled: true
  - name: "Gemini Pro"
    provider: "openrouter"
    model_id: "google/gemini-2.5-pro"
    enabled: true
```

### Paid DeepSeek models (direct API)

DeepSeek's API is OpenAI-compatible at `https://api.deepseek.com/v1`. Use `provider: custom` with the per-model `api_key` override:

```yaml
models:
  - name: "DeepSeek-Pro"
    provider: "custom"
    model_id: "deepseek-chat"
    base_url: "https://api.deepseek.com/v1"
    api_key: "sk-your-deepseek-key"
    enabled: true
```

### Any other OpenAI-compatible endpoint

Perplexity, Together, Groq, local vLLM, etc. — same pattern as DeepSeek:

```yaml
models:
  - name: "Perplexity"
    provider: "custom"
    model_id: "llama-3.1-sonar-large-128k-online"
    base_url: "https://api.perplexity.ai"
    api_key: "your-perplexity-key"
    enabled: false
```

### API keys — three ways to pass them

1. **Yaml top-level fields:** `openai_api_key`, `openrouter_api_key` (pulled by models with `provider: openai` / `provider: openrouter`)
2. **Env vars:** `AI_COUNCIL_OPENAI_API_KEY`, `AI_COUNCIL_OPENROUTER_API_KEY`
3. **Per-model `api_key` override:** set `api_key` directly on the model entry (used by `provider: custom` models; also overrides the top-level key for `openai`/`openrouter` providers)

### The `max_models` cap

`max_models` (default 3, hard cap 10) controls how many consultants actually run per call. You can configure 8 models and only fire 3 — or use the per-call `models` arg to pick a subset. The first `max_models` enabled models in the list fire by default.

### Mixed local + API considerations

The `max_concurrent_consultants` semaphore (default 3) caps how many consultant loops run at once — this keeps you within Ollama Cloud plan limits (Pro = 3, Max = 10). Paid API models also go through the same semaphore, but since they're remote and fast, capping them at 3 is rarely a bottleneck. If you add many API models and want them unbounded while keeping Ollama capped, that needs a per-provider semaphore (not currently implemented — open an issue if you need it).

---

## Agentic consultants (TRANSLATOR & SCHOLAR modes)

When `synthesizer_tools.enabled: true` in config, TRANSLATOR and SCHOLAR mode calls give every consultant its own **read-only** tool-calling loop sandboxed to `workspace_root`:

| Tool | Purpose |
|---|---|
| `read_file(path)` | Read a UTF-8 file inside the sandbox |
| `list_dir(path)` | List directory entries |
| `glob_search(pattern)` | Find files by glob pattern |
| `think(thought)` | Internal reflection step (the council's built-in thinking tool) |

**Sandbox & safety:**
- Every path is resolved and checked against `workspace_root` via `Path.resolve()` + `relative_to()`. Symlinks escaping the root are rejected.
- No write, bash, or network tools — consultants can *look*, not *act*.
- Hard cap on tool iterations per consultant: `max_tool_iterations` (TRANSLATOR, default 12) or `scholar_max_tool_iterations` (SCHOLAR, default 64). After the cap, a final analysis is forced.
- Each consultant is told its exact tool budget up front and given an optional `scope_hint`. In TRANSLATOR mode the scope is a cage; in SCHOLAR mode it's a starting point.

---

## Configuration reference

```yaml
# Top-level keys
max_models: 3                          # how many consultants fire per call (1..10)
parallel_timeout: 240                   # server-side cap per call, seconds
log_level: "INFO"                       # DEBUG | INFO | WARNING | ERROR
anonymous_perspectives: false           # true = label perspectives Alpha/Beta/Gamma; false = real model names
max_concurrent_consultants: 3           # semaphore cap; Ollama Pro=3, Max=10

# API keys (optional — only needed if you add paid models)
openai_api_key: "sk-..."
openrouter_api_key: "sk-or-..."

# Agentic consultant tool loop (TRANSLATOR & SCHOLAR modes)
synthesizer_tools:
  enabled: true                         # makes TRANSLATOR/SCHOLAR modes available
  workspace_root: null                  # sandbox default; usually passed per-call
  max_tool_iterations: 12               # TRANSLATOR mode per-consultant tool budget
  scholar_max_tool_iterations: 64      # SCHOLAR mode per-consultant tool budget
  allowed_tools: ["read_file", "list_dir", "glob_search", "think"]

# The consultant roster
models:
  - name: "GLM"
    provider: "custom"
    model_id: "glm-5.2:cloud"
    base_url: "http://localhost:11434/v1"
    api_key: "ollama"
    enabled: true
  # ... add more here
```

---

## CLI args

`--config <path>` (config file) · `--max-models N` · `--parallel-timeout N` · `--log-level DEBUG|INFO|WARNING|ERROR` · `--openai-api-key` · `--openrouter-api-key`

---

## How it works (v0.4.x architecture)

1. **Orchestrator** (you, the MCP client) decides the question is council-worthy and picks a mode (SCRIBE / TRANSLATOR / SCHOLAR).
2. **`ai_council_list_models`** (optional) — show the user the roster, let them pick a subset.
3. **`ai_council`** fires the selected consultants in parallel (concurrency-capped by `max_concurrent_consultants`).
   - **SCRIBE:** each consultant answers plain-text from `context`. One inference each. No tool calls.
   - **TRANSLATOR:** each consultant runs a bounded, scope-caged tool loop against `workspace_root`, investigates within scope, then emits its analysis.
   - **SCHOLAR:** each consultant runs a liberated tool loop with a generous budget, explores broadly, then emits its analysis.
4. The server returns a **list of independent perspectives** (no synthesizer, no merging), each tagged with the mode it ran in.
5. The orchestrator reads all perspectives, weighs them, and decides what to do. **Do not treat any single perspective as ground truth.**

---

## Acknowledgments

This project was inspired by [Cognition Wheel](https://github.com/Hormold/cognition-wheel) — the wisdom-of-crowds approach to AI consultation that seeded the multi-model philosophy.

The v0.4.x line is a significant architectural departure from it: no synthesizer, three named modes (SCRIBE / TRANSLATOR / SCHOLAR), agentic consultants, dynamic concurrency, per-call model selection, and a baked-in decision rule. The medieval House of Wisdom lent the project its name and its guiding principle: diverse perspectives, weighed by the reader, not merged by an authority.