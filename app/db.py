"""SQLite: схема, настройки и мелкие помощники.

Все обращения идут из одного event loop, запросы короткие, поэтому хватает
синхронного sqlite3 в режиме WAL.
"""
import json
import sqlite3
import time
from datetime import datetime

from .paths import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE,              -- имя файла сессии без расширения (обычно телефон)
    session_path TEXT,
    params TEXT,                   -- JSON: api_id, api_hash, device...
    proxy TEXT,                    -- строка прокси, у каждого аккаунта свой
    status TEXT DEFAULT 'new',     -- new | active | flood | dead | frozen | proxy_error | disabled
    flood_until REAL DEFAULT 0,
    day TEXT,
    resolves_today INTEGER DEFAULT 0,
    searches_today INTEGER DEFAULT 0,
    joins_today INTEGER DEFAULT 0,
    checked_total INTEGER DEFAULT 0,
    me TEXT,
    last_error TEXT,
    added_at REAL
);

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE,               -- u:username | i:invitehash
    kind TEXT,                     -- username | invite
    value TEXT,
    link TEXT,
    source TEXT,                   -- import | search:<запрос> | snowball:<чат>
    depth INTEGER DEFAULT 0,
    preferred_account INTEGER,
    status TEXT DEFAULT 'pending', -- pending | checking | checked | not_found | private | not_chat | error
    attempts INTEGER DEFAULT 0,
    error TEXT,
    tg_id INTEGER,
    type TEXT,                     -- channel | group
    title TEXT,
    about TEXT,
    members INTEGER,
    online INTEGER,
    metrics TEXT,                  -- JSON с эвристиками
    sample TEXT,                   -- JSON выборки сообщений для ИИ, чистится после анализа
    h_live INTEGER,
    h_spam INTEGER,
    msgs_per_day REAL,
    last_msg_at REAL,
    llm_status TEXT DEFAULT 'none',-- none | pending | done | error | skipped
    llm_error TEXT,
    category TEXT,
    topic TEXT,
    audience TEXT,
    summary TEXT,
    language TEXT,
    llm_live INTEGER,
    llm_spam INTEGER,
    live_score INTEGER,
    spam_score INTEGER,
    verdict TEXT,                  -- live | low | dead | spam | private
    is_target INTEGER DEFAULT 0,
    added_at REAL,
    checked_at REAL
);
CREATE INDEX IF NOT EXISTS ix_chats_status ON chats(status);
CREATE INDEX IF NOT EXISTS ix_chats_llm ON chats(llm_status);
CREATE INDEX IF NOT EXISTS ix_chats_tgid ON chats(tg_id);

CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY,
    q TEXT UNIQUE,
    status TEXT DEFAULT 'pending', -- pending | done | error
    found INTEGER DEFAULT 0,
    error TEXT,
    added_at REAL,
    done_at REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_CATEGORIES = [
    "Маркетплейсы (WB, Ozon и др.)",
    "Недвижимость / риелторы",
    "Услуги",
    "Бизнес / предприниматели",
    "Строительство и ремонт",
    "Авто",
    "Красота и здоровье",
    "Образование",
    "Финансы / инвестиции",
    "Криптовалюта",
    "Работа / вакансии",
    "Локальный чат (город, район, ЖК)",
    "Развлечения / общение",
    "Новости / СМИ",
    "Другое",
]

DEFAULTS = {
    "openrouter_key": "",
    "model": "google/gemini-2.5-flash-lite",
    "llm_enabled": True,
    "llm_concurrency": 5,
    "llm_messages": 40,            # сколько сообщений отдавать ИИ
    "llm_msg_chars": 300,          # обрезка каждого сообщения
    "keep_sample": False,          # хранить выборку после анализа
    "messages_limit": 150,         # сколько последних сообщений читать
    "delay_min": 4,                # пауза между чатами на одном аккаунте, сек
    "delay_max": 9,
    "resolve_limit": 150,          # поисков username в сутки на аккаунт
    "search_limit": 40,            # поисковых запросов в сутки на аккаунт
    "max_flood_sleep": 60,         # FloodWait меньше этого просто пережидаем
    "allow_no_proxy": False,       # запускать аккаунты без прокси (с IP этого компьютера)
    "snowball": True,              # добавлять чаты из ссылок и репостов
    "snowball_depth": 2,
    "snowball_only_target": True,  # брать ссылки только из целевых живых чатов
    "join_private": False,         # вступать в закрытые чаты по инвайтам
    "join_limit": 5,
    "leave_after_join": True,
    "categories": DEFAULT_CATEGORIES,
    "target_categories": [
        "Маркетплейсы (WB, Ozon и др.)",
        "Недвижимость / риелторы",
        "Услуги",
        "Бизнес / предприниматели",
        "Строительство и ремонт",
    ],
    "target_description": "Продавцы на маркетплейсах, риелторы, люди, оказывающие услуги, "
                          "предприниматели малого бизнеса",
    "live_threshold": 50,
    "spam_threshold": 60,
    "dead_days": 30,
    "llm_spent": 0.0,              # сколько потрачено на OpenRouter, $
}

_conn = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
    return _conn


def q(sql, args=()):
    return conn().execute(sql, args)


def all_(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def now() -> float:
    return time.time()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------- настройки ----------

def get_settings() -> dict:
    s = dict(DEFAULTS)
    for r in conn().execute("SELECT key, value FROM settings"):
        try:
            s[r["key"]] = json.loads(r["value"])
        except (TypeError, ValueError):
            pass
    return s


def setting(key):
    return get_settings()[key]


def save_settings(values: dict):
    for k, v in values.items():
        if k in DEFAULTS:
            q("INSERT INTO settings(key, value) VALUES(?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v, ensure_ascii=False)))


# ---------- чаты ----------

def add_chats(items, source="import", depth=0, preferred_account=None) -> int:
    """items: [(kind, value)]; возвращает число новых."""
    from .links import chat_key, link_for
    added = 0
    ts = now()
    c = conn()
    c.execute("BEGIN")
    try:
        for kind, value in items:
            cur = c.execute(
                "INSERT OR IGNORE INTO chats(key, kind, value, link, source, depth, preferred_account, added_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (chat_key(kind, value), kind, value, link_for(kind, value), source, depth, preferred_account, ts))
            added += cur.rowcount
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return added


def reset_stale():
    """После аварийного выхода возвращает «зависшие» задачи в очередь."""
    q("UPDATE chats SET status='pending' WHERE status='checking'")
    q("UPDATE chats SET llm_status='pending' WHERE llm_status='running'")
