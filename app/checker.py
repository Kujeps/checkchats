"""Проверка одного чата через Telegram: метаданные, последние сообщения, ссылки на другие чаты."""
from collections import Counter
from datetime import datetime, timedelta, timezone

from telethon import errors, utils
from telethon.tl.functions.channels import GetChannelRecommendationsRequest, GetFullChannelRequest
from telethon.tl.functions.contacts import ResolveUsernameRequest, SearchRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest, SearchGlobalRequest
from telethon.tl.types import (Channel, ChatInvite, ChatInviteAlready, ChatInvitePeek, InputMessagesFilterEmpty,
                               InputPeerChannel, InputPeerEmpty, MessageEntityTextUrl, MessageEntityUrl,
                               MessageService, PeerUser, User)

from .links import extract_links


class ChatGone(Exception):
    """Чата нет или он недоступен: status, текст."""

    def __init__(self, status, msg=""):
        super().__init__(msg)
        self.status = status


class NeedResolve(Exception):
    """Username не в кэше аккаунта, а дневной лимит поиска исчерпан."""


def _username(ent):
    if getattr(ent, "username", None):
        return ent.username
    for u in getattr(ent, "usernames", None) or []:
        if getattr(u, "active", False):
            return u.username
    return None


def _conv(msg):
    if msg is None or isinstance(msg, MessageService):
        return None
    text = msg.message or ""
    links = sum(1 for e in (msg.entities or []) if isinstance(e, (MessageEntityUrl, MessageEntityTextUrl)))
    reactions = 0
    if msg.reactions and msg.reactions.results:
        reactions = sum(r.count for r in msg.reactions.results)
    sender = msg.sender
    is_bot = bool(isinstance(sender, User) and sender.bot)
    # Кто написал: живой человек, бот (или через бота), либо от имени чата/канала —
    # анонимный админ, пост канала, автопересылка из канала в группу обсуждения
    if isinstance(msg.from_id, PeerUser):
        kind = "bot" if is_bot or msg.via_bot_id else "user"
    else:
        kind = "chat"
    return {
        "id": msg.id,
        "date": msg.date.timestamp() if msg.date else None,
        "sender": msg.sender_id,
        "kind": kind,
        "bot": is_bot,
        "text": text,
        "reply_to": msg.reply_to.reply_to_msg_id if msg.reply_to else None,
        "fwd": msg.fwd_from is not None,
        "links": links,
        "views": msg.views,
        "reactions": reactions,
        "replies": msg.replies.replies if msg.replies else None,
    }


def _found_links(raw_msgs, own_username):
    """Ссылки на другие чаты: t.me в тексте и каналы, откуда сделаны репосты."""
    found = []
    for msg in raw_msgs:
        if msg is None or isinstance(msg, MessageService):
            continue
        for e in msg.entities or []:
            if isinstance(e, MessageEntityTextUrl):
                found += extract_links(e.url)
        found += extract_links(msg.message or "")
        fwd = getattr(msg, "forward", None)
        if fwd is not None and isinstance(getattr(fwd, "chat", None), Channel):
            u = _username(fwd.chat)
            if u:
                found.append(("username", u.lower()))
    own = (own_username or "").lower()
    out, seen = [], set()
    for kind, val in found:
        k = (kind, val.lower() if kind == "username" else val)
        if k in seen or (kind == "username" and k[1] == own):
            continue
        seen.add(k)
        out.append(k)
    return out[:40]


class Checker:
    """Работает поверх одного TelegramClient; бюджеты считает воркер."""

    def __init__(self, client, budget):
        self.client = client
        self.budget = budget   # объект с методами can_resolve(), use_resolve(), can_join(), use_join()

    async def _peer_by_username(self, username):
        row = self.client.session.get_entity_rows_by_username(username)
        if row:
            return await self.client.get_input_entity(username), False
        if not self.budget.can_resolve():
            raise NeedResolve()
        self.budget.use_resolve()
        try:
            res = await self.client(ResolveUsernameRequest(username))
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            raise ChatGone("not_found", "username не существует")
        peer = res.peer
        ent = next((c for c in res.chats if c.id == getattr(peer, "channel_id", None)), None)
        if ent is None:
            raise ChatGone("not_chat", "это пользователь или бот, а не чат")
        return utils.get_input_peer(ent), True

    async def check(self, chat: dict, messages_limit: int, join_private: bool, leave_after_join: bool,
                    similar: bool = False, history_days: int = 7, history_max: int = 1000):
        joined = None
        if chat["kind"] == "invite":
            peer, meta = await self._by_invite(chat["value"], join_private)
            if peer is None:
                return meta
            joined = peer if meta.get("joined") else None
        else:
            peer, _ = await self._peer_by_username(chat["value"])

        try:
            return await self._read(peer, messages_limit, similar, history_days, history_max)
        finally:
            if joined is not None and leave_after_join:
                try:
                    await self.client.delete_dialog(joined)
                except errors.RPCError:
                    pass

    async def _by_invite(self, invite_hash, join_private):
        if not self.budget.can_resolve():
            raise NeedResolve()
        self.budget.use_resolve()
        try:
            inv = await self.client(CheckChatInviteRequest(invite_hash))
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError, errors.InviteHashEmptyError):
            raise ChatGone("not_found", "инвайт недействителен")
        if isinstance(inv, (ChatInviteAlready, ChatInvitePeek)):
            return utils.get_input_peer(inv.chat), {}
        assert isinstance(inv, ChatInvite)
        if join_private and not inv.request_needed and self.budget.can_join():
            self.budget.use_join()
            try:
                upd = await self.client(ImportChatInviteRequest(invite_hash))
                ent = upd.chats[0]
                return utils.get_input_peer(ent), {"joined": True}
            except errors.UserAlreadyParticipantError:
                pass
            except (errors.InviteRequestSentError, errors.ChannelsTooMuchError):
                pass
        # Без вступления доступны только название, описание и число участников
        return None, {
            "status": "private",
            "tg_id": None,
            "type": "channel" if inv.broadcast else "group",
            "title": inv.title,
            "about": inv.about or "",
            "members": inv.participants_count,
            "online": None,
            "username": None,
            "messages": [],
            "linked": [],
            "found": [],
        }

    async def _history(self, entity, min_count, days, max_count):
        """Последние сообщения: не меньше min_count и так, чтобы покрыть days дней, но не больше max_count.

        Читаем страницами по 100 и останавливаемся, как только дошли до нужной давности.
        """
        since = datetime.now(timezone.utc) - timedelta(days=days)
        out = []
        async for m in self.client.iter_messages(entity, limit=max(min_count, max_count)):
            out.append(m)
            if len(out) >= min_count and m.date and m.date < since:
                break
        return out

    async def _read(self, peer, messages_limit, similar=False, history_days=7, history_max=1000):
        if not isinstance(peer, InputPeerChannel):
            raise ChatGone("not_chat", "не канал и не супергруппа")
        try:
            full = await self.client(GetFullChannelRequest(peer))
        except (errors.ChannelPrivateError, errors.ChannelInvalidError):
            raise ChatGone("private", "чат закрыт или аккаунт забанен в нём")
        fc = full.full_chat
        ent = next((c for c in full.chats if c.id == fc.id), full.chats[0])
        ctype = "channel" if getattr(ent, "broadcast", False) else "group"

        try:
            raw = await self._history(peer, messages_limit, history_days, history_max)
        except errors.ChannelPrivateError:
            raise ChatGone("private", "история скрыта")
        msgs = [m for m in (_conv(x) for x in raw) if m]

        linked = []
        if ctype == "channel" and fc.linked_chat_id:
            lent = next((c for c in full.chats if c.id == fc.linked_chat_id), None)
            if lent is not None:
                try:
                    lraw = await self._history(lent, min(80, messages_limit), history_days, max(80, history_max // 2))
                    linked = [m for m in (_conv(x) for x in lraw) if m]
                except errors.RPCError:
                    pass

        username = _username(ent)
        return {
            "similar": await self._similar(peer) if similar and ctype == "channel" else [],
            "status": "checked",
            "tg_id": ent.id,
            "type": ctype,
            "title": ent.title,
            "about": fc.about or "",
            "members": fc.participants_count,
            "online": getattr(fc, "online_count", None),
            "username": username,
            "messages": msgs,
            "linked": linked,
            "found": _found_links(raw, username),
        }

    async def _similar(self, peer) -> list:
        """Похожие каналы по рекомендациям Telegram. Ошибки здесь не мешают проверке самого канала."""
        try:
            res = await self.client(GetChannelRecommendationsRequest(channel=utils.get_input_channel(peer)))
        except (errors.FloodWaitError, errors.RPCError):
            return []
        own = peer.channel_id
        return list(dict.fromkeys(u.lower() for c in res.chats
                                  if isinstance(c, Channel) and c.id != own and (u := _username(c))))[:30]

    async def search_messages(self, query: str, days: int, pages: int, take_budget) -> tuple[list, int]:
        """Глобальный поиск по сообщениям в публичных группах за последние days дней.

        Находит чаты, где люди прямо сейчас пишут об этом. Возвращает ([(username, совпадений)…] по убыванию,
        сколько страниц запрошено). take_budget() списывает один запрос из лимита или говорит, что его нет.
        """
        hits = Counter()
        rate, offset_peer, offset_id = 0, InputPeerEmpty(), 0
        min_date = datetime.now(timezone.utc) - timedelta(days=days)
        used = 0
        for _ in range(max(1, pages)):
            if not take_budget():
                break
            try:
                res = await self.client(SearchGlobalRequest(
                    q=query, filter=InputMessagesFilterEmpty(), min_date=min_date, max_date=None,
                    offset_rate=rate, offset_peer=offset_peer, offset_id=offset_id, limit=100, groups_only=True))
            except errors.FloodWaitError:
                if used:
                    break            # уже что-то нашли — отдадим это, а FloodWait поймаем в следующий раз
                raise
            used += 1
            chats = {c.id: c for c in res.chats}
            for m in res.messages:
                c = chats.get(getattr(m.peer_id, "channel_id", None))
                u = _username(c) if c is not None else None
                if isinstance(c, Channel) and not c.broadcast and u:
                    hits[u.lower()] += 1
            next_rate = getattr(res, "next_rate", None)
            if not res.messages or not next_rate:
                break
            last = res.messages[-1]
            last_chat = chats.get(getattr(last.peer_id, "channel_id", None))
            if last_chat is None:
                break
            rate, offset_peer, offset_id = next_rate, utils.get_input_peer(last_chat), last.id
        return hits.most_common(), used

    async def search(self, query: str):
        res = await self.client(SearchRequest(q=query, limit=100))
        out = []
        for c in res.chats:
            u = _username(c)
            if isinstance(c, Channel) and u:
                out.append(u.lower())
        return list(dict.fromkeys(out))
