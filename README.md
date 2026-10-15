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
python ocgo.py setup

# 3. Launch Claude Code through OpenCode Go
python ocgo.py launch claude --model kimi-k2.6

# Or launch Codex CLI through OpenCode Go
python ocgo.py launch codex --model kimi-k2.6
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
  "port": 3456
}
```

## Model Mappings

Map Claude Code/Codex model names to OpenCode Go models:
```bash
# Map Claude's "claude-sonnet" to OpenCode Go's "kimi-k2.6"
python ocgo.py mapping claude claude-sonnet kimi-k2.6

# Map Codex's "gpt-4" to OpenCode Go's "gpt-5.3-codex"
python ocgo.py mapping codex gpt-4 gpt-5.3-codex
```

## Requirements

- Python 3.10+
- OpenCode Go subscription (get API key from https://opencode.ai)
- Claude Code (`claude` CLI) or Codex CLI (`codex` CLI) installed

## OpenCode Go API Endpoints Used

- Anthropic-compatible: `https://opencode.ai/inference/go/anthropic/v1/messages`
- OpenAI-compatible: `https://opencode.ai/inference/go/openai/v1/chat/completions`
- OpenAI Responses: `https://opencode.ai/inference/go/openai/v1/responses`
- Models list: `https://opencode.ai/inference/go/v1/models`