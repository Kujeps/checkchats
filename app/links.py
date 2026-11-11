"""Разбор ссылок на чаты: @username, t.me/username, t.me/+hash, t.me/joinchat/hash."""
import re

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TME_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(?P<path>[^\s?#]+)", re.I
)
# Служебные пути t.me, которые не являются username
_RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "addlist", "c", "s",
             "iv", "login", "setlanguage", "addtheme", "addemoji", "boost", "bg", "m"}

# Находит ссылки на t.me внутри текста сообщений (для «снежного кома»)
TME_IN_TEXT_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(\+?[A-Za-z0-9_/\-]+)", re.I
)


def parse_link(raw: str):
    """Возвращает ("username", name) | ("invite", hash) | None."""
    s = (raw or "").strip().strip(",;")
    if not s:
        return None
    m = _TME_RE.search(s)
    if m:
        parts = [p for p in m.group("path").split("/") if p]
        if not parts:
            return None
        head = parts[0]
        if head.startswith("+"):
            return ("invite", head[1:]) if len(head) > 1 else None
        if head.lower() == "joinchat" and len(parts) > 1:
            return ("invite", parts[1])
        if head.lower() == "s" and len(parts) > 1:
            head = parts[1]
        elif head.lower() in _RESERVED:
            return None
        return ("username", head.lower()) if _USERNAME_RE.match(head) else None
    if s.startswith("@"):
        s = s[1:]
    if _USERNAME_RE.match(s):
        return ("username", s.lower())
    return None


def chat_key(kind: str, value: str) -> str:
    """Ключ для дедупликации."""
    return f"u:{value.lower()}" if kind == "username" else f"i:{value}"


def link_for(kind: str, value: str) -> str:
    return f"https://t.me/{value}" if kind == "username" else f"https://t.me/+{value}"


def extract_links(text: str):
    """Все ссылки на чаты из текста."""
    out = []
    for m in TME_IN_TEXT_RE.finditer(text or ""):
        p = parse_link("t.me/" + m.group(1))
        if p:
            out.append(p)
    return out
