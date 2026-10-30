#!/usr/bin/env bash
# Подтягивает изменения из git и обновляет зависимости (macOS / Linux).
set -euo pipefail
cd "$(dirname "$0")"

command -v git >/dev/null || { echo "[X] Git не найден. macOS: brew install git"; exit 1; }
[ -d .git ] || { echo "[X] Эта папка не подключена к git. Как подключить — в README, раздел «Обновление через git»."; exit 1; }

echo "Скачиваю обновления ..."
git pull --ff-only

if [ ! -x .venv/bin/python ]; then
    echo "Окружения ещё нет — запускаю setup.sh"
    exec ./setup.sh
fi
echo "Обновляю зависимости ..."
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m pip install -q --upgrade yt-dlp
echo
echo "Готово. Запуск бота: ./start.sh"
