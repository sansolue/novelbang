#!/usr/bin/env bash
# Start NovelBang. Any arguments are passed through, e.g.:
#   ./start.sh                       # projects in ~/Novels, opens the browser
#   ./start.sh ~/Writing --port 9000
#   ./start.sh --no-browser
# Set NOVELBANG_NO_OLLAMA=1 to skip checking for / starting Ollama.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

# Start Ollama in the background if it's installed but not running.
if [[ -z "${NOVELBANG_NO_OLLAMA:-}" ]] && command -v ollama >/dev/null 2>&1; then
  if ! curl -s --max-time 2 http://localhost:11434/api/version >/dev/null; then
    echo "Starting Ollama (log: /tmp/novelbang-ollama.log)…"
    nohup ollama serve >/tmp/novelbang-ollama.log 2>&1 &
    for _ in {1..20}; do
      curl -s --max-time 1 http://localhost:11434/api/version >/dev/null && break
      sleep 0.5
    done
  fi
fi

exec uv run novelbang "$@"
