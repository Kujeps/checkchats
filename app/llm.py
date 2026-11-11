"""Анализ чата через OpenRouter."""
import json
import re

import httpx

API = "https://openrouter.ai/api/v1"

SYSTEM = """Ты аналитик Telegram-сообществ. По описанию чата и выборке последних сообщений определи:
- о чём чат и кто в нём сидит;
- ОБЩАЮТСЯ ли там живые люди между собой, или это боты, спам-рассылки, объявления \
«работа на свежем воздухе», посты админа, накрутка, мёртвый чат;
- относится ли аудитория к целевой.

Заказчику нужны только чаты, где реальные люди переписываются друг с другом.
Авторы помечены так: [u1], [u2]… — разные живые люди; [бот] — бот или сообщение через бота; \
[от имени чата] — анонимный админ, пост канала или автопересылка. Сообщения [бот] и \
[от имени чата] общением НЕ являются, даже если их много и они содержательные.

Признаки живого общения: несколько разных людей [uN] отвечают друг другу по теме, задают вопросы, \
спорят, обсуждают конкретику; сообщения разные по стилю и длине.
Признаки, что общения нет: пишет в основном админ, бот или «от имени чата»; люди только публикуют \
объявления и не отвечают друг другу; одинаковые или шаблонные тексты, лёгкий заработок, «пиши в ЛС».
Для канала живость — это только обсуждение людей в комментариях; если комментариев нет, live_score ниже 20.

Отвечай ТОЛЬКО JSON-объектом без пояснений:
{"category": "<одна из категорий списка>",
 "topic": "<тематика в 3-8 словах>",
 "audience": "<кто участники, 3-10 слов>",
 "live_score": <0-100, насколько это живое общение реальных людей>,
 "spam_score": <0-100, доля ботов, спама и мусорной рекламы>,
 "language": "<код языка, например ru>",
 "summary": "<1-2 предложения о чате>"}"""


def _who(s) -> str:
    """Метка автора: u<номер> — человек, остальное — бот или «от имени чата»."""
    if s == "bot":
        return "бот"
    if s == "chat" or s is None:
        return "от имени чата"
    return f"u{s}"


def build_prompt(chat: dict, sample: dict, categories: list, target: str, msg_chars: int, msg_limit: int) -> str:
    lines = [
        f"Категории: {'; '.join(categories)}",
        f"Целевая аудитория заказчика: {target}",
        "",
        f"Тип: {'канал' if chat.get('type') == 'channel' else 'группа (чат)'}",
        f"Название: {chat.get('title') or ''}",
        f"Описание: {(chat.get('about') or '')[:800]}",
        f"Участников: {chat.get('members') or 'неизвестно'}",
    ]
    m = sample.get("metrics") or {}
    if m:
        human = f"{round(m['human_share'] * 100)}%" if m.get("human_share") is not None else "?"
        lines.append(f"Сообщений в день: {m.get('msgs_per_day')}, из них от людей: {human}, "
                     f"разных людей в выборке: {m.get('unique_authors', '-')}, повторов текста: {m.get('dup_ratio')}")
    if sample.get("invite_only"):
        lines.append("Сообщения недоступны (закрытый чат), оценивай по названию и описанию.")

    def fmt(msgs):
        out = []
        for x in msgs[:msg_limit]:
            t = re.sub(r"\s+", " ", x.get("text") or "").strip()
            if not t:
                continue
            who = _who(x.get("s"))
            r = x.get("r")
            if r:
                who += {"?": " (ответ)", "bot": " (ответ боту)", "chat": " (ответ на сообщение от имени чата)"}.get(
                    r, f" (ответ на {_who(r)})")
            out.append(f"[{who}] {t[:msg_chars]}")
        return out

    msgs = fmt(sample.get("messages") or [])
    if msgs:
        lines += ["", "Последние сообщения:", *msgs]
    linked = fmt(sample.get("linked") or [])
    if linked:
        lines += ["", "Комментарии в привязанной группе обсуждения:", *linked[: max(10, msg_limit // 2)]]
    return "\n".join(lines)


def _parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if not m:
            raise ValueError(f"ИИ вернул не JSON: {text[:120]!r}")
        return json.loads(m.group(0))


class RateLimited(Exception):
    """429: модель перегружена у провайдера или исчерпан лимит — нужно подождать."""

    def __init__(self, msg, retry_after=0):
        super().__init__(msg)
        self.retry_after = retry_after


class Transient(Exception):
    """Временный сбой (5xx, таймаут, обрыв) — повторить позже."""


TRANSIENT_CODES = {408, 500, 502, 503, 504, 520, 522, 524, 529}


def models_from(s: dict) -> list:
    """Основная модель и резервные; OpenRouter принимает не больше трёх."""
    out = []
    for m in [s["model"], *(s.get("fallback_models") or [])]:
        m = (m or "").strip()
        if m and m not in out:
            out.append(m)
    return out[:3]


def _error_text(payload) -> str:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        raw = (err.get("metadata") or {}).get("raw")
        return str(raw or err.get("message") or err)[:300]
    return str(payload)[:300]


async def chat_json(client: httpx.AsyncClient, key: str, models, system: str, user: str,
                    max_tokens=600) -> tuple[dict, dict]:
    content, usage = await chat_text(client, key, models, system, user, max_tokens)
    res = _parse_json(content)
    if isinstance(res, list):   # модель иногда отдаёт [ {...} ] вместо {...}
        res = next((x for x in res if isinstance(x, dict)), {})
    if not isinstance(res, dict):
        raise ValueError("ИИ вернул не JSON-объект")
    return res, usage


async def chat_text(client: httpx.AsyncClient, key: str, models, system: str, user: str,
                    max_tokens=600) -> tuple[str, dict]:
    models = [models] if isinstance(models, str) else list(models)
    body = {
        "model": models[0],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "usage": {"include": True},   # OpenRouter вернёт стоимость запроса
    }
    if len(models) > 1:
        body["models"] = models       # при сбое основной OpenRouter сам перейдёт на следующую
    headers = {"Authorization": f"Bearer {key}", "X-Title": "CheckChats"}
    try:
        r = await client.post(f"{API}/chat/completions", json=body, headers=headers, timeout=90)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise Transient(f"нет ответа от OpenRouter: {type(e).__name__}")
    try:
        data = r.json()
    except ValueError:
        data = {"error": {"message": r.text[:300]}}
    code = r.status_code
    if code < 400 and isinstance(data, dict) and "error" in data:
        code = (data["error"] or {}).get("code") if isinstance(data["error"], dict) else 500
        code = code if isinstance(code, int) else 500
    if code == 429:
        try:
            retry = float(r.headers.get("retry-after") or 0)
        except ValueError:
            retry = 0
        raise RateLimited(f"OpenRouter 429: {_error_text(data)}", retry)
    if code in TRANSIENT_CODES:
        raise Transient(f"OpenRouter {code}: {_error_text(data)}")
    if code >= 400:
        raise RuntimeError(f"OpenRouter {code}: {_error_text(data)}")
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        raise Transient(f"OpenRouter вернул пустой ответ: {str(data)[:200]}")
    content = (choices[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    usage["model"] = data.get("model")
    usage["finish_reason"] = choices[0].get("finish_reason")
    return content, usage


async def analyze(client, key, models, chat, sample, categories, target, msg_chars, msg_limit) -> tuple[dict, dict]:
    prompt = build_prompt(chat, sample, categories, target, msg_chars, msg_limit)
    res, usage = await chat_json(client, key, models, SYSTEM, prompt)
    cat = str(res.get("category") or "Другое").strip()
    # модель иногда пишет категорию чуть иначе — приводим к ближайшей из списка
    if cat not in categories:
        low = cat.lower()
        cat = next((c for c in categories if low[:6] and (low[:6] in c.lower() or c.lower()[:6] in low)), "Другое")

    def score(v):
        try:
            return max(0, min(100, int(float(v))))
        except (TypeError, ValueError):
            return None

    return {
        "category": cat,
        "topic": str(res.get("topic") or "")[:200],
        "audience": str(res.get("audience") or "")[:200],
        "live": score(res.get("live_score")),
        "spam": score(res.get("spam_score")),
        "language": str(res.get("language") or "")[:10],
        "summary": str(res.get("summary") or "")[:600],
    }, usage


async def generate_queries(client, key, models, topics: str, cities: str, n: int = 60) -> list:
    system = ("Ты помогаешь находить публичные Telegram-чаты через встроенный поиск Telegram. "
              "Поиск ищет по названию и username чата, поэтому запросы должны быть короткими "
              "(1-3 слова), как люди называют такие чаты. Ответ — JSON {\"queries\": [..]}.")
    user = (f"Нужны чаты, где общается такая аудитория: {topics}\n"
            f"Города/регионы (если пусто — без привязки): {cities or '-'}\n"
            f"Придумай {n} разных поисковых запросов на русском и английском, включая сленг "
            "(вб, озон, селлеры, риелторы, мастера и т.п.) и сочетания с городами.")
    content, _ = await chat_text(client, key, models, system, user, max_tokens=max(1500, n * 40))
    out = []
    for x in _queries_from(content):
        x = re.sub(r"\s+", " ", str(x)).strip().strip("\"'«»")
        if x and len(x) <= 64 and x.lower() not in {o.lower() for o in out}:
            out.append(x)
    if not out:
        raise ValueError(f"ИИ не вернул ни одного запроса: {content[:150]!r}")
    return out


def _queries_from(content: str) -> list:
    """Список запросов из ответа в любом виде: {"queries": [...]}, [...], или обрезанный JSON."""
    try:
        res = _parse_json(content)
        if isinstance(res, list):
            return res
        if isinstance(res, dict):
            if isinstance(res.get("queries"), list):
                return res["queries"]
            # модель могла назвать ключ иначе — берём первый список
            return next((v for v in res.values() if isinstance(v, list)), [])
    except ValueError:
        pass
    # ответ обрезался по длине: достаём всё, что успело прийти в кавычках
    items = re.findall(r'"((?:[^"\\]|\\.){1,64})"', content or "")
    return [x for x in items if x.lower() not in ("queries", "query")]


async def list_models(client) -> list:
    r = await client.get(f"{API}/models", timeout=30)
    r.raise_for_status()
    out = []
    for m in r.json().get("data", []):
        p = m.get("pricing") or {}
        try:
            pin = float(p.get("prompt") or 0) * 1e6
            pout = float(p.get("completion") or 0) * 1e6
        except ValueError:
            continue
        out.append({"id": m["id"], "name": m.get("name") or m["id"],
                    "in": round(pin, 3), "out": round(pout, 3), "ctx": m.get("context_length")})
    out.sort(key=lambda x: (x["in"] + x["out"], x["id"]))
    return out
