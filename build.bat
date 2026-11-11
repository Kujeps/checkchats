@echo off
rem Сборка CheckChats.exe (запускать на Windows). Результат: dist\CheckChats\CheckChats.exe
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller || (pause & exit /b 1)

".venv\Scripts\pyinstaller.exe" --noconfirm --clean --name CheckChats --console ^
    --add-data "app\static;app\static" ^
    --collect-submodules uvicorn ^
    --collect-submodules telethon ^
    --collect-submodules python_socks ^
    run.py || (pause & exit /b 1)

echo.
echo Готово: dist\CheckChats\CheckChats.exe
echo Папку dist\CheckChats можно переносить целиком. Данные (база, сессии, выгрузки) будут в dist\CheckChats\data
pause
