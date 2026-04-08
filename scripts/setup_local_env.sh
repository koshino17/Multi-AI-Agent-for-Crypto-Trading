#!/bin/zsh
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo ".env already exists, leaving it untouched"
fi

chmod +x "Launch Trading Agents.command" scripts/*.sh

echo
echo "Setup complete."
echo "Next steps:"
echo "1. Edit .env with your exchange / Notion settings"
echo "2. Make sure Ollama is running if you use MODEL_BACKEND=ollama"
echo "3. Start the web console with: ./Launch Trading Agents.command"
