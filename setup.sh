#!/usr/bin/env bash
# Создаёт окружение .venv, ставит зависимости и копирует .env.example в .env (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "[X] Python не найден. macOS: brew install python   Linux: sudo apt install python3 python3-venv"
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "[X] Нужен Python 3.10 или новее (сейчас: $("$PY" --version 2>&1))"
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "Создаю окружение .venv ..."
    "$PY" -m venv .venv
fi

echo "Ставлю зависимости ..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Создал .env из .env.example"
else
    echo ".env уже есть — не трогаю"
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo
    echo "[!] ffmpeg не найден. Без него бот не вырежет кадры и звук из роликов."
    echo "    macOS: brew install ffmpeg   Linux: sudo apt install ffmpeg"
fi

echo
echo "Готово. Впишите ключи в .env, затем запустите ./start.sh"
