"""Оценка «живости» и спамности чата по выборке сообщений, без ИИ.

«Живой» — значит в чате ОБЩАЮТСЯ ЛЮДИ: много разных людей пишут и отвечают друг другу.
Сообщения ботов, через ботов и от имени чата/канала (анонимный админ, посты канала)
общением не считаются.

Сообщение на входе — словарь:
    id, date (unix), sender (id|None), kind ("user"|"bot"|"chat"), bot (bool), text,
    reply_to (id|None), fwd (bool), links (int), views (int|None), reactions (int), replies (int|None)
"""
import math
import re
import time
from collections import Counter

SPAM_PATTERNS = [
    r"заработ\w*", r"доход\w*\s+от", r"пассивн\w+\s+доход", r"подработ\w*",
    r"без\s+опыта", r"удал[её]нн\w+\s+работ", r"в\s+(?:лс|личку|личные)", r"пиши\w*\s+(?:в\s+)?(?:лс|личку|\+)",
    r"от\s*\d[\d\s]*\s*(?:₽|р\b|руб|\$|usdt)\s*(?:в|за)\s*(?:день|час|сутки|недел)",
    r"\d[\d\s]*\s*(?:₽|руб|\$)\s*(?:в|за)\s*(?:день|час|сутки)",
    r"набира\w+\s+(?:людей|сотрудников|команду)", r"требуются\s+курьер", r"закладк\w*", r"кладмен",
    r"легальн\w+\s+схем", r"арбитраж\w*\s+трафик", r"ставк\w+\s+на\s+спорт", r"казино", r"\b1xbet\b",
    r"интим", r"эскорт", r"18\+", r"займ\w*\s+без", r"обнал\w*", r"дроп\w*", r"\bp2p\b",
    r"крипт\w+\s+(?:сигнал|бот)", r"инвестиц\w+\s+от", r"гарантированн\w+",
]
SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.I)
_NORM_RE = re.compile(r"https?://\S+|[@#]\w+|\d+|[^\w]+", re.U)


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def recency_score(days: float) -> float:
    if days is None:
        return 0.0
    if days <= 1:
        return 1.0
    if days <= 3:
        return 0.75
    if days <= 7:
        return 0.45
    if days <= 30:
        return 0.15
    return 0.0


def kind_of(x) -> str:
    """Тип автора; для старых данных без поля kind — по косвенным признакам."""
    k = x.get("kind")
    if k:
        return k
    if x.get("bot"):
        return "bot"
    s = x.get("sender")
    return "user" if isinstance(s, int) and s > 0 else "chat"


def compute(messages: list, chat_type: str, members: int = None, online: int = None,
            linked_messages: list = None, now: float = None, dead_days: int = 30) -> dict:
    """Возвращает метрики и оценки live (0..100), spam (0..100), предварительный вердикт."""
    now = now or time.time()
    msgs = [x for x in messages if x.get("date")]
    m = {"n": len(msgs), "type": chat_type, "members": members, "online": online}
    if chat_type == "channel":
        return _channel(m, msgs, linked_messages or [], now, dead_days)
    return _group(m, msgs, online, now, dead_days)


def _content(msgs):
    """Признаки спама по текстам: повторы, спам-фразы, ссылки, репосты."""
    n = len(msgs) or 1
    texts = [x.get("text") or "" for x in msgs]
    long_norm = [t for t in (_norm(t) for t in texts) if len(t) >= 20]
    cnt = Counter(long_norm)
    dup = sum(c for c in cnt.values() if c > 1) / len(long_norm) if long_norm else 0.0
    kw = sum(1 for t in texts if SPAM_RE.search(t)) / n
    links = sum(1 for x in msgs if x.get("links")) / n
    fwd = sum(1 for x in msgs if x.get("fwd")) / n
    return dup, kw, links, fwd


def _group(m, msgs, online, now, dead_days):
    n = len(msgs)
    if n == 0:
        m.update(live=0, spam=0, verdict="dead", msgs_per_day=0, last_msg_at=None, days_since_last=None,
                 human_share=0, unique_authors=0, dialog_ratio=0, reason="нет сообщений")
        return m
    dates = sorted(x["date"] for x in msgs)
    span_days = max((now - dates[0]) / 86400, 1 / 24)
    days_since_last = (now - dates[-1]) / 86400

    human = [x for x in msgs if kind_of(x) == "user"]
    hn = len(human)
    human_share = hn / n
    bot_ratio = sum(1 for x in msgs if kind_of(x) == "bot") / n
    chat_ratio = sum(1 for x in msgs if kind_of(x) == "chat") / n

    by_sender = Counter(x.get("sender") for x in human)
    unique = len(by_sender)
    top_share = by_sender.most_common(1)[0][1] / hn if hn else 1.0
    repeat_authors = sum(1 for c in by_sender.values() if c >= 2)

    # Ответ человека другому человеку — главный признак живого общения
    human_sender = {x["id"]: x.get("sender") for x in human}
    dialog = sum(1 for x in human
                 if x.get("reply_to") in human_sender and human_sender[x["reply_to"]] != x.get("sender"))
    dialog_ratio = dialog / hn if hn else 0.0

    human_mpd = hn / span_days
    days_since_human = (now - max(x["date"] for x in human)) / 86400 if human else None

    dup, kw, links, fwd = _content(msgs)
    m.update(msgs_per_day=round(n / span_days, 2), human_msgs_per_day=round(human_mpd, 2),
             last_msg_at=dates[-1], days_since_last=round(days_since_last, 2),
             days_since_human=round(days_since_human, 2) if days_since_human is not None else None,
             human_share=round(human_share, 3), bot_ratio=round(bot_ratio, 3), chat_ratio=round(chat_ratio, 3),
             unique_authors=unique, repeat_authors=repeat_authors, top_author_share=round(top_share, 3),
             dialog_ratio=round(dialog_ratio, 3), dup_ratio=round(dup, 3), spam_kw_ratio=round(kw, 3),
             link_ratio=round(links, 3), fwd_ratio=round(fwd, 3))

    activity = _clamp(math.log10(1 + human_mpd) / math.log10(51))   # 50 сообщений людей в день = 1
    diversity = _clamp(unique / 15) * (1 - _clamp((top_share - 0.4) * 2) * 0.7)
    dialog_s = _clamp(dialog_ratio / 0.25)
    rec = recency_score(days_since_human)
    online_s = _clamp((online or 0) / 20)

    spam = _clamp(0.9 * dup + 0.9 * kw + 0.35 * links + 0.6 * bot_ratio
                  + (0.25 if top_share > 0.7 and unique < 5 else 0) - 0.4 * dialog_s)
    live = 0.18 * activity + 0.15 * rec + 0.25 * diversity + 0.34 * dialog_s + 0.08 * online_s
    live *= _clamp(human_share / 0.6)       # меньше 60% сообщений от людей — штраф
    live *= 1 - 0.8 * spam
    if dialog_ratio < 0.03:
        live = min(live, 0.45)              # люди почти не отвечают друг другу — одни объявления
    m.update(live=round(100 * live), spam=round(100 * spam))
    m["verdict"], m["reason"] = _group_verdict(m, dead_days)
    if m["verdict"] == "noconv":
        m["live"] = min(m["live"], 25)     # иначе «живость 64, но нет общения» сбивает с толку
    return m


def _group_verdict(m, dead_days):
    if m["days_since_last"] > dead_days:
        return "dead", f"последнее сообщение больше {dead_days} дн. назад"
    if m["spam"] >= 60:
        return "spam", "много повторов, спам-фраз или ботов"
    if not m["unique_authors"] or m["days_since_human"] is None or m["days_since_human"] > dead_days:
        return "noconv", "люди не пишут: только боты, админ или сообщения от имени чата"
    if m["human_share"] < 0.3:
        return "noconv", (f"от людей только {round(m['human_share'] * 100)}% сообщений, "
                          "остальное — боты и сообщения от имени чата")
    if m["unique_authors"] < 3:
        return "noconv", f"пишут всего {m['unique_authors']} чел."
    if m["live"] >= 50:
        return "live", "люди активно общаются"
    if m["dialog_ratio"] < 0.03:
        return "low", "люди почти не отвечают друг другу"
    return "low", "слабое общение"


def _channel(m, msgs, linked, now, dead_days):
    """В канале пишут только админы — общаются люди лишь в комментариях (группе обсуждения)."""
    if not msgs:
        m.update(live=0, spam=0, verdict="dead", msgs_per_day=0, last_msg_at=None, days_since_last=None,
                 reason="нет постов")
        return m
    dates = sorted(x["date"] for x in msgs)
    span_days = max((now - dates[0]) / 86400, 1 / 24)
    days_since_last = (now - dates[-1]) / 86400
    views = [x["views"] for x in msgs if x.get("views")]
    avg_views = sum(views) / len(views) if views else 0
    comments = [x["replies"] for x in msgs if x.get("replies") is not None]
    dup, kw, links, fwd = _content(msgs)
    members = m.get("members")
    m.update(msgs_per_day=round(len(msgs) / span_days, 2), last_msg_at=dates[-1],
             days_since_last=round(days_since_last, 2), avg_views=round(avg_views),
             reach=round(avg_views / members, 3) if members else None,
             avg_comments=round(sum(comments) / len(comments), 2) if comments else 0,
             has_comments=bool(comments), dup_ratio=round(dup, 3), spam_kw_ratio=round(kw, 3))
    post_spam = _clamp(0.8 * dup + 0.9 * kw)

    if days_since_last > dead_days:
        m.update(live=0, spam=round(100 * post_spam), verdict="dead",
                 reason=f"последний пост больше {dead_days} дн. назад")
        return m
    if not linked:
        m.update(live=0, spam=round(100 * post_spam), verdict="noconv",
                 reason="канал без комментариев — люди не общаются")
        return m

    # Группу обсуждения оцениваем как обычный чат; автопересылки постов там — «от имени чата»
    d = _group({"n": len(linked)}, [x for x in linked if x.get("date")], None, now, dead_days)
    for k in ("human_share", "unique_authors", "dialog_ratio", "human_msgs_per_day", "days_since_human",
              "bot_ratio", "chat_ratio", "top_author_share"):
        m[k] = d.get(k)
    m["discussion_live"] = d["live"]
    m["live"] = d["live"]
    m["spam"] = max(d["spam"], round(100 * post_spam * 0.5))
    if d["verdict"] in ("dead", "noconv"):
        m["verdict"], m["reason"] = "noconv", "в комментариях не общаются: " + d["reason"]
    else:
        m["verdict"], m["reason"] = d["verdict"], "комментарии: " + d["reason"]
    return m


# Вердикты-факты: мнение ИИ их не отменяет
HARD_VERDICTS = ("dead", "noconv")


def combine(h_live, h_spam, llm_live, llm_spam, h_verdict, live_thr, spam_thr):
    """Итоговая оценка: эвристика и мнение ИИ поровну."""
    live = h_live if llm_live is None else round((h_live + llm_live) / 2)
    spam = h_spam if llm_spam is None else round((h_spam + llm_spam) / 2)
    if h_verdict in HARD_VERDICTS:
        verdict = h_verdict
        live = min(live, h_live)           # ИИ не поднимает живость чата без общения
    elif spam >= spam_thr:
        verdict = "spam"
    elif live >= live_thr:
        verdict = "live"
    else:
        verdict = "low"
    return live, spam, verdict
