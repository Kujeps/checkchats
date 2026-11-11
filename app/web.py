"""HTTP API и раздача интерфейса."""
import asyncio
import io
import json
import logging
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import accounts, db, export, llm, proxycheck
from .engine import GLOBAL_PROXY, LOG, check_account_proxy, check_global_proxy, engine, say
from .links import parse_link
from .paths import STATIC_DIR

app = FastAPI(title="CheckChats")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # Вместо безликого «Internal Server Error» — текст ошибки в интерфейсе и трейс в журнале
    logging.getLogger("web").exception("Ошибка в %s", request.url.path)
    say(f"Ошибка в {request.url.path}: {type(exc).__name__}: {exc}", "error")
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    # После обновления программы браузер не должен показывать старый интерфейс
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- управление ----------

@app.get("/api/status")
async def status():
    return engine.status()


@app.post("/api/start")
async def start():
    scan_accounts_folder(force=True)
    await engine.start()
    return {"ok": True}


@app.post("/api/stop")
async def stop():
    await engine.stop()
    return {"ok": True}


@app.get("/api/log")
async def get_log(since: float = 0):
    return [x for x in LOG if x["t"] > since]


# ---------- настройки ----------

@app.get("/api/settings")
async def get_settings():
    s = db.get_settings()
    s["openrouter_key_set"] = bool(s["openrouter_key"])
    s["openrouter_key"] = ""
    return s


@app.post("/api/settings")
async def set_settings(request: Request):
    data = await request.json()
    if not data.get("openrouter_key"):
        data.pop("openrouter_key", None)
    data.pop("llm_spent", None)
    ints = {"llm_concurrency", "llm_messages", "llm_msg_chars", "messages_limit", "resolve_limit", "search_limit",
            "max_flood_sleep", "snowball_depth", "join_limit", "live_threshold", "spam_threshold", "dead_days"}
    for k in list(data):
        if k in ints:
            data[k] = int(data[k])
        elif k in ("delay_min", "delay_max"):
            data[k] = float(data[k])
    db.save_settings(data)
    return {"ok": True}


@app.get("/api/models")
async def models():
    async with httpx.AsyncClient() as c:
        return await llm.list_models(c)


@app.post("/api/llm/test")
async def llm_test():
    s = db.get_settings()
    if not s["openrouter_key"]:
        raise HTTPException(400, "Не задан ключ OpenRouter")
    sample = {"messages": [
        {"s": 1, "r": None, "text": "Девочки, кто знает, как сейчас на WB с приёмкой на Коледино? Неделю жду слот"},
        {"s": 2, "r": 1, "text": "Через транзит в Казань быстрее, я так везу последние 2 поставки"},
        {"s": 3, "r": None, "text": "💰 Работа на дому от 8000₽ в день! Пиши в ЛС +"},
        {"s": 1, "r": 2, "text": "Спасибо! А по коэффициенту там сколько выходит?"},
    ]}
    chat = {"type": "group", "title": "Селлеры WB | общение", "about": "Чат продавцов Wildberries", "members": 4200}
    async with httpx.AsyncClient() as c:
        ai, usage = await llm.analyze(c, s["openrouter_key"], llm.models_from(s), chat, sample, s["categories"],
                                      s["target_description"], s["llm_msg_chars"], s["llm_messages"])
    return {"result": ai, "usage": usage}


# ---------- аккаунты ----------

_last_scan = 0.0


def scan_accounts_folder(force=False):
    """Находит аккаунты, скопированные в data/accounts вручную (не чаще раза в 5 секунд)."""
    global _last_scan
    if not force and time.time() - _last_scan < 5:
        return
    _last_scan = time.time()
    res = accounts.scan_folder()
    if res["added"]:
        say(f"Найдено в папке accounts: {len(res['added'])} новых аккаунтов")
    for e in res["errors"]:
        say(f"Папка accounts: {e}", "warn")


@app.get("/api/accounts")
async def list_accounts():
    scan_accounts_folder()
    rows = db.all_("SELECT id, name, proxy, status, flood_until, day, resolves_today, searches_today, joins_today, "
                   "checked_total, me, last_error, proxy_ok, proxy_info FROM accounts ORDER BY id")
    today = db.today()
    s = db.get_settings()
    for r in rows:
        own = r.pop("proxy")
        _, r["uses_global"] = accounts.effective_proxy({"proxy": own}, s)
        r["proxy_label"] = accounts.proxy_label(own) if own and not r["uses_global"] else ""
        if r["day"] != today:
            r["resolves_today"] = r["searches_today"] = r["joins_today"] = 0
        w = engine.workers.get(r["id"])
        r["worker"] = w.state if w else None
    return rows


@app.post("/api/accounts/upload")
async def upload_accounts(files: list[UploadFile] = File(...)):
    data = {f.filename: await f.read() for f in files}
    res = accounts.import_files(data)
    say(f"Импорт аккаунтов: добавлено {len(res['added'])}, обновлено {len(res['updated'])}")
    return res


@app.post("/api/accounts/proxies")
async def set_proxies(request: Request):
    data = await request.json()
    return accounts.assign_proxies((data.get("text") or "").splitlines(), data.get("mode") or "empty")


@app.post("/api/proxies/check")
async def check_proxy_list(request: Request):
    lines = [ln.strip() for ln in ((await request.json()).get("text") or "").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    return await proxycheck.check_many(lines)


@app.get("/api/proxy/global")
async def global_proxy_state():
    s = db.get_settings()
    r = GLOBAL_PROXY["r"] if GLOBAL_PROXY["raw"] == s["global_proxy"] else None
    return {"proxy": s["global_proxy"], "all": s["global_proxy_all"], "check": r,
            "checked_at": GLOBAL_PROXY["t"] if r else None}


@app.post("/api/proxy/global")
async def set_global_proxy(request: Request):
    """Сохраняет общий прокси и сразу проверяет его (пустая строка — убрать)."""
    data = await request.json()
    raw = (data.get("proxy") or "").strip()
    if raw:
        try:
            accounts.parse_proxy(raw)
        except ValueError as e:
            raise HTTPException(400, str(e))
    db.save_settings({"global_proxy": raw, "global_proxy_all": bool(data.get("all", True))})
    if not raw:
        say("Общий прокси убран")
        return {"proxy": "", "check": None}
    r = await check_global_proxy(force=True)
    say(f"Общий прокси: {'работает, ' + r['type'] + ', ' + str(r['ms']) + ' мс' if r['ok'] else 'не работает — ' + r['error']}",
        "info" if r["ok"] else "warn")
    if engine.running:
        say("Новый прокси применится к аккаунтам после перезапуска (Остановить → Запустить)", "warn")
    return {"proxy": db.get_settings()["global_proxy"], "check": r}


@app.post("/api/accounts/check_proxies")
async def check_account_proxies():
    s = db.get_settings()
    rows = [a for a in db.all_("SELECT * FROM accounts WHERE proxy IS NOT NULL AND proxy!='' "
                               "AND status NOT IN ('dead','frozen')")
            if not accounts.effective_proxy(a, s)[1]]
    sem = asyncio.Semaphore(30)

    async def one(acc):
        async with sem:
            return await check_account_proxy(acc)

    res = await asyncio.gather(*(one(a) for a in rows))
    ok = sum(1 for r in res if r["ok"])
    say(f"Проверка прокси: рабочих {ok}, нерабочих {len(res) - ok}")
    return {"ok": ok, "bad": len(res) - ok}


@app.get("/api/net/direct")
async def net_direct():
    return await proxycheck.check_direct()


@app.post("/api/accounts/{acc_id}")
async def update_account(acc_id: int, request: Request):
    data = await request.json()
    if acc_id in engine.workers:
        raise HTTPException(400, "Аккаунт работает — сначала остановите процесс")
    if "proxy" in data:
        p = (data["proxy"] or "").strip()
        if p:
            try:
                accounts.parse_proxy(p)
            except ValueError as e:
                raise HTTPException(400, str(e))
        db.q("UPDATE accounts SET proxy=? WHERE id=?", (p or None, acc_id))
    if data.get("status") in ("new", "disabled"):
        db.q("UPDATE accounts SET status=?, flood_until=0, last_error=NULL WHERE id=?", (data["status"], acc_id))
    return {"ok": True}


@app.delete("/api/accounts/{acc_id}")
async def delete_account(acc_id: int):
    if acc_id in engine.workers:
        raise HTTPException(400, "Аккаунт работает — сначала остановите процесс")
    accounts.delete_account(acc_id)
    return {"ok": True}


@app.post("/api/accounts/check")
async def check_accounts(request: Request):
    data = await request.json() if await request.body() else {}
    asyncio.create_task(engine.check_accounts(data.get("ids")))
    return {"ok": True}


# ---------- чаты ----------

def _lines_from_upload(name: str, content: bytes):
    if name.lower().endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if v:
                        yield str(v)
        return
    text = content.decode("utf-8-sig", "ignore")
    for ln in text.splitlines():
        for part in ln.replace(";", " ").replace(",", " ").replace("\t", " ").split():
            yield part


def _import(raw_items, source="import"):
    items, bad = [], 0
    for x in raw_items:
        p = parse_link(x)
        if p:
            items.append(p)
        elif x.strip():
            bad += 1
    added = db.add_chats(items, source=source)
    say(f"Импорт чатов: распознано {len(items)}, новых {added}, не распознано {bad}")
    return {"recognized": len(items), "added": added, "bad": bad}


@app.post("/api/chats/import")
async def import_chats(request: Request):
    data = await request.json()
    text = data.get("text") or ""
    return _import(text.replace(",", " ").replace(";", " ").split())


@app.post("/api/chats/upload")
async def upload_chats(files: list[UploadFile] = File(...)):
    out = {"recognized": 0, "added": 0, "bad": 0}
    for f in files:
        r = _import(_lines_from_upload(f.filename, await f.read()), source=f"import:{Path(f.filename).name}")
        for k in out:
            out[k] += r[k]
    return out


SORTS = {"live": "live_score DESC", "members": "members DESC", "activity": "msgs_per_day DESC",
         "recent": "checked_at DESC", "added": "id DESC", "title": "title"}


@app.get("/api/chats")
async def list_chats(request: Request, page: int = 1, size: int = 50, sort: str = "live"):
    f = dict(request.query_params)
    if f.get("verdict"):
        f["verdict"] = f["verdict"].split(",")
    where, args = export.build_filter(f)
    total = db.one(f"SELECT COUNT(*) n FROM chats WHERE {where}", args)["n"]
    order = SORTS.get(sort, SORTS["live"])
    rows = db.all_(f"""SELECT id, link, value, title, type, members, online, msgs_per_day, last_msg_at, status, error,
                       live_score, spam_score, h_live, verdict, is_target, category, topic, summary, about, source,
                       llm_status, llm_error, metrics FROM chats WHERE {where}
                       ORDER BY {order} NULLS LAST, id LIMIT ? OFFSET ?""",
                   args + [size, (max(page, 1) - 1) * size])
    for r in rows:
        m = json.loads(r.pop("metrics") or "{}")
        r["authors"] = m.get("unique_authors")
        r["dialog"] = m.get("dialog_ratio")
        r["human"] = m.get("human_share")
        r["reason"] = m.get("reason")
    return {"total": total, "rows": rows}


@app.get("/api/categories")
async def categories():
    return [r["category"] for r in db.all_("SELECT DISTINCT category FROM chats WHERE category IS NOT NULL "
                                           "ORDER BY category")]


@app.post("/api/chats/retry")
async def retry(request: Request):
    what = (await request.json()).get("what")
    if what == "errors":
        n = db.q("UPDATE chats SET status='pending', attempts=0, error=NULL WHERE status='error'").rowcount
    elif what == "llm":
        n = db.q("UPDATE chats SET llm_status='pending', llm_error=NULL WHERE llm_status='error' "
                 "AND sample IS NOT NULL").rowcount
    elif what == "recheck":
        # Перечитать уже проверенные чаты тем же аккаунтом (чат у него в кэше — лимит не тратится)
        n = db.q("""UPDATE chats SET status='pending', attempts=0, error=NULL, llm_status='none',
                    preferred_account=COALESCE(checked_by, preferred_account),
                    verdict=NULL, live_score=NULL, spam_score=NULL, is_target=0, category=NULL, topic=NULL,
                    audience=NULL, summary=NULL WHERE status='checked'""").rowcount
    elif what == "private":
        n = db.q("UPDATE chats SET status='pending', error=NULL WHERE status='private' AND kind='invite'").rowcount
    else:
        raise HTTPException(400, "unknown")
    return {"n": n}


@app.post("/api/chats/clear")
async def clear_chats(request: Request):
    if engine.running:
        raise HTTPException(400, "Сначала остановите процесс")
    what = (await request.json()).get("what")
    if what == "pending":
        n = db.q("DELETE FROM chats WHERE status='pending'").rowcount
    elif what == "all":
        n = db.q("DELETE FROM chats").rowcount
    else:
        raise HTTPException(400, "unknown")
    return {"n": n}


@app.get("/api/export")
async def do_export(request: Request):
    f = dict(request.query_params)
    if f.get("verdict"):
        f["verdict"] = f["verdict"].split(",")
    # Большая выгрузка занимает секунды — в отдельном потоке, чтобы не тормозить аккаунты
    path = await asyncio.to_thread(export.export, f)
    return FileResponse(path, filename=Path(path).name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------- поиск ----------

@app.get("/api/queries")
async def list_queries():
    return db.all_("SELECT * FROM queries ORDER BY id DESC LIMIT 1000")


@app.post("/api/queries")
async def add_queries(request: Request):
    text = (await request.json()).get("text") or ""
    n = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            n += db.q("INSERT OR IGNORE INTO queries(q, added_at) VALUES(?, ?)", (ln[:64], db.now())).rowcount
    return {"added": n}


@app.post("/api/queries/generate")
async def gen_queries(request: Request):
    data = await request.json()
    s = db.get_settings()
    if not s["openrouter_key"]:
        raise HTTPException(400, "Не задан ключ OpenRouter")
    try:
        n = max(5, min(200, int(data.get("n") or 60)))
    except ValueError:
        n = 60
    async with httpx.AsyncClient() as c:
        for attempt in range(3):
            try:
                qs = await llm.generate_queries(c, s["openrouter_key"], llm.models_from(s),
                                                data.get("topics") or s["target_description"],
                                                data.get("cities") or "", n)
                return {"queries": qs}
            except (llm.RateLimited, llm.Transient) as e:
                # модель перегружена — подождём и попробуем ещё раз
                if attempt == 2:
                    raise HTTPException(503, f"Модель сейчас перегружена, попробуйте через минуту ({e})")
                await asyncio.sleep(5 * (attempt + 1))
            except (ValueError, RuntimeError) as e:
                raise HTTPException(502, f"Не получилось сгенерировать: {e}")


@app.post("/api/queries/clear")
async def clear_queries(request: Request):
    what = (await request.json()).get("what")
    if what == "all":
        n = db.q("DELETE FROM queries WHERE status!='running'").rowcount
    else:
        n = db.q("UPDATE queries SET status='pending', error=NULL WHERE status='error'").rowcount
    return {"n": n}
