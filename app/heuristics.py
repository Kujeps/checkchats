"""Оценка «живости» и спамности чата по выборке сообщений, без ИИ.

Сообщение на входе — словарь:
    id, date (unix), sender (id|None), bot (bool), text, reply_to (id|None),
    fwd (bool), links (int), views (int|None), reactions (int), replies (int|None)
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


def compute(messages: list, chat_type: str, members: int = None, online: int = None,
            linked_messages: list = None, now: float = None, dead_days: int = 30) -> dict:
    """Возвращает метрики и оценки live (0..100), spam (0..100), предварительный вердикт."""
    now = now or time.time()
    msgs = [m for m in messages if m.get("date")]
    n = len(msgs)
    m = {"n": n, "type": chat_type, "members": members, "online": online}
    if n == 0:
        m.update(live=0, spam=0, verdict="dead", msgs_per_day=0, last_msg_at=None,
                 days_since_last=None, reason="нет сообщений")
        return m

    dates = sorted(x["date"] for x in msgs)
    last, first = dates[-1], dates[0]
    span_days = max((now - first) / 86400, 1 / 24)
    days_since_last = (now - last) / 86400
    mpd = n / span_days
    m.update(msgs_per_day=round(mpd, 2), last_msg_at=last, days_since_last=round(days_since_last, 2))

    texts = [x.get("text") or "" for x in msgs]
    norm = [_norm(t) for t in texts]
    long_norm = [t for t in norm if len(t) >= 20]
    cnt = Counter(long_norm)
    dup = sum(c for c in cnt.values() if c > 1) / len(long_norm) if long_norm else 0.0
    kw = sum(1 for t in texts if SPAM_RE.search(t)) / n
    link_ratio = sum(1 for x in msgs if x.get("links")) / n
    fwd_ratio = sum(1 for x in msgs if x.get("fwd")) / n
    m.update(dup_ratio=round(dup, 3), spam_kw_ratio=round(kw, 3),
             link_ratio=round(link_ratio, 3), fwd_ratio=round(fwd_ratio, 3))

    if chat_type == "channel":
        return _channel(m, msgs, mpd, days_since_last, dup, kw, members, linked_messages, now, dead_days)
    return _group(m, msgs, mpd, days_since_last, dup, kw, link_ratio, members, online, dead_days)


def _group(m, msgs, mpd, days_since_last, dup, kw, link_ratio, members, online, dead_days):
    n = len(msgs)
    senders = [x.get("sender") for x in msgs if x.get("sender") is not None]
    by_sender = Counter(senders)
    unique = len(by_sender)
    top_share = (by_sender.most_common(1)[0][1] / len(senders)) if senders else 1.0
    repeat_authors = sum(1 for c in by_sender.values() if c >= 2)
    bot_ratio = sum(1 for x in msgs if x.get("bot")) / n

    # Ответы одного человека другому — главный признак живого общения
    sender_of = {x["id"]: x.get("sender") for x in msgs}
    dialog = 0
    for x in msgs:
        r = x.get("reply_to")
        if r and r in sender_of and sender_of[r] is not None and sender_of[r] != x.get("sender"):
            dialog += 1
    dialog_ratio = dialog / n

    m.update(unique_authors=unique, repeat_authors=repeat_authors, top_author_share=round(top_share, 3),
             bot_ratio=round(bot_ratio, 3), dialog_ratio=round(dialog_ratio, 3))

    activity = _clamp(math.log10(1 + mpd) / math.log10(51))  # 50 сообщений в день и больше = 1
    diversity = _clamp(unique / 15) * (1 - _clamp((top_share - 0.5) * 2) * 0.6)
    dialog_s = _clamp(dialog_ratio / 0.25)
    rec = recency_score(days_since_last)
    online_s = _clamp((online or 0) / 20)

    spam = _clamp(0.9 * dup + 0.9 * kw + 0.35 * link_ratio + 0.6 * bot_ratio
                  + (0.25 if top_share > 0.7 and unique < 5 else 0)
                  - 0.4 * dialog_s)
    live = (0.22 * activity + 0.2 * rec + 0.25 * diversity + 0.25 * dialog_s + 0.08 * online_s)
    live *= 1 - 0.8 * spam
    m.update(live=round(100 * live), spam=round(100 * spam))
    m["verdict"], m["reason"] = _verdict(m, days_since_last, dead_days)
    return m


def _channel(m, msgs, mpd, days_since_last, dup, kw, members, linked_messages, now, dead_days):
    views = [x["views"] for x in msgs if x.get("views")]
    avg_views = sum(views) / len(views) if views else 0
    reactions = sum(x.get("reactions") or 0 for x in msgs) / len(msgs)
    comments = [x["replies"] for x in msgs if x.get("replies") is not None]
    avg_comments = sum(comments) / len(comments) if comments else 0
    reach = (avg_views / members) if members else 0
    m.update(avg_views=round(avg_views), reach=round(reach, 3), avg_reactions=round(reactions, 2),
             avg_comments=round(avg_comments, 2), has_comments=bool(comments))

    activity = _clamp(math.log10(1 + mpd) / math.log10(11))  # 10 постов в день = 1
    rec = recency_score(days_since_last)
    reach_s = _clamp(reach / 0.2)                            # 20% охвата — хорошо
    engage = _clamp((avg_comments / 5) * 0.6 + (reactions / max(avg_views, 1) / 0.02) * 0.4)

    discussion = None
    if linked_messages:
        d = compute(linked_messages, "group", now=now, dead_days=dead_days)
        discussion = d["live"]
        m["discussion_live"] = discussion
        engage = max(engage, d["live"] / 100)

    spam = _clamp(0.8 * dup + 0.9 * kw)
    live = 0.25 * activity + 0.2 * rec + 0.3 * reach_s + 0.25 * engage
    live *= 1 - 0.8 * spam
    m.update(live=round(100 * live), spam=round(100 * spam))
    m["verdict"], m["reason"] = _verdict(m, days_since_last, dead_days)
    return m


def _verdict(m, days_since_last, dead_days):
    if days_since_last is None or days_since_last > dead_days:
        return "dead", f"последнее сообщение больше {dead_days} дн. назад"
    if m["spam"] >= 60:
        return "spam", "много повторов / спам-фраз"
    if m["live"] >= 50:
        return "live", "активное общение"
    return "low", "слабая активность"


def combine(h_live, h_spam, llm_live, llm_spam, days_since_last, dead_days, live_thr, spam_thr):
    """Итоговая оценка: эвристика + мнение ИИ поровну."""
    live = h_live if llm_live is None else round((h_live + llm_live) / 2)
    spam = h_spam if llm_spam is None else round((h_spam + llm_spam) / 2)
    if days_since_last is None or days_since_last > dead_days:
        verdict = "dead"
    elif spam >= spam_thr:
        verdict = "spam"
    elif live >= live_thr:
        verdict = "live"
    else:
        verdict = "low"
    return live, spam, verdict
