#!/usr/bin/env bash
# ocgo-bridge - Shell wrapper for the OpenCode Go Bridge
# Source this file in your shell, or add the aliases directly:
#   source /path/to/ocgo-bridge.sh

ocgo() {
    local SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    python3 "$SCRIPT_DIR/ocgo_bridge.py" "$@"
}

alias ocgo-setup='ocgo setup'
alias ocgo-serve='ocgo serve'
alias ocgo-stop='ocgo stop'
alias ocgo-status='ocgo status'
alias ocgo-list='ocgo list'
alias ocgo-claude='ocgo launch claude'
alias ocgo-codex='ocgo launch codex'

# Examples:
#   ocgo-claude                          # default model
#   ocgo-claude -- -p "review this diff"  # headless prompt
#   ocgo-claude --model mimo-v2.6-flash  # pick a model
#   ocgo-codex -- exec "explain this"     # Codex
