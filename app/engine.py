"""Движок: по воркеру на каждый аккаунт, отдельная очередь анализа через ИИ."""
import asyncio
import json
import logging
import random
import time
from collections import deque
from datetime import datetime, timedelta

import httpx
from telethon import errors

from . import db, heuristics, llm, proxycheck
from .accounts import make_client
from .checker import Checker, ChatGone, NeedResolve
from .proxycheck import net_error_text

log = logging.getLogger("engine")
LOG = deque(maxlen=400)

DEAD_ERRORS = (errors.AuthKeyUnregisteredError, errors.UserDeactivatedError, errors.UserDeactivatedBanError,
               errors.SessionRevokedError, errors.SessionExpiredError, errors.AuthKeyDuplicatedError,
               errors.PhoneNumberBannedError)
FROZEN_ERRORS = (errors.FrozenMethodInvalidError,)
NET_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError)


def say(msg, level="info"):
    LOG.append({"t": time.time(), "level": level, "msg": msg})
    getattr(log, "warning" if level == "warn" else level, log.info)(msg)


async def check_account_proxy(acc) -> dict:
    """Проверяет прокси аккаунта, запоминает результат; найденный тип дописывает в строку прокси."""
    r = await proxycheck.check(acc["proxy"])
    info = f"{r['type']}, {r['ms']} мс" if r["ok"] else r["error"]
    db.q("UPDATE accounts SET proxy=?, proxy_ok=?, proxy_info=?, proxy_checked_at=? WHERE id=?",
         (r["fixed"] if r["ok"] else acc["proxy"], int(r["ok"]), info[:300], time.time(), acc["id"]))
    if r["ok"] and acc["status"] == "proxy_error":
        db.q("UPDATE accounts SET status='new', last_error=NULL WHERE id=?", (acc["id"],))
    return r


def _seconds_to_midnight():
    n = datetime.now()
    return ((n + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0) - n).total_seconds()


# ---------- сохранение результатов ----------

def _sample(res, metrics, s):
    """Компактная выборка для ИИ: авторы заменены на u1, u2..., только тексты."""
    ids = {}

    def alias(sid):
        if sid is None:
            return None
        if sid not in ids:
            ids[sid] = len(ids) + 1
        return ids[sid]

    def pack(msgs, limit):
        msgs = sorted(msgs, key=lambda x: x["date"] or 0)[-limit:]
        by_id = {x["id"]: x.get("sender") for x in msgs}
        out = []
        for x in msgs:
            if not (x.get("text") or "").strip():
                continue
            r = None
            if x.get("reply_to"):
                r = alias(by_id[x["reply_to"]]) if x["reply_to"] in by_id else "?"
            out.append({"s": alias(x.get("sender")), "r": r, "text": x["text"][: s["llm_msg_chars"]]})
        return out

    return {
        "messages": pack(res["messages"], s["llm_messages"]),
        "linked": pack(res["linked"], max(10, s["llm_messages"] // 2)),
        "metrics": {k: metrics.get(k) for k in ("msgs_per_day", "unique_authors", "dup_ratio")},
        "found": res.get("found") or [],
        "invite_only": res["status"] == "private",
    }


def save_check(chat, res, acc_id, s):
    db.q("UPDATE accounts SET checked_total=checked_total+1 WHERE id=?", (acc_id,))
    # Один и тот же чат мог прийти и по username, и по инвайту
    if res["tg_id"] and db.one("SELECT id FROM chats WHERE tg_id=? AND id!=? AND status='checked'",
                               (res["tg_id"], chat["id"])):
        db.q("UPDATE chats SET status='duplicate', tg_id=?, title=?, checked_at=? WHERE id=?",
             (res["tg_id"], res["title"], db.now(), chat["id"]))
        return
    metrics = {}
    h_live = h_spam = None
    if res["status"] == "checked":
        metrics = heuristics.compute(res["messages"], res["type"], res["members"], res["online"],
                                     res["linked"], dead_days=s["dead_days"])
        h_live, h_spam = metrics["live"], metrics["spam"]
    sample = _sample(res, metrics, s)
    db.q("""UPDATE chats SET status='checked', error=NULL, tg_id=?, type=?, title=?, about=?, members=?, online=?,
            metrics=?, sample=?, h_live=?, h_spam=?, msgs_per_day=?, last_msg_at=?, checked_at=?,
            llm_status='pending' WHERE id=?""",
         (res["tg_id"], res["type"], res["title"], res["about"], res["members"], res["online"],
          json.dumps(metrics, ensure_ascii=False), json.dumps(sample, ensure_ascii=False), h_live, h_spam,
          metrics.get("msgs_per_day"), metrics.get("last_msg_at"), db.now(), chat["id"]))

    has_text = sample["messages"] or sample["linked"] or res["title"]
    dead = metrics.get("verdict") == "dead"
    if not s["llm_enabled"] or dead or not has_text:
        finalize(chat["id"], None, s)


def finalize(chat_id, ai, s):
    """Итоговый вердикт; ai — результат llm.analyze или None."""
    row = db.one("SELECT * FROM chats WHERE id=?", (chat_id,))
    metrics = json.loads(row["metrics"] or "{}")
    sample = json.loads(row["sample"] or "{}")
    if row["h_live"] is None:          # закрытый чат: сообщений нет
        live, spam, verdict = (ai or {}).get("live"), (ai or {}).get("spam"), "private"
    else:
        live, spam, verdict = heuristics.combine(
            row["h_live"], row["h_spam"], (ai or {}).get("live"), (ai or {}).get("spam"),
            metrics.get("days_since_last"), s["dead_days"], s["live_threshold"], s["spam_threshold"])
    category = (ai or {}).get("category")
    is_target = int(bool(category and category in s["target_categories"]))
    db.q("""UPDATE chats SET llm_status=?, llm_error=NULL, category=?, topic=?, audience=?, summary=?, language=?,
            llm_live=?, llm_spam=?, live_score=?, spam_score=?, verdict=?, is_target=?, sample=? WHERE id=?""",
         ("done" if ai else "skipped", category, (ai or {}).get("topic"), (ai or {}).get("audience"),
          (ai or {}).get("summary"), (ai or {}).get("language"), (ai or {}).get("live"), (ai or {}).get("spam"),
          live, spam, verdict, is_target, row["sample"] if s["keep_sample"] else None, chat_id))

    # «Снежный ком»: ссылки из подходящих чатов идут в очередь
    found = sample.get("found") or []
    if (s["snowball"] and found and row["depth"] < s["snowball_depth"] and verdict in ("live", "low")
            and (is_target or not s["snowball_only_target"])):
        title = row["title"] or row["value"]
        n = db.add_chats([tuple(x) for x in found], source=f"snowball:{title}"[:120],
                         depth=row["depth"] + 1)
        if n:
            say(f"+{n} новых чатов из ссылок в «{title}»")


# ---------- воркер аккаунта ----------

class Worker:
    def __init__(self, engine, acc):
        self.engine = engine
        self.acc_id = acc["id"]
        self.name = acc["name"]
        self.client = None
        self.state = "starting"
        self.current = None
        self.last_search = 0

    # бюджеты на сутки (храним в БД, чтобы переживали перезапуск)
    def _acc(self):
        a = db.one("SELECT * FROM accounts WHERE id=?", (self.acc_id,))
        if a["day"] != db.today():
            db.q("UPDATE accounts SET day=?, resolves_today=0, searches_today=0, joins_today=0 WHERE id=?",
                 (db.today(), self.acc_id))
            a = db.one("SELECT * FROM accounts WHERE id=?", (self.acc_id,))
        return a

    def can_resolve(self):
        return self._acc()["resolves_today"] < self.engine.s["resolve_limit"]

    def use_resolve(self):
        db.q("UPDATE accounts SET resolves_today=resolves_today+1 WHERE id=?", (self.acc_id,))

    def can_join(self):
        return self._acc()["joins_today"] < self.engine.s["join_limit"]

    def use_join(self):
        db.q("UPDATE accounts SET joins_today=joins_today+1 WHERE id=?", (self.acc_id,))

    def _set(self, status, error=None, **extra):
        sets = ["status=?", "last_error=?"] + [f"{k}=?" for k in extra]
        db.q(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", (status, error, *extra.values(), self.acc_id))

    async def connect(self):
        acc = db.one("SELECT * FROM accounts WHERE id=?", (self.acc_id,))
        if acc["proxy"]:
            # Сначала быстро проверяем прокси, чтобы не мучить Telegram заведомо мёртвым соединением
            r = await check_account_proxy(acc)
            if not r["ok"]:
                self._set("proxy_error", f"прокси: {r['error']}")
                return False
            acc = db.one("SELECT * FROM accounts WHERE id=?", (self.acc_id,))
        try:
            self.client = make_client(acc)
        except Exception as e:  # noqa: BLE001
            self._set("dead", f"файл сессии повреждён: {type(e).__name__}: {e}")
            return False
        try:
            await asyncio.wait_for(self.client.connect(), 45)
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                label = " ".join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
                if me.username:
                    label += f" @{me.username}"
                self._set("active", None, me=label)
                return True
            self._set("dead", "сессия не авторизована")
        except DEAD_ERRORS as e:
            self._set("dead", type(e).__name__)
        except FROZEN_ERRORS as e:
            self._set("frozen", type(e).__name__)
        except errors.FloodWaitError as e:
            self._set("flood", f"FloodWait {e.seconds} c", flood_until=time.time() + e.seconds)
        except NET_ERRORS as e:
            via = "через прокси" if acc["proxy"] else "напрямую (Telegram может быть заблокирован)"
            self._set("proxy_error", f"нет связи с Telegram {via}: {net_error_text(e)}")
        except Exception as e:  # noqa: BLE001 — показываем любую ошибку в интерфейсе
            self._set("proxy_error", f"{type(e).__name__}: {e}")
        await self.disconnect()
        return False

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _claim(self):
        # Сначала чаты, найденные этим же аккаунтом: они уже в его кэше и не тратят лимит
        row = db.one("SELECT * FROM chats WHERE status='pending' "
                     "ORDER BY (preferred_account = ?) DESC, depth, id LIMIT 1", (self.acc_id,))
        if row:
            db.q("UPDATE chats SET status='checking' WHERE id=?", (row["id"],))
        return row

    async def run(self):
        try:
            acc = db.one("SELECT * FROM accounts WHERE id=?", (self.acc_id,))
            if acc["flood_until"] and acc["flood_until"] > time.time():
                self.state = "flood"
                await asyncio.sleep(acc["flood_until"] - time.time())
            self.state = "connecting"
            if not await self.connect():
                self.state = db.one("SELECT status FROM accounts WHERE id=?", (self.acc_id,))["status"]
                say(f"[{self.name}] не подключился: {self.state}", "warn")
                return
            say(f"[{self.name}] подключён")
            checker = Checker(self.client, self)
            while not self.engine.stopping:
                s = self.engine.s = db.get_settings()
                if await self._maybe_search(checker, s):
                    await self._pause(s)
                    continue
                if not self.can_resolve():
                    self.state = "limit"
                    self._set("active", "дневной лимит исчерпан")
                    await self._sleep(min(_seconds_to_midnight(), 600))
                    continue
                chat = self._claim()
                if not chat:
                    self.state = "idle"
                    await self._sleep(5)
                    continue
                self.state = "working"
                self.current = chat["link"]
                ok = await self._check(checker, chat, s)
                self.current = None
                if ok is False:
                    return
                await self._pause(s)
        finally:
            await self.disconnect()
            if self.state not in ("dead", "frozen", "proxy_error"):
                self.state = "stopped"

    async def _sleep(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.engine.stopping:
            await asyncio.sleep(min(1, end - time.time()))

    async def _pause(self, s):
        await self._sleep(random.uniform(s["delay_min"], max(s["delay_min"], s["delay_max"])))

    async def _flood(self, seconds, s):
        if seconds <= s["max_flood_sleep"]:
            await self._sleep(seconds + 1)
            return
        self.state = "flood"
        self._set("flood", f"FloodWait {seconds} c", flood_until=time.time() + seconds)
        say(f"[{self.name}] FloodWait {seconds} c — отдыхает", "warn")
        await self._sleep(seconds + 2)
        self._set("active")

    async def _check(self, checker, chat, s):
        """True — продолжаем, False — аккаунт больше не работает."""
        try:
            res = await checker.check(chat, s["messages_limit"], s["join_private"], s["leave_after_join"])
            if res["status"] == "private" and not res.get("title"):
                raise ChatGone("private", "нет доступа")
            save_check(chat, res, self.acc_id, s)
            say(f"[{self.name}] {chat['link']} — {res['title']!r}, {len(res['messages'])} сообщ.")
            return True
        except NeedResolve:
            db.q("UPDATE chats SET status='pending' WHERE id=?", (chat["id"],))
            return True
        except ChatGone as e:
            db.q("UPDATE chats SET status=?, error=?, checked_at=?, verdict=? WHERE id=?",
                 (e.status, str(e), db.now(), "private" if e.status == "private" else None, chat["id"]))
            return True
        except errors.FloodWaitError as e:
            db.q("UPDATE chats SET status='pending' WHERE id=?", (chat["id"],))
            await self._flood(e.seconds, s)
            return True
        except DEAD_ERRORS + FROZEN_ERRORS as e:
            db.q("UPDATE chats SET status='pending' WHERE id=?", (chat["id"],))
            st = "frozen" if isinstance(e, FROZEN_ERRORS) else "dead"
            self.state = st
            self._set(st, type(e).__name__)
            say(f"[{self.name}] аккаунт выбыл: {type(e).__name__}", "error")
            return False
        except NET_ERRORS as e:
            db.q("UPDATE chats SET status='pending' WHERE id=?", (chat["id"],))
            say(f"[{self.name}] сеть: {net_error_text(e)}, переподключение", "warn")
            await self._sleep(15)
            await self.disconnect()
            if not await self.connect():
                self.state = "proxy_error"
                return False
            checker.client = self.client
            return True
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            if "FROZEN" in str(e).upper():
                db.q("UPDATE chats SET status='pending' WHERE id=?", (chat["id"],))
                self.state = "frozen"
                self._set("frozen", msg)
                return False
            attempts = chat["attempts"] + 1
            status = "error" if attempts >= 3 else "pending"
            db.q("UPDATE chats SET status=?, attempts=?, error=? WHERE id=?", (status, attempts, msg[:300], chat["id"]))
            say(f"[{self.name}] {chat['link']}: {msg}", "warn")
            return True

    async def _maybe_search(self, checker, s):
        if time.time() - self.last_search < 60:
            return False
        if self._acc()["searches_today"] >= s["search_limit"]:
            return False
        qrow = db.one("SELECT * FROM queries WHERE status='pending' ORDER BY id LIMIT 1")
        if not qrow:
            return False
        db.q("UPDATE queries SET status='running' WHERE id=?", (qrow["id"],))
        self.last_search = time.time()
        db.q("UPDATE accounts SET searches_today=searches_today+1 WHERE id=?", (self.acc_id,))
        self.state = "search"
        try:
            names = await checker.search(qrow["q"])
            n = db.add_chats([("username", u) for u in names], source=f"search:{qrow['q']}"[:120],
                             preferred_account=self.acc_id)
            db.q("UPDATE queries SET status='done', found=?, done_at=? WHERE id=?", (n, db.now(), qrow["id"]))
            say(f"[{self.name}] поиск «{qrow['q']}»: найдено {len(names)}, новых {n}")
        except errors.FloodWaitError as e:
            db.q("UPDATE queries SET status='pending' WHERE id=?", (qrow["id"],))
            await self._flood(e.seconds, s)
        except Exception as e:  # noqa: BLE001
            db.q("UPDATE queries SET status='error', error=? WHERE id=?", (f"{type(e).__name__}: {e}"[:300], qrow["id"]))
            say(f"[{self.name}] поиск «{qrow['q']}»: {type(e).__name__}: {e}", "warn")
        return True


# ---------- движок ----------

class Engine:
    def __init__(self):
        self.workers: dict[int, Worker] = {}
        self.tasks: list[asyncio.Task] = []
        self.stopping = False
        self.running = False
        self.s = db.get_settings()
        self.http = None
        self.llm_stats = {"done": 0, "errors": 0, "last_error": None}
        self.checking_accounts = False

    async def start(self):
        if self.running:
            return
        self.s = db.get_settings()
        self.stopping = False
        self.running = True
        self.http = httpx.AsyncClient()
        db.reset_stale()
        cond = "status NOT IN ('dead', 'frozen', 'disabled')"
        if not self.s["allow_no_proxy"]:
            cond += " AND proxy IS NOT NULL AND proxy != ''"
        accs = db.all_(f"SELECT * FROM accounts WHERE {cond} ORDER BY id")
        skipped = db.one("SELECT COUNT(*) n FROM accounts WHERE status NOT IN ('dead','frozen','disabled') "
                         "AND (proxy IS NULL OR proxy='')")["n"]
        if skipped and not self.s["allow_no_proxy"]:
            say(f"{skipped} акк. без прокси пропущено (включите в настройках, если нужно)", "warn")
        say(f"Старт: аккаунтов {len(accs)}")
        for i, acc in enumerate(accs):
            w = Worker(self, acc)
            self.workers[acc["id"]] = w
            self.tasks.append(asyncio.create_task(self._delayed(w, i * 1.5)))
        for _ in range(max(1, int(self.s["llm_concurrency"]))):
            self.tasks.append(asyncio.create_task(self._llm_loop()))

    async def _delayed(self, w, delay):
        await asyncio.sleep(delay)   # не подключаем всех одновременно
        try:
            await w.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            say(f"[{w.name}] воркер упал: {type(e).__name__}: {e}", "error")

    async def stop(self):
        if not self.running:
            return
        say("Остановка…")
        self.stopping = True
        try:
            await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 30)
        except asyncio.TimeoutError:
            for t in self.tasks:
                t.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.workers.clear()
        if self.http:
            await self.http.aclose()
            self.http = None
        db.reset_stale()
        db.q("UPDATE queries SET status='pending' WHERE status='running'")
        self.running = False
        say("Остановлено")

    async def _llm_loop(self):
        while not self.stopping:
            s = db.get_settings()
            if not s["llm_enabled"] or not s["openrouter_key"]:
                await asyncio.sleep(3)
                continue
            row = db.one("SELECT * FROM chats WHERE llm_status='pending' AND status='checked' ORDER BY id LIMIT 1")
            if not row:
                await asyncio.sleep(2)
                continue
            db.q("UPDATE chats SET llm_status='running' WHERE id=?", (row["id"],))
            try:
                sample = json.loads(row["sample"] or "{}")
                ai, usage = await llm.analyze(self.http, s["openrouter_key"], s["model"], row, sample,
                                              s["categories"], s["target_description"],
                                              s["llm_msg_chars"], s["llm_messages"])
                finalize(row["id"], ai, s)
                self.llm_stats["done"] += 1
                cost = usage.get("cost")
                if cost:
                    db.save_settings({"llm_spent": round(float(s.get("llm_spent") or 0) + float(cost), 6)})
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"[:300]
                self.llm_stats["errors"] += 1
                self.llm_stats["last_error"] = msg
                fatal = any(x in msg for x in ("401", "402", "403"))
                db.q("UPDATE chats SET llm_status=?, llm_error=? WHERE id=?",
                     ("pending" if fatal else "error", msg, row["id"]))
                say(f"ИИ: {msg}", "error")
                await asyncio.sleep(30 if fatal else 3)

    async def check_accounts(self, ids=None):
        """Подключиться к каждому аккаунту, проверить авторизацию и отключиться."""
        if self.checking_accounts:
            return
        self.checking_accounts = True
        try:
            rows = db.all_("SELECT * FROM accounts ORDER BY id") if not ids else \
                db.all_(f"SELECT * FROM accounts WHERE id IN ({','.join('?' * len(ids))})", ids)
            s = db.get_settings()
            sem = asyncio.Semaphore(5)

            async def one(acc):
                if acc["id"] in self.workers:
                    return
                if not acc["proxy"] and not s["allow_no_proxy"]:
                    db.q("UPDATE accounts SET last_error=? WHERE id=?", ("нет прокси", acc["id"]))
                    return
                async with sem:
                    w = Worker(self, acc)
                    if await w.connect():
                        say(f"[{acc['name']}] аккаунт в порядке")
                    await w.disconnect()

            await asyncio.gather(*(one(a) for a in rows))
        finally:
            self.checking_accounts = False

    def status(self):
        st = {}
        for k, v in db.one("""SELECT COUNT(*) total,
                SUM(status='pending') pending, SUM(status='checking') checking,
                SUM(status='checked') checked, SUM(status IN ('not_found','not_chat')) not_found,
                SUM(status='private') private, SUM(status='error') error,
                SUM(llm_status IN ('pending','running') AND status='checked') llm_queue,
                SUM(verdict='live') live, SUM(verdict='low') low, SUM(verdict='dead') dead,
                SUM(verdict='spam') spam, SUM(is_target=1) target, SUM(is_target=1 AND verdict='live') target_live
                FROM chats""").items():
            st[k] = v or 0
        accs = db.all_("SELECT status, COUNT(*) n FROM accounts GROUP BY status")
        q = db.one("SELECT COUNT(*) total, SUM(status='pending') pending, SUM(found) found FROM queries")
        return {
            "running": self.running,
            "stopping": self.stopping and self.running,
            "chats": st,
            "accounts": {r["status"]: r["n"] for r in accs},
            "workers": [{"id": w.acc_id, "name": w.name, "state": w.state, "current": w.current}
                        for w in self.workers.values()],
            "queries": {k: v or 0 for k, v in q.items()},
            "llm": {**self.llm_stats, "spent": db.get_settings().get("llm_spent", 0)},
            "checking_accounts": self.checking_accounts,
        }


engine = Engine()
