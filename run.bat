@echo off
chcp 65001 >nul
cd /d "%~dp0"
title CheckChats

if not exist ".venv\Scripts\python.exe" (
    echo Первый запуск: создаю окружение и ставлю зависимости...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo Не найден Python. Установите Python 3.11+ с python.org ^(галочка "Add to PATH"^) или используйте собранный CheckChats.exe
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
)

".venv\Scripts\python.exe" run.py
if %errorlevel% EQU 3 goto restart
pause
exit /b
:restart
".venv\Scripts\python.exe" run.py
if %errorlevel% EQU 3 goto restart
pause
exit /b
REM Код 3 — программа обновилась и просит перезапуск (кнопка «Обновить программу»).
REM Строки до первого запуска run.py не меняйте: после обновления Windows дочитывает этот файл
REM с того же места, поэтому их положение должно совпадать со старой версией.
