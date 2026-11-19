#!/bin/sh
# AgentLens Cloud installer: curl -fsSL __AGENTLENS_URL__/install.sh | sh -s -- YOUR_KEY
# Installs ~/.agentlens/agentlens-send.py (read it first if you like: __AGENTLENS_URL__/agentlens-send.py),
# starts it, and adds a Claude Code SessionStart hook that restarts it. Undo: python3 ~/.agentlens/agentlens-send.py uninstall
set -e
KEY="$1"
URL="__AGENTLENS_URL__"
if [ -z "$KEY" ]; then echo "Usage: curl -fsSL $URL/install.sh | sh -s -- YOUR_KEY" >&2; exit 1; fi
command -v python3 >/dev/null 2>&1 || { echo "AgentLens needs python3 on PATH." >&2; exit 1; }
mkdir -p "$HOME/.agentlens"
curl -fsSL "$URL/agentlens-send.py" -o "$HOME/.agentlens/agentlens-send.py"
python3 "$HOME/.agentlens/agentlens-send.py" setup --url "$URL" --key "$KEY"
