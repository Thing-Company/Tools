# Tools - OpenCode Go Subscription Bridge

A lightweight Python-based bridge that lets you use your **OpenCode Go subscription** with **Claude Code** or **Codex CLI** from the command line.

## How it works

This tool starts a local compatibility proxy that:
- Translates Anthropic Messages API requests (Claude Code) → OpenCode Go API
- Translates OpenAI Chat Completions/Responses API requests (Codex) → OpenCode Go API
- Injects your OpenCode Go API key automatically

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your OpenCode Go API key
python ocgo_bridge.py setup

# 3. Launch either tool with no model flag needed
python ocgo_bridge.py launch claude
python ocgo_bridge.py launch codex

# Or pick a model per run
python ocgo_bridge.py launch claude --model mimo-v2.6-flash
```

## Commands

| Command | Description |
|---------|-------------|
| `setup` | Save your OpenCode Go API key |
| `launch claude` | Start Claude Code via local proxy |
| `launch codex` | Start Codex CLI via local proxy |
| `serve` | Run only the local proxy server |
| `status` | Check proxy status |
| `stop` | Stop background proxy |
| `list` | List known OpenCode Go models |
| `mapping` | Configure model name mappings |

## Configuration

Config is stored in `~/.config/opencode-go-bridge/config.json`:
```json
{
  "api_key": "your-opencode-go-api-key",
  "host": "127.0.0.1",
  "port": 3456,
  "model": "longcat-2.0",
  "codex_model": "grok-4.7"
}
```

`model` is the default for Claude Code, `codex_model` for Codex. They cannot be
the same value: Codex speaks only the OpenAI Responses API and most Go models
do not serve it.

## How Config Is Passed

Nothing is written to `~/.claude` or `~/.codex`, and no login is required for
either tool.

Claude Code reads a different env var per job, so all of them are set to the
same model: `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL` for background work,
and the `ANTHROPIC_DEFAULT_OPUS_MODEL` / `_SONNET_` / `_HAIKU_` trio for its
model picker. Leaving any slot unset makes it ask for a real Claude model and
fail before a request is sent.

Codex is the opposite. It ignores `OPENAI_BASE_URL` for its built-in provider,
which is why it needs an explicit provider entry. That entry is passed as `-c`
flags, scoped to the single launch, and `CODEX_HOME` points at a throwaway
temp directory so a stored ChatGPT login cannot override the API key and trip
Codex's own client-side model validation. The temp directory is removed on exit.

## Model Mappings

Map Claude Code/Codex model names to OpenCode Go models:
```bash
# Map Claude's "claude-sonnet" to OpenCode Go's "kimi-k2.6"
python ocgo.py mapping claude claude-sonnet kimi-k2.6

# Map Codex's "gpt-4" to OpenCode Go's "gpt-5.3-codex"
python ocgo.py mapping codex gpt-4 gpt-5.3-codex
```

## Verified Model Matrix

Measured 2026-10-15 on a Go subscription. Output tokens are what a coding
agent actually burns, which is what a metered plan charges against.

### Claude Code (Anthropic Messages API)

| Model | Speed | Output tokens | Tool calls | Notes |
|---|---|---|---|---|
| `longcat-2.0` | 2.2s | 82 | yes | **Best value.** Correct code, fast, cheap |
| `longcat-2.5-preview-free` | 4.5s | 144 | yes | Free tier, correct code |
| `mimo-v2.6-flash` | 2.6s | 35 | yes | Cheapest. Good code, weak math |
| `kimi-k2.7-code` | 1.3s | 248 | yes | Code-focused |
| `minimax-m2.5` | 1.5s | 81 | yes | Cheap |
| `glm-5.3-flash` | 0.6s | 185 | yes | Fastest; sometimes calls a tool when asked for plain text |
| `kimi-k2.6` | 27s | 987 | yes | Avoid. 20-40x the tokens of the flash models |
| `kimi-k3` | slow | 1600+ | yes | Avoid for cost |

### Codex (OpenAI Responses API)

| Model | Status |
|---|---|
| `grok-4.7` | Works |
| `kimi-*`, `gpt-5-3-codex` | Rejected: these models do not serve the Responses protocol |
| `longcat-*`, `glm-*`, `mimo-*` | Untested with Codex |

`deepseek-*` models are blocked unless the workspace Privacy setting is
"Global". Raise that to unlock them.

### The cost trap

Reasoning models spend tokens on hidden `reasoning` before answering. On a
trivial "say hi in 3 words" prompt, `kimi-k2.6` consumed 330 output tokens and
`kimi-k3` consumed 1600+, while `mimo-v2.6-flash` used 35. The bridge forwards
`max_tokens` as sent, so a low budget makes reasoning models return empty
content with `finish_reason: length` rather than an answer. Set `max_tokens`
above 2000 when driving reasoning models, or use a flash model.

## Requirements

- Python 3.10+
- OpenCode Go subscription (get API key from https://opencode.ai)
- Claude Code (`claude` CLI) or Codex CLI (`codex` CLI) installed

## OpenCode Go API Endpoints Used

- Anthropic-compatible: `https://opencode.ai/inference/go/anthropic/v1/messages`
- OpenAI-compatible: `https://opencode.ai/inference/go/openai/v1/chat/completions`
- OpenAI Responses: `https://opencode.ai/inference/go/openai/v1/responses`
- Models list: `https://opencode.ai/inference/go/v1/models`