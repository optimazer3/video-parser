@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Python: сначала лаунчер py, потом python из PATH
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY python --version >nul 2>nul && set "PY=python"
if not defined PY (
    echo [X] Python не найден. Установите Python 3.12 с https://www.python.org/downloads/
    echo     и в установщике отметьте "Add python.exe to PATH". Потом запустите setup.bat ещё раз.
    goto :fail
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo [X] Нужен Python 3.10 или новее. Установите Python 3.12 с https://www.python.org/downloads/
    goto :fail
)

if not exist ".venv\Scripts\python.exe" (
    echo Создаю окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)

echo Ставлю зависимости ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Создал .env из .env.example
) else (
    echo .env уже есть - не трогаю
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [!] ffmpeg не найден. Без него бот не вырежет кадры и звук из роликов.
    echo     Установите: winget install --id Gyan.FFmpeg -e   и откройте окно заново.
)

echo.
echo Готово. Впишите ключи в .env (сейчас откроется Блокнот), сохраните и запустите start.bat
start "" notepad ".env"
pause
exit /b 0

:fail
echo.
echo Установка не завершена - см. сообщения выше.
pause
exit /b 1
