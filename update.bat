@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo [X] Git не найден. Установите: winget install --id Git.Git -e   и откройте терминал заново.
    exit /b 1
)
if not exist ".git" (
    echo [X] Эта папка не подключена к git. Как подключить - в README, раздел "Обновление через git".
    exit /b 1
)

echo Скачиваю обновления ...
git pull --ff-only
if errorlevel 1 (
    echo [X] git pull не прошёл - см. сообщение выше.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Окружения ещё нет - запускаю setup.bat
    call setup.bat
    exit /b
)
echo Обновляю зависимости ...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" -m pip install -q --upgrade yt-dlp
echo.
echo Готово. Запуск бота: .venv\Scripts\python -m bot   (или start.bat)
