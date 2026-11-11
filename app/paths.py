"""Где лежат данные программы.

В собранном .exe данные хранятся рядом с exe, а статика берётся из распакованного бандла.
"""
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
    STATIC_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "app" / "static"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    STATIC_DIR = Path(__file__).resolve().parent / "static"

DATA_DIR = BASE_DIR / "data"
ACCOUNTS_DIR = DATA_DIR / "accounts"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "checkchats.db"

for _d in (DATA_DIR, ACCOUNTS_DIR, EXPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
