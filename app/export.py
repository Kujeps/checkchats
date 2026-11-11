"""Выгрузка результатов в Excel."""
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .db import add_functions
from .geo import COUNTRY_NAMES
from .paths import DB_PATH, EXPORTS_DIR

VERDICTS = {"live": "Живой", "low": "Слабый", "dead": "Мёртвый", "spam": "Спам/боты", "noconv": "Нет общения", "private": "Закрытый"}
STATUSES = {"pending": "В очереди", "checking": "Проверяется", "checked": "Проверен", "not_found": "Не найден",
            "not_chat": "Не чат", "private": "Нет доступа", "error": "Ошибка", "duplicate": "Дубль"}
FILLS = {"live": "C6EFCE", "low": "FFEB9C", "dead": "D9D9D9", "spam": "FFC7CE", "noconv": "F4D9C6", "private": "DDEBF7"}

COLUMNS = [
    ("Ссылка", 32), ("Название", 34), ("Тип", 9), ("Страна", 14), ("Город", 16), ("Участники", 11), ("Онлайн", 9),
    ("Сообщ./день", 11), ("Последнее сообщение", 18), ("Людей в выборке", 11), ("От людей, %", 10), ("Диалогов, %", 10),
    ("Живость", 9), ("Спам", 7), ("Вердикт", 12), ("Целевой", 9), ("Категория", 28), ("Тематика", 32),
    ("Аудитория", 32), ("Описание чата", 50), ("Вывод ИИ", 60), ("Язык", 6), ("Источник", 24),
    ("Статус", 12), ("Проверен", 17),
]


def build_filter(f: dict):
    where, args = ["1=1"], []
    if f.get("verdict"):
        vs = f["verdict"] if isinstance(f["verdict"], list) else [f["verdict"]]
        where.append(f"verdict IN ({','.join('?' * len(vs))})")
        args += vs
    if f.get("target"):
        where.append("is_target=1")
    geo_f = f.get("country")
    if geo_f == "RU":
        where.append("country='RU'")
    elif geo_f == "RU?":
        where.append("(country='RU' OR country='?' OR country IS NULL)")
    elif geo_f == "unknown":
        where.append("(country='?' OR country IS NULL)")
    elif geo_f == "other":
        where.append("country IS NOT NULL AND country NOT IN ('RU', '?')")
    if f.get("city"):
        where.append("ulower(city) LIKE ?")
        args.append(f"%{f['city'].lower()}%")
    if f.get("category"):
        where.append("category=?")
        args.append(f["category"])
    if f.get("type"):
        where.append("type=?")
        args.append(f["type"])
    if f.get("status"):
        where.append("status=?")
        args.append(f["status"])
    if f.get("min_members"):
        where.append("members>=?")
        args.append(int(f["min_members"]))
    if f.get("min_live"):
        where.append("live_score>=?")
        args.append(int(f["min_live"]))
    if f.get("search"):
        where.append("(ulower(title) LIKE ? OR ulower(value) LIKE ? OR ulower(topic) LIKE ? OR ulower(about) LIKE ?)")
        args += [f"%{f['search'].lower()}%"] * 4
    if f.get("source"):
        where.append("source LIKE ?")
        args.append(f"{f['source']}%")
    return " AND ".join(where), args


def _ts(v):
    return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M") if v else ""


def export(f: dict) -> str:
    """Вызывается из отдельного потока, поэтому читает базу своим соединением."""
    import json
    import sqlite3
    where, args = build_filter(f)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    add_functions(c)
    try:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM chats WHERE {where} ORDER BY is_target DESC, live_score DESC, members DESC", args)]
    finally:
        c.close()
    wb = Workbook()
    ws = wb.active
    ws.title = "Чаты"
    head_fill = PatternFill("solid", fgColor="305496")
    for i, (name, width) in enumerate(COLUMNS, 1):
        c = ws.cell(row=1, column=i, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[1].height = 30

    for r, ch in enumerate(rows, 2):
        m = json.loads(ch["metrics"] or "{}")
        vals = [
            ch["link"], ch["title"], {"channel": "Канал", "group": "Группа"}.get(ch["type"], ""),
            COUNTRY_NAMES.get(ch["country"], ch["country"]) if ch["country"] else "", ch["city"] or "",
            ch["members"], ch["online"], ch["msgs_per_day"], _ts(ch["last_msg_at"]),
            m.get("unique_authors"),
            round(m["human_share"] * 100) if m.get("human_share") is not None else None,
            round(m["dialog_ratio"] * 100) if m.get("dialog_ratio") is not None else None,
            ch["live_score"], ch["spam_score"], VERDICTS.get(ch["verdict"], ""), "Да" if ch["is_target"] else "",
            ch["category"], ch["topic"], ch["audience"], (ch["about"] or "")[:1000], ch["summary"], ch["language"],
            ch["source"], STATUSES.get(ch["status"], ch["status"]), _ts(ch["checked_at"]),
        ]
        for i, v in enumerate(vals, 1):
            ws.cell(row=r, column=i, value=v)
        ws.cell(row=r, column=1).hyperlink = ch["link"]
        ws.cell(row=r, column=1).font = Font(color="0563C1", underline="single")
        if ch["verdict"] in FILLS:
            ws.cell(row=r, column=12).fill = PatternFill("solid", fgColor=FILLS[ch["verdict"]])
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(1, len(rows) + 1)}"

    # Сводка по категориям
    s = wb.create_sheet("Сводка")
    s.append(["Категория", "Всего", "Живых", "Слабых", "Мёртвых", "Спам", "Нет общения", "Закрытых", "Участников в живых"])
    for c in s[1]:
        c.font = Font(bold=True)
    agg = {}
    for ch in rows:
        a = agg.setdefault(ch["category"] or "— без анализа ИИ —", [0, 0, 0, 0, 0, 0, 0, 0])
        a[0] += 1
        idx = {"live": 1, "low": 2, "dead": 3, "spam": 4, "noconv": 5, "private": 6}.get(ch["verdict"])
        if idx:
            a[idx] += 1
        if ch["verdict"] == "live":
            a[7] += ch["members"] or 0
    for cat, a in sorted(agg.items(), key=lambda x: -x[1][1]):
        s.append([cat, *a])
    s.column_dimensions["A"].width = 36
    for col in "BCDEFGH":
        s.column_dimensions[col].width = 13

    name = f"chats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    path = EXPORTS_DIR / name
    wb.save(path)
    return str(path)
